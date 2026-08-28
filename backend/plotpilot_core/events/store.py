from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Iterator, Literal, Mapping, Sequence

from backend.plotpilot_plugin_sdk import ContractError, ErrorCode, assert_valid

from ..repositories.authority import CoreAuthorityRepository


StreamKind = Literal["core_event", "job_event"]
_CORE_EVENT_TYPES = {
    "workspace.created",
    "revision.published",
    "candidate.staged",
    "candidate.decided",
    "job.state.changed",
    "job.terminal",
    "plugin.generation.changed",
    "plugin.release.retiring",
    "backup.completed",
    "restore.completed",
}


def _decode_event(raw: str, schema: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError(ErrorCode.ASSET_ERROR, "stored event is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ContractError(ErrorCode.ASSET_ERROR, "stored event is not an object")
    assert_valid(schema, value)
    return value


def _decode_core_row(row: sqlite3.Row) -> dict[str, Any]:
    value = _decode_event(row["event_json"], "core-event/v1")
    expected = (
        int(row["core_event_seq"]),
        row["event_id"],
        row["workspace_id"],
        row["aggregate_id"],
        int(row["aggregate_revision"]),
    )
    actual = (
        value["core_event_seq"],
        value["event_id"],
        value["workspace_id"],
        value["aggregate_id"],
        value["aggregate_revision"],
    )
    if actual != expected:
        raise ContractError(ErrorCode.ASSET_ERROR, "stored Core Event row identity drifted")
    return value


def _decode_job_row(row: sqlite3.Row) -> dict[str, Any]:
    value = _decode_event(row["event_json"], "plugin-job-event/v1")
    expected = (
        row["job_id"],
        int(row["job_event_seq"]),
        row["event_id"],
        row["attempt_id"],
        int(row["local_seq"]),
    )
    actual = (
        value["job_id"],
        value["job_event_seq"],
        value["event_id"],
        value["attempt_id"],
        value["local_seq"],
    )
    if actual != expected:
        raise ContractError(ErrorCode.ASSET_ERROR, "stored Plugin Job Event row identity drifted")
    return value


def _encode_event(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_after_seq(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "event cursor must be a non-negative integer")


def _require_limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 10_000:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "event replay limit must be between 1 and 10000")


@dataclass(frozen=True, slots=True)
class ReplayWindow:
    stream_kind: StreamKind
    aggregate_id: str | None
    requested_after_seq: int
    replay_floor_seq: int
    durable_high_water_seq: int
    events: tuple[dict[str, Any], ...]
    next_after_seq: int

    @property
    def gap(self) -> bool:
        return self.requested_after_seq < self.replay_floor_seq


class _SQLiteEventStore:
    def __init__(self, repository: CoreAuthorityRepository) -> None:
        self.repository = repository

    @contextmanager
    def _writer(self, connection: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
            return
        with self.repository.transaction() as owned:
            yield owned


class CoreEventStore(_SQLiteEventStore):
    """Durable global Core Event log backed by the accepted authority table.

    The SQLite AUTOINCREMENT sequence is the durable high-water.  Retention may
    delete old rows, but it never rewinds that high-water or reuses a committed
    cursor.  Appending through an existing authority transaction keeps the
    aggregate mutation and its Core Event atomic.
    """

    @staticmethod
    def _high_water(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='execution_core_event'").fetchone()
        return 0 if row is None else int(row[0])

    def high_water(self) -> int:
        with self.repository.read_connection() as connection:
            return self._high_water(connection)

    def append(
        self,
        event: Mapping[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        if connection is None or not connection.in_transaction:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Core Event append requires the caller's authoritative aggregate transaction",
            )
        candidate = dict(event)
        event_id = candidate.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Core Event requires event_id")
        expected_seq = candidate.pop("core_event_seq", None)
        if expected_seq is not None:
            _require_after_seq(expected_seq)
            if expected_seq == 0:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "Core Event sequence starts at one")
        payload_pair = (candidate.get("payload_asset_id"), candidate.get("payload_hash"))
        if (payload_pair[0] is None) != (payload_pair[1] is None):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Core Event payload ID/hash must be paired")

        with self._writer(connection) as writer:
            existing = writer.execute(
                "SELECT event_json FROM execution_core_event WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                stored = _decode_event(existing[0], "core-event/v1")
                comparable = dict(stored)
                stored_seq = comparable.pop("core_event_seq")
                if comparable != candidate or (expected_seq is not None and expected_seq != stored_seq):
                    raise ContractError(ErrorCode.DUPLICATE_REQUEST, "Core Event ID was reused with different content")
                return stored

            sequence = self._high_water(writer) + 1
            if expected_seq is not None and expected_seq != sequence:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "Core Event sequence is not the next durable cursor")
            stored = {**candidate, "core_event_seq": sequence}
            assert_valid("core-event-v1", stored)
            writer.execute(
                "INSERT INTO execution_core_event(core_event_seq,event_id,workspace_id,aggregate_id,aggregate_revision,event_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    sequence,
                    stored["event_id"],
                    stored["workspace_id"],
                    stored["aggregate_id"],
                    stored["aggregate_revision"],
                    _encode_event(stored),
                ),
            )
            return stored

    def prune_through(self, sequence: int) -> int:
        _require_after_seq(sequence)
        with self.repository.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM execution_core_event WHERE core_event_seq<=?", (sequence,)
            )
            return int(cursor.rowcount)

    def window(
        self,
        after_seq: int,
        *,
        workspace_id: str | None = None,
        event_types: Sequence[str] = (),
        limit: int = 1000,
    ) -> ReplayWindow:
        _require_after_seq(after_seq)
        _require_limit(limit)
        normalized_types = tuple(sorted(set(event_types)))
        if any(not isinstance(item, str) or not item for item in normalized_types):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Core Event type filter is invalid")
        with self.repository.read_connection() as connection:
            high_water = self._high_water(connection)
            if after_seq > high_water:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "SSE cursor is ahead of the durable high-water mark")
            first = connection.execute(
                "SELECT MIN(core_event_seq) FROM execution_core_event"
            ).fetchone()[0]
            replay_floor = high_water if first is None else int(first) - 1
            clauses = ["core_event_seq>?", "core_event_seq<=?"]
            parameters: list[Any] = [after_seq, high_water]
            if workspace_id is not None:
                clauses.append("workspace_id=?")
                parameters.append(workspace_id)
            if normalized_types:
                clauses.append(
                    "json_extract(event_json,'$.event_type') IN ("
                    + ",".join("?" for _ in normalized_types)
                    + ")"
                )
                parameters.extend(normalized_types)
            rows = connection.execute(
                "SELECT core_event_seq,event_id,workspace_id,aggregate_id,aggregate_revision,event_json "
                "FROM execution_core_event WHERE "
                + " AND ".join(clauses)
                + " ORDER BY core_event_seq LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        visible = rows[:limit]
        events = tuple(_decode_core_row(row) for row in visible)
        next_after = int(visible[-1]["core_event_seq"]) if has_more else high_water
        return ReplayWindow(
            "core_event", None, after_seq, replay_floor, high_water, events, next_after
        )


class JobEventStore(_SQLiteEventStore):
    """Durable per-Job Plugin Event log with an independent cursor domain."""

    @staticmethod
    def _job_row(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT job_event_high_water FROM execution_job WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "unknown Job event stream")
        return row

    def high_water(self, job_id: str) -> int:
        with self.repository.read_connection() as connection:
            return int(self._job_row(connection, job_id)["job_event_high_water"])

    def append(
        self,
        event: Mapping[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        candidate = dict(event)
        event_id = candidate.get("event_id")
        job_id = candidate.get("job_id")
        if not isinstance(event_id, str) or not event_id or not isinstance(job_id, str) or not job_id:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Plugin Job Event requires event_id and job_id")
        expected_seq = candidate.pop("job_event_seq", None)
        if expected_seq is not None:
            _require_after_seq(expected_seq)
            if expected_seq == 0:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "Job Event sequence starts at one")
        payload_pair = (candidate.get("payload_asset_id"), candidate.get("payload_hash"))
        if (payload_pair[0] is None) != (payload_pair[1] is None):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Plugin Job Event payload ID/hash must be paired")

        with self._writer(connection) as writer:
            existing = writer.execute(
                "SELECT event_json FROM execution_job_event WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                stored = _decode_event(existing[0], "plugin-job-event/v1")
                comparable = dict(stored)
                stored_seq = comparable.pop("job_event_seq")
                if comparable != candidate or (expected_seq is not None and expected_seq != stored_seq):
                    raise ContractError(ErrorCode.DUPLICATE_REQUEST, "Plugin Job Event ID was reused with different content")
                return stored

            high_water = int(self._job_row(writer, job_id)["job_event_high_water"])
            attempt = writer.execute(
                "SELECT job_id,step_id,plugin_id,release_id FROM execution_attempt WHERE attempt_id=?",
                (candidate.get("attempt_id"),),
            ).fetchone()
            if (
                attempt is None
                or attempt["job_id"] != job_id
                or attempt["step_id"] != candidate.get("step_id")
                or attempt["plugin_id"] != candidate.get("plugin_id")
                or attempt["release_id"] != candidate.get("release_id")
            ):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Plugin Job Event attempt is outside the Job/Step")
            required_prefix = f"plugin.{candidate.get('plugin_id')}."
            event_type = candidate.get("event_type")
            if not isinstance(event_type, str) or not event_type.startswith(required_prefix):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Plugin Job Event type is not bound to its plugin_id")
            if event_type in _CORE_EVENT_TYPES or event_type.startswith("plugin.generation."):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Plugin Job Event uses a Core-reserved event type")
            sequence = high_water + 1
            if expected_seq is not None and expected_seq != sequence:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "Job Event sequence is not the next durable cursor")
            stored = {**candidate, "job_event_seq": sequence}
            assert_valid("plugin-job-event/v1", stored)
            try:
                writer.execute(
                    "INSERT INTO execution_job_event(job_id,job_event_seq,event_id,attempt_id,local_seq,event_json) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        job_id,
                        sequence,
                        stored["event_id"],
                        stored["attempt_id"],
                        stored["local_seq"],
                        _encode_event(stored),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "Plugin Job Event local sequence was reused") from exc
            updated = writer.execute(
                "UPDATE execution_job SET job_event_high_water=? WHERE job_id=? AND job_event_high_water=?",
                (sequence, job_id, high_water),
            )
            if updated.rowcount != 1:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "Job Event high-water changed concurrently")
            return stored

    def prune_through(self, job_id: str, sequence: int) -> int:
        _require_after_seq(sequence)
        with self.repository.transaction() as connection:
            self._job_row(connection, job_id)
            cursor = connection.execute(
                "DELETE FROM execution_job_event WHERE job_id=? AND job_event_seq<=?",
                (job_id, sequence),
            )
            return int(cursor.rowcount)

    def window(self, job_id: str, after_seq: int, *, limit: int = 1000) -> ReplayWindow:
        _require_after_seq(after_seq)
        _require_limit(limit)
        with self.repository.read_connection() as connection:
            high_water = int(self._job_row(connection, job_id)["job_event_high_water"])
            if after_seq > high_water:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "SSE cursor is ahead of the durable high-water mark")
            first = connection.execute(
                "SELECT MIN(job_event_seq) FROM execution_job_event WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            replay_floor = high_water if first is None else int(first) - 1
            rows = connection.execute(
                "SELECT job_id,job_event_seq,event_id,attempt_id,local_seq,event_json FROM execution_job_event "
                "WHERE job_id=? AND job_event_seq>? AND job_event_seq<=? "
                "ORDER BY job_event_seq LIMIT ?",
                (job_id, after_seq, high_water, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        visible = rows[:limit]
        events = tuple(_decode_job_row(row) for row in visible)
        next_after = int(visible[-1]["job_event_seq"]) if has_more else high_water
        return ReplayWindow(
            "job_event", job_id, after_seq, replay_floor, high_water, events, next_after
        )


__all__ = ["CoreEventStore", "JobEventStore", "ReplayWindow", "StreamKind"]
