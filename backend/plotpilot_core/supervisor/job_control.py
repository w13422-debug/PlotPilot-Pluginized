"""RunSnapshot-bound process requests.

Core owns RunSnapshot selection and durable job authority.  This adapter only
asks the P2 process supervisor for the already-selected exact release and
fails closed before a replacement release can be spawned.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from plotpilot_plugin_sdk.errors import ContractError, ErrorCode

from .models import WorkerTicket
from .rpc import RpcEvent

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def _require_id(value: str, label: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, f"{label} is not a closed identifier")


def _require_hash(value: str, label: str) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, f"{label} is not lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class RunSnapshotWorker:
    """The immutable worker binding selected by Core for one RunSnapshot."""

    run_snapshot_id: str
    worker_id: str
    plugin_id: str
    generation_id: str
    release_id: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.run_snapshot_id, "run_snapshot_id"),
            (self.worker_id, "worker_id"),
            (self.plugin_id, "plugin_id"),
            (self.generation_id, "generation_id"),
        ):
            _require_id(value, label)
        _require_hash(self.release_id, "release_id")


class ProcessSupervisorPort(Protocol):
    """Narrow P2 process-only surface consumed by job control."""

    def acquire(self, worker_id: str, *, expected_release_id: str | None = None) -> WorkerTicket: ...

    def release(self, ticket: WorkerTicket) -> None: ...

    def _peek_host_events(self, ticket: WorkerTicket) -> tuple[RpcEvent, ...]: ...

    def _dispose_host_event(self, ticket: WorkerTicket, request_id: str) -> None: ...


class JobControl:
    """Request process capacity without selecting, mutating, or retrying a run."""

    def __init__(self, supervisor: ProcessSupervisorPort) -> None:
        self._supervisor = supervisor

    def request_worker(self, run_snapshot: RunSnapshotWorker) -> WorkerTicket:
        """Acquire only the release already bound by the supplied RunSnapshot."""

        if not isinstance(run_snapshot, RunSnapshotWorker):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "request_worker requires a RunSnapshotWorker")
        ticket = self._supervisor.acquire(
            run_snapshot.worker_id,
            expected_release_id=run_snapshot.release_id,
        )
        if (
            not isinstance(ticket, WorkerTicket)
            or ticket.worker_id != run_snapshot.worker_id
            or ticket.release_id != run_snapshot.release_id
        ):
            if isinstance(ticket, WorkerTicket):
                try:
                    self._supervisor.release(ticket)
                except Exception as exc:
                    raise ContractError(
                        ErrorCode.STALE_LEASE,
                        "supervisor returned a substituted release and could not release its retain",
                    ) from exc
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "supervisor returned a worker outside the immutable RunSnapshot release",
            )
        return ticket

    def release_worker(self, ticket: WorkerTicket) -> None:
        """Release a process retain; Core remains responsible for job transitions."""

        if not isinstance(ticket, WorkerTicket):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "release_worker requires a WorkerTicket")
        self._supervisor.release(ticket)

    def peek_host_events(self, ticket: WorkerTicket) -> tuple[RpcEvent, ...]:
        """Expose a non-destructive P2 transport view to the durable job dispatcher."""

        return self._supervisor._peek_host_events(ticket)

    def dispose_host_event(self, ticket: WorkerTicket, request_id: str) -> None:
        """Dispose one already-persisted Host RPC event through the process adapter."""

        self._supervisor._dispose_host_event(ticket, request_id)


def request_worker(*, supervisor: ProcessSupervisorPort, run_snapshot: RunSnapshotWorker) -> WorkerTicket:
    """Functional entry point for composition layers that do not retain a controller."""

    return JobControl(supervisor).request_worker(run_snapshot)


__all__ = ["JobControl", "ProcessSupervisorPort", "RunSnapshotWorker", "request_worker"]
