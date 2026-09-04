"""Frozen v2 Job SSE recovery over the sole P1 SQLite authority."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any, cast

from backend.plotpilot_core.events import (
    EventRecoveryService,
    JobEventStore,
    JobSnapshotStore,
)
from backend.plotpilot_core.events.store import ReplayWindow, pinned_read_transaction
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode
from backend.plotpilot_plugin_sdk.core_api_v2 import (
    parse_job_http_v2,
    parse_job_sse_recovery_v2,
)


class _PinnedJobEventStore:
    """Bind the existing JobEventStore reader to one caller-owned snapshot."""

    def __init__(self, events: JobEventStore, connection: sqlite3.Connection) -> None:
        self._events = events
        self._connection = connection

    def window(
        self,
        job_id: str,
        after_seq: int,
        *,
        limit: int = 1000,
    ) -> ReplayWindow:
        return self._events.window(
            job_id,
            after_seq,
            limit=limit,
            connection=self._connection,
        )


class JobSSEAdapter:
    """Return a contract-validated finite recovery view for one durable Job.

    The Job row, retention floor, optional snapshot, and replay tail are all
    read through one deferred SQLite transaction.  The adapter owns no cursor
    or Job state and remains valid after the repository is reopened.
    """

    def __init__(
        self,
        recovery: EventRecoveryService,
        snapshots: JobSnapshotStore,
        *,
        replay_limit: int = 10_000,
    ) -> None:
        if (
            isinstance(replay_limit, bool)
            or not isinstance(replay_limit, int)
            or replay_limit < 1
            or replay_limit > 10_000
        ):
            raise ValueError("SSE replay_limit must be between 1 and 10000")
        repository = recovery.job_events.repository
        if (
            snapshots.repository is not repository
            or recovery.core_events.repository is not repository
        ):
            raise TypeError("v2 SSE recovery requires one P1 repository authority")
        self.recovery = recovery
        self.snapshots = snapshots
        self.events = recovery.job_events
        self.repository = repository
        self.replay_limit = replay_limit

    def recover_query(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and execute a frozen ``job-sse-recovery-query/v2``."""

        parsed = parse_job_http_v2(request)
        if parsed.get("schema") != "job-sse-recovery-query/v2":
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Job SSE adapter received a non-recovery query",
            )
        return self._recover_parsed(parsed)

    def recover(
        self,
        *,
        workspace_id: str,
        job_id: str,
        after_seq: int,
        last_event_id: str,
        requested_cursor_domain: str = "job",
    ) -> dict[str, Any]:
        return self.recover_query(
            {
                "schema": "job-sse-recovery-query/v2",
                "workspace_id": workspace_id,
                "job_id": job_id,
                "after_seq": after_seq,
                "last_event_id": last_event_id,
                "requested_cursor_domain": requested_cursor_domain,
            }
        )

    def _recover_parsed(self, request: Mapping[str, Any]) -> dict[str, Any]:
        workspace_id = str(request["workspace_id"])
        job_id = str(request["job_id"])
        after_seq = int(request["after_seq"])

        with pinned_read_transaction(self.repository) as connection:
            job = connection.execute(
                "SELECT job_revision,job_event_high_water FROM execution_job "
                "WHERE workspace_id=? AND job_id=?",
                (workspace_id, job_id),
            ).fetchone()
            if job is None:
                # Deliberately do not disclose whether the Job exists elsewhere.
                raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown Job")

            pinned_events = _PinnedJobEventStore(self.events, connection)
            pinned_recovery = EventRecoveryService(
                self.recovery.core_events,
                cast(JobEventStore, pinned_events),
            )
            captured_v2 = None

            def snapshot_provider(high_water: int):
                nonlocal captured_v2
                # EventRecoveryService still verifies its frozen v1 metadata;
                # capture that compatibility Asset and the additive v2 value
                # from the exact same P1 read transaction.
                compatibility = self.snapshots.capture(
                    job_id,
                    high_water,
                    connection=connection,
                )
                captured_v2 = self.snapshots.capture_v2(
                    job_id,
                    high_water,
                    workspace_id=workspace_id,
                    connection=connection,
                )
                if compatibility.high_water_seq != captured_v2.high_water_seq:
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "v1/v2 Job snapshot high-water binding diverged",
                    )
                return compatibility

            plan = pinned_recovery.job(
                job_id,
                after_seq,
                limit=self.replay_limit,
                snapshot_provider=snapshot_provider,
            )
            durable = int(plan.recovery["durable_high_water_seq"])
            if durable != int(job["job_event_high_water"]):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "SSE durable high-water diverged from the authoritative Job row",
                )
            if plan.next_after_seq != durable:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "SSE recovery tail exceeds the bounded durable replay window",
                )
            tail = tuple(
                self._project_event(event, aggregate_revision=int(job["job_revision"]))
                for event in plan.events
            )

        gap = bool(plan.recovery["gap"])
        if gap != (captured_v2 is not None):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "SSE retention gap is not paired with its v2 Job snapshot",
            )
        snapshot = None if captured_v2 is None else dict(captured_v2.value)
        snapshot_sequence = (
            durable if captured_v2 is None else captured_v2.high_water_seq
        )
        result = {
            "schema": "job-sse-recovery-result/v2",
            "workspace_id": workspace_id,
            "job_id": job_id,
            "requested_after_seq": after_seq,
            "replay_floor_seq": int(plan.recovery["replay_floor_seq"]),
            "durable_high_water_seq": durable,
            "gap": gap,
            "snapshot_required": gap,
            "snapshot": snapshot,
            "snapshot_cursor": f"job/{job_id}/{snapshot_sequence}",
            "tail": list(tail),
        }
        return parse_job_sse_recovery_v2(result)

    @staticmethod
    def _project_event(
        event: Mapping[str, Any],
        *,
        aggregate_revision: int,
    ) -> dict[str, Any]:
        """Narrow a validated Plugin Job Event to the public v2 event shape."""

        return {
            "event_id": event.get("event_id"),
            "job_id": event.get("job_id"),
            "job_event_seq": event.get("job_event_seq"),
            "event_type": event.get("event_type"),
            "aggregate_revision": max(1, aggregate_revision),
            "payload_asset_id": event.get("payload_asset_id"),
            "payload_hash": event.get("payload_hash"),
            "occurred_at": event.get("occurred_at"),
        }

    job_replay = recover
    replay = recover


JobSSERecoveryAdapter = JobSSEAdapter

__all__ = ["JobSSEAdapter", "JobSSERecoveryAdapter"]
