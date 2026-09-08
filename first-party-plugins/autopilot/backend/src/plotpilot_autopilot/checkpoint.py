"""Pure checkpoint envelopes for the Core-owned durable checkpoint authority.

This module only builds and verifies immutable checkpoint Assets.  It has no
filesystem, database, cache, or process-local replay ledger.  Recovery accepts
only the exact checkpoint locator selected by ``job.resume`` and validates its
direct source Attempt edge; it never discovers a checkpoint on its own.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(
    r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$"
)
_RESULT_CONTRACTS = frozenset(
    {"candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"}
)
_CHECKPOINT_FIELDS = frozenset(
    {
        "schema",
        "checkpoint_id",
        "checkpoint_seq",
        "job_id",
        "step_id",
        "source_attempt_id",
        "lease_epoch",
        "run_snapshot_hash",
        "replay_policy",
        "completed_units",
        "total_units",
        "unit_set_hash",
        "state_asset_id",
        "created_at",
        "checkpoint_hash",
    }
)
_STATE_FIELDS = frozenset(
    {
        "schema",
        "workspace_id",
        "job_id",
        "step_id",
        "attempt_id",
        "lease_epoch",
        "plugin_release_id",
        "run_snapshot_hash",
        "created_at",
        "dag_hash",
        "checkpoint_id",
        "checkpoint_seq",
        "previous_checkpoint_hash",
        "completed_stages",
    }
)
_EFFECT_FIELDS = frozenset(
    {
        "stage_id",
        "operation_key",
        "child_job_id",
        "child_step_id",
        "child_run_snapshot_asset_id",
        "child_run_snapshot_hash",
        "result_bundle_asset_id",
        "provenance_receipt_id",
        "child_result_contract",
        "effect_hash",
    }
)


class AutopilotCheckpointError(ValueError):
    """Raised when a checkpoint lacks a closed durable identity binding."""


def canonical_json_bytes(value: object) -> bytes:
    """Encode the primitive-only checkpoint profile deterministically.

    Checkpoint values intentionally contain only IDs, hashes, booleans,
    integers, nulls, and ASCII timestamps.  For that closed profile this
    serializer is byte-equivalent to the repository's RFC 8785 profile while
    keeping this source package dependency-free.
    """
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise AutopilotCheckpointError(
            f"value is not canonical checkpoint JSON: {exc}"
        ) from exc


def parse_canonical_json_bytes(data: bytes) -> object:
    """Decode strict UTF-8 canonical JSON without duplicate keys or a BOM."""
    if not isinstance(data, bytes):
        raise AutopilotCheckpointError("checkpoint Asset must contain bytes")
    if data.startswith(b"\xef\xbb\xbf"):
        raise AutopilotCheckpointError("checkpoint Asset cannot contain a UTF-8 BOM")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AutopilotCheckpointError(
                    "checkpoint Asset contains duplicate JSON keys"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise AutopilotCheckpointError(
            f"checkpoint Asset contains non-finite JSON: {value}"
        )

    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AutopilotCheckpointError(
            f"checkpoint Asset is not strict UTF-8 JSON: {exc}"
        ) from exc
    if canonical_json_bytes(value) != data:
        raise AutopilotCheckpointError("checkpoint Asset is not canonical JSON")
    return value


def sha256_hex(data: bytes) -> str:
    if not isinstance(data, bytes):
        raise TypeError("sha256_hex expects bytes")
    return sha256(data).hexdigest()


def hash_payload(prefix: str, value: object) -> str:
    if not isinstance(prefix, str) or not prefix.isascii() or not prefix:
        raise AutopilotCheckpointError("checkpoint hash prefix must be non-empty ASCII")
    return sha256_hex(prefix.encode("ascii") + b"\n" + canonical_json_bytes(value))


def validate_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise AutopilotCheckpointError(f"{field} must be a PlotPilot identifier")
    return value


def validate_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise AutopilotCheckpointError(f"{field} must be a lowercase SHA-256")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise AutopilotCheckpointError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise AutopilotCheckpointError(f"{field} must be a non-negative integer")
    return value


def _utc(value: object, field: str) -> str:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise AutopilotCheckpointError(f"{field} must use the frozen UTC profile")
    return value


def _closed_mapping(
    value: object, expected: frozenset[str], label: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AutopilotCheckpointError(f"{label} must be an object")
    actual = set(value)
    if actual != set(expected):
        raise AutopilotCheckpointError(
            f"{label} fields are not closed: missing={sorted(set(expected) - actual)}, extra={sorted(actual - set(expected))}"
        )
    return value


def _checkpoint_id(
    identity: AutopilotIdentity,
    dag_hash: str,
    checkpoint_seq: int,
    previous_checkpoint_hash: str | None,
    completed_stages: tuple[StageEffect, ...],
) -> str:
    payload = {
        "checkpoint_seq": checkpoint_seq,
        "completed_effect_hashes": [effect.effect_hash for effect in completed_stages],
        "dag_hash": dag_hash,
        "identity": identity.to_dict(),
        "previous_checkpoint_hash": previous_checkpoint_hash,
        "schema": "autopilot-checkpoint-id/v1",
    }
    return (
        "autopilot-checkpoint-"
        + hash_payload("autopilot-checkpoint-id/v1", payload)[:48]
    )


@dataclass(frozen=True, slots=True)
class AutopilotIdentity:
    """The complete identity that a resumed Autopilot checkpoint must bind."""

    workspace_id: str
    job_id: str
    step_id: str
    attempt_id: str
    lease_epoch: int
    plugin_release_id: str
    run_snapshot_hash: str
    created_at: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.workspace_id, "workspace_id"),
            (self.job_id, "job_id"),
            (self.step_id, "step_id"),
            (self.attempt_id, "attempt_id"),
        ):
            validate_identifier(value, field)
        _positive_int(self.lease_epoch, "lease_epoch")
        validate_sha256(self.plugin_release_id, "plugin_release_id")
        validate_sha256(self.run_snapshot_hash, "run_snapshot_hash")
        _utc(self.created_at, "created_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "created_at": self.created_at,
            "job_id": self.job_id,
            "lease_epoch": self.lease_epoch,
            "plugin_release_id": self.plugin_release_id,
            "run_snapshot_hash": self.run_snapshot_hash,
            "step_id": self.step_id,
            "workspace_id": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class StageEffect:
    """Immutable child-job outcome used to make one stage replay-safe."""

    stage_id: str
    operation_key: str
    child_job_id: str
    child_step_id: str
    child_run_snapshot_asset_id: str
    child_run_snapshot_hash: str
    result_bundle_asset_id: str
    provenance_receipt_id: str
    child_result_contract: str
    effect_hash: str = ""

    def __post_init__(self) -> None:
        for value, field in (
            (self.stage_id, "stage_id"),
            (self.operation_key, "operation_key"),
            (self.child_job_id, "child_job_id"),
            (self.child_step_id, "child_step_id"),
            (self.child_run_snapshot_asset_id, "child_run_snapshot_asset_id"),
            (self.result_bundle_asset_id, "result_bundle_asset_id"),
            (self.provenance_receipt_id, "provenance_receipt_id"),
        ):
            validate_identifier(value, field)
        validate_sha256(self.child_run_snapshot_hash, "child_run_snapshot_hash")
        if self.child_result_contract not in _RESULT_CONTRACTS:
            raise AutopilotCheckpointError(
                "child_result_contract is outside the frozen Host enum"
            )
        expected = hash_payload("autopilot-stage-effect/v1", self._unsigned_dict())
        if self.effect_hash:
            if self.effect_hash != expected:
                raise AutopilotCheckpointError(
                    "stage effect_hash does not match its immutable outcome"
                )
        else:
            object.__setattr__(self, "effect_hash", expected)

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "child_job_id": self.child_job_id,
            "child_result_contract": self.child_result_contract,
            "child_run_snapshot_asset_id": self.child_run_snapshot_asset_id,
            "child_run_snapshot_hash": self.child_run_snapshot_hash,
            "child_step_id": self.child_step_id,
            "operation_key": self.operation_key,
            "provenance_receipt_id": self.provenance_receipt_id,
            "result_bundle_asset_id": self.result_bundle_asset_id,
            "stage_id": self.stage_id,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._unsigned_dict(), "effect_hash": self.effect_hash}

    @classmethod
    def from_dict(cls, value: object) -> StageEffect:
        record = _closed_mapping(value, _EFFECT_FIELDS, "stage effect")
        return cls(
            stage_id=record["stage_id"],  # type: ignore[arg-type]
            operation_key=record["operation_key"],  # type: ignore[arg-type]
            child_job_id=record["child_job_id"],  # type: ignore[arg-type]
            child_step_id=record["child_step_id"],  # type: ignore[arg-type]
            child_run_snapshot_asset_id=record["child_run_snapshot_asset_id"],  # type: ignore[arg-type]
            child_run_snapshot_hash=record["child_run_snapshot_hash"],  # type: ignore[arg-type]
            result_bundle_asset_id=record["result_bundle_asset_id"],  # type: ignore[arg-type]
            provenance_receipt_id=record["provenance_receipt_id"],  # type: ignore[arg-type]
            child_result_contract=record["child_result_contract"],  # type: ignore[arg-type]
            effect_hash=record["effect_hash"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CheckpointState:
    """Private state Asset referenced by a public ``checkpoint/v1`` record."""

    identity: AutopilotIdentity
    dag_hash: str
    checkpoint_id: str
    checkpoint_seq: int
    previous_checkpoint_hash: str | None
    completed_stages: tuple[StageEffect, ...]

    def __post_init__(self) -> None:
        validate_sha256(self.dag_hash, "dag_hash")
        validate_identifier(self.checkpoint_id, "checkpoint_id")
        _positive_int(self.checkpoint_seq, "checkpoint_seq")
        if self.previous_checkpoint_hash is None:
            if self.checkpoint_seq != 1:
                raise AutopilotCheckpointError(
                    "only the first checkpoint may omit previous_checkpoint_hash"
                )
        else:
            if self.checkpoint_seq == 1:
                raise AutopilotCheckpointError(
                    "the first checkpoint cannot carry previous_checkpoint_hash"
                )
            validate_sha256(self.previous_checkpoint_hash, "previous_checkpoint_hash")
        if (
            not isinstance(self.completed_stages, tuple)
            or len(self.completed_stages) != self.checkpoint_seq
        ):
            raise AutopilotCheckpointError(
                "checkpoint_seq must equal the immutable completed stage count"
            )
        if any(not isinstance(effect, StageEffect) for effect in self.completed_stages):
            raise AutopilotCheckpointError(
                "completed_stages must contain StageEffect values"
            )
        stage_ids = tuple(effect.stage_id for effect in self.completed_stages)
        if len(set(stage_ids)) != len(stage_ids):
            raise AutopilotCheckpointError("completed stages cannot repeat")
        expected = _checkpoint_id(
            self.identity,
            self.dag_hash,
            self.checkpoint_seq,
            self.previous_checkpoint_hash,
            self.completed_stages,
        )
        if self.checkpoint_id != expected:
            raise AutopilotCheckpointError(
                "checkpoint_id is not bound to its identity and effects"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.identity.attempt_id,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_seq": self.checkpoint_seq,
            "completed_stages": [effect.to_dict() for effect in self.completed_stages],
            "created_at": self.identity.created_at,
            "dag_hash": self.dag_hash,
            "job_id": self.identity.job_id,
            "lease_epoch": self.identity.lease_epoch,
            "plugin_release_id": self.identity.plugin_release_id,
            "previous_checkpoint_hash": self.previous_checkpoint_hash,
            "run_snapshot_hash": self.identity.run_snapshot_hash,
            "schema": "autopilot-runtime-state/v1",
            "step_id": self.identity.step_id,
            "workspace_id": self.identity.workspace_id,
        }

    @property
    def json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def build(
        cls,
        identity: AutopilotIdentity,
        *,
        dag_hash: str,
        previous_checkpoint_hash: str | None,
        completed_stages: tuple[StageEffect, ...],
    ) -> CheckpointState:
        seq = len(completed_stages)
        if seq < 1:
            raise AutopilotCheckpointError(
                "a durable checkpoint requires at least one completed stage"
            )
        checkpoint_id = _checkpoint_id(
            identity, dag_hash, seq, previous_checkpoint_hash, completed_stages
        )
        return cls(
            identity=identity,
            dag_hash=dag_hash,
            checkpoint_id=checkpoint_id,
            checkpoint_seq=seq,
            previous_checkpoint_hash=previous_checkpoint_hash,
            completed_stages=completed_stages,
        )

    @classmethod
    def from_dict(cls, value: object) -> CheckpointState:
        record = _closed_mapping(value, _STATE_FIELDS, "Autopilot runtime state")
        raw_effects = record["completed_stages"]
        if not isinstance(raw_effects, list):
            raise AutopilotCheckpointError("completed_stages must be an array")
        identity = AutopilotIdentity(
            workspace_id=record["workspace_id"],  # type: ignore[arg-type]
            job_id=record["job_id"],  # type: ignore[arg-type]
            step_id=record["step_id"],  # type: ignore[arg-type]
            attempt_id=record["attempt_id"],  # type: ignore[arg-type]
            lease_epoch=record["lease_epoch"],  # type: ignore[arg-type]
            plugin_release_id=record["plugin_release_id"],  # type: ignore[arg-type]
            run_snapshot_hash=record["run_snapshot_hash"],  # type: ignore[arg-type]
            created_at=record["created_at"],  # type: ignore[arg-type]
        )
        if record["schema"] != "autopilot-runtime-state/v1":
            raise AutopilotCheckpointError("runtime state schema is invalid")
        return cls(
            identity=identity,
            dag_hash=record["dag_hash"],  # type: ignore[arg-type]
            checkpoint_id=record["checkpoint_id"],  # type: ignore[arg-type]
            checkpoint_seq=record["checkpoint_seq"],  # type: ignore[arg-type]
            previous_checkpoint_hash=record["previous_checkpoint_hash"],  # type: ignore[arg-type]
            completed_stages=tuple(StageEffect.from_dict(item) for item in raw_effects),
        )


@dataclass(frozen=True, slots=True)
class CheckpointEnvelope:
    """A public checkpoint plus the private state Asset that it closes over."""

    state: CheckpointState
    state_asset_id: str
    checkpoint: dict[str, object]
    checkpoint_asset_id: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.state_asset_id, "state_asset_id")
        if self.checkpoint_asset_id is not None:
            validate_identifier(self.checkpoint_asset_id, "checkpoint_asset_id")
        _validate_public_checkpoint(
            self.checkpoint, state=self.state, state_asset_id=self.state_asset_id
        )
        object.__setattr__(self, "checkpoint", copy.deepcopy(dict(self.checkpoint)))

    @property
    def checkpoint_hash(self) -> str:
        value = self.checkpoint["checkpoint_hash"]
        assert isinstance(value, str)
        return value

    @property
    def checkpoint_id(self) -> str:
        value = self.checkpoint["checkpoint_id"]
        assert isinstance(value, str)
        return value

    @property
    def json_bytes(self) -> bytes:
        return canonical_json_bytes(dict(self.checkpoint))

    def with_checkpoint_asset_id(self, checkpoint_asset_id: str) -> CheckpointEnvelope:
        return CheckpointEnvelope(
            state=self.state,
            state_asset_id=self.state_asset_id,
            checkpoint=self.checkpoint,
            checkpoint_asset_id=checkpoint_asset_id,
        )


def _validate_public_checkpoint(
    value: Mapping[str, object], *, state: CheckpointState, state_asset_id: str
) -> None:
    record = _closed_mapping(value, _CHECKPOINT_FIELDS, "checkpoint/v1")
    if record["schema"] != "checkpoint/v1":
        raise AutopilotCheckpointError("checkpoint schema is invalid")
    validate_identifier(record["checkpoint_id"], "checkpoint_id")
    _positive_int(record["checkpoint_seq"], "checkpoint_seq")
    validate_identifier(record["job_id"], "job_id")
    validate_identifier(record["step_id"], "step_id")
    validate_identifier(record["source_attempt_id"], "source_attempt_id")
    _positive_int(record["lease_epoch"], "lease_epoch")
    validate_sha256(record["run_snapshot_hash"], "run_snapshot_hash")
    if record["replay_policy"] != "checkpoint_resume":
        raise AutopilotCheckpointError(
            "Autopilot checkpoint replay_policy must be checkpoint_resume"
        )
    _nonnegative_int(record["completed_units"], "completed_units")
    _nonnegative_int(record["total_units"], "total_units")
    validate_sha256(record["unit_set_hash"], "unit_set_hash")
    validate_identifier(record["state_asset_id"], "state_asset_id")
    _utc(record["created_at"], "created_at")
    validate_sha256(record["checkpoint_hash"], "checkpoint_hash")

    expected_hash = hash_payload(
        "checkpoint/v1",
        {
            key: copy.deepcopy(item)
            for key, item in record.items()
            if key != "checkpoint_hash"
        },
    )
    if record["checkpoint_hash"] != expected_hash:
        raise AutopilotCheckpointError(
            "checkpoint_hash does not match the public checkpoint"
        )
    expected = {
        "checkpoint_id": state.checkpoint_id,
        "checkpoint_seq": state.checkpoint_seq,
        "job_id": state.identity.job_id,
        "step_id": state.identity.step_id,
        "source_attempt_id": state.identity.attempt_id,
        "lease_epoch": state.identity.lease_epoch,
        "run_snapshot_hash": state.identity.run_snapshot_hash,
        "completed_units": len(state.completed_stages),
        "unit_set_hash": state.dag_hash,
        "state_asset_id": state_asset_id,
        "created_at": state.identity.created_at,
    }
    for field, expected_value in expected.items():
        if record[field] != expected_value:
            raise AutopilotCheckpointError(
                f"checkpoint {field} is not bound to its runtime state"
            )
    if record["total_units"] < record["completed_units"]:
        raise AutopilotCheckpointError(
            "checkpoint total_units is below completed_units"
        )


def build_checkpoint_envelope(
    state: CheckpointState, *, state_asset_id: str, total_units: int
) -> CheckpointEnvelope:
    """Build the public P1 checkpoint which points at an immutable state Asset."""
    validate_identifier(state_asset_id, "state_asset_id")
    total = _nonnegative_int(total_units, "total_units")
    if total < len(state.completed_stages):
        raise AutopilotCheckpointError("total_units cannot be below completed stages")
    payload: dict[str, object] = {
        "schema": "checkpoint/v1",
        "checkpoint_id": state.checkpoint_id,
        "checkpoint_seq": state.checkpoint_seq,
        "job_id": state.identity.job_id,
        "step_id": state.identity.step_id,
        "source_attempt_id": state.identity.attempt_id,
        "lease_epoch": state.identity.lease_epoch,
        "run_snapshot_hash": state.identity.run_snapshot_hash,
        "replay_policy": "checkpoint_resume",
        "completed_units": len(state.completed_stages),
        "total_units": total,
        "unit_set_hash": state.dag_hash,
        "state_asset_id": state_asset_id,
        "created_at": state.identity.created_at,
        "checkpoint_hash": "",
    }
    payload["checkpoint_hash"] = hash_payload(
        "checkpoint/v1",
        {key: value for key, value in payload.items() if key != "checkpoint_hash"},
    )
    return CheckpointEnvelope(
        state=state, state_asset_id=state_asset_id, checkpoint=payload
    )


def _validate_resume_identity(
    source: AutopilotIdentity,
    current: AutopilotIdentity,
    *,
    resume_of_attempt_id: str,
) -> None:
    """Fence a checkpoint to the one direct Core-authoritative resume edge.

    Generation/package/capability and the current RunSnapshot are already
    bound by the shared worker before this pure parser runs.  The checkpoint
    itself closes over the stable workspace/job/step/release/snapshot/DAG
    identity and must name the exact prior Attempt supplied by ``job.resume``.
    """

    validate_identifier(resume_of_attempt_id, "resume_of_attempt_id")
    if current.attempt_id == resume_of_attempt_id:
        raise AutopilotCheckpointError(
            "job.resume must name a distinct direct source Attempt"
        )
    if source.attempt_id != resume_of_attempt_id:
        raise AutopilotCheckpointError(
            "checkpoint source Attempt is not job.resume direct lineage"
        )
    source_stable = (
        source.workspace_id,
        source.job_id,
        source.step_id,
        source.plugin_release_id,
        source.run_snapshot_hash,
    )
    current_stable = (
        current.workspace_id,
        current.job_id,
        current.step_id,
        current.plugin_release_id,
        current.run_snapshot_hash,
    )
    if source_stable != current_stable:
        raise AutopilotCheckpointError(
            "checkpoint direct resume identity drifted"
        )


def recover_checkpoint_envelope(
    checkpoint: dict[str, object],
    runtime_state: Mapping[str, object],
    *,
    state_asset_id: str,
    checkpoint_asset_id: str | None,
    identity: AutopilotIdentity,
    dag_hash: str,
    total_units: int,
    resume_of_attempt_id: str,
) -> CheckpointEnvelope:
    """Fail closed unless a current Host-selected checkpoint matches this run.

    The parsed Asset is bound to exactly one direct Core ``job.resume`` edge;
    this pure parser never accepts a same-Attempt or discovered checkpoint.
    """
    validate_identifier(state_asset_id, "state_asset_id")
    if checkpoint_asset_id is not None:
        validate_identifier(checkpoint_asset_id, "checkpoint_asset_id")
    expected_total = _nonnegative_int(total_units, "total_units")
    state = CheckpointState.from_dict(runtime_state)
    _validate_resume_identity(
        state.identity,
        identity,
        resume_of_attempt_id=resume_of_attempt_id,
    )
    if state.dag_hash != dag_hash:
        raise AutopilotCheckpointError("checkpoint belongs to a different durable DAG")
    if len(state.completed_stages) > expected_total:
        raise AutopilotCheckpointError(
            "checkpoint completed stage count exceeds the current DAG"
        )
    _validate_public_checkpoint(checkpoint, state=state, state_asset_id=state_asset_id)
    if checkpoint["total_units"] != expected_total:
        raise AutopilotCheckpointError(
            "checkpoint total_units does not match the current DAG"
        )
    return CheckpointEnvelope(
        state=state,
        state_asset_id=state_asset_id,
        checkpoint=checkpoint,
        checkpoint_asset_id=checkpoint_asset_id,
    )


__all__ = [
    "AutopilotCheckpointError",
    "AutopilotIdentity",
    "CheckpointEnvelope",
    "CheckpointState",
    "StageEffect",
    "build_checkpoint_envelope",
    "canonical_json_bytes",
    "hash_payload",
    "parse_canonical_json_bytes",
    "recover_checkpoint_envelope",
    "sha256_hex",
    "validate_identifier",
    "validate_sha256",
]
