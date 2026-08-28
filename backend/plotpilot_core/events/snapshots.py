from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import sqlite3
from typing import Any

from backend.plotpilot_plugin_sdk import assert_valid, canonical_bytes, verify_core_snapshot
from backend.plotpilot_plugin_sdk.verifier import hash_without_field, verify_job_snapshot

from ..assets import AssetStore
from ..broker.service import TERMINAL_STATES
from ..domain.entities import utc_now
from ..repositories.authority import CoreAuthorityRepository
from .store import CoreEventStore, JobEventStore, StreamKind


@dataclass(frozen=True, slots=True)
class PersistedSnapshot:
    stream_kind: StreamKind
    aggregate_id: str | None
    revision: int
    high_water_seq: int
    asset_id: str
    snapshot_hash: str
    value: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PersistedJobEventPage:
    job_id: str
    after_job_event_seq: int
    next_job_event_seq: int
    high_water_seq: int
    asset_id: str
    value: Mapping[str, Any]


JobRuntimeProjectionReader = Callable[[sqlite3.Connection, str], Mapping[str, Any]]


class SnapshotHighWaterChanged(RuntimeError):
    """The authority advanced between recovery planning and snapshot capture."""


class JobSnapshotStore:
    """Materialize a contract-valid Job snapshot from durable authority rows."""

    def __init__(
        self,
        repository: CoreAuthorityRepository,
        assets: AssetStore,
        runtime_projection_reader: JobRuntimeProjectionReader,
    ) -> None:
        self.repository = repository
        self.assets = assets
        self.runtime_projection_reader = runtime_projection_reader

    def capture(self, job_id: str, high_water_seq: int | None = None) -> PersistedSnapshot:
        with self.repository.read_connection() as connection:
            job = connection.execute("SELECT * FROM execution_job WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            durable_high_water = int(job["job_event_high_water"])
            if high_water_seq is not None and high_water_seq != durable_high_water:
                raise SnapshotHighWaterChanged("Job snapshot high-water advanced during capture")
            steps = [
                {"step_id": row["step_id"], "state": row["state"], "revision": int(row["revision"])}
                for row in connection.execute(
                    "SELECT step_id,state,revision FROM execution_step WHERE job_id=? ORDER BY step_ordinal,step_id",
                    (job_id,),
                ).fetchall()
            ]
            attempts = [
                {"attempt_id": row["attempt_id"], "state": row["state"], "lease_epoch": int(row["lease_epoch"])}
                for row in connection.execute(
                    "SELECT attempt_id,state,lease_epoch FROM execution_attempt WHERE job_id=? ORDER BY ordinal,attempt_id",
                    (job_id,),
                ).fetchall()
            ]
            candidates = [
                row[0]
                for row in connection.execute(
                    "SELECT candidate_id FROM execution_candidate_binding WHERE job_id=? ORDER BY candidate_id",
                    (job_id,),
                ).fetchall()
            ]
            runtime = dict(self.runtime_projection_reader(connection, job_id))
            if set(runtime) != {"current_checkpoint_id", "stream_high_waters"}:
                raise ValueError("Job runtime projection must be a closed checkpoint/stream view")
            value: dict[str, Any] = {
                "schema": "job-snapshot/v1",
                "job_id": job_id,
                "workspace_id": job["workspace_id"],
                "job_state": job["job_state"],
                "job_revision": int(job["job_revision"]),
                "steps": steps,
                "attempts": attempts,
                "candidate_ids": candidates,
                "current_checkpoint_id": runtime["current_checkpoint_id"],
                "stream_high_waters": runtime["stream_high_waters"],
                "core_event_high_water": int(job["core_event_high_water"]),
                "job_event_high_water": durable_high_water,
                "created_at": utc_now(),
            }
        value["snapshot_hash"] = hash_without_field(value, "snapshot_hash", "job-snapshot/v1")
        verify_job_snapshot(value)
        metadata = self.assets.put(
            canonical_bytes(value),
            mime="application/json",
            logical_role="job-snapshot",
            provenance=f"event-sse:{job_id}:{durable_high_water}",
        )
        return PersistedSnapshot(
            "job_event",
            job_id,
            int(value["job_revision"]),
            durable_high_water,
            metadata.asset_id,
            value["snapshot_hash"],
            value,
        )


class JobEventPageStore:
    """Materialize the frozen Broker Job Event page as an immutable Asset."""

    def __init__(self, events: JobEventStore, assets: AssetStore) -> None:
        self.events = events
        self.assets = assets

    def capture(self, job_id: str, after_job_event_seq: int, *, limit: int = 1000) -> PersistedJobEventPage:
        window = self.events.window(job_id, after_job_event_seq, limit=limit)
        if window.gap:
            raise ValueError("Job Event page cursor is below the retention floor")
        events = list(window.events)
        next_sequence = (
            int(events[-1]["job_event_seq"]) + 1 if events else after_job_event_seq + 1
        )
        value = {
            "schema": "job-event-page/v1",
            "job_id": job_id,
            "after_job_event_seq": after_job_event_seq,
            "events": events,
            "next_job_event_seq": next_sequence,
            "high_water_seq": window.durable_high_water_seq,
        }
        assert_valid("job-event-page/v1", value)
        metadata = self.assets.put(
            canonical_bytes(value),
            mime="application/json",
            logical_role="job-event-page",
            provenance=f"event-sse:{job_id}:{after_job_event_seq}:{window.durable_high_water_seq}",
        )
        return PersistedJobEventPage(
            job_id,
            after_job_event_seq,
            next_sequence,
            window.durable_high_water_seq,
            metadata.asset_id,
            value,
        )


class JobPollProjectionStore:
    """Production adapter for BrokerChildSnapshotPort.project_poll."""

    _TERMINAL = set(TERMINAL_STATES)

    def __init__(
        self,
        repository: CoreAuthorityRepository,
        snapshots: JobSnapshotStore,
        pages: JobEventPageStore,
    ) -> None:
        self.repository = repository
        self.snapshots = snapshots
        self.pages = pages

    def project_poll(
        self,
        *,
        child_job_id: str,
        after_job_event_seq: int,
        execution_terminal: bool,
        execution_events: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        # Hold the repository reader gate across both projections so their
        # high-waters cannot diverge.  Observed execution events are not used
        # as authority; the durable table is the only page source.
        with self.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT job_state,result_bundle_asset_id,provenance_receipt_id,job_event_high_water "
                "FROM execution_job WHERE job_id=?",
                (child_job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(child_job_id)
            snapshot = self.snapshots.capture(child_job_id, int(row["job_event_high_water"]))
            page = self.pages.capture(child_job_id, after_job_event_seq)
        if snapshot.high_water_seq != page.high_water_seq:
            raise RuntimeError("Job snapshot and Event page high-waters diverged")
        terminal = row["job_state"] in self._TERMINAL
        if terminal != execution_terminal:
            raise ValueError("execution terminal observation drifted from durable Job authority")
        if any(not isinstance(item, Mapping) for item in execution_events):
            raise ValueError("execution events must be objects")
        return {
            "job_snapshot_asset_id": snapshot.asset_id,
            "job_event_page_asset_id": page.asset_id,
            "next_job_event_seq": page.next_job_event_seq,
            "terminal": terminal,
            "result_bundle_asset_id": row["result_bundle_asset_id"],
            "provenance_receipt_id": row["provenance_receipt_id"],
            "child_state": row["job_state"],
        }


AggregateReader = Callable[
    [sqlite3.Connection, str | None, tuple[str, ...]], Sequence[Mapping[str, Any]]
]


class CoreSnapshotStore:
    """Persist a coverage-complete Core snapshot supplied by the Core projector.

    Aggregate state remains Core-owned.  This store intentionally accepts a
    projector callback instead of deriving a second state authority from Event
    payloads.
    """

    def __init__(
        self,
        repository: CoreAuthorityRepository,
        assets: AssetStore,
        aggregate_reader: AggregateReader,
    ) -> None:
        self.repository = repository
        self.assets = assets
        self.aggregate_reader = aggregate_reader
        self.events = CoreEventStore(repository)

    def capture(
        self,
        high_water_seq: int,
        *,
        workspace_id: str | None,
        event_types: Sequence[str],
    ) -> PersistedSnapshot:
        normalized_types = tuple(sorted(set(event_types)))
        with self.repository.read_connection() as connection:
            durable = self.events._high_water(connection)
            if durable != high_water_seq:
                raise SnapshotHighWaterChanged("Core snapshot high-water advanced during capture")
            aggregates = [dict(item) for item in self.aggregate_reader(connection, workspace_id, normalized_types)]
        aggregates.sort(key=lambda item: (item["aggregate_type"], item["aggregate_id"]))
        for aggregate in aggregates:
            self.assets.require(aggregate["state_asset_id"], sha256=aggregate["state_hash"])
        scope_fingerprint = hashlib.sha256(
            canonical_bytes({"workspace_id": workspace_id, "event_types": list(normalized_types)})
        ).hexdigest()[:32]
        revision = max(1, high_water_seq)
        value: dict[str, Any] = {
            "schema": "core-snapshot/v1",
            "snapshot_id": f"core-snapshot-{scope_fingerprint}-{revision}",
            "subscription_scope": {
                "workspace_id": workspace_id,
                "event_types": list(normalized_types),
            },
            "core_snapshot_revision": revision,
            "core_event_high_water": high_water_seq,
            "coverage_complete": True,
            "covered_aggregates": aggregates,
            "created_at": utc_now(),
        }
        value["snapshot_hash"] = hash_without_field(value, "snapshot_hash", "core-snapshot/v1")
        verify_core_snapshot(value)
        metadata = self.assets.put(
            canonical_bytes(value),
            mime="application/json",
            logical_role="core-snapshot",
            provenance=f"event-sse:core:{scope_fingerprint}:{high_water_seq}",
        )
        return PersistedSnapshot(
            "core_event",
            None,
            revision,
            high_water_seq,
            metadata.asset_id,
            value["snapshot_hash"],
            value,
        )


__all__ = [
    "AggregateReader",
    "CoreSnapshotStore",
    "JobEventPageStore",
    "JobPollProjectionStore",
    "JobRuntimeProjectionReader",
    "JobSnapshotStore",
    "PersistedJobEventPage",
    "PersistedSnapshot",
    "SnapshotHighWaterChanged",
]
