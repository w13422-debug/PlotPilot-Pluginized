"""Durable P1 checkpoint and orchestration-control authority.

This module is intentionally an internal adapter.  It owns no second database
and does not add an HTTP surface: all writes use the connection and
``BEGIN IMMEDIATE`` transaction supplied by :class:`CoreAuthorityRepository`.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    ErrorCode,
    assert_valid,
    canonical_bytes,
    parse_json_bytes,
    sha256_hex,
    verify_checkpoint,
    verify_snapshot,
)
from backend.plotpilot_plugin_sdk.verifier import validate_rpc_result

from ..domain.entities import utc_now
from ..events.store import CoreEventStore, JobEventStore
from ..jobs.states import ATTEMPT_EDGES, JOB_EDGES, STEP_EDGES, can_transition
from .authority import CoreAuthorityRepository

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_ATTEMPT_STATES = frozenset({"running", "cancelling"})
_TERMINAL_ATTEMPT_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})
_TERMINAL_JOB_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})
_CONTROL_AUTHORITY_RESULT = "authority_result"
_CONTROL_PUBLIC_RESPONSE = "public_response"
_AWAIT_USER_REASONS = frozenset({"user_input", "external_confirmation"})
_CHECKPOINT_METHOD = "host.checkpoint.commit/v1"
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


def _verify_snapshot_release_binding(
    snapshot: Mapping[str, Any], attempt: Mapping[str, Any]
) -> None:
    """Bind rollout Generation separately from the release's data Generation."""

    verify_snapshot(snapshot)
    releases = [
        release
        for release in snapshot["plugin_releases"]
        if release["plugin_id"] == attempt["plugin_id"]
    ]
    if len(releases) != 1:
        raise ValueError("Attempt plugin is not uniquely bound by the RunSnapshot")
    release = releases[0]
    if (
        release["release_id"],
        release["package_hash"],
        snapshot["scope"]["operation"],
    ) != (
        attempt["release_id"],
        attempt["package_hash"],
        attempt["capability_id"],
    ):
        raise ValueError("Attempt release identity is not bound by the RunSnapshot")


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
        allow_content_drift: bool = False,
    ) -> tuple[dict[str, Any], str | None, str | None]:
        asset_id: str | None = checkpoint_asset_id
        if isinstance(source, str) and asset_id is None:
            # The string form is the immutable Asset ID shorthand used by
            # the internal control port.  It still resolves through the
            # AssetStore below; it is not an inline checkpoint bypass.
            asset_id = source
        if asset_id is None:
            raise ContractError(
                ErrorCode.CHECKPOINT_INVALID,
                "checkpoint commit requires an immutable checkpoint Asset",
            )
        _require_id(asset_id, "checkpoint_asset_id")
        if isinstance(source, Mapping):
            value = dict(source)
        elif isinstance(source, str):
            if asset_id != source:
                raise ContractValidationError("checkpoint source Asset IDs disagree")
            value = {}
        else:
            raise ContractValidationError("checkpoint must be an object or checkpoint Asset ID")

        if self.assets is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset authority is unavailable")
        try:
            raw = self.assets.read(asset_id)
            loaded = parse_json_bytes(raw)
            if not isinstance(loaded, Mapping):
                raise ContractValidationError("checkpoint Asset is not an object")
            loaded_value = dict(loaded)
            if (
                isinstance(source, Mapping)
                and loaded_value != value
                and not allow_content_drift
            ):
                raise ContractError(ErrorCode.CHECKPOINT_INVALID, "checkpoint Asset content drifted")
            verify_checkpoint(loaded_value)
            if raw != canonical_bytes(loaded_value):
                raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset is not canonical JSON")
            asset_hash = self.assets.require(asset_id).sha256
            if asset_hash != sha256_hex(raw):
                raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset hash authority drifted")
            if not (isinstance(source, Mapping) and loaded_value != value):
                value = loaded_value
            else:
                # The caller's value remains the request identity when a
                # retry supplies a mapping that differs from the immutable
                # Asset.  The transaction path fences the Attempt first,
                # then returns DUPLICATE_REQUEST for an existing key and
                # rejects a new key without materializing the drift.
                verify_checkpoint(value)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset is missing or invalid") from exc
        return value, asset_id, asset_hash

    def asset_hash(self, asset_id: str, *, label: str = "Asset") -> str:
        """Return the immutable hash for an Asset referenced by authority data."""

        _require_id(asset_id, f"{label.lower()}_id")
        if self.assets is None:
            raise ContractError(ErrorCode.ASSET_ERROR, f"{label} authority is unavailable")
        try:
            return str(self.assets.require(asset_id).sha256)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, f"{label} is missing or invalid") from exc

    @staticmethod
    def _operation_hash(value: Mapping[str, Any]) -> str:
        return sha256_hex(canonical_bytes(dict(value)))

    @staticmethod
    def _checkpoint_request(
        *,
        operation_key: str,
        checkpoint: Mapping[str, Any],
        checkpoint_asset_id: str,
        checkpoint_asset_hash: str,
        local_seq: int | None,
        worker_run_id: str,
    ) -> dict[str, Any]:
        """Return the closed, durable request identity for checkpoint commit.

        The checkpoint row is the result of this request, not its idempotency
        identity.  Keeping the method, caller, Asset binding, and every
        caller-supplied field in one canonical value prevents a retry from
        silently changing the operation while retaining its key.
        """

        return {
            "method": _CHECKPOINT_METHOD,
            "operation_key": operation_key,
            "checkpoint": dict(checkpoint),
            "checkpoint_asset_id": checkpoint_asset_id,
            "checkpoint_asset_hash": checkpoint_asset_hash,
            "local_seq": local_seq,
            "worker_run_id": worker_run_id,
        }

    @staticmethod
    def _validate_snapshot_attempt_binding(
        job: sqlite3.Row,
        attempt: sqlite3.Row,
    ) -> None:
        """Fence release/generation identity before an operation is replayed."""

        package_hash = attempt["package_hash"]
        if not isinstance(package_hash, str) or _HASH.fullmatch(package_hash) is None:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "Attempt package identity is invalid")
        if not isinstance(attempt["release_id"], str) or _HASH.fullmatch(attempt["release_id"]) is None:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "Attempt release identity is invalid")
        if not isinstance(attempt["generation_id"], str) or not attempt["generation_id"]:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "Attempt generation identity is invalid")
        try:
            snapshot = json.loads(str(job["run_snapshot_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "Attempt RunSnapshot authority is invalid") from exc
        if not isinstance(snapshot, Mapping):
            raise ContractError(ErrorCode.ASSET_ERROR, "Attempt RunSnapshot authority is invalid")
        if snapshot.get("snapshot_hash") is not None and snapshot.get("snapshot_hash") != job["run_snapshot_hash"]:
            raise ContractError(ErrorCode.ASSET_ERROR, "Attempt RunSnapshot hash authority drifted")
        if snapshot.get("schema") == "run-snapshot/v1":
            try:
                _verify_snapshot_release_binding(snapshot, attempt)
            except Exception as exc:
                raise ContractError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "Attempt release/generation is not bound to its RunSnapshot",
                ) from exc
        elif snapshot.get("schema") != "broker-child-snapshot-binding/v1":
            raise ContractError(ErrorCode.ASSET_ERROR, "Attempt Snapshot profile is unsupported")

    def _fence_attempt(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        step_id: str,
        attempt_id: str,
        lease_epoch: int,
        worker_run_id: str,
        now: str,
        require_active: bool = False,
        expected_snapshot_hash: str | None = None,
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        job = connection.execute(
            "SELECT * FROM execution_job WHERE job_id=?", (job_id,)
        ).fetchone()
        step = connection.execute(
            "SELECT * FROM execution_step WHERE job_id=? AND step_id=?",
            (job_id, step_id),
        ).fetchone()
        attempt = connection.execute(
            "SELECT * FROM execution_attempt WHERE attempt_id=? AND job_id=? AND step_id=?",
            (attempt_id, job_id, step_id),
        ).fetchone()
        if job is None or step is None or attempt is None:
            raise ContractError(ErrorCode.STALE_LEASE, "Attempt lineage is stale")
        if int(attempt["lease_epoch"]) != lease_epoch or step["active_attempt_id"] != attempt_id:
            raise ContractError(ErrorCode.STALE_LEASE, "Attempt lease epoch is stale")
        if attempt["worker_run_id"] != worker_run_id:
            raise ContractError(ErrorCode.STALE_LEASE, "worker run is not authoritative")
        if attempt["owner_instance_id"] is None or attempt["owner_instance_id"] != attempt["worker_run_id"]:
            raise ContractError(ErrorCode.STALE_LEASE, "Attempt owner authority drifted")
        if attempt["lease_expires_at"] is not None and _is_expired(str(attempt["lease_expires_at"]), now):
            raise ContractError(ErrorCode.STALE_LEASE, "Attempt lease has expired")
        if expected_snapshot_hash is not None and job["run_snapshot_hash"] != expected_snapshot_hash:
            raise _checkpoint_error("Attempt belongs to another RunSnapshot")
        self._validate_snapshot_attempt_binding(job, attempt)
        if require_active:
            if attempt["state"] not in _ACTIVE_ATTEMPT_STATES or step["state"] != "running":
                raise ContractError(ErrorCode.STALE_LEASE, "Attempt is no longer active")
            if job["job_state"] in {"succeeded", "partial", "failed", "cancelled"}:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "terminal Job cannot accept a checkpoint")
        return job, step, attempt

    @staticmethod
    def _row_value(row: sqlite3.Row) -> dict[str, Any]:
        try:
            value = json.loads(str(row["checkpoint_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "stored checkpoint is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ContractError(ErrorCode.ASSET_ERROR, "stored checkpoint is not an object")
        if str(row["checkpoint_json"]) != _json(value):
            raise ContractError(ErrorCode.ASSET_ERROR, "stored checkpoint is not canonical JSON")
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
            "SELECT workspace_id,run_snapshot_hash,job_event_high_water,current_checkpoint_id "
            "FROM execution_job WHERE job_id=?",
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
        attempt = connection.execute(
            "SELECT attempt_id,job_id,step_id,lease_epoch,plugin_id,release_id,package_hash,"
            "generation_id,owner_instance_id,worker_run_id "
            "FROM execution_attempt WHERE attempt_id=? AND job_id=? AND step_id=?",
            (row["source_attempt_id"], row["job_id"], row["step_id"]),
        ).fetchone()
        if attempt is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Attempt authority is missing")
        try:
            assert_valid("plugin-job-event/v1", event_value)
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Job Event is invalid") from exc
        if (
            not isinstance(event_value, dict)
            or event_value.get("job_id") != row["job_id"]
            or event_value.get("job_event_seq") != row["job_event_seq"]
            or event_value.get("step_id") != row["step_id"]
            or event_value.get("attempt_id") != row["source_attempt_id"]
            or event_value.get("event_id") != _stable_id(
                "job-event", row["job_id"], row["operation_key"]
            )
            or event_value.get("event_type")
            != f"plugin.{attempt['plugin_id']}.job.checkpoint"
            or event_value.get("release_id") != attempt["release_id"]
            or int(attempt["lease_epoch"]) != int(value["lease_epoch"])
            or attempt["owner_instance_id"] != attempt["worker_run_id"]
            or event_value.get("payload_asset_id") is None
            or event_value.get("payload_hash") is None
            or int(row["job_event_seq"]) > int(job["job_event_high_water"])
        ):
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Job Event identity drifted")
        if self.assets is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset authority is unavailable")
        try:
            asset_id = str(event_value["payload_asset_id"])
            asset_hash = str(event_value["payload_hash"])
            asset_raw = self.assets.read(asset_id)
            self.assets.require(asset_id, sha256=asset_hash)
            asset_value = parse_json_bytes(asset_raw)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset closure is missing") from exc
        if (
            not isinstance(asset_value, Mapping)
            or dict(asset_value) != value
            or asset_raw != canonical_bytes(value)
            or sha256_hex(asset_raw) != asset_hash
        ):
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset closure drifted")
        operation = connection.execute(
            "SELECT * FROM execution_checkpoint_operation WHERE job_id=? AND operation_key=?",
            (row["job_id"], row["operation_key"]),
        ).fetchone()
        if operation is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint operation authority is missing")
        if (
            operation["checkpoint_id"] != row["checkpoint_id"]
            or int(operation["job_event_seq"]) != int(row["job_event_seq"])
            or operation["payload_hash"] != row["payload_hash"]
            or operation["method"] != _CHECKPOINT_METHOD
        ):
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint operation identity drifted")
        try:
            response = json.loads(str(operation["response_json"]))
            request = json.loads(str(operation["request_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint operation closure is invalid") from exc
        expected_response = {
            "accepted": True,
            "checkpoint_id": row["checkpoint_id"],
            "completed_units": value["completed_units"],
            "total_units": value["total_units"],
            "job_event_seq": row["job_event_seq"],
        }
        if not isinstance(response, dict) or response != expected_response:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint operation response is inconsistent")
        if not isinstance(request, dict) or request.get("method") != _CHECKPOINT_METHOD:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint operation method is invalid")
        if sha256_hex(canonical_bytes(request)) != row["payload_hash"]:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint operation request hash drifted")
        expected_request = {
            "method": _CHECKPOINT_METHOD,
            "operation_key": row["operation_key"],
            "checkpoint": value,
            "checkpoint_asset_id": event_value["payload_asset_id"],
            "checkpoint_asset_hash": event_value["payload_hash"],
            "local_seq": None,
            "worker_run_id": attempt["worker_run_id"],
        }
        if request.get("local_seq") is not None:
            expected_request["local_seq"] = event_value["local_seq"]
        if request != expected_request:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint operation request binding drifted")
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
        if total != previous_total:
            raise _checkpoint_error("total_units changed within a checkpoint chain")
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
        require_active: bool = False,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        if worker_run_id is None:
            raise ContractValidationError("checkpoint requires a worker_run_id")
        job, _step, attempt = self._fence_attempt(
            connection,
            job_id=str(value["job_id"]),
            step_id=str(value["step_id"]),
            attempt_id=str(value["source_attempt_id"]),
            lease_epoch=int(value["lease_epoch"]),
            worker_run_id=worker_run_id,
            now=now,
            require_active=require_active,
            expected_snapshot_hash=str(value["run_snapshot_hash"]),
        )
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
        request_json: str,
        local_seq: int | None = None,
        worker_run_id: str | None = None,
        now: str,
    ) -> CheckpointCommit:
        if connection.execute(
            "SELECT 1 FROM execution_control_operation WHERE job_id=? AND operation_key=?",
            (value["job_id"], operation_key),
        ).fetchone() is not None:
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST,
                "operation key is already bound to a control method",
            )
        if checkpoint_asset_id is None or checkpoint_asset_hash is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint operation Asset binding is incomplete")
        try:
            request = json.loads(request_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint operation request is invalid") from exc
        expected_request = self._checkpoint_request(
            operation_key=operation_key,
            checkpoint=value,
            checkpoint_asset_id=checkpoint_asset_id,
            checkpoint_asset_hash=checkpoint_asset_hash,
            local_seq=local_seq,
            worker_run_id=str(worker_run_id),
        )
        if (
            not isinstance(request, dict)
            or request != expected_request
            or _json(request) != request_json
            or sha256_hex(canonical_bytes(request)) != payload_hash
        ):
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint operation request closure is invalid")
        job, attempt = self._validate_attempt_binding(
            connection, value, now=now, worker_run_id=worker_run_id
        )
        existing = connection.execute(
            "SELECT * FROM execution_checkpoint_operation WHERE job_id=? AND operation_key=?",
            (value["job_id"], operation_key),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "checkpoint operation key was reused with a different payload")
            if existing["method"] != _CHECKPOINT_METHOD or existing["request_json"] != request_json:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "checkpoint operation method or request drifted")
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

        try:
            asset_raw = self.assets.read(str(checkpoint_asset_id))
            asset_value = parse_json_bytes(asset_raw)
            if (
                not isinstance(asset_value, Mapping)
                or dict(asset_value) != dict(value)
                or asset_raw != canonical_bytes(value)
                or sha256_hex(asset_raw) != checkpoint_asset_hash
            ):
                raise _checkpoint_error("checkpoint Asset content drifted")
            self.assets.require(str(checkpoint_asset_id), sha256=checkpoint_asset_hash)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "checkpoint Asset closure is invalid") from exc

        self._validate_attempt_binding(
            connection,
            value,
            now=now,
            worker_run_id=worker_run_id,
            require_active=True,
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
        if (
            previous is not None
            and previous["source_attempt_id"] != value["source_attempt_id"]
            and (
                attempt["resume_of_attempt_id"] != previous["source_attempt_id"]
                or attempt["resume_checkpoint_id"] != previous["checkpoint_id"]
            )
        ):
            # A new source Attempt may continue a chain only when its durable
            # Attempt row explicitly records the exact resume edge.
            raise _checkpoint_error(
                "checkpoint source Attempt changed without an explicit resume"
            )
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
            "INSERT INTO execution_checkpoint_operation(job_id,operation_key,payload_hash,checkpoint_id,job_event_seq,response_json,created_at,method,request_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                value["job_id"], operation_key, payload_hash, value["checkpoint_id"],
                stored_event["job_event_seq"], _json(result), now, _CHECKPOINT_METHOD,
                request_json,
            ),
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
            checkpoint,
            checkpoint_asset_id=checkpoint_asset_id,
            allow_content_drift=True,
        )
        key = operation_key or str(value["checkpoint_id"])
        _require_id(key, "operation_key")
        if worker_run_id is None:
            raise ContractValidationError("checkpoint requires a worker_run_id")
        _require_id(worker_run_id, "worker_run_id")
        request = self._checkpoint_request(
            operation_key=key,
            checkpoint=value,
            checkpoint_asset_id=str(asset_id),
            checkpoint_asset_hash=str(asset_hash),
            local_seq=local_seq,
            worker_run_id=worker_run_id,
        )
        request_json = _json(request)
        now = _clock_value(self.clock)
        payload_hash = self._operation_hash(request)
        with self.repository.transaction() as connection:
            return self._commit_in_transaction(
                connection,
                value,
                operation_key=key,
                payload_hash=payload_hash,
                checkpoint_asset_id=asset_id,
                checkpoint_asset_hash=asset_hash,
                request_json=request_json,
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
        checkpoint_asset_id: str | None = None,
        now: str,
        local_seq: int | None = None,
    ) -> tuple[CheckpointCommit, dict[str, Any], str | None]:
        value, asset_id, asset_hash = self._load_source(
            source, checkpoint_asset_id=checkpoint_asset_id
        )
        if worker_run_id is None:
            raise ContractValidationError("checkpoint requires a worker_run_id")
        _require_id(worker_run_id, "worker_run_id")
        request = self._checkpoint_request(
            operation_key=operation_key,
            checkpoint=value,
            checkpoint_asset_id=str(asset_id),
            checkpoint_asset_hash=str(asset_hash),
            local_seq=local_seq,
            worker_run_id=worker_run_id,
        )
        result = self._commit_in_transaction(
            connection,
            value,
            operation_key=operation_key,
            payload_hash=self._operation_hash(request),
            checkpoint_asset_id=asset_id,
            checkpoint_asset_hash=asset_hash,
            request_json=_json(request),
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
            from ..api.v1.jobs.rpc.command_query import (
                JobCheckpointBinding,
                JobSnapshotExtensions,
            )

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
        lease_epoch: int | None = None,
        lease_expires_at: str | None = None,
        lease_ttl_seconds: int | None = None,
        now: str | None = None,
    ) -> OrchestrationOwnerLease:
        _require_id(workspace_id, "workspace_id")
        _require_id(owner_instance_id, "owner_instance_id")
        if owner_token is not None and not owner_token:
            raise ContractValidationError("owner_token cannot be empty")
        if owner_token is not None:
            _require_id(owner_token, "owner_token")
        if lease_epoch is not None:
            _require_positive_int(lease_epoch, "lease_epoch")
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
                if lease_epoch not in {None, 1}:
                    raise ContractError(ErrorCode.STALE_LEASE, "initial owner lease epoch is not one")
                connection.execute(
                    "INSERT INTO execution_orchestration_owner(workspace_id,owner_instance_id,owner_token,lease_epoch,lease_expires_at,revision,created_at,updated_at) VALUES(?,?,?,1,?,1,?,?)",
                    (workspace_id, owner_instance_id, token, expiry, current_now, current_now),
                )
                return OrchestrationOwnerLease(workspace_id, owner_instance_id, token, 1, expiry, 1)
            same_owner = row["owner_instance_id"] == owner_instance_id
            stale = _is_expired(str(row["lease_expires_at"]), current_now)
            if same_owner and not stale:
                # A tokenless acquire is a claim/takeover operation, never a
                # renewal.  A live row must be renewed with both halves of
                # its current fence so a stale caller cannot extend it.
                if owner_token is None or lease_epoch is None:
                    raise ContractError(
                        ErrorCode.STALE_LEASE,
                        "live owner renewal requires owner_token and lease_epoch",
                    )
                if owner_token != row["owner_token"] or lease_epoch != int(row["lease_epoch"]):
                    raise ContractError(ErrorCode.STALE_LEASE, "owner token is not authoritative")
                connection.execute(
                    "UPDATE execution_orchestration_owner SET lease_expires_at=?,revision=revision+1,updated_at=? "
                    "WHERE workspace_id=? AND owner_instance_id=? AND owner_token=? AND lease_epoch=? "
                    "AND lease_expires_at=?",
                    (
                        expiry,
                        current_now,
                        workspace_id,
                        row["owner_instance_id"],
                        row["owner_token"],
                        row["lease_epoch"],
                        row["lease_expires_at"],
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM execution_orchestration_owner WHERE workspace_id=?", (workspace_id,)
                ).fetchone()
                return self._decode(updated)  # type: ignore[return-value]
            if not stale:
                raise ContractError(ErrorCode.STALE_LEASE, "Workspace already has a live orchestration owner")
            if lease_epoch is not None and lease_epoch != int(row["lease_epoch"]):
                raise ContractError(ErrorCode.STALE_LEASE, "expired owner lease epoch is stale")
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
        self.core_events = CoreEventStore(repository)

    @staticmethod
    def _payload(operation: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "method": _CONTROL_METHODS[operation],
            "operation": operation,
            **dict(values),
        }

    @staticmethod
    def _method(operation: str) -> str:
        try:
            return _CONTROL_METHODS[operation]
        except KeyError as exc:
            raise ContractValidationError(f"unsupported execution control operation: {operation}") from exc

    def _request(self, operation: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return self._payload(operation, values)

    def _validate_prompt_asset(self, prompt_asset_id: Any, expected_hash: Any = None) -> str:
        if not isinstance(prompt_asset_id, str):
            raise ContractValidationError("await_user requires a prompt_asset_id")
        actual_hash = self.checkpoints.asset_hash(prompt_asset_id, label="prompt Asset")
        if expected_hash is not None and expected_hash != actual_hash:
            raise ContractError(ErrorCode.ASSET_ERROR, "prompt Asset hash authority drifted")
        return actual_hash

    def _validate_checkpoint_request_asset(self, request: Mapping[str, Any]) -> None:
        asset_id = request.get("checkpoint_asset_id")
        if not isinstance(asset_id, str):
            raise ContractError(ErrorCode.ASSET_ERROR, "control checkpoint Asset binding is missing")
        source = request.get("checkpoint")
        if isinstance(source, Mapping):
            value, loaded_id, loaded_hash = self.checkpoints._load_source(
                source,
                checkpoint_asset_id=asset_id,
            )
        else:
            value, loaded_id, loaded_hash = self.checkpoints._load_source(str(asset_id))
        if (
            value != request.get("checkpoint")
            or loaded_id != asset_id
            or loaded_hash != request.get("checkpoint_asset_hash")
        ):
            raise ContractError(ErrorCode.ASSET_ERROR, "control checkpoint Asset closure drifted")

    def _validate_control_request_closure(
        self,
        connection: sqlite3.Connection,
        operation: str,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        if request.get("method") != self._method(operation) or request.get("operation") != operation:
            raise ContractError(ErrorCode.ASSET_ERROR, "control operation method identity drifted")
        if operation in {"pause", "await_user", "resume"}:
            self._validate_checkpoint_request_asset(request)
        if operation == "await_user":
            reason = request.get("reason")
            if reason not in _AWAIT_USER_REASONS:
                raise ContractValidationError("await_user reason is not in the frozen enum")
            self._validate_prompt_asset(
                request.get("prompt_asset_id"), request.get("prompt_asset_hash")
            )

        checkpoint_value = request.get("checkpoint")
        if operation in {"pause", "await_user", "resume"}:
            if not isinstance(checkpoint_value, Mapping):
                raise ContractError(ErrorCode.ASSET_ERROR, "control checkpoint closure is not an object")
            checkpoint_row = connection.execute(
                "SELECT * FROM execution_checkpoint WHERE job_id=? AND checkpoint_id=?",
                (request.get("job_id"), checkpoint_value.get("checkpoint_id")),
            ).fetchone()
            if checkpoint_row is None:
                raise ContractError(ErrorCode.ASSET_ERROR, "control checkpoint row is missing")
            stored = self.checkpoints._decode_row(connection, checkpoint_row)
            if stored.checkpoint != dict(checkpoint_value):
                raise ContractError(ErrorCode.ASSET_ERROR, "control checkpoint row drifted")
            if operation in {"pause", "await_user"} and checkpoint_row["operation_key"] != request.get("operation_key"):
                raise ContractError(ErrorCode.ASSET_ERROR, "control checkpoint operation is not bound")

        self._validate_control_state_closure(
            connection,
            operation=operation,
            operation_key=str(request.get("operation_key")),
            request=request,
            result=result,
            checkpoint_row=checkpoint_row if operation in {"pause", "await_user", "resume"} else None,
        )

    def _validate_control_state_closure(
        self,
        connection: sqlite3.Connection,
        *,
        operation: str,
        operation_key: str,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
        checkpoint_row: sqlite3.Row | None,
    ) -> None:
        """Verify the durable Job/Core Event pair behind a replayed control."""

        job = connection.execute(
            "SELECT * FROM execution_job WHERE job_id=?", (request.get("job_id"),)
        ).fetchone()
        if job is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "control Job authority is missing")
        attempt_id = request.get("attempt_id")
        if operation == "resume":
            attempt_id = request.get("new_attempt_id")
        attempt = connection.execute(
            "SELECT * FROM execution_attempt WHERE attempt_id=? AND job_id=? AND step_id=?",
            (attempt_id, request.get("job_id"), request.get("step_id")),
        ).fetchone()
        if attempt is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "control Attempt authority is missing")
        if operation == "resume":
            if (
                attempt["resume_of_attempt_id"] != request.get("source_attempt_id")
                or attempt["resume_checkpoint_id"] != request["checkpoint"]["checkpoint_id"]
                or attempt["worker_run_id"] != request.get("worker_run_id")
            ):
                raise ContractError(ErrorCode.ASSET_ERROR, "resume Attempt closure drifted")
        elif attempt_id != request.get("attempt_id"):
            raise ContractError(ErrorCode.ASSET_ERROR, "control Attempt identity drifted")

        if operation == "cancel" and result.get("terminal_known") is True:
            if (
                result.get("attempt_state") != attempt["state"]
                or (
                    attempt["state"] not in _TERMINAL_ATTEMPT_STATES
                    and job["job_state"] not in {"succeeded", "partial", "failed", "cancelled"}
                )
            ):
                raise ContractError(ErrorCode.ASSET_ERROR, "terminal cancel closure drifted")
            return

        control_event_id = _stable_id("job-event", request["job_id"], operation, operation_key)
        control_event_row = connection.execute(
            "SELECT * FROM execution_job_event WHERE event_id=?", (control_event_id,)
        ).fetchone()
        if control_event_row is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "control Job Event authority is missing")
        try:
            control_event = json.loads(str(control_event_row["event_json"]))
            assert_valid("plugin-job-event/v1", control_event)
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "control Job Event is invalid") from exc
        expected_payload = (None, None)
        if checkpoint_row is not None:
            checkpoint_event = connection.execute(
                "SELECT event_json FROM execution_job_event WHERE job_id=? AND job_event_seq=?",
                (checkpoint_row["job_id"], checkpoint_row["job_event_seq"]),
            ).fetchone()
            if checkpoint_event is None:
                raise ContractError(ErrorCode.ASSET_ERROR, "control checkpoint Event is missing")
            checkpoint_event_value = json.loads(str(checkpoint_event["event_json"]))
            expected_payload = (
                checkpoint_event_value.get("payload_asset_id"),
                checkpoint_event_value.get("payload_hash"),
            )
        if (
            control_event.get("event_id") != control_event_row["event_id"]
            or control_event.get("job_id") != request["job_id"]
            or control_event.get("step_id") != request["step_id"]
            or control_event.get("attempt_id") != attempt_id
            or control_event.get("job_event_seq") != control_event_row["job_event_seq"]
            or control_event.get("local_seq") != control_event_row["local_seq"]
            or control_event.get("event_type") != f"plugin.{attempt['plugin_id']}.job.{operation}"
            or control_event.get("plugin_id") != attempt["plugin_id"]
            or control_event.get("release_id") != attempt["release_id"]
            or (
                control_event.get("payload_asset_id"),
                control_event.get("payload_hash"),
            )
            != expected_payload
            or int(control_event_row["job_event_seq"]) > int(job["job_event_high_water"])
        ):
            raise ContractError(ErrorCode.ASSET_ERROR, "control Job Event identity drifted")

        core_event_id = _stable_id(
            "core-event", request["job_id"], operation_key, "job.state.changed"
        )
        core_event_row = connection.execute(
            "SELECT * FROM execution_core_event WHERE event_id=?", (core_event_id,)
        ).fetchone()
        if core_event_row is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "control Core Event authority is missing")
        try:
            core_event = json.loads(str(core_event_row["event_json"]))
            assert_valid("core-event/v1", core_event)
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "control Core Event is invalid") from exc
        if (
            core_event.get("event_id") != core_event_row["event_id"]
            or core_event.get("core_event_seq") != core_event_row["core_event_seq"]
            or core_event.get("workspace_id") != job["workspace_id"]
            or core_event.get("aggregate_id") != request["job_id"]
            or core_event.get("aggregate_revision") != core_event_row["aggregate_revision"]
            or core_event.get("event_type") != "job.state.changed"
            or core_event.get("correlation_id") != operation_key
            or core_event.get("causation_id") != attempt_id
            or (
                core_event.get("payload_asset_id"),
                core_event.get("payload_hash"),
            )
            != expected_payload
            or int(core_event_row["core_event_seq"]) > int(job["core_event_high_water"])
            or int(core_event_row["aggregate_revision"]) > int(job["job_revision"])
        ):
            raise ContractError(ErrorCode.ASSET_ERROR, "control Core Event identity drifted")

    @staticmethod
    def _validate_resume_result(result: Mapping[str, Any]) -> None:
        """Validate the frozen, but schema-ambiguous, ``job.resume`` result."""

        if (
            set(result)
            != {"accepted", "worker_run_id", "provenance_receipt_id", "output_streams"}
            or not isinstance(result["accepted"], bool)
            or not isinstance(result["worker_run_id"], str)
            or not isinstance(result["provenance_receipt_id"], str)
            or not isinstance(result["output_streams"], list)
        ):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "invalid job.resume result")
        _require_id(result["worker_run_id"], "worker_run_id")
        _require_id(result["provenance_receipt_id"], "provenance_receipt_id")

    def _existing(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        operation_key: str,
        operation: str,
        payload_hash: str,
        request_json: str,
    ) -> ControlDecision | None:
        row = connection.execute(
            "SELECT * FROM execution_control_operation WHERE job_id=? AND operation_key=?",
            (job_id, operation_key),
        ).fetchone()
        if row is None:
            return None
        method = self._method(operation)
        if (
            row["operation"] != operation
            or row["method"] != method
            or row["payload_hash"] != payload_hash
            or row["request_json"] != request_json
        ):
            raise ContractError(ErrorCode.DUPLICATE_REQUEST, "control operation key was reused with a different payload")
        try:
            stored_response = json.loads(str(row["response_json"]))
            request = json.loads(str(row["request_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "control operation response is invalid") from exc
        if not isinstance(stored_response, dict) or not isinstance(request, dict):
            raise ContractError(ErrorCode.ASSET_ERROR, "control operation response is not an object")
        result = stored_response
        if set(stored_response) == {
            _CONTROL_AUTHORITY_RESULT,
            _CONTROL_PUBLIC_RESPONSE,
        }:
            authority_result = stored_response[_CONTROL_AUTHORITY_RESULT]
            public_response = stored_response[_CONTROL_PUBLIC_RESPONSE]
            if not isinstance(authority_result, dict) or not isinstance(public_response, dict):
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "control operation response envelope is invalid",
                )
            result = authority_result
        if _json(request) != request_json or sha256_hex(canonical_bytes(request)) != payload_hash:
            raise ContractError(ErrorCode.ASSET_ERROR, "control operation request closure is invalid")
        self._validate_control_request_closure(connection, operation, request, result)
        try:
            if method == "job.resume":
                self._validate_resume_result(result)
            else:
                validate_rpc_result(method, result)
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "control operation response drifted") from exc
        return ControlDecision(result, operation, True)

    def _caller(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        step_id: str,
        attempt_id: str,
        lease_epoch: int,
        worker_run_id: str | None,
        now: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        if worker_run_id is None:
            raise ContractValidationError("control operation requires a worker_run_id")
        _require_id(worker_run_id, "worker_run_id")
        _require_positive_int(lease_epoch, "lease_epoch")
        return self.checkpoints._fence_attempt(
            connection,
            job_id=job_id,
            step_id=step_id,
            attempt_id=attempt_id,
            lease_epoch=lease_epoch,
            worker_run_id=worker_run_id,
            now=now,
            require_active=False,
        )

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
        if local_seq is not None:
            _require_positive_int(local_seq, "local_seq")
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

    def _append_state_core_event(
        self,
        connection: sqlite3.Connection,
        *,
        job: sqlite3.Row,
        attempt: sqlite3.Row,
        operation_key: str,
        now: str,
        payload_asset_id: str | None = None,
        payload_hash: str | None = None,
    ) -> dict[str, Any]:
        """Append the authoritative Job state Core Event in this transaction."""

        if (payload_asset_id is None) != (payload_hash is None):
            raise ContractError(ErrorCode.ASSET_ERROR, "Core state event Asset ID/hash must be paired")
        aggregate_revision = int(job["job_revision"]) + 1
        event = {
            "schema": "core-event/v1",
            "event_id": _stable_id("core-event", job["job_id"], operation_key, "job.state.changed"),
            "workspace_id": job["workspace_id"],
            "aggregate_id": job["job_id"],
            "aggregate_revision": aggregate_revision,
            "event_type": "job.state.changed",
            "producer": {
                "producer_type": "core",
                "producer_id": "execution-control",
                "release_id": None,
            },
            "correlation_id": operation_key,
            "causation_id": attempt["attempt_id"],
            "payload_asset_id": payload_asset_id,
            "payload_hash": payload_hash,
            "occurred_at": now,
        }
        return self.core_events.append(event, connection=connection)

    @staticmethod
    def _commit_job_state(
        connection: sqlite3.Connection,
        *,
        job: sqlite3.Row,
        target_state: str,
        core_event: Mapping[str, Any],
        now: str,
    ) -> None:
        aggregate_revision = int(job["job_revision"]) + 1
        if int(core_event["aggregate_revision"]) != aggregate_revision:
            raise ContractError(ErrorCode.ASSET_ERROR, "Core state Event aggregate revision drifted")
        updated = connection.execute(
            "UPDATE execution_job SET job_state=?,job_revision=?,core_event_high_water=?,updated_at=? "
            "WHERE job_id=? AND job_state=? AND job_revision=? AND core_event_high_water=?",
            (
                target_state,
                aggregate_revision,
                int(core_event["core_event_seq"]),
                now,
                job["job_id"],
                job["job_state"],
                int(job["job_revision"]),
                int(job["core_event_high_water"]),
            ),
        )
        if updated.rowcount != 1:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Job state CAS lost")

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
        request_json: str,
        result: Mapping[str, Any],
        now: str,
        store_public_response: bool = False,
    ) -> ControlDecision:
        method = self._method(operation)
        if sha256_hex(canonical_bytes(json.loads(request_json))) != payload_hash:
            raise ContractError(ErrorCode.ASSET_ERROR, "control operation request hash is invalid")
        if method == "job.resume":
            # The frozen success schema contains identical ``job.start`` and
            # ``job.resume`` branches.  Its oneOf rejects this otherwise
            # valid method result as ambiguous, so retain the closed,
            # method-specific validation locally rather than weakening the
            # public schema or changing the verifier.
            self._validate_resume_result(result)
        else:
            validate_rpc_result(method, result)
        stored_response: Mapping[str, Any] = result
        if store_public_response:
            job = connection.execute(
                "SELECT workspace_id,job_state,job_revision,job_event_high_water "
                "FROM execution_job WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "control operation lost its Job authority",
                )
            public_state = (
                "needs_attention"
                if job["job_state"] in {"waiting_user", "interrupted"}
                else str(job["job_state"])
            )
            stored_response = {
                _CONTROL_AUTHORITY_RESULT: dict(result),
                _CONTROL_PUBLIC_RESPONSE: {
                    "schema": "job-command-result/v2",
                    "operation_key": operation_key,
                    "workspace_id": str(job["workspace_id"]),
                    "job_id": job_id,
                    "command": operation,
                    "accepted": True,
                    "idempotent": False,
                    "terminal_known": public_state in _TERMINAL_JOB_STATES,
                    "state": public_state,
                    "job_revision": int(job["job_revision"]),
                    "snapshot_cursor": (
                        f"job/{job_id}/{int(job['job_event_high_water'])}"
                    ),
                },
            }
        connection.execute(
            "INSERT INTO execution_control_operation("
            "job_id,operation_key,operation,payload_hash,attempt_id,lease_epoch,response_json,created_at,method,request_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                operation_key,
                operation,
                payload_hash,
                attempt_id,
                lease_epoch,
                _json(stored_response),
                now,
                method,
                request_json,
            ),
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
        expected_job_revision: int | None = None,
        store_public_response: bool = False,
    ) -> ControlDecision:
        _require_id(operation_key, "operation_key")
        _require_id(job_id, "job_id")
        _require_id(step_id, "step_id")
        _require_id(attempt_id, "attempt_id")
        if worker_run_id is None:
            raise ContractValidationError("cancel requires a worker_run_id")
        _require_id(worker_run_id, "worker_run_id")
        _require_positive_int(lease_epoch, "lease_epoch")
        if not isinstance(reason, str) or not reason:
            raise ContractValidationError("reason must be non-empty")
        if expected_job_revision is not None:
            _require_positive_int(expected_job_revision, "expected_job_revision")
        request_values = {
            "job_id": job_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
            "lease_epoch": lease_epoch,
            "operation_key": operation_key,
            "worker_run_id": worker_run_id,
            "reason": reason,
        }
        if expected_job_revision is not None:
            request_values["expected_job_revision"] = expected_job_revision
        request = self._request(
            "cancel",
            request_values,
        )
        request_json = _json(request)
        payload_hash = sha256_hex(canonical_bytes(request))
        now = _clock_value(self.clock)
        with self.repository.transaction() as connection:
            job, _step, attempt = self._caller(
                connection,
                job_id=job_id,
                step_id=step_id,
                attempt_id=attempt_id,
                lease_epoch=lease_epoch,
                worker_run_id=worker_run_id,
                now=now,
            )
            replay = self._existing(
                connection, job_id, operation_key, "cancel", payload_hash, request_json
            )
            if replay is not None:
                return replay
            if (
                expected_job_revision is not None
                and int(job["job_revision"]) != expected_job_revision
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "expected Job revision CAS failed"
                )
            if connection.execute(
                "SELECT 1 FROM execution_checkpoint_operation WHERE job_id=? AND operation_key=?",
                (job_id, operation_key),
            ).fetchone() is not None:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation key is already bound to a checkpoint")
            if attempt["state"] in _TERMINAL_ATTEMPT_STATES or job["job_state"] in {
                "succeeded",
                "partial",
                "failed",
                "cancelled",
            }:
                result = {
                    "accepted": True,
                    "terminal_known": True,
                    "attempt_state": attempt["state"],
                }
                return self._store_operation(
                    connection,
                    job_id=job_id,
                    operation_key=operation_key,
                    operation="cancel",
                    payload_hash=payload_hash,
                    attempt_id=attempt_id,
                    lease_epoch=lease_epoch,
                    request_json=request_json,
                    result=result,
                    now=now,
                    store_public_response=store_public_response,
                )
            if attempt["state"] != "running" or job["job_state"] != "running":
                raise ContractError(ErrorCode.INVALID_TRANSITION, "cancel race lost to an earlier state decision")
            if not can_transition(ATTEMPT_EDGES, attempt["state"], "cancelling") or not can_transition(JOB_EDGES, job["job_state"], "cancelling"):
                raise ContractError(ErrorCode.INVALID_TRANSITION, "invalid cancel transition")
            updated_attempt = connection.execute(
                "UPDATE execution_attempt SET state='cancelling',revision=revision+1,updated_at=? WHERE attempt_id=? AND state='running' AND lease_epoch=?",
                (now, attempt_id, lease_epoch),
            )
            if updated_attempt.rowcount != 1:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "cancel CAS lost")
            self._append_event(connection, job=job, step_id=step_id, attempt=attempt, operation="cancel", operation_key=operation_key, now=now)
            core_event = self._append_state_core_event(
                connection,
                job=job,
                attempt=attempt,
                operation_key=operation_key,
                now=now,
            )
            self._commit_job_state(
                connection,
                job=job,
                target_state="cancelling",
                core_event=core_event,
                now=now,
            )
            result = {"accepted": True, "terminal_known": False, "attempt_state": "cancelling"}
            validate_rpc_result("job.cancel", result)
            return self._store_operation(
                connection,
                job_id=job_id,
                operation_key=operation_key,
                operation="cancel",
                payload_hash=payload_hash,
                attempt_id=attempt_id,
                lease_epoch=lease_epoch,
                request_json=request_json,
                result=result,
                now=now,
                store_public_response=store_public_response,
            )

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
        expected_job_revision: int | None = None,
        store_public_response: bool = False,
    ) -> ControlDecision:
        _require_id(operation_key, "operation_key")
        _require_id(job_id, "job_id")
        _require_id(step_id, "step_id")
        _require_id(attempt_id, "attempt_id")
        if worker_run_id is None:
            raise ContractValidationError(f"{operation} requires a worker_run_id")
        _require_id(worker_run_id, "worker_run_id")
        _require_positive_int(lease_epoch, "lease_epoch")
        if not isinstance(reason, str) or not reason:
            raise ContractValidationError("reason must be non-empty")
        if operation == "await_user" and reason not in _AWAIT_USER_REASONS:
            raise ContractValidationError("await_user reason is not in the frozen enum")
        if expected_job_revision is not None:
            _require_positive_int(expected_job_revision, "expected_job_revision")
        if checkpoint is None and checkpoint_asset_id is None:
            raise ContractError(ErrorCode.CHECKPOINT_INVALID, "suspension requires a checkpoint Asset")
        checkpoint_source: Mapping[str, Any] | str = (
            checkpoint if checkpoint is not None else str(checkpoint_asset_id)
        )
        checkpoint_value, source_asset_id, source_asset_hash = self.checkpoints._load_source(
            checkpoint_source,
            checkpoint_asset_id=checkpoint_asset_id if isinstance(checkpoint_source, Mapping) else None,
        )
        prompt_asset_hash = None
        if operation == "await_user" or prompt_asset_id is not None:
            prompt_asset_hash = self._validate_prompt_asset(prompt_asset_id)
        request_values = {
                "job_id": job_id,
                "step_id": step_id,
                "attempt_id": attempt_id,
                "lease_epoch": lease_epoch,
                "operation_key": operation_key,
                "worker_run_id": worker_run_id,
                "reason": reason,
                "checkpoint": checkpoint_value,
                "checkpoint_asset_id": source_asset_id,
                "checkpoint_asset_hash": source_asset_hash,
                "prompt_asset_id": prompt_asset_id,
                "prompt_asset_hash": prompt_asset_hash,
            }
        if expected_job_revision is not None:
            request_values["expected_job_revision"] = expected_job_revision
        request = self._request(operation, request_values)
        request_json = _json(request)
        payload_hash = sha256_hex(canonical_bytes(request))
        now = _clock_value(self.clock)
        with self.repository.transaction() as connection:
            job, step, attempt = self._caller(
                connection,
                job_id=job_id,
                step_id=step_id,
                attempt_id=attempt_id,
                lease_epoch=lease_epoch,
                worker_run_id=worker_run_id,
                now=now,
            )
            replay = self._existing(
                connection, job_id, operation_key, operation, payload_hash, request_json
            )
            if replay is not None:
                return replay
            if (
                expected_job_revision is not None
                and int(job["job_revision"]) != expected_job_revision
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "expected Job revision CAS failed"
                )
            if connection.execute(
                "SELECT 1 FROM execution_checkpoint_operation WHERE job_id=? AND operation_key=?",
                (job_id, operation_key),
            ).fetchone() is not None:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation key is already bound to a checkpoint")
            if attempt["state"] != "running" or job["job_state"] != "running" or step["state"] != "running":
                raise ContractError(ErrorCode.INVALID_TRANSITION, "suspension race lost to an earlier state decision")
            if not can_transition(ATTEMPT_EDGES, "running", "suspended") or not can_transition(STEP_EDGES, "running", target_state) or not can_transition(JOB_EDGES, "running", target_state):
                raise ContractError(ErrorCode.INVALID_TRANSITION, "invalid suspension transition")
            _checkpoint_result, checkpoint_value, source_asset_id = self.checkpoints._commit_source_in_transaction(
                connection,
                checkpoint_value,
                operation_key=operation_key,
                worker_run_id=worker_run_id,
                checkpoint_asset_id=source_asset_id,
                now=now,
            )
            asset_id = source_asset_id or checkpoint_value.get("state_asset_id")
            if not isinstance(asset_id, str):
                raise ContractError(ErrorCode.CHECKPOINT_INVALID, "accepted suspension requires a checkpoint Asset")
            asset_hash = source_asset_hash
            updated_attempt = connection.execute(
                "UPDATE execution_attempt SET state='suspended',revision=revision+1,updated_at=? "
                "WHERE attempt_id=? AND state='running' AND lease_epoch=? AND worker_run_id=?",
                (now, attempt_id, lease_epoch, worker_run_id),
            )
            updated_step = connection.execute(
                "UPDATE execution_step SET state=?,revision=revision+1,updated_at=? "
                "WHERE job_id=? AND step_id=? AND state='running' AND active_attempt_id=?",
                (target_state, now, job_id, step_id, attempt_id),
            )
            if updated_attempt.rowcount != 1 or updated_step.rowcount != 1:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "suspension CAS lost")
            control_event = self._append_event(
                connection,
                job=job,
                step_id=step_id,
                attempt=attempt,
                operation=operation,
                operation_key=operation_key,
                now=now,
                payload_asset_id=source_asset_id or asset_id,
                payload_hash=asset_hash,
            )
            core_event = self._append_state_core_event(
                connection,
                job=job,
                attempt=attempt,
                operation_key=operation_key,
                now=now,
                payload_asset_id=source_asset_id or asset_id,
                payload_hash=asset_hash,
            )
            self._commit_job_state(
                connection,
                job=job,
                target_state=target_state,
                core_event=core_event,
                now=now,
            )
            if operation == "pause":
                result = {"accepted": True, "checkpoint_asset_id": asset_id}
            else:
                result = {
                    "accepted": True,
                    "attempt_state": "suspended",
                    "step_state": target_state,
                    "job_state": target_state,
                    "job_event_seq": int(control_event["job_event_seq"]),
                }
            validate_rpc_result(host_method, result)
            return self._store_operation(
                connection,
                job_id=job_id,
                operation_key=operation_key,
                operation=operation,
                payload_hash=payload_hash,
                attempt_id=attempt_id,
                lease_epoch=lease_epoch,
                request_json=request_json,
                result=result,
                now=now,
                store_public_response=store_public_response,
            )

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
        expected_job_revision: int | None = None,
        store_public_response: bool = False,
    ) -> ControlDecision:
        source_attempt_id = resume_of_attempt_id or attempt_id
        _require_id(job_id, "job_id")
        _require_id(step_id, "step_id")
        _require_id(operation_key, "operation_key")
        if source_attempt_id is None:
            raise ContractValidationError("resume requires resume_of_attempt_id")
        _require_id(source_attempt_id, "resume_of_attempt_id")
        if checkpoint is None and checkpoint_asset_id is None:
            raise ContractError(ErrorCode.CHECKPOINT_INVALID, "resume requires a checkpoint Asset")
        if worker_run_id is None:
            raise ContractValidationError("resume requires a new worker_run_id")
        _require_id(worker_run_id, "worker_run_id")
        if lease_epoch is not None:
            _require_positive_int(lease_epoch, "lease_epoch")
        if not isinstance(resume_reason, str) or not resume_reason:
            raise ContractValidationError("resume_reason must be non-empty")
        if expected_job_revision is not None:
            _require_positive_int(expected_job_revision, "expected_job_revision")
        checkpoint_source: Mapping[str, Any] | str = (
            checkpoint if checkpoint is not None else str(checkpoint_asset_id)
        )
        checkpoint_value, source_asset_id, source_asset_hash = self.checkpoints._load_source(
            checkpoint_source,
            checkpoint_asset_id=checkpoint_asset_id if isinstance(checkpoint_source, Mapping) else None,
        )
        request_values = {
                "job_id": job_id,
                "step_id": step_id,
                "operation_key": operation_key,
                "attempt_id": attempt_id,
                "resume_of_attempt_id": resume_of_attempt_id,
                "source_attempt_id": source_attempt_id,
                "lease_epoch": lease_epoch,
                "worker_run_id": worker_run_id,
                "new_attempt_id": new_attempt_id,
                "plugin_id": plugin_id,
                "release_id": release_id,
                "package_hash": package_hash,
                "capability_id": capability_id,
                "generation_id": generation_id,
                "preallocated_receipt_id": preallocated_receipt_id,
                "lease_expires_at": lease_expires_at,
                "checkpoint": checkpoint_value,
                "checkpoint_asset_id": source_asset_id,
                "checkpoint_asset_hash": source_asset_hash,
                "resume_reason": resume_reason,
            }
        if expected_job_revision is not None:
            request_values["expected_job_revision"] = expected_job_revision
        request = self._request("resume", request_values)
        request_json = _json(request)
        payload_hash = sha256_hex(canonical_bytes(request))
        now = _clock_value(self.clock)
        with self.repository.transaction() as connection:
            job = connection.execute("SELECT * FROM execution_job WHERE job_id=?", (job_id,)).fetchone()
            step = connection.execute("SELECT * FROM execution_step WHERE job_id=? AND step_id=?", (job_id, step_id)).fetchone()
            old = connection.execute("SELECT * FROM execution_attempt WHERE attempt_id=? AND job_id=? AND step_id=?", (source_attempt_id, job_id, step_id)).fetchone()
            if job is None or step is None or old is None:
                raise ContractError(ErrorCode.STALE_LEASE, "resume Attempt lineage is stale")
            if old["owner_instance_id"] is None or old["owner_instance_id"] != old["worker_run_id"]:
                raise ContractError(ErrorCode.STALE_LEASE, "resume source Attempt owner authority drifted")
            if lease_epoch is not None and int(old["lease_epoch"]) != lease_epoch:
                raise ContractError(ErrorCode.STALE_LEASE, "resume Attempt lease is stale")
            self.checkpoints._validate_snapshot_attempt_binding(job, old)
            stored_operation = connection.execute(
                "SELECT attempt_id,lease_epoch FROM execution_control_operation "
                "WHERE job_id=? AND operation_key=?",
                (job_id, operation_key),
            ).fetchone()
            if stored_operation is not None:
                if stored_operation["attempt_id"] is None or stored_operation["lease_epoch"] is None:
                    raise ContractError(ErrorCode.ASSET_ERROR, "resume operation fence is incomplete")
                self.checkpoints._fence_attempt(
                    connection,
                    job_id=job_id,
                    step_id=step_id,
                    attempt_id=str(stored_operation["attempt_id"]),
                    lease_epoch=int(stored_operation["lease_epoch"]),
                    worker_run_id=worker_run_id,
                    now=now,
                    require_active=False,
                )
            elif new_attempt_id is not None:
                candidate = connection.execute(
                    "SELECT lease_epoch FROM execution_attempt WHERE attempt_id=? "
                    "AND job_id=? AND step_id=?",
                    (new_attempt_id, job_id, step_id),
                ).fetchone()
                if candidate is not None:
                    self.checkpoints._fence_attempt(
                        connection,
                        job_id=job_id,
                        step_id=step_id,
                        attempt_id=new_attempt_id,
                        lease_epoch=int(candidate["lease_epoch"]),
                        worker_run_id=worker_run_id,
                        now=now,
                        require_active=False,
                    )
            replay = self._existing(
                connection, job_id, operation_key, "resume", payload_hash, request_json
            )
            if replay is not None:
                return replay
            if (
                expected_job_revision is not None
                and int(job["job_revision"]) != expected_job_revision
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "expected Job revision CAS failed"
                )
            if connection.execute(
                "SELECT 1 FROM execution_checkpoint_operation WHERE job_id=? AND operation_key=?",
                (job_id, operation_key),
            ).fetchone() is not None:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation key is already bound to a checkpoint")
            if job["job_state"] not in {"paused", "waiting_user", "needs_attention"} or step["state"] not in {"paused", "waiting_user", "needs_attention"} or old["state"] != "suspended":
                raise ContractError(ErrorCode.INVALID_TRANSITION, "terminal or non-suspended Attempt cannot be resumed")
            value, loaded_asset_id, loaded_asset_hash = self.checkpoints._load_source(
                checkpoint_value,
                checkpoint_asset_id=source_asset_id,
            )
            if loaded_asset_id != source_asset_id or loaded_asset_hash != source_asset_hash:
                raise ContractError(ErrorCode.ASSET_ERROR, "resume checkpoint Asset closure drifted")
            verify_checkpoint(value, expected_snapshot_hash=str(job["run_snapshot_hash"]))
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
            _require_id(selected["plugin_id"], "plugin_id")
            _require_hash(selected["release_id"], "release_id")
            _require_hash(selected["package_hash"], "package_hash")
            _require_id(selected["capability_id"], "capability_id")
            _require_id(selected["generation_id"], "generation_id")
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
            updated_step = connection.execute(
                "UPDATE execution_step SET state='running',active_attempt_id=?,next_lease_epoch=?,revision=revision+1,updated_at=? "
                "WHERE job_id=? AND step_id=? AND state=? AND active_attempt_id=?",
                (new_id, epoch + 1, now, job_id, step_id, step["state"], source_attempt_id),
            )
            if updated_step.rowcount != 1:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "resume Step CAS lost")
            new_attempt = connection.execute(
                "SELECT * FROM execution_attempt WHERE attempt_id=?", (new_id,)
            ).fetchone()
            if new_attempt is None:
                raise ContractError(ErrorCode.ASSET_ERROR, "resume Attempt allocation disappeared")
            self._append_event(
                connection,
                job=job,
                step_id=step_id,
                attempt=new_attempt,
                operation="resume",
                operation_key=operation_key,
                now=now,
                payload_asset_id=source_asset_id,
                payload_hash=source_asset_hash,
                local_seq=1,
            )
            core_event = self._append_state_core_event(
                connection,
                job=job,
                attempt=new_attempt,
                operation_key=operation_key,
                now=now,
                payload_asset_id=source_asset_id,
                payload_hash=source_asset_hash,
            )
            self._commit_job_state(
                connection,
                job=job,
                target_state="running",
                core_event=core_event,
                now=now,
            )
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
            return self._store_operation(
                connection,
                job_id=job_id,
                operation_key=operation_key,
                operation="resume",
                payload_hash=payload_hash,
                attempt_id=new_id,
                lease_epoch=epoch,
                request_json=request_json,
                result=result,
                now=now,
                store_public_response=store_public_response,
            )

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
