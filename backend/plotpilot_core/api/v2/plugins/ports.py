"""Composition-only lifecycle façade over the P2 process adapter."""
from __future__ import annotations

from typing import Protocol

from plotpilot_core.supervisor import WorkerTicket
from plotpilot_core.supervisor.job_control import RunSnapshotWorker
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode

from .models import LifecycleFault, project_worker_ticket

_CONTRACT_MESSAGES = {
    ErrorCode.INCOMPATIBLE_GENERATION: "worker generation is incompatible",
    ErrorCode.STALE_LEASE: "worker authority no longer holds the requested release",
    ErrorCode.CANCELLED: "worker request was cancelled",
    ErrorCode.DEADLINE_EXCEEDED: "worker request deadline was exceeded",
    ErrorCode.ASSET_ERROR: "worker runtime assets are unavailable",
    ErrorCode.SETTINGS_INVALID: "worker settings are invalid",
    ErrorCode.MIGRATION_FAILED: "worker migration failed",
    ErrorCode.DUPLICATE_REQUEST: "worker request was already admitted",
    ErrorCode.UNCERTAIN_EXTERNAL_EFFECT: "worker request outcome is uncertain",
    ErrorCode.INVALID_TRANSITION: "worker lifecycle transition is invalid",
    ErrorCode.RESULT_CONTRACT_MISMATCH: "worker authority returned an invalid result",
    ErrorCode.DATA_INTERPRETER_UNAVAILABLE: "worker data interpreter is unavailable",
    ErrorCode.RELEASE_RETIRING: "requested worker release is retiring",
    ErrorCode.CHECKPOINT_INVALID: "worker checkpoint is invalid",
}
_UNKNOWN_AUTHORITY_FAILURE = "worker authority request failed"


class WorkerRequestPort(Protocol):
    def request_worker(self, run_snapshot: RunSnapshotWorker) -> WorkerTicket: ...


class PluginLifecycleFacade:
    """Project process receipts without choosing a generation or a release."""

    def __init__(self, jobs: WorkerRequestPort) -> None:
        self._jobs = jobs

    def request_worker(self, run_snapshot: RunSnapshotWorker) -> dict[str, object]:
        try:
            ticket = self._jobs.request_worker(run_snapshot)
        except ContractError as exc:
            try:
                code = ErrorCode(exc.code)
            except ValueError:
                code = None
            if code in {ErrorCode.STALE_LEASE, ErrorCode.RELEASE_RETIRING}:
                raise LifecycleFault(409, "worker_unavailable", _CONTRACT_MESSAGES[code]) from exc
            message = _UNKNOWN_AUTHORITY_FAILURE if code is None else _CONTRACT_MESSAGES[code]
            raise LifecycleFault(502, "authority_contract_error", message) from exc
        except Exception as exc:
            raise LifecycleFault(502, "authority_contract_error", _UNKNOWN_AUTHORITY_FAILURE) from exc
        return project_worker_ticket(ticket, run_snapshot=run_snapshot)


__all__ = ["PluginLifecycleFacade", "WorkerRequestPort"]
