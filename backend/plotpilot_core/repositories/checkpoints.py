"""Durable P1 checkpoint and orchestration-control authority.

This module is intentionally an internal adapter.  It owns no second database
and does not add an HTTP surface: all writes use the connection and
``BEGIN IMMEDIATE`` transaction supplied by :class:`CoreAuthorityRepository`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
from typing import Any

from backend.plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    ErrorCode,
    canonical_bytes,
    parse_json_bytes,
    sha256_hex,
    verify_checkpoint,
)
from backend.plotpilot_plugin_sdk.verifier import validate_rpc_result

from ..domain.entities import utc_now
from ..events.store import JobEventStore
from ..jobs.states import ATTEMPT_EDGES, JOB_EDGES, STEP_EDGES, can_transition
from .authority import CoreAuthorityRepository


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_ATTEMPT_STATES = frozenset({"running", "cancelling"})
_CONTROL_METHODS = {
    "pause": "job.pause",
    "resume": "job.resume",
    "cancel": "job.cancel",
    "await_user": "host.job.await_user/v1",
}


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\n".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:48]}"


def _require_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ContractValidationError(f"{name} is not a v1 ID")
    return value


def _require_hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ContractValidationError(f"{name} is not a SHA-256 digest")
    return value


def _require_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractValidationError(f"{name} must be a positive integer")
    return value


def _require_nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(f"{name} must be a non-negative integer")
    return value


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("lease expiry is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractValidationError("lease expiry must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _clock_value(clock: Callable[[], str], value: str | None = None) -> str:
    candidate = value or clock()
    if not isinstance(candidate, str):
        raise ContractValidationError("authority clock must return a timestamp")
    _timestamp(candidate)
    return candidate


def _is_expired(expires_at: str, now: str) -> bool:
    return _timestamp(expires_at) <= _timestamp(now)


def _checkpoint_error(message: str) -> ContractError:
    return ContractError(ErrorCode.CHECKPOINT_INVALID, message)


@dataclass(frozen=True, slots=True)
class CheckpointCommit:
    """The internal result of one durable checkpoint decision."""

    result: Mapping[str, Any]
    replayed: bool = False

    @property
    def accepted(self) -> bool:
        return bool(self.result["accepted"])

    @property
    def checkpoint_id(self) -> str:
        return str(self.result["checkpoint_id"])

    @property
    def completed_units(self) -> int:
        return int(self.result["completed_units"])

    @property
    def total_units(self) -> int | None:
        value = self.result["total_units"]
        return None if value is None else int(value)

    @property
    def job_event_seq(self) -> int:
        return int(self.result["job_event_seq"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.result)

    def __getitem__(self, key: str) -> Any:
        return self.result[key]


@dataclass(frozen=True, slots=True)
class ControlDecision:
    """Internal control result with exact first-decision replay semantics."""

    result: Mapping[str, Any]
    operation: str
    replayed: bool = False

    @property
    def accepted(self) -> bool:
        return bool(self.result["accepted"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.result)

    def __getitem__(self, key: str) -> Any:
        return self.result[key]


@dataclass(frozen=True, slots=True)
class DurableCheckpoint:
    """Decoded checkpoint plus the durable cursor/operation anchors."""

    checkpoint: Mapping[str, Any]
    workspace_id: str
    job_event_seq: int
    operation_key: str
    payload_hash: str

    @property
    def checkpoint_id(self) -> str:
        return str(self.checkpoint["checkpoint_id"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.checkpoint)

    def __getitem__(self, key: str) -> Any:
        return self.checkpoint[key]


@dataclass(frozen=True, slots=True)
class OrchestrationOwnerLease:
    workspace_id: str
    owner_instance_id: str
    owner_token: str
    lease_epoch: int
    lease_expires_at: str
    revision: int
    acquired: bool = True
    replayed: bool = False

    @property
    def stale(self) -> bool:
        return _is_expired(self.lease_expires_at, utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "owner_instance_id": self.owner_instance_id,
            "owner_token": self.owner_token,
            "lease_epoch": self.lease_epoch,
            "lease_expires_at": self.lease_expires_at,
            "revision": self.revision,
            "acquired": self.acquired,
            "replayed": self.replayed,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


class SQLiteCheckpointStore:
    """The P1 durable ``checkpoint/v1`` store and Job snapshot extension."""

    def __init__(
        self,
        repository: CoreAuthorityRepository,
        assets: Any | None = None,
        *,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.repository = repository
        self.assets = assets
        self.clock = clock
        self.events = JobEventStore(repository)

    def _load_source(
        self,
        source: Mapping[str, Any] | str,
        *,
        checkpoint_asset_id: str | None = None,
    ) -> tuple[dict[str, Any], str | None, str | None]:
        asset_id: str | None = checkpoint_asset_id
        if isinstance(source, Mapping):
            value = dict(source)
            if asset_id is not None:
                _require_id(asset_id, "checkpoint_asset_id")
                if self.assets is None:
                    raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset authority is unavailable")
                try:
                    raw = self.assets.read(asset_id)
                    loaded = parse_json_bytes(raw)
                except ContractError:
                    raise
                except Exception as exc:
                    raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset is missing or invalid") from exc
                if not isinstance(loaded, Mapping) or dict(loaded) != value:
                    raise ContractError(ErrorCode.CHECKPOINT_INVALID, "checkpoint Asset content drifted")
        elif isinstance(source, str):
            if asset_id is not None and asset_id != source:
                raise ContractValidationError("checkpoint source Asset IDs disagree")
            asset_id = source
            _require_id(asset_id, "checkpoint_asset_id")
            if self.assets is None:
                raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset authority is unavailable")
            try:
                raw = self.assets.read(asset_id)
                loaded = parse_json_bytes(raw)
            except ContractError:
                raise
            except Exception as exc:
                raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset is missing or invalid") from exc
            if not isinstance(loaded, Mapping):
                raise ContractError(ErrorCode.CHECKPOINT_INVALID, "checkpoint Asset is not an object")
            value = dict(loaded)
        else:
            raise ContractValidationError("checkpoint must be an object or checkpoint Asset ID")

        verify_checkpoint(value)
        asset_hash: str | None = None
        if asset_id is not None:
            try:
                if self.assets is None:
                    raise OSError("Asset authority is unavailable")
                asset_hash = self.assets.require(asset_id).sha256
            except Exception as exc:
                raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset is missing or invalid") from exc
        return value, asset_id, asset_hash

    @staticmethod
    def _operation_hash(value: Mapping[str, Any]) -> str:
        return sha256_hex(canonical_bytes(dict(value)))

    @staticmethod
    def _row_value(row: sqlite3.Row) -> dict[str, Any]:
        try:
            value = json.loads(str(row["checkpoint_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "stored checkpoint is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ContractError(ErrorCode.ASSET_ERROR, "stored checkpoint is not an object")
        return value

    @staticmethod
    def _row_identity(row: sqlite3.Row, value: Mapping[str, Any]) -> None:
        expected = (
            row["checkpoint_id"], row["job_id"], row["step_id"], row["source_attempt_id"],
            int(row["checkpoint_seq"]), int(row["lease_epoch"]), row["run_snapshot_hash"],
            row["replay_policy"], int(row["completed_units"]), row["total_units"],
            row["unit_set_hash"], row["state_asset_id"], row["checkpoint_hash"],
        )
        actual = (
            value.get("checkpoint_id"), value.get("job_id"), value.get("step_id"), value.get("source_attempt_id"),
            value.get("checkpoint_seq"), value.get("lease_epoch"), value.get("run_snapshot_hash"),
            value.get("replay_policy"), value.get("completed_units"), value.get("total_units"),
            value.get("unit_set_hash"), value.get("state_asset_id"), value.get("checkpoint_hash"),
        )
        if actual != expected:
            raise ContractError(ErrorCode.ASSET_ERROR, "stored checkpoint row identity drifted")

    def _decode_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        expected_workspace_id: str | None = None,
    ) -> DurableCheckpoint:
        job = connection.execute(
            "SELECT workspace_id,run_snapshot_hash FROM execution_job WHERE job_id=?",
            (row["job_id"],),
        ).fetchone()
        if job is None or (expected_workspace_id is not None and job["workspace_id"] != expected_workspace_id):
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Job authority is missing")
        value = self._row_value(row)
        try:
            verify_checkpoint(value, expected_snapshot_hash=str(job["run_snapshot_hash"]))
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "stored checkpoint failed contract verification") from exc
        self._row_identity(row, value)
        event = connection.execute(
            "SELECT job_id,job_event_seq,attempt_id,event_json FROM execution_job_event "
            "WHERE job_id=? AND job_event_seq=?",
            (row["job_id"], row["job_event_seq"]),
        ).fetchone()
        if event is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Job Event authority is missing")
        try:
            event_value = json.loads(str(event["event_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Job Event is not valid JSON") from exc
        if (
            not isinstance(event_value, dict)
            or event_value.get("job_id") != row["job_id"]
            or event_value.get("job_event_seq") != row["job_event_seq"]
            or event_value.get("step_id") != row["step_id"]
            or event_value.get("attempt_id") != row["source_attempt_id"]
        ):
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Job Event identity drifted")
        return DurableCheckpoint(
            value,
            str(job["workspace_id"]),
            int(row["job_event_seq"]),
            str(row["operation_key"]),
            str(row["payload_hash"]),
        )

    @staticmethod
    def _latest_row(
        connection: sqlite3.Connection,
        job_id: str,
        step_id: str | None = None,
    ) -> sqlite3.Row | None:
        if step_id is None:
            return connection.execute(
                "SELECT * FROM execution_checkpoint WHERE job_id=? "
                "ORDER BY job_event_seq DESC,checkpoint_seq DESC,checkpoint_id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return connection.execute(
            "SELECT * FROM execution_checkpoint WHERE job_id=? AND step_id=? "
            "ORDER BY checkpoint_seq DESC,job_event_seq DESC,checkpoint_id DESC LIMIT 1",
            (job_id, step_id),
        ).fetchone()

    def get_latest_record(self, job_id: str, step_id: str | None = None) -> DurableCheckpoint | None:
        _require_id(job_id, "job_id")
        if step_id is not None:
            _require_id(step_id, "step_id")
        with self.repository.read_connection() as connection:
            row = self._latest_row(connection, job_id, step_id)
            return None if row is None else self._decode_row(connection, row)

    def get_latest(self, job_id: str, step_id: str | None = None) -> dict[str, Any] | None:
        record = self.get_latest_record(job_id, step_id)
        return None if record is None else record.to_dict()

    def latest(self, job_id: str, step_id: str | None = None) -> dict[str, Any] | None:
        return self.get_latest(job_id, step_id)

    def get_checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        _require_id(checkpoint_id, "checkpoint_id")
        with self.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_checkpoint WHERE checkpoint_id=?", (checkpoint_id,)
            ).fetchone()
            return None if row is None else self._decode_row(connection, row).to_dict()

    def list_for_job(self, job_id: str, step_id: str | None = None) -> tuple[dict[str, Any], ...]:
        _require_id(job_id, "job_id")
        with self.repository.read_connection() as connection:
            if step_id is None:
                rows = connection.execute(
                    "SELECT * FROM execution_checkpoint WHERE job_id=? "
                    "ORDER BY job_event_seq,checkpoint_seq,checkpoint_id", (job_id,)
                ).fetchall()
            else:
                _require_id(step_id, "step_id")
                rows = connection.execute(
                    "SELECT * FROM execution_checkpoint WHERE job_id=? AND step_id=? "
                    "ORDER BY checkpoint_seq,job_event_seq,checkpoint_id", (job_id, step_id)
                ).fetchall()
            return tuple(self._decode_row(connection, row).to_dict() for row in rows)

    def _validate_monotonic(
        self,
        value: Mapping[str, Any],
        previous: Mapping[str, Any] | None,
    ) -> None:
        completed = int(value["completed_units"])
        total = value["total_units"]
        if total is not None and completed > int(total):
            raise _checkpoint_error("completed_units cannot exceed total_units")
        if previous is None:
            if int(value["checkpoint_seq"]) != 1:
                raise _checkpoint_error("checkpoint sequence must start at one")
            return
        if int(value["checkpoint_seq"]) <= int(previous["checkpoint_seq"]):
            raise _checkpoint_error("checkpoint sequence moved backwards")
        if completed < int(previous["completed_units"]):
            raise _checkpoint_error("completed_units moved backwards")
        previous_total = previous["total_units"]
        if previous_total is not None and (total is None or int(total) < int(previous_total)):
            raise _checkpoint_error("total_units moved backwards")
        previous_unit_set = previous["unit_set_hash"]
        if previous_unit_set is not None and value["unit_set_hash"] != previous_unit_set:
            raise _checkpoint_error("unit_set_hash changed within a checkpoint chain")
        if value["replay_policy"] != previous["replay_policy"]:
            raise _checkpoint_error("replay_policy changed within a checkpoint chain")

    def _validate_attempt_binding(
        self,
        connection: sqlite3.Connection,
        value: Mapping[str, Any],
        *,
        now: str,
        worker_run_id: str | None = None,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        job = connection.execute("SELECT * FROM execution_job WHERE job_id=?", (value["job_id"],)).fetchone()
        if job is None:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown Job")
        step = connection.execute(
            "SELECT * FROM execution_step WHERE job_id=? AND step_id=?",
            (value["job_id"], value["step_id"]),
        ).fetchone()
        if step is None:
            raise _checkpoint_error("checkpoint Step is outside the Job")
        attempt = connection.execute(
            "SELECT * FROM execution_attempt WHERE attempt_id=? AND job_id=? AND step_id=?",
            (value["source_attempt_id"], value["job_id"], value["step_id"]),
        ).fetchone()
        if attempt is None:
            raise _checkpoint_error("checkpoint Attempt is outside the Job/Step")
        if attempt["lease_epoch"] != value["lease_epoch"]:
            raise ContractError(ErrorCode.STALE_LEASE, "checkpoint lease epoch is stale")
        if step["active_attempt_id"] != attempt["attempt_id"]:
            raise ContractError(ErrorCode.STALE_LEASE, "checkpoint Attempt is no longer active")
        if attempt["state"] not in _ACTIVE_ATTEMPT_STATES:
            raise ContractError(ErrorCode.STALE_LEASE, "checkpoint Attempt is no longer active")
        if attempt["owner_instance_id"] is None or attempt["owner_instance_id"] != attempt["worker_run_id"]:
            raise ContractError(ErrorCode.STALE_LEASE, "checkpoint Attempt owner authority drifted")
        if worker_run_id is not None and attempt["worker_run_id"] != worker_run_id:
            raise ContractError(ErrorCode.STALE_LEASE, "checkpoint worker run is not authoritative")
        if attempt["lease_expires_at"] is not None and _is_expired(str(attempt["lease_expires_at"]), now):
            raise ContractError(ErrorCode.STALE_LEASE, "checkpoint Attempt lease has expired")
        if job["job_state"] in {"succeeded", "partial", "failed", "cancelled"}:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "terminal Job cannot accept a checkpoint")
        if str(job["run_snapshot_hash"]) != str(value["run_snapshot_hash"]):
            raise _checkpoint_error("checkpoint belongs to another RunSnapshot")
        return job, attempt

    def _commit_in_transaction(
        self,
        connection: sqlite3.Connection,
        value: Mapping[str, Any],
        *,
        operation_key: str,
        payload_hash: str,
        checkpoint_asset_id: str | None,
        checkpoint_asset_hash: str | None,
        local_seq: int | None = None,
        worker_run_id: str | None = None,
        now: str,
    ) -> CheckpointCommit:
        existing = connection.execute(
            "SELECT * FROM execution_checkpoint_operation WHERE job_id=? AND operation_key=?",
            (value["job_id"], operation_key),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "checkpoint operation key was reused with a different payload")
            row = connection.execute(
                "SELECT * FROM execution_checkpoint WHERE job_id=? AND checkpoint_id=?",
                (value["job_id"], existing["checkpoint_id"]),
            ).fetchone()
            if row is None or int(row["job_event_seq"]) != int(existing["job_event_seq"]):
                raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint operation is missing its committed row")
            stored = self._decode_row(connection, row)
            result = json.loads(str(existing["response_json"]))
            if not isinstance(result, dict) or result != {
                "accepted": True,
                "checkpoint_id": stored.checkpoint_id,
                "completed_units": stored.checkpoint["completed_units"],
                "total_units": stored.checkpoint["total_units"],
                "job_event_seq": stored.job_event_seq,
            }:
                raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint operation response is inconsistent")
            return CheckpointCommit(result, True)

        job, attempt = self._validate_attempt_binding(
            connection, value, now=now, worker_run_id=worker_run_id
        )
        previous_row = self._latest_row(connection, str(value["job_id"]), str(value["step_id"]))
        previous = None if previous_row is None else self._row_value(previous_row)
        try:
            verify_checkpoint(
                value,
                expected_snapshot_hash=str(job["run_snapshot_hash"]),
                previous_seq=None if previous is None else int(previous["checkpoint_seq"]),
            )
        except ContractError:
            raise
        except Exception as exc:
            raise _checkpoint_error("checkpoint failed contract verification") from exc
        if previous is not None and previous["source_attempt_id"] != value["source_attempt_id"]:
            # A new source Attempt may continue a chain only when its durable
            # Attempt row explicitly records the exact resume edge.
            if (
                attempt["resume_of_attempt_id"] != previous["source_attempt_id"]
                or attempt["resume_checkpoint_id"] != previous["checkpoint_id"]
            ):
                raise _checkpoint_error("checkpoint source Attempt changed without an explicit resume")
        self._validate_monotonic(value, previous)

        by_id = connection.execute(
            "SELECT * FROM execution_checkpoint WHERE checkpoint_id=?", (value["checkpoint_id"],)
        ).fetchone()
        if by_id is not None:
            stored_value = self._row_value(by_id)
            if stored_value != dict(value) or by_id["job_id"] != value["job_id"]:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "checkpoint ID was reused with different content")
            raise ContractError(ErrorCode.DUPLICATE_REQUEST, "checkpoint ID is already committed")

        if local_seq is None:
            local_row = connection.execute(
                "SELECT COALESCE(MAX(local_seq),0)+1 FROM execution_job_event WHERE attempt_id=?",
                (attempt["attempt_id"],),
            ).fetchone()
            local_seq = int(local_row[0])
        else:
            _require_positive_int(local_seq, "local_seq")
        payload_id = checkpoint_asset_id
        payload_digest = checkpoint_asset_hash
        if (payload_id is None) != (payload_digest is None):
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint event Asset ID/hash must be paired")
        event = {
            "schema": "plugin-job-event/v1",
            "event_id": _stable_id("job-event", value["job_id"], operation_key),
            "job_id": value["job_id"],
            "step_id": value["step_id"],
            "attempt_id": value["source_attempt_id"],
            "job_event_seq": int(job["job_event_high_water"]) + 1,
            "event_type": f"plugin.{attempt['plugin_id']}.job.checkpoint",
            "plugin_id": attempt["plugin_id"],
            "release_id": attempt["release_id"],
            "local_seq": local_seq,
            "payload_asset_id": payload_id,
            "payload_hash": payload_digest,
            "occurred_at": now,
        }
        stored_event = self.events.append(event, connection=connection)
        try:
            connection.execute(
                "INSERT INTO execution_checkpoint("
                "checkpoint_id,job_id,step_id,source_attempt_id,checkpoint_seq,lease_epoch,"
                "run_snapshot_hash,replay_policy,completed_units,total_units,unit_set_hash,"
                "state_asset_id,checkpoint_hash,checkpoint_json,job_event_seq,operation_key,"
                "payload_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value["checkpoint_id"], value["job_id"], value["step_id"], value["source_attempt_id"],
                    value["checkpoint_seq"], value["lease_epoch"], value["run_snapshot_hash"],
                    value["replay_policy"], value["completed_units"], value["total_units"],
                    value["unit_set_hash"], value["state_asset_id"], value["checkpoint_hash"], _json(value),
                    stored_event["job_event_seq"], operation_key, payload_hash, value["created_at"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ContractError(ErrorCode.DUPLICATE_REQUEST, "checkpoint identity or operation key was reused") from exc
        connection.execute(
            "UPDATE execution_job SET current_checkpoint_id=?,updated_at=? WHERE job_id=?",
            (value["checkpoint_id"], now, value["job_id"]),
        )
        result = {
            "accepted": True,
            "checkpoint_id": value["checkpoint_id"],
            "completed_units": value["completed_units"],
            "total_units": value["total_units"],
            "job_event_seq": stored_event["job_event_seq"],
        }
        validate_rpc_result("host.checkpoint.commit/v1", result)
        connection.execute(
            "INSERT INTO execution_checkpoint_operation(job_id,operation_key,payload_hash,checkpoint_id,job_event_seq,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (value["job_id"], operation_key, payload_hash, value["checkpoint_id"], stored_event["job_event_seq"], _json(result), now),
        )
        return CheckpointCommit(result, False)

    def commit_checkpoint(
        self,
        checkpoint: Mapping[str, Any] | str,
        *,
        operation_key: str | None = None,
        checkpoint_asset_id: str | None = None,
        local_seq: int | None = None,
        worker_run_id: str | None = None,
    ) -> CheckpointCommit:
        value, asset_id, asset_hash = self._load_source(
            checkpoint, checkpoint_asset_id=checkpoint_asset_id
        )
        key = operation_key or str(value["checkpoint_id"])
        _require_id(key, "operation_key")
        now = _clock_value(self.clock)
        payload_hash = self._operation_hash(value)
        with self.repository.transaction() as connection:
            return self._commit_in_transaction(
                connection,
                value,
                operation_key=key,
                payload_hash=payload_hash,
                checkpoint_asset_id=asset_id,
                checkpoint_asset_hash=asset_hash,
                local_seq=local_seq,
                worker_run_id=worker_run_id,
                now=now,
            )

    def commit(
        self,
        checkpoint: Mapping[str, Any] | str,
        *,
        operation_key: str | None = None,
        checkpoint_asset_id: str | None = None,
        local_seq: int | None = None,
        worker_run_id: str | None = None,
    ) -> CheckpointCommit:
        return self.commit_checkpoint(
            checkpoint,
            operation_key=operation_key,
            checkpoint_asset_id=checkpoint_asset_id,
            local_seq=local_seq,
            worker_run_id=worker_run_id,
        )

    def _commit_source_in_transaction(
        self,
        connection: sqlite3.Connection,
        source: Mapping[str, Any] | str,
        *,
        operation_key: str,
        worker_run_id: str | None,
        now: str,
        local_seq: int | None = None,
    ) -> tuple[CheckpointCommit, dict[str, Any], str | None]:
        value, asset_id, asset_hash = self._load_source(source)
        result = self._commit_in_transaction(
            connection,
            value,
            operation_key=operation_key,
            payload_hash=self._operation_hash(value),
            checkpoint_asset_id=asset_id,
            checkpoint_asset_hash=asset_hash,
            local_seq=local_seq,
            worker_run_id=worker_run_id,
            now=now,
        )
        return result, value, asset_id

    def read(self, connection: sqlite3.Connection, *, job_id: str, workspace_id: str) -> Any:
        """Return the typed P3 Job snapshot extension from the same read view."""

        _require_id(job_id, "job_id")
        _require_id(workspace_id, "workspace_id")
        row = self._latest_row(connection, job_id)
        if row is not None:
            record = self._decode_row(connection, row, expected_workspace_id=workspace_id)
            from ..api.v1.jobs.rpc.command_query import JobCheckpointBinding, JobSnapshotExtensions

            return JobSnapshotExtensions(
                JobCheckpointBinding(workspace_id, job_id, str(record.checkpoint["step_id"]), record.checkpoint_id, record.to_dict()),
                (),
            )
        from ..api.v1.jobs.rpc.command_query import JobSnapshotExtensions

        return JobSnapshotExtensions(None, ())


class SQLiteOrchestrationOwnerStore:
    """Durable single-active Workspace owner with an expiring epoch fence."""

    def __init__(
        self,
        repository: CoreAuthorityRepository,
        *,
        clock: Callable[[], str] = utc_now,
        default_ttl_seconds: int = 30,
    ) -> None:
        if isinstance(default_ttl_seconds, bool) or default_ttl_seconds < 1:
            raise ValueError("default_ttl_seconds must be positive")
        self.repository = repository
        self.clock = clock
        self.default_ttl_seconds = default_ttl_seconds

    def _expiry(self, now: str, lease_expires_at: str | None, lease_ttl_seconds: int | None) -> str:
        if lease_expires_at is not None:
            _timestamp(lease_expires_at)
            if _timestamp(lease_expires_at) <= _timestamp(now):
                raise ContractValidationError("owner lease expiry must be in the future")
            return lease_expires_at
        ttl = self.default_ttl_seconds if lease_ttl_seconds is None else lease_ttl_seconds
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
            raise ContractValidationError("lease_ttl_seconds must be positive")
        return ( _timestamp(now) + timedelta(seconds=ttl) ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> OrchestrationOwnerLease | None:
        if row is None:
            return None
        return OrchestrationOwnerLease(
            str(row["workspace_id"]), str(row["owner_instance_id"]), str(row["owner_token"]),
            int(row["lease_epoch"]), str(row["lease_expires_at"]), int(row["revision"]),
        )

    def get(self, workspace_id: str) -> OrchestrationOwnerLease | None:
        _require_id(workspace_id, "workspace_id")
        with self.repository.read_connection() as connection:
            return self._decode(connection.execute(
                "SELECT * FROM execution_orchestration_owner WHERE workspace_id=?", (workspace_id,)
            ).fetchone())

    def current(self, workspace_id: str) -> OrchestrationOwnerLease | None:
        return self.get(workspace_id)

    def acquire(
        self,
        workspace_id: str,
        owner_instance_id: str,
        *,
        owner_token: str | None = None,
        lease_expires_at: str | None = None,
        lease_ttl_seconds: int | None = None,
        now: str | None = None,
    ) -> OrchestrationOwnerLease:
        _require_id(workspace_id, "workspace_id")
        _require_id(owner_instance_id, "owner_instance_id")
        if owner_token is not None and not owner_token:
            raise ContractValidationError("owner_token cannot be empty")
        current_now = _clock_value(self.clock, now)
        expiry = self._expiry(current_now, lease_expires_at, lease_ttl_seconds)
        token = owner_token or _stable_id("owner-token", workspace_id, owner_instance_id, current_now)
        _require_id(token, "owner_token")
        with self.repository.transaction() as connection:
            if connection.execute("SELECT 1 FROM workspace WHERE workspace_id=?", (workspace_id,)).fetchone() is None:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown Workspace")
            row = connection.execute(
                "SELECT * FROM execution_orchestration_owner WHERE workspace_id=?", (workspace_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO execution_orchestration_owner(workspace_id,owner_instance_id,owner_token,lease_epoch,lease_expires_at,revision,created_at,updated_at) VALUES(?,?,?,1,?,1,?,?)",
                    (workspace_id, owner_instance_id, token, expiry, current_now, current_now),
                )
                return OrchestrationOwnerLease(workspace_id, owner_instance_id, token, 1, expiry, 1)
            same_owner = row["owner_instance_id"] == owner_instance_id
            stale = _is_expired(str(row["lease_expires_at"]), current_now)
            if same_owner and not stale:
                if owner_token is not None and owner_token != row["owner_token"]:
                    raise ContractError(ErrorCode.STALE_LEASE, "owner token is not authoritative")
                connection.execute(
                    "UPDATE execution_orchestration_owner SET lease_expires_at=?,revision=revision+1,updated_at=? WHERE workspace_id=? AND owner_instance_id=? AND owner_token=? AND lease_epoch=?",
                    (expiry, current_now, workspace_id, row["owner_instance_id"], row["owner_token"], row["lease_epoch"]),
                )
                updated = connection.execute(
                    "SELECT * FROM execution_orchestration_owner WHERE workspace_id=?", (workspace_id,)
                ).fetchone()
                return self._decode(updated)  # type: ignore[return-value]
            if not stale:
                raise ContractError(ErrorCode.STALE_LEASE, "Workspace already has a live orchestration owner")
            next_epoch = int(row["lease_epoch"]) + 1
            updated = connection.execute(
                "UPDATE execution_orchestration_owner SET owner_instance_id=?,owner_token=?,lease_epoch=?,lease_expires_at=?,revision=revision+1,updated_at=? WHERE workspace_id=? AND lease_epoch=? AND owner_token=? AND lease_expires_at=?",
                (owner_instance_id, token, next_epoch, expiry, current_now, workspace_id, row["lease_epoch"], row["owner_token"], row["lease_expires_at"]),
            )
            if updated.rowcount != 1:
                raise ContractError(ErrorCode.STALE_LEASE, "Workspace owner fence changed concurrently")
            return self._decode(connection.execute(
                "SELECT * FROM execution_orchestration_owner WHERE workspace_id=?", (workspace_id,)
            ).fetchone())  # type: ignore[return-value]

    def claim(self, workspace_id: str, owner_instance_id: str, **kwargs: Any) -> OrchestrationOwnerLease:
        return self.acquire(workspace_id, owner_instance_id, **kwargs)

    def assert_owner(
        self,
        workspace_id: str,
        owner_instance_id: str,
        owner_token: str,
        lease_epoch: int,
        *,
        now: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> OrchestrationOwnerLease:
        _require_id(workspace_id, "workspace_id")
        _require_id(owner_instance_id, "owner_instance_id")
        _require_id(owner_token, "owner_token")
        _require_positive_int(lease_epoch, "lease_epoch")
        current_now = _clock_value(self.clock, now)
        own_connection = connection is None
        if own_connection:
            context = self.repository.transaction()
        else:
            if not connection.in_transaction:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "owner fence requires an active transaction")
            context = None
        if own_connection:
            with context as checked:  # type: ignore[union-attr]
                return self.assert_owner(workspace_id, owner_instance_id, owner_token, lease_epoch, now=current_now, connection=checked)
        row = connection.execute(
            "SELECT * FROM execution_orchestration_owner WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        if (
            row is None
            or row["owner_instance_id"] != owner_instance_id
            or row["owner_token"] != owner_token
            or int(row["lease_epoch"]) != lease_epoch
            or _is_expired(str(row["lease_expires_at"]), current_now)
        ):
            raise ContractError(ErrorCode.STALE_LEASE, "orchestration owner fence is stale")
        return self._decode(row)  # type: ignore[return-value]

    def renew(
        self,
        workspace_id: str,
        owner_instance_id: str,
        owner_token: str,
        lease_epoch: int,
        *,
        lease_expires_at: str | None = None,
        lease_ttl_seconds: int | None = None,
        now: str | None = None,
    ) -> OrchestrationOwnerLease:
        current_now = _clock_value(self.clock, now)
        expiry = self._expiry(current_now, lease_expires_at, lease_ttl_seconds)
        with self.repository.transaction() as connection:
            self.assert_owner(workspace_id, owner_instance_id, owner_token, lease_epoch, now=current_now, connection=connection)
            updated = connection.execute(
                "UPDATE execution_orchestration_owner SET lease_expires_at=?,revision=revision+1,updated_at=? WHERE workspace_id=? AND owner_instance_id=? AND owner_token=? AND lease_epoch=?",
                (expiry, current_now, workspace_id, owner_instance_id, owner_token, lease_epoch),
            )
            if updated.rowcount != 1:
                raise ContractError(ErrorCode.STALE_LEASE, "orchestration owner fence changed concurrently")
            return self._decode(connection.execute(
                "SELECT * FROM execution_orchestration_owner WHERE workspace_id=?", (workspace_id,)
            ).fetchone())  # type: ignore[return-value]

    def release(
        self,
        workspace_id: str,
        owner_instance_id: str,
        owner_token: str,
        lease_epoch: int,
        *,
        now: str | None = None,
    ) -> OrchestrationOwnerLease:
        current_now = _clock_value(self.clock, now)
        with self.repository.transaction() as connection:
            self.assert_owner(workspace_id, owner_instance_id, owner_token, lease_epoch, now=current_now, connection=connection)
            updated = connection.execute(
                "UPDATE execution_orchestration_owner SET lease_expires_at=?,revision=revision+1,updated_at=? WHERE workspace_id=? AND owner_instance_id=? AND owner_token=? AND lease_epoch=?",
                (current_now, current_now, workspace_id, owner_instance_id, owner_token, lease_epoch),
            )
            if updated.rowcount != 1:
                raise ContractError(ErrorCode.STALE_LEASE, "orchestration owner release lost its fence")
            return self._decode(connection.execute(
                "SELECT * FROM execution_orchestration_owner WHERE workspace_id=?", (workspace_id,)
            ).fetchone())  # type: ignore[return-value]


class SQLiteExecutionControlPort:
    """Internal pause/resume/cancel CAS port; not an ExecutionAuthority API."""

    def __init__(
        self,
        repository: CoreAuthorityRepository,
        checkpoints: SQLiteCheckpointStore | Any | None = None,
        *,
        assets: Any | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.repository = repository
        if isinstance(checkpoints, SQLiteCheckpointStore):
            self.checkpoints = checkpoints
        else:
            self.checkpoints = SQLiteCheckpointStore(repository, assets if assets is not None else checkpoints, clock=clock)
        self.clock = clock
        self.events = JobEventStore(repository)

    @staticmethod
    def _payload(operation: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return {"operation": operation, **dict(values)}

    def _existing(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        operation_key: str,
        operation: str,
        payload_hash: str,
    ) -> ControlDecision | None:
        row = connection.execute(
            "SELECT * FROM execution_control_operation WHERE job_id=? AND operation_key=?",
            (job_id, operation_key),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["payload_hash"] != payload_hash:
            raise ContractError(ErrorCode.DUPLICATE_REQUEST, "control operation key was reused with a different payload")
        try:
            result = json.loads(str(row["response_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "control operation response is invalid") from exc
        if not isinstance(result, dict):
            raise ContractError(ErrorCode.ASSET_ERROR, "control operation response is not an object")
        return ControlDecision(result, operation, True)

    @staticmethod
    def _caller(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        step_id: str,
        attempt_id: str,
        lease_epoch: int,
        worker_run_id: str | None,
        now: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        job = connection.execute("SELECT * FROM execution_job WHERE job_id=?", (job_id,)).fetchone()
        step = connection.execute("SELECT * FROM execution_step WHERE job_id=? AND step_id=?", (job_id, step_id)).fetchone()
        attempt = connection.execute("SELECT * FROM execution_attempt WHERE attempt_id=? AND job_id=? AND step_id=?", (attempt_id, job_id, step_id)).fetchone()
        if job is None or step is None or attempt is None:
            raise ContractError(ErrorCode.STALE_LEASE, "control Attempt lineage is stale")
        if step["active_attempt_id"] != attempt_id or int(attempt["lease_epoch"]) != lease_epoch:
            raise ContractError(ErrorCode.STALE_LEASE, "control Attempt lease is stale")
        if worker_run_id is not None and attempt["worker_run_id"] != worker_run_id:
            raise ContractError(ErrorCode.STALE_LEASE, "control worker run is not authoritative")
        if attempt["owner_instance_id"] != attempt["worker_run_id"] or attempt["owner_instance_id"] is None:
            raise ContractError(ErrorCode.STALE_LEASE, "control Attempt owner authority drifted")
        if attempt["lease_expires_at"] is not None and _is_expired(str(attempt["lease_expires_at"]), now):
            raise ContractError(ErrorCode.STALE_LEASE, "control Attempt lease has expired")
        return job, step, attempt

    @staticmethod
    def _next_local_seq(connection: sqlite3.Connection, attempt_id: str) -> int:
        return int(connection.execute(
            "SELECT COALESCE(MAX(local_seq),0)+1 FROM execution_job_event WHERE attempt_id=?", (attempt_id,)
        ).fetchone()[0])

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        job: sqlite3.Row,
        step_id: str,
        attempt: sqlite3.Row,
        operation: str,
        operation_key: str,
        now: str,
        payload_asset_id: str | None = None,
        payload_hash: str | None = None,
        local_seq: int | None = None,
    ) -> dict[str, Any]:
        if (payload_asset_id is None) != (payload_hash is None):
            raise ContractError(ErrorCode.ASSET_ERROR, "control event Asset ID/hash must be paired")
        high_water_row = connection.execute(
            "SELECT job_event_high_water FROM execution_job WHERE job_id=?",
            (job["job_id"],),
        ).fetchone()
        if high_water_row is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "control Job event authority is missing")
        event = {
            "schema": "plugin-job-event/v1",
            "event_id": _stable_id("job-event", job["job_id"], operation, operation_key),
            "job_id": job["job_id"],
            "step_id": step_id,
            "attempt_id": attempt["attempt_id"],
            "job_event_seq": int(high_water_row["job_event_high_water"]) + 1,
            "event_type": f"plugin.{attempt['plugin_id']}.job.{operation}",
            "plugin_id": attempt["plugin_id"],
            "release_id": attempt["release_id"],
            "local_seq": self._next_local_seq(connection, attempt["attempt_id"]) if local_seq is None else local_seq,
            "payload_asset_id": payload_asset_id,
            "payload_hash": payload_hash,
            "occurred_at": now,
        }
        return self.events.append(event, connection=connection)

    def _store_operation(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        operation_key: str,
        operation: str,
        payload_hash: str,
        attempt_id: str,
        lease_epoch: int,
        result: Mapping[str, Any],
        now: str,
    ) -> ControlDecision:
        connection.execute(
            "INSERT INTO execution_control_operation(job_id,operation_key,operation,payload_hash,attempt_id,lease_epoch,response_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (job_id, operation_key, operation, payload_hash, attempt_id, lease_epoch, _json(result), now),
        )
        return ControlDecision(dict(result), operation, False)

    def cancel(
        self,
        *,
        job_id: str,
        step_id: str,
        attempt_id: str,
        lease_epoch: int,
        operation_key: str,
        worker_run_id: str | None = None,
        reason: str,
    ) -> ControlDecision:
        _require_id(operation_key, "operation_key")
        if not isinstance(reason, str) or not reason:
            raise ContractValidationError("reason must be non-empty")
        payload = self._payload("cancel", {"job_id": job_id, "step_id": step_id, "attempt_id": attempt_id, "lease_epoch": lease_epoch, "worker_run_id": worker_run_id, "reason": reason})
        payload_hash = sha256_hex(canonical_bytes(payload))
        now = _clock_value(self.clock)
        with self.repository.transaction() as connection:
            replay = self._existing(connection, job_id, operation_key, "cancel", payload_hash)
            if replay is not None:
                return replay
            job, step, attempt = self._caller(connection, job_id=job_id, step_id=step_id, attempt_id=attempt_id, lease_epoch=lease_epoch, worker_run_id=worker_run_id, now=now)
            if attempt["state"] != "running" or job["job_state"] != "running":
                raise ContractError(ErrorCode.INVALID_TRANSITION, "cancel race lost to an earlier state decision")
            if not can_transition(ATTEMPT_EDGES, attempt["state"], "cancelling") or not can_transition(JOB_EDGES, job["job_state"], "cancelling"):
                raise ContractError(ErrorCode.INVALID_TRANSITION, "invalid cancel transition")
            updated_attempt = connection.execute(
                "UPDATE execution_attempt SET state='cancelling',revision=revision+1,updated_at=? WHERE attempt_id=? AND state='running' AND lease_epoch=?",
                (now, attempt_id, lease_epoch),
            )
            updated_job = connection.execute(
                "UPDATE execution_job SET job_state='cancelling',job_revision=job_revision+1,updated_at=? WHERE job_id=? AND job_state='running'",
                (now, job_id),
            )
            if updated_attempt.rowcount != 1 or updated_job.rowcount != 1:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "cancel CAS lost")
            self._append_event(connection, job=job, step_id=step_id, attempt=attempt, operation="cancel", operation_key=operation_key, now=now)
            result = {"accepted": True, "terminal_known": False, "attempt_state": "cancelling"}
            validate_rpc_result("job.cancel", result)
            return self._store_operation(connection, job_id=job_id, operation_key=operation_key, operation="cancel", payload_hash=payload_hash, attempt_id=attempt_id, lease_epoch=lease_epoch, result=result, now=now)

    def _suspend(
        self,
        *,
        operation: str,
        target_state: str,
        host_method: str,
        job_id: str,
        step_id: str,
        attempt_id: str,
        lease_epoch: int,
        operation_key: str,
        worker_run_id: str | None,
        reason: str,
        checkpoint: Mapping[str, Any] | str | None = None,
        checkpoint_asset_id: str | None = None,
        prompt_asset_id: str | None = None,
    ) -> ControlDecision:
        _require_id(operation_key, "operation_key")
        if not isinstance(reason, str) or not reason:
            raise ContractValidationError("reason must be non-empty")
        if checkpoint is None and checkpoint_asset_id is None:
            raise ContractError(ErrorCode.CHECKPOINT_INVALID, "suspension requires a checkpoint Asset")
        payload = self._payload(operation, {"job_id": job_id, "step_id": step_id, "attempt_id": attempt_id, "lease_epoch": lease_epoch, "worker_run_id": worker_run_id, "reason": reason, "checkpoint": None if checkpoint is None else (dict(checkpoint) if isinstance(checkpoint, Mapping) else checkpoint), "checkpoint_asset_id": checkpoint_asset_id, "prompt_asset_id": prompt_asset_id})
        payload_hash = sha256_hex(canonical_bytes(payload))
        now = _clock_value(self.clock)
        with self.repository.transaction() as connection:
            replay = self._existing(connection, job_id, operation_key, operation, payload_hash)
            if replay is not None:
                return replay
            job, step, attempt = self._caller(connection, job_id=job_id, step_id=step_id, attempt_id=attempt_id, lease_epoch=lease_epoch, worker_run_id=worker_run_id, now=now)
            if attempt["state"] != "running" or job["job_state"] != "running" or step["state"] != "running":
                raise ContractError(ErrorCode.INVALID_TRANSITION, "suspension race lost to an earlier state decision")
            if not can_transition(ATTEMPT_EDGES, "running", "suspended") or not can_transition(STEP_EDGES, "running", target_state) or not can_transition(JOB_EDGES, "running", target_state):
                raise ContractError(ErrorCode.INVALID_TRANSITION, "invalid suspension transition")
            checkpoint_result, checkpoint_value, source_asset_id = self.checkpoints._commit_source_in_transaction(
                connection,
                checkpoint if checkpoint is not None else str(checkpoint_asset_id),
                operation_key=operation_key,
                worker_run_id=worker_run_id,
                now=now,
            )
            asset_id = source_asset_id or checkpoint_value.get("state_asset_id")
            if not isinstance(asset_id, str):
                raise ContractError(ErrorCode.CHECKPOINT_INVALID, "accepted suspension requires a checkpoint Asset")
            asset_hash = None
            if source_asset_id is not None:
                asset_hash = self.checkpoints.assets.require(source_asset_id).sha256 if self.checkpoints.assets is not None else None
            connection.execute("UPDATE execution_attempt SET state='suspended',revision=revision+1,updated_at=? WHERE attempt_id=? AND state='running' AND lease_epoch=?", (now, attempt_id, lease_epoch))
            connection.execute("UPDATE execution_step SET state=?,revision=revision+1,updated_at=? WHERE step_id=? AND state='running'", (target_state, now, step_id))
            connection.execute("UPDATE execution_job SET job_state=?,job_revision=job_revision+1,updated_at=? WHERE job_id=? AND job_state='running'", (target_state, now, job_id))
            self._append_event(connection, job=job, step_id=step_id, attempt=attempt, operation=operation, operation_key=operation_key, now=now, payload_asset_id=source_asset_id or asset_id, payload_hash=asset_hash)
            if operation == "pause":
                result = {"accepted": True, "checkpoint_asset_id": asset_id}
            else:
                result = {"accepted": True, "attempt_state": "suspended", "step_state": target_state, "job_state": target_state, "job_event_seq": int(job["job_event_high_water"]) + 2}
            validate_rpc_result(host_method, result)
            return self._store_operation(connection, job_id=job_id, operation_key=operation_key, operation=operation, payload_hash=payload_hash, attempt_id=attempt_id, lease_epoch=lease_epoch, result=result, now=now)

    def pause(self, **kwargs: Any) -> ControlDecision:
        return self._suspend(operation="pause", target_state="paused", host_method="job.pause", **kwargs)

    def await_user(self, **kwargs: Any) -> ControlDecision:
        return self._suspend(operation="await_user", target_state="waiting_user", host_method="host.job.await_user/v1", **kwargs)

    def resume(
        self,
        *,
        job_id: str,
        step_id: str,
        operation_key: str,
        checkpoint: Mapping[str, Any] | str | None = None,
        checkpoint_asset_id: str | None = None,
        attempt_id: str | None = None,
        resume_of_attempt_id: str | None = None,
        lease_epoch: int | None = None,
        worker_run_id: str | None = None,
        new_attempt_id: str | None = None,
        plugin_id: str | None = None,
        release_id: str | None = None,
        package_hash: str | None = None,
        capability_id: str | None = None,
        generation_id: str | None = None,
        preallocated_receipt_id: str | None = None,
        lease_expires_at: str | None = None,
        resume_reason: str = "resume",
    ) -> ControlDecision:
        source_attempt_id = resume_of_attempt_id or attempt_id
        _require_id(operation_key, "operation_key")
        if source_attempt_id is None:
            raise ContractValidationError("resume requires resume_of_attempt_id")
        if checkpoint is None and checkpoint_asset_id is None:
            raise ContractError(ErrorCode.CHECKPOINT_INVALID, "resume requires a checkpoint Asset")
        if worker_run_id is None:
            raise ContractValidationError("resume requires a new worker_run_id")
        _require_id(worker_run_id, "worker_run_id")
        if not isinstance(resume_reason, str) or not resume_reason:
            raise ContractValidationError("resume_reason must be non-empty")
        payload = self._payload("resume", {"job_id": job_id, "step_id": step_id, "source_attempt_id": source_attempt_id, "lease_epoch": lease_epoch, "worker_run_id": worker_run_id, "new_attempt_id": new_attempt_id, "plugin_id": plugin_id, "release_id": release_id, "package_hash": package_hash, "capability_id": capability_id, "generation_id": generation_id, "checkpoint": None if checkpoint is None else (dict(checkpoint) if isinstance(checkpoint, Mapping) else checkpoint), "checkpoint_asset_id": checkpoint_asset_id, "resume_reason": resume_reason})
        payload_hash = sha256_hex(canonical_bytes(payload))
        now = _clock_value(self.clock)
        with self.repository.transaction() as connection:
            replay = self._existing(connection, job_id, operation_key, "resume", payload_hash)
            if replay is not None:
                return replay
            job = connection.execute("SELECT * FROM execution_job WHERE job_id=?", (job_id,)).fetchone()
            step = connection.execute("SELECT * FROM execution_step WHERE job_id=? AND step_id=?", (job_id, step_id)).fetchone()
            old = connection.execute("SELECT * FROM execution_attempt WHERE attempt_id=? AND job_id=? AND step_id=?", (source_attempt_id, job_id, step_id)).fetchone()
            if job is None or step is None or old is None:
                raise ContractError(ErrorCode.STALE_LEASE, "resume Attempt lineage is stale")
            if job["job_state"] not in {"paused", "waiting_user", "needs_attention"} or step["state"] not in {"paused", "waiting_user", "needs_attention"} or old["state"] != "suspended":
                raise ContractError(ErrorCode.INVALID_TRANSITION, "terminal or non-suspended Attempt cannot be resumed")
            if lease_epoch is not None and int(old["lease_epoch"]) != lease_epoch:
                raise ContractError(ErrorCode.STALE_LEASE, "resume Attempt lease is stale")
            if checkpoint is None:
                checkpoint_source: Mapping[str, Any] | str = str(checkpoint_asset_id)
            else:
                checkpoint_source = checkpoint
            value, source_asset_id, source_asset_hash = self.checkpoints._load_source(checkpoint_source, checkpoint_asset_id=checkpoint_asset_id if isinstance(checkpoint_source, Mapping) else None)
            try:
                verify_checkpoint(value, expected_snapshot_hash=str(job["run_snapshot_hash"]))
            except ContractError:
                raise
            if value["job_id"] != job_id or value["step_id"] != step_id or value["source_attempt_id"] != source_attempt_id or int(value["lease_epoch"]) != int(old["lease_epoch"]):
                raise _checkpoint_error("resume checkpoint is not bound to the suspended Attempt")
            latest = self.checkpoints._latest_row(connection, job_id, step_id)
            if latest is None or latest["checkpoint_id"] != value["checkpoint_id"]:
                raise _checkpoint_error("resume checkpoint is not the latest committed checkpoint")
            new_id = new_attempt_id or _stable_id("attempt-resume", job_id, operation_key)
            _require_id(new_id, "new_attempt_id")
            if new_id == source_attempt_id:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "resume must allocate a fresh Attempt")
            if connection.execute("SELECT 1 FROM execution_attempt WHERE attempt_id=?", (new_id,)).fetchone() is not None:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "resume Attempt ID is already allocated")
            selected = {
                "plugin_id": plugin_id or old["plugin_id"],
                "release_id": release_id or old["release_id"],
                "package_hash": old["package_hash"] if package_hash is None else package_hash,
                "capability_id": capability_id or old["capability_id"],
                "generation_id": generation_id or old["generation_id"],
            }
            for name in ("plugin_id", "release_id", "package_hash", "capability_id", "generation_id"):
                if selected[name] != old[name]:
                    raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, f"resume {name} drifted from the suspended Attempt")
            _require_hash(selected["package_hash"], "package_hash")
            epoch = int(step["next_lease_epoch"])
            expiry = lease_expires_at or "9999-12-31T23:59:59Z"
            if _is_expired(expiry, now):
                raise ContractValidationError("resume lease expiry must be in the future")
            receipt_id = preallocated_receipt_id or _stable_id("receipt", new_id)
            ordinal = int(connection.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM execution_attempt WHERE step_id=?", (step_id,)).fetchone()[0])
            connection.execute(
                "INSERT INTO execution_attempt("
                "attempt_id,job_id,step_id,state,lease_epoch,worker_run_id,plugin_id,release_id,"
                "package_hash,capability_id,generation_id,preallocated_receipt_id,revision,"
                "created_at,updated_at,ordinal,owner_instance_id,lease_expires_at,"
                "expected_result_contract,resume_of_attempt_id,resume_checkpoint_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id, job_id, step_id, "running", epoch, worker_run_id,
                    selected["plugin_id"], selected["release_id"], selected["package_hash"],
                    selected["capability_id"], selected["generation_id"], receipt_id, 1,
                    now, now, ordinal, worker_run_id, expiry,
                    step["expected_result_contract"], source_attempt_id, value["checkpoint_id"],
                ),
            )
            connection.execute("UPDATE execution_step SET state='running',active_attempt_id=?,next_lease_epoch=?,revision=revision+1,updated_at=? WHERE job_id=? AND step_id=? AND state=?", (new_id, epoch + 1, now, job_id, step_id, step["state"]))
            connection.execute("UPDATE execution_job SET job_state='running',job_revision=job_revision+1,updated_at=? WHERE job_id=? AND job_state=?", (now, job_id, job["job_state"]))
            event = self._append_event(connection, job=job, step_id=step_id, attempt=connection.execute("SELECT * FROM execution_attempt WHERE attempt_id=?", (new_id,)).fetchone(), operation="resume", operation_key=operation_key, now=now, payload_asset_id=source_asset_id, payload_hash=source_asset_hash, local_seq=1)
            result = {"accepted": True, "worker_run_id": worker_run_id, "provenance_receipt_id": receipt_id, "output_streams": []}
            # The frozen v1 result schema has identical ``job.start`` and
            # ``job.resume`` branches.  Its oneOf therefore rejects this
            # valid result as ambiguous when the method has no discriminator.
            # Keep the method-specific closed-field/type check here rather
            # than weakening or changing the public v1 schema.
            if (
                set(result) != {"accepted", "worker_run_id", "provenance_receipt_id", "output_streams"}
                or not isinstance(result["accepted"], bool)
                or not isinstance(result["worker_run_id"], str)
                or not isinstance(result["provenance_receipt_id"], str)
                or not isinstance(result["output_streams"], list)
            ):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "invalid job.resume result")
            _require_id(result["worker_run_id"], "worker_run_id")
            _require_id(result["provenance_receipt_id"], "provenance_receipt_id")
            return self._store_operation(connection, job_id=job_id, operation_key=operation_key, operation="resume", payload_hash=payload_hash, attempt_id=new_id, lease_epoch=epoch, result=result, now=now)

    def apply(self, operation: str, **kwargs: Any) -> ControlDecision:
        if operation == "cancel":
            return self.cancel(**kwargs)
        if operation == "pause":
            return self.pause(**kwargs)
        if operation == "resume":
            return self.resume(**kwargs)
        if operation == "await_user":
            return self.await_user(**kwargs)
        raise ContractValidationError(f"unsupported execution control operation: {operation}")

    execute = apply


# Compatibility aliases for internal composition/tests.  None of these are
# mounted on ExecutionAuthority under the forbidden P3 method names.
CheckpointStore = SQLiteCheckpointStore
CheckpointAuthority = SQLiteCheckpointStore
OrchestrationOwnerStore = SQLiteOrchestrationOwnerStore
ExecutionControlPort = SQLiteExecutionControlPort
ControlPort = SQLiteExecutionControlPort


__all__ = [
    "CheckpointAuthority",
    "CheckpointCommit",
    "CheckpointStore",
    "ControlDecision",
    "ControlPort",
    "DurableCheckpoint",
    "ExecutionControlPort",
    "OrchestrationOwnerLease",
    "OrchestrationOwnerStore",
    "SQLiteCheckpointStore",
    "SQLiteExecutionControlPort",
    "SQLiteOrchestrationOwnerStore",
]
