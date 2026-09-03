"""Closed local transport projections for the Stage 1 lifecycle runtime."""
from __future__ import annotations

from collections.abc import Mapping
from typing import NoReturn

from plotpilot_core.supervisor import WorkerTicket
from plotpilot_core.supervisor.job_control import RunSnapshotWorker


class LifecycleFault(Exception):
    """Finite local failure until P0 mounts the frozen public error family."""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(message)

    def payload(self) -> dict[str, object]:
        return {
            "schema": "plugin-lifecycle-local-error/v1",
            "error_code": self.error_code,
            "message": self.message,
            "retryable": False,
        }


def _invalid(message: str) -> NoReturn:
    raise LifecycleFault(422, "invalid_request", message)


def parse_run_snapshot_worker(value: Mapping[str, object]) -> RunSnapshotWorker:
    """Accept exactly the immutable Core-selected worker binding, no aliases."""

    if not isinstance(value, Mapping):
        _invalid("request body must be an object")
    expected = {
        "run_snapshot_id",
        "worker_id",
        "plugin_id",
        "generation_id",
        "release_id",
    }
    actual = set(value)
    if actual != expected:
        _invalid("request body must contain exactly the RunSnapshot worker binding")
    try:
        return RunSnapshotWorker(
            run_snapshot_id=value["run_snapshot_id"],  # type: ignore[arg-type]
            worker_id=value["worker_id"],  # type: ignore[arg-type]
            plugin_id=value["plugin_id"],  # type: ignore[arg-type]
            generation_id=value["generation_id"],  # type: ignore[arg-type]
            release_id=value["release_id"],  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001 -- closed local transport boundary
        _invalid(f"invalid RunSnapshot worker binding: {exc}")


def project_worker_ticket(ticket: WorkerTicket, *, run_snapshot: RunSnapshotWorker) -> dict[str, object]:
    """Return a closed process receipt bound back to the original RunSnapshot."""

    if not isinstance(ticket, WorkerTicket):
        raise LifecycleFault(502, "authority_contract_error", "job control returned an untyped worker ticket")
    if ticket.worker_id != run_snapshot.worker_id or ticket.release_id != run_snapshot.release_id:
        raise LifecycleFault(409, "release_mismatch", "worker ticket differs from the immutable RunSnapshot release")
    return {
        "schema": "plugin-worker-ticket-view/v1",
        "run_snapshot_id": run_snapshot.run_snapshot_id,
        "worker_id": ticket.worker_id,
        "lifecycle_id": ticket.lifecycle_id,
        "retain_id": ticket.retain_id,
        "release_id": ticket.release_id,
        "pin_epoch": ticket.pin_epoch,
        "retire_epoch": ticket.retire_epoch,
    }


__all__ = ["LifecycleFault", "RunSnapshotWorker", "parse_run_snapshot_worker", "project_worker_ticket"]
