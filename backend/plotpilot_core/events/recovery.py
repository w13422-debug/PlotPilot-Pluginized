from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from backend.plotpilot_plugin_sdk import ContractError, ErrorCode, verify_sse_recovery

from .snapshots import PersistedSnapshot, SnapshotHighWaterChanged
from .store import CoreEventStore, JobEventStore, ReplayWindow, StreamKind


SnapshotProvider = Callable[[int], PersistedSnapshot]


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    recovery: Mapping[str, Any]
    snapshot: PersistedSnapshot | None
    events: tuple[dict[str, Any], ...]
    next_after_seq: int


def _recovery_value(window: ReplayWindow, snapshot: PersistedSnapshot | None) -> dict[str, Any]:
    derived_gap = window.requested_after_seq < window.replay_floor_seq
    if derived_gap != (snapshot is not None):
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "SSE gap does not match the retention floor")
    if window.stream_kind == "core_event" and window.aggregate_id is not None:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "Core cursor cannot carry a Job aggregate")
    if window.stream_kind == "job_event" and not window.aggregate_id:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "Job cursor requires its Job aggregate")
    value = {
        "schema": "sse-recovery/v1",
        "stream_kind": window.stream_kind,
        "aggregate_id": window.aggregate_id,
        "requested_after_seq": window.requested_after_seq,
        "replay_floor_seq": window.replay_floor_seq,
        "durable_high_water_seq": window.durable_high_water_seq,
        "gap": derived_gap,
        "snapshot_required": derived_gap,
        "snapshot_schema": None if snapshot is None else str(snapshot.value["schema"]),
        "snapshot_revision": None if snapshot is None else snapshot.revision,
        "snapshot_asset_id": None if snapshot is None else snapshot.asset_id,
        "snapshot_hash": None if snapshot is None else snapshot.snapshot_hash,
    }
    verify_sse_recovery(value)
    return value


def _validate_snapshot(snapshot: PersistedSnapshot, window: ReplayWindow) -> None:
    if snapshot.stream_kind != window.stream_kind or snapshot.aggregate_id != window.aggregate_id:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "snapshot belongs to another event cursor domain")
    if snapshot.high_water_seq < window.replay_floor_seq:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "snapshot does not cover the retention gap")
    if snapshot.high_water_seq > window.durable_high_water_seq:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "snapshot is ahead of the observed durable high-water")
    field = "core_event_high_water" if window.stream_kind == "core_event" else "job_event_high_water"
    if snapshot.value.get(field) != snapshot.high_water_seq:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "snapshot high-water binding drifted")


class EventRecoveryService:
    def __init__(self, core_events: CoreEventStore, job_events: JobEventStore) -> None:
        self.core_events = core_events
        self.job_events = job_events

    def core(
        self,
        after_seq: int,
        *,
        workspace_id: str | None = None,
        event_types: Sequence[str] = (),
        limit: int = 1000,
        snapshot_provider: SnapshotProvider | None = None,
    ) -> RecoveryPlan:
        initial = self.core_events.window(
            after_seq, workspace_id=workspace_id, event_types=event_types, limit=limit
        )
        return self._finish(
            initial,
            snapshot_provider,
            lambda sequence: self.core_events.window(
                sequence, workspace_id=workspace_id, event_types=event_types, limit=limit
            ),
        )

    def job(
        self,
        job_id: str,
        after_seq: int,
        *,
        limit: int = 1000,
        snapshot_provider: SnapshotProvider | None = None,
    ) -> RecoveryPlan:
        initial = self.job_events.window(job_id, after_seq, limit=limit)
        return self._finish(
            initial,
            snapshot_provider,
            lambda sequence: self.job_events.window(job_id, sequence, limit=limit),
        )

    @staticmethod
    def _finish(
        initial: ReplayWindow,
        snapshot_provider: SnapshotProvider | None,
        replay_after: Callable[[int], ReplayWindow],
    ) -> RecoveryPlan:
        current = initial
        for _attempt in range(3):
            if not current.gap:
                return RecoveryPlan(
                    _recovery_value(current, None), None, current.events, current.next_after_seq
                )
            if snapshot_provider is None:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "retention gap requires a durable snapshot")
            try:
                snapshot = snapshot_provider(current.durable_high_water_seq)
            except SnapshotHighWaterChanged:
                current = replay_after(current.requested_after_seq)
                continue
            _validate_snapshot(snapshot, current)
            tail = replay_after(snapshot.high_water_seq)
            if tail.gap:
                current = replay_after(current.requested_after_seq)
                continue
            final_window = ReplayWindow(
                current.stream_kind,
                current.aggregate_id,
                current.requested_after_seq,
                tail.replay_floor_seq,
                tail.durable_high_water_seq,
                tail.events,
                tail.next_after_seq,
            )
            return RecoveryPlan(
                _recovery_value(final_window, snapshot), snapshot, tail.events, tail.next_after_seq
            )
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "event retention/high-water changed too quickly to capture a convergent snapshot",
        )


__all__ = ["EventRecoveryService", "RecoveryPlan", "SnapshotProvider"]
