from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

from backend.plotpilot_plugin_sdk import (
    ContractError,
    ErrorCode,
    assert_valid,
    canonical_bytes,
    verify_core_snapshot,
)
from backend.plotpilot_plugin_sdk.core_api_v2 import parse_job_snapshot_v2
from backend.plotpilot_plugin_sdk.verifier import (
    hash_without_field,
    verify_job_snapshot,
)

from ..assets import AssetStore
from ..broker.service import TERMINAL_STATES
from ..domain.entities import utc_now
from ..repositories.authority import CoreAuthorityRepository
from .store import CoreEventStore, JobEventStore, StreamKind, pinned_read_transaction


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


def next_job_event_seq_to_after(next_job_event_seq: int) -> int:
    """Convert the page's next unread sequence to an exclusive replay cursor."""

    if (
        isinstance(next_job_event_seq, bool)
        or not isinstance(next_job_event_seq, int)
        or next_job_event_seq < 1
    ):
        raise ValueError("next_job_event_seq must be a positive integer")
    return next_job_event_seq - 1


@contextmanager
def _snapshot_reader(
    repository: CoreAuthorityRepository,
    connection: sqlite3.Connection | None,
) -> Iterator[sqlite3.Connection]:
    if connection is not None:
        if not connection.in_transaction:
            raise RuntimeError(
                "Snapshot projection requires an active SQLite read transaction"
            )
        yield connection
        return
    with pinned_read_transaction(repository) as owned:
        yield owned


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

    def _runtime_projection(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        """Normalize the accepted P1 extension reader without adding authority."""

        read = getattr(self.runtime_projection_reader, "read", None)
        if callable(read):
            raw = read(connection, job_id=job_id, workspace_id=workspace_id)
        else:
            raw = self.runtime_projection_reader(connection, job_id)

        if isinstance(raw, Mapping):
            keys = set(raw)
            if "stream_high_waters" not in keys or not keys <= {
                "current_checkpoint_id",
                "checkpoint_id",
                "stream_high_waters",
            }:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "Job runtime projection must be a closed checkpoint/stream view",
                )
            checkpoint_values = [
                raw[name]
                for name in ("current_checkpoint_id", "checkpoint_id")
                if name in raw
            ]
            if not checkpoint_values or any(
                value != checkpoint_values[0] for value in checkpoint_values[1:]
            ):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "Job runtime checkpoint projection is missing or divergent",
                )
            streams = raw["stream_high_waters"]
            checkpoint_id = checkpoint_values[0]
        else:
            checkpoint = getattr(raw, "checkpoint", ...)
            streams = getattr(raw, "stream_high_waters", ...)
            if checkpoint is ... or streams is ...:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "Job runtime projection reader returned an untyped view",
                )
            checkpoint_id = None
            if checkpoint is not None:
                if (
                    getattr(checkpoint, "workspace_id", None) != workspace_id
                    or getattr(checkpoint, "job_id", None) != job_id
                ):
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "checkpoint projection crosses Workspace or Job identity",
                    )
                checkpoint_id = getattr(checkpoint, "checkpoint_id", None)
            projected_streams: list[Mapping[str, Any]] = []
            for binding in streams:
                if (
                    getattr(binding, "workspace_id", None) != workspace_id
                    or getattr(binding, "job_id", None) != job_id
                    or not isinstance(getattr(binding, "high_water", None), Mapping)
                ):
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "stream projection crosses Workspace or Job identity",
                    )
                projected_streams.append(binding.high_water)
            streams = projected_streams

        if checkpoint_id is not None and not isinstance(checkpoint_id, str):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Job runtime checkpoint ID is invalid",
            )
        if isinstance(streams, (str, bytes, bytearray)) or not isinstance(
            streams, Sequence
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Job runtime stream high-waters must be a sequence",
            )
        if any(not isinstance(item, Mapping) for item in streams):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Job runtime stream high-water is not an object",
            )
        return {
            "current_checkpoint_id": checkpoint_id,
            "stream_high_waters": [dict(item) for item in streams],
        }

    @staticmethod
    def _current_writer(
        connection: sqlite3.Connection,
        job: sqlite3.Row,
    ) -> tuple[str | None, int]:
        """Project the scalar v2 writer from P1 Attempt/Step authority."""

        rows = connection.execute(
            "SELECT s.step_id,s.active_attempt_id,s.is_output,s.step_ordinal,"
            "a.attempt_id,a.job_id AS attempt_job_id,a.step_id AS attempt_step_id,"
            "a.state AS attempt_state,a.lease_epoch "
            "FROM execution_step s LEFT JOIN execution_attempt a "
            "ON a.attempt_id=s.active_attempt_id WHERE s.job_id=? "
            "ORDER BY s.step_ordinal,s.step_id",
            (job["job_id"],),
        ).fetchall()
        for row in rows:
            if row["active_attempt_id"] is not None and (
                row["attempt_id"] != row["active_attempt_id"]
                or row["attempt_job_id"] != job["job_id"]
                or row["attempt_step_id"] != row["step_id"]
            ):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "active Attempt pointer is outside the authoritative Job Step",
                )

        live_states = {"created", "running", "cancelling", "suspended"}
        live = [row for row in rows if row["attempt_state"] in live_states]
        if len(live) > 1:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Job has multiple live Attempts but the v2 snapshot writer is scalar",
            )
        current = live[0] if live else None
        if current is None and job["job_state"] in {
            "succeeded",
            "partial",
            "failed",
            "cancelled",
        }:
            output = [
                row
                for row in rows
                if row["step_id"] == job["output_step_id"]
                and row["active_attempt_id"] is not None
            ]
            current = output[0] if len(output) == 1 else None

        latest_epoch = connection.execute(
            "SELECT MAX(lease_epoch) FROM execution_attempt WHERE job_id=?",
            (job["job_id"],),
        ).fetchone()[0]
        if current is None:
            return None, max(1, 0 if latest_epoch is None else int(latest_epoch))
        return str(current["attempt_id"]), int(current["lease_epoch"])

    @staticmethod
    def _v2_stream_high_waters(
        streams: Sequence[Mapping[str, Any]],
        *,
        workspace_id: str,
    ) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for stream in streams:
            if {
                "acked_prefix_seq",
                "acked_bytes",
                "acked_prefix_hash",
            } <= set(stream):
                sequence = stream["acked_prefix_seq"]
                byte_length = stream["acked_bytes"]
                prefix_hash = stream["acked_prefix_hash"]
            elif {"prefix_seq", "byte_length", "prefix_hash"} <= set(stream):
                sequence = stream["prefix_seq"]
                byte_length = stream["byte_length"]
                prefix_hash = stream["prefix_hash"]
            else:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "stream projection has no durable acknowledged prefix",
                )
            value = {
                "stream_id": stream.get("stream_id"),
                "output_role": stream.get("output_role"),
                "target": stream.get("target"),
                "acked_prefix_seq": sequence,
                "acked_bytes": byte_length,
                "acked_prefix_hash": prefix_hash,
            }
            target = value["target"]
            if (
                not isinstance(target, Mapping)
                or target.get("workspace_id") != workspace_id
            ):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "stream target crosses the authoritative Job Workspace",
                )
            projected.append(value)
        projected.sort(
            key=lambda item: (str(item["output_role"]), str(item["stream_id"]))
        )
        stream_ids = [item["stream_id"] for item in projected]
        if len(stream_ids) != len(set(stream_ids)):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "stream projection contains duplicate stream_id",
            )
        return projected

    def capture(
        self,
        job_id: str,
        high_water_seq: int | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> PersistedSnapshot:
        with _snapshot_reader(self.repository, connection) as reader:
            job = reader.execute(
                "SELECT * FROM execution_job WHERE job_id=?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            durable_high_water = int(job["job_event_high_water"])
            if high_water_seq is not None and high_water_seq != durable_high_water:
                raise SnapshotHighWaterChanged(
                    "Job snapshot high-water advanced during capture"
                )
            steps = [
                {
                    "step_id": row["step_id"],
                    "state": row["state"],
                    "revision": int(row["revision"]),
                }
                for row in reader.execute(
                    "SELECT step_id,state,revision FROM execution_step WHERE job_id=? ORDER BY step_ordinal,step_id",
                    (job_id,),
                ).fetchall()
            ]
            attempts = [
                {
                    "attempt_id": row["attempt_id"],
                    "state": row["state"],
                    "lease_epoch": int(row["lease_epoch"]),
                }
                for row in reader.execute(
                    "SELECT attempt_id,state,lease_epoch FROM execution_attempt WHERE job_id=? ORDER BY ordinal,attempt_id",
                    (job_id,),
                ).fetchall()
            ]
            candidates = [
                row[0]
                for row in reader.execute(
                    "SELECT candidate_id FROM execution_candidate_binding WHERE job_id=? ORDER BY candidate_id",
                    (job_id,),
                ).fetchall()
            ]
            runtime = self._runtime_projection(
                reader,
                job_id=job_id,
                workspace_id=str(job["workspace_id"]),
            )
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
                "created_at": str(job["created_at"]),
            }
        value["snapshot_hash"] = hash_without_field(
            value, "snapshot_hash", "job-snapshot/v1"
        )
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

    def capture_v2(
        self,
        job_id: str,
        high_water_seq: int | None = None,
        *,
        workspace_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> PersistedSnapshot:
        """Capture the frozen public v2 Job projection in one SQLite read view."""

        with _snapshot_reader(self.repository, connection) as reader:
            job = reader.execute(
                "SELECT * FROM execution_job WHERE workspace_id=? AND job_id=?",
                (workspace_id, job_id),
            ).fetchone()
            if job is None:
                # Unknown and cross-Workspace Jobs deliberately have one shape.
                raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown Job")
            durable_high_water = int(job["job_event_high_water"])
            if high_water_seq is not None and high_water_seq != durable_high_water:
                raise SnapshotHighWaterChanged(
                    "Job snapshot high-water advanced during capture"
                )
            runtime = self._runtime_projection(
                reader,
                job_id=job_id,
                workspace_id=workspace_id,
            )
            checkpoint_id = runtime["current_checkpoint_id"]
            if checkpoint_id != job["current_checkpoint_id"]:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "runtime checkpoint diverged from the authoritative Job row",
                )
            if checkpoint_id is not None:
                checkpoint = reader.execute(
                    "SELECT job_id FROM execution_checkpoint WHERE checkpoint_id=?",
                    (checkpoint_id,),
                ).fetchone()
                if checkpoint is None or checkpoint["job_id"] != job_id:
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "authoritative Job checkpoint is missing or cross-Job",
                    )
            current_attempt_id, writer_epoch = self._current_writer(reader, job)
            candidate_ids = [
                str(row["candidate_id"])
                for row in reader.execute(
                    "SELECT candidate_id FROM execution_candidate_binding "
                    "WHERE job_id=? ORDER BY candidate_id",
                    (job_id,),
                ).fetchall()
            ]
            state = str(job["job_state"])
            if state in {"waiting_user", "interrupted"}:
                state = "needs_attention"
            value: dict[str, Any] = {
                "job_id": job_id,
                "workspace_id": workspace_id,
                "state": state,
                "job_revision": int(job["job_revision"]),
                "writer_epoch": writer_epoch,
                "current_attempt_id": current_attempt_id,
                "candidate_ids": candidate_ids,
                "checkpoint_id": checkpoint_id,
                "stream_high_waters": self._v2_stream_high_waters(
                    runtime["stream_high_waters"], workspace_id=workspace_id
                ),
                "job_event_high_water": durable_high_water,
                "core_event_high_water": int(job["core_event_high_water"]),
                "created_at": str(job["created_at"]),
                "updated_at": str(job["updated_at"]),
                "snapshot_hash": "",
            }
        value["snapshot_hash"] = hash_without_field(
            value, "snapshot_hash", "job-snapshot/v2"
        )
        value = dict(parse_job_snapshot_v2(value))
        metadata = self.assets.put(
            canonical_bytes(value),
            mime="application/json",
            logical_role="job-snapshot",
            provenance=f"event-sse:v2:{job_id}:{durable_high_water}",
        )
        return PersistedSnapshot(
            "job_event",
            job_id,
            int(value["job_revision"]),
            durable_high_water,
            metadata.asset_id,
            str(value["snapshot_hash"]),
            value,
        )


class JobEventPageStore:
    """Materialize the frozen Broker Job Event page as an immutable Asset."""

    def __init__(self, events: JobEventStore, assets: AssetStore) -> None:
        self.events = events
        self.assets = assets

    def capture(
        self,
        job_id: str,
        after_job_event_seq: int,
        *,
        limit: int = 1000,
        connection: sqlite3.Connection | None = None,
    ) -> PersistedJobEventPage:
        window = self.events.window(
            job_id,
            after_job_event_seq,
            limit=limit,
            connection=connection,
        )
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

    _TERMINAL: ClassVar[set[str]] = set(TERMINAL_STATES)

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
        # Pin one real SQLite snapshot across terminal anchors, aggregate
        # projection and Event page. Observed execution events are not used as
        # authority; the durable table is the only page source.
        with pinned_read_transaction(self.repository) as connection:
            row = connection.execute(
                "SELECT job_state,result_bundle_asset_id,provenance_receipt_id,job_event_high_water "
                "FROM execution_job WHERE job_id=?",
                (child_job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(child_job_id)
            snapshot = self.snapshots.capture(
                child_job_id,
                int(row["job_event_high_water"]),
                connection=connection,
            )
            page = self.pages.capture(
                child_job_id,
                after_job_event_seq,
                connection=connection,
            )
        if snapshot.high_water_seq != page.high_water_seq:
            raise RuntimeError("Job snapshot and Event page high-waters diverged")
        terminal = row["job_state"] in self._TERMINAL
        if terminal != execution_terminal:
            raise ValueError(
                "execution terminal observation drifted from durable Job authority"
            )
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
        connection: sqlite3.Connection | None = None,
    ) -> PersistedSnapshot:
        normalized_types = tuple(sorted(set(event_types)))
        with _snapshot_reader(self.repository, connection) as reader:
            durable = self.events._high_water(reader)
            if durable != high_water_seq:
                raise SnapshotHighWaterChanged(
                    "Core snapshot high-water advanced during capture"
                )
            aggregates = [
                dict(item)
                for item in self.aggregate_reader(
                    reader, workspace_id, normalized_types
                )
            ]
        aggregates.sort(key=lambda item: (item["aggregate_type"], item["aggregate_id"]))
        for aggregate in aggregates:
            self.assets.require(
                aggregate["state_asset_id"], sha256=aggregate["state_hash"]
            )
        scope_fingerprint = hashlib.sha256(
            canonical_bytes(
                {"workspace_id": workspace_id, "event_types": list(normalized_types)}
            )
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
        value["snapshot_hash"] = hash_without_field(
            value, "snapshot_hash", "core-snapshot/v1"
        )
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
    "next_job_event_seq_to_after",
]
