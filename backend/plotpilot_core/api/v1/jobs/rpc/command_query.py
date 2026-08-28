"""Thin Job command/query application adapter over the accepted authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Protocol

from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import ContractError, ContractValidationError, ErrorCode
from backend.plotpilot_plugin_sdk.verifier import (
    assert_valid,
    hash_without_field,
    verify_job_snapshot,
)


@dataclass(frozen=True, slots=True)
class JobCommandResult:
    """Stable application result for request-key-suppressed Job creation."""

    job_id: str
    workspace_id: str
    job_state: str
    job_revision: int
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "workspace_id": self.workspace_id,
            "job_state": self.job_state,
            "job_revision": self.job_revision,
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class JobSnapshotExtensions:
    """Projection data owned by later checkpoint/stream source nodes.

    The extension reader is invoked while the accepted repository read gate is
    held, so the base Job rows and extension high-waters share one authority
    view.  No fallback table or second store is created here.
    """

    current_checkpoint_id: str | None
    stream_high_waters: tuple[Mapping[str, Any], ...]


class JobSnapshotExtensionReader(Protocol):
    def read(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        workspace_id: str,
    ) -> JobSnapshotExtensions: ...


class JobCommandQueryAdapter:
    """Application boundary that never owns a database or Job state machine."""

    def __init__(
        self,
        authority: ExecutionAuthority,
        *,
        snapshot_extensions: JobSnapshotExtensionReader,
    ) -> None:
        repository = getattr(authority, "repository", None)
        if repository is None or not callable(getattr(repository, "read_connection", None)):
            raise TypeError("Job adapter requires the accepted ExecutionAuthority repository")
        self._authority = authority
        self._repository = repository
        if not callable(getattr(snapshot_extensions, "read", None)):
            raise TypeError("Job snapshot extension authority is required")
        self._snapshot_extensions = snapshot_extensions

    @property
    def authority(self) -> ExecutionAuthority:
        return self._authority

    def create_or_get(
        self,
        *,
        job_id: str,
        run_snapshot: Mapping[str, Any],
    ) -> JobCommandResult:
        """Create one Job or return the request-key-bound existing Job."""

        snapshot = dict(run_snapshot)
        existing = self._authority.find_by_request_key(
            str(snapshot.get("workspace_id", "")),
            str(snapshot.get("request_key", "")),
        )
        row = self._authority.create_from_verified_snapshot(job_id, snapshot)
        replayed = existing is not None or row["job_id"] != job_id
        return JobCommandResult(
            job_id=str(row["job_id"]),
            workspace_id=str(row["workspace_id"]),
            job_state=str(row["job_state"]),
            job_revision=int(row["job_revision"]),
            replayed=replayed,
        )

    def freeze_plan(
        self,
        *,
        job_id: str,
        steps: list[Mapping[str, Any]],
        output_step_id: str,
    ) -> None:
        """Delegate plan freezing; the adapter performs no state mutation."""

        self._authority.freeze_plan(job_id, steps, output_step_id=output_step_id)

    def start_attempt(self, **command: Any) -> None:
        """Delegate Attempt start to the accepted Job authority unchanged."""

        self._authority.start_attempt(**command)

    def get_snapshot(self, *, workspace_id: str, job_id: str) -> dict[str, Any]:
        """Project one authoritative ``job-snapshot/v1`` under one read gate."""

        with self._repository.read_connection() as connection:
            job = connection.execute(
                "SELECT * FROM execution_job WHERE workspace_id=? AND job_id=?",
                (workspace_id, job_id),
            ).fetchone()
            if job is None:
                # Cross-Workspace and unknown Job deliberately share one error.
                raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown Job")
            steps = connection.execute(
                "SELECT step_id,state,revision FROM execution_step WHERE job_id=? "
                "ORDER BY step_ordinal,step_id",
                (job_id,),
            ).fetchall()
            attempts = connection.execute(
                "SELECT a.attempt_id,a.state,a.lease_epoch FROM execution_attempt a "
                "JOIN execution_step s ON s.step_id=a.step_id "
                "WHERE a.job_id=? ORDER BY s.step_ordinal,a.ordinal,a.attempt_id",
                (job_id,),
            ).fetchall()
            candidate_rows = connection.execute(
                "SELECT candidate_id FROM execution_candidate_binding WHERE job_id=? "
                "ORDER BY candidate_id",
                (job_id,),
            ).fetchall()
            extensions = self._snapshot_extensions.read(
                connection,
                job_id=job_id,
                workspace_id=workspace_id,
            )

            snapshot: dict[str, Any] = {
                "schema": "job-snapshot/v1",
                "job_id": str(job["job_id"]),
                "workspace_id": str(job["workspace_id"]),
                "job_state": str(job["job_state"]),
                "job_revision": int(job["job_revision"]),
                "steps": [
                    {
                        "step_id": str(row["step_id"]),
                        "state": str(row["state"]),
                        "revision": int(row["revision"]),
                    }
                    for row in steps
                ],
                "attempts": [
                    {
                        "attempt_id": str(row["attempt_id"]),
                        "state": str(row["state"]),
                        "lease_epoch": int(row["lease_epoch"]),
                    }
                    for row in attempts
                ],
                "candidate_ids": [str(row["candidate_id"]) for row in candidate_rows],
                "current_checkpoint_id": extensions.current_checkpoint_id,
                "stream_high_waters": [dict(value) for value in extensions.stream_high_waters],
                "core_event_high_water": int(job["core_event_high_water"]),
                "job_event_high_water": int(job["job_event_high_water"]),
                "created_at": str(job["created_at"]),
                "snapshot_hash": "",
            }
            snapshot["snapshot_hash"] = hash_without_field(
                snapshot,
                "snapshot_hash",
                "job-snapshot/v1",
            )
            verify_job_snapshot(snapshot)
            return snapshot

    def get_event_page(
        self,
        *,
        workspace_id: str,
        job_id: str,
        after_job_event_seq: int = 0,
    ) -> dict[str, Any]:
        """Project committed Job events without inventing a second cursor store."""

        if (
            isinstance(after_job_event_seq, bool)
            or not isinstance(after_job_event_seq, int)
            or after_job_event_seq < 0
        ):
            raise ContractValidationError(
                "after_job_event_seq must be a non-negative integer"
            )
        with self._repository.read_connection() as connection:
            job = connection.execute(
                "SELECT job_event_high_water FROM execution_job "
                "WHERE workspace_id=? AND job_id=?",
                (workspace_id, job_id),
            ).fetchone()
            if job is None:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown Job")
            high_water = int(job["job_event_high_water"])
            rows = connection.execute(
                "SELECT job_event_seq,event_json FROM execution_job_event "
                "WHERE job_id=? AND job_event_seq>? AND job_event_seq<=? "
                "ORDER BY job_event_seq",
                (job_id, after_job_event_seq, high_water),
            ).fetchall()
            events: list[dict[str, Any]] = []
            for row in rows:
                event = json.loads(str(row["event_json"]))
                assert_valid("plugin-job-event/v1", event)
                if event["job_id"] != job_id or event["job_event_seq"] != row["job_event_seq"]:
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "stored Job event identity is inconsistent",
                    )
                events.append(event)
            next_seq = (
                int(rows[-1]["job_event_seq"]) + 1
                if rows
                else after_job_event_seq + 1
            )
            page = {
                "schema": "job-event-page/v1",
                "job_id": job_id,
                "after_job_event_seq": after_job_event_seq,
                "events": events,
                "next_job_event_seq": next_seq,
                "high_water_seq": high_water,
            }
            assert_valid("job-event-page/v1", page)
            return page
