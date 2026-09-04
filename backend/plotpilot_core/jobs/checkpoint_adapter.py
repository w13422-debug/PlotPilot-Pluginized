"""Asset-backed chapter stream checkpoints over the P1 execution authority.

The adapter deliberately owns neither a table nor a state machine.  A stream
high-water is represented by three immutable Assets (raw bytes, the public
``stream-prefix/v1`` contract, and a small internal runtime-state document)
and is committed through :class:`SQLiteCheckpointStore` as an ordinary
``checkpoint/v1``.  Recovery revalidates that complete closure against the
currently authoritative Attempt before returning any bytes.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from backend.plotpilot_core.api.v1.jobs.rpc import (
    AttemptStartBinding,
    JobCheckpointBinding,
    JobSnapshotExtensions,
    JobStreamHighWaterBinding,
)
from backend.plotpilot_core.domain.entities import utc_now
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    ErrorCode,
    canonical_bytes,
    parse_json_bytes,
    sha256_hex,
    verify_checkpoint,
    verify_stream_prefix,
)
from backend.plotpilot_plugin_sdk.verifier import hash_without_field

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_STATE_SCHEMA = "chapter-runtime-state/v1"
_RUNTIME_STATE_FIELDS = frozenset(
    {
        "schema",
        "workspace_id",
        "job_id",
        "step_id",
        "attempt_id",
        "lease_epoch",
        "worker_run_id",
        "run_snapshot_hash",
        "expected_result_contract",
        "checkpoint_id",
        "checkpoint_seq",
        "replay_policy",
        "stream_id",
        "stream_prefix_asset_id",
        "stream_prefix_hash",
        "provider_outcome",
        "provider_replay_policy",
    }
)
_REPLAY_POLICIES = frozenset(
    {"idempotent_auto", "checkpoint_resume", "manual_if_unknown", "never_replay"}
)
_PROVIDER_REPLAY_POLICIES = frozenset(
    {"idempotent_auto", "manual_if_unknown", "never_replay"}
)
_NONTERMINAL_ATTEMPT_STATES = frozenset({"running", "cancelling", "suspended"})


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ContractValidationError(f"{label} is not a v1 ID")
    return value


def _require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ContractValidationError(f"{label} is not lowercase SHA-256")
    return value


def _require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(f"{label} must be a non-negative integer")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractValidationError(f"{label} must be a positive integer")
    return value


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\n".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:48]}"


def _validate_provider_recovery(
    *, provider_outcome: str, provider_replay_policy: str
) -> None:
    _require_id(provider_outcome, "provider_outcome")
    if provider_replay_policy not in _PROVIDER_REPLAY_POLICIES:
        raise ContractValidationError(
            "provider_replay_policy is not in the frozen enum"
        )
    if provider_outcome == "unknown" and provider_replay_policy not in {
        "manual_if_unknown",
        "never_replay",
    }:
        raise ContractError(
            ErrorCode.UNCERTAIN_EXTERNAL_EFFECT,
            "an unknown provider outcome can never be replayed automatically",
        )


def _recovery_decision(runtime_state: Mapping[str, Any]) -> tuple[str, bool]:
    outcome = str(runtime_state["provider_outcome"])
    provider_policy = str(runtime_state["provider_replay_policy"])
    if outcome == "unknown":
        return provider_policy, False
    replay_policy = str(runtime_state["replay_policy"])
    if provider_policy != "idempotent_auto":
        return provider_policy, False
    if replay_policy in {"manual_if_unknown", "never_replay"}:
        return replay_policy, False
    return replay_policy, True


@dataclass(frozen=True, slots=True)
class StreamCheckpointRecovery:
    """A fully verified stream high-water and all of its immutable anchors."""

    replayed: bool
    workspace_id: str
    job_id: str
    step_id: str
    attempt_id: str
    lease_epoch: int
    worker_run_id: str
    run_snapshot_hash: str
    expected_result_contract: str
    checkpoint_id: str
    checkpoint: Mapping[str, Any]
    checkpoint_asset_id: str
    runtime_state: Mapping[str, Any]
    runtime_state_asset_id: str
    stream_prefix: Mapping[str, Any]
    stream_prefix_asset_id: str
    prefix: bytes
    prefix_asset_id: str
    prefix_hash: str
    recovery_action: str
    automatic_replay_allowed: bool


class AuthorityAttemptStartAdapter:
    """Return the exact Attempt row allocated by ``ExecutionAuthority``.

    ``CoreAuthorityRepository.read_connection`` is a re-entrant gate.  Holding
    it across ``start_attempt`` and the following SELECT prevents another
    process-local writer from replacing the Step's active Attempt between the
    authority commit and this binding projection.
    """

    def __init__(self, authority: ExecutionAuthority) -> None:
        repository = getattr(authority, "repository", None)
        if repository is None or not callable(
            getattr(repository, "read_connection", None)
        ):
            raise TypeError("Attempt start requires the accepted authority repository")
        if not callable(getattr(authority, "start_attempt", None)):
            raise TypeError("Attempt start requires ExecutionAuthority.start_attempt")
        self._authority = authority
        self._repository = repository

    def start_attempt(self, **command: Any) -> AttemptStartBinding:
        attempt_id = _require_id(command.get("attempt_id"), "attempt_id")
        with self._repository.read_connection() as connection:
            self._authority.start_attempt(**command)
            row = connection.execute(
                "SELECT job_id,step_id,attempt_id,worker_run_id,plugin_id,release_id,"
                "package_hash,capability_id,generation_id,lease_epoch,"
                "preallocated_receipt_id,expected_result_contract "
                "FROM execution_attempt WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "Attempt authority did not materialize its allocated row",
                )
            values = dict(row)
        for name in (
            "job_id",
            "step_id",
            "attempt_id",
            "worker_run_id",
            "plugin_id",
            "release_id",
            "package_hash",
            "capability_id",
            "generation_id",
            "preallocated_receipt_id",
            "expected_result_contract",
        ):
            if not isinstance(values[name], str) or not values[name]:
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    f"allocated Attempt {name} is absent",
                )
        return AttemptStartBinding(
            job_id=str(values["job_id"]),
            step_id=str(values["step_id"]),
            attempt_id=str(values["attempt_id"]),
            worker_run_id=str(values["worker_run_id"]),
            plugin_id=str(values["plugin_id"]),
            release_id=str(values["release_id"]),
            package_hash=str(values["package_hash"]),
            capability_id=str(values["capability_id"]),
            generation_id=str(values["generation_id"]),
            lease_epoch=int(values["lease_epoch"]),
            preallocated_receipt_id=str(values["preallocated_receipt_id"]),
            expected_result_contract=str(values["expected_result_contract"]),
        )


class DurableCheckpointAdapter:
    """Translate stream ACK high-waters into the existing checkpoint authority."""

    def __init__(self, authority: ExecutionAuthority) -> None:
        repository = getattr(authority, "repository", None)
        assets = getattr(authority, "assets", None)
        checkpoints = getattr(authority, "checkpoint_store", None)
        control = getattr(authority, "control_port", None)
        if repository is None or not callable(
            getattr(repository, "read_connection", None)
        ):
            raise TypeError(
                "checkpoint adapter requires the accepted authority repository"
            )
        if assets is None or not callable(getattr(assets, "put", None)):
            raise TypeError("checkpoint adapter requires AssetStore")
        if checkpoints is None or not callable(
            getattr(checkpoints, "commit_checkpoint", None)
        ):
            raise TypeError("checkpoint adapter requires SQLiteCheckpointStore")
        if control is None or not callable(getattr(control, "apply", None)):
            raise TypeError("checkpoint adapter requires SQLiteExecutionControlPort")
        self.authority = authority
        self.repository = repository
        self.assets = assets
        self.checkpoint_store = checkpoints
        self.control_port = control

    def _json_asset(
        self, asset_id: str, *, label: str
    ) -> tuple[dict[str, Any], bytes, str]:
        _require_id(asset_id, f"{label}_asset_id")
        try:
            raw = self.assets.read(asset_id)
            metadata = self.assets.require(asset_id)
            value = parse_json_bytes(raw)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR, f"{label} Asset is missing or invalid"
            ) from exc
        if not isinstance(value, dict) or raw != canonical_bytes(value):
            raise ContractError(
                ErrorCode.ASSET_ERROR, f"{label} Asset is not canonical JSON"
            )
        if metadata.sha256 != sha256_hex(raw):
            raise ContractError(ErrorCode.ASSET_ERROR, f"{label} Asset hash drifted")
        return value, raw, str(metadata.sha256)

    def _load_stream_prefix(
        self, stream_prefix_asset_id: str
    ) -> tuple[dict[str, Any], bytes, str]:
        value, _raw, contract_hash = self._json_asset(
            stream_prefix_asset_id, label="stream prefix contract"
        )
        try:
            verify_stream_prefix(value)
            prefix_asset_id = str(value["prefix_asset_id"])
            prefix = self.assets.read(prefix_asset_id)
            metadata = self.assets.require(prefix_asset_id)
            verify_stream_prefix(value, content=prefix)
            prefix.decode("utf-8", errors="strict")
        except ContractError:
            raise
        except UnicodeDecodeError as exc:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "stream prefix bytes are not valid UTF-8",
            ) from exc
        except Exception as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR, "stream prefix Asset closure is missing"
            ) from exc
        if metadata.sha256 != value["prefix_hash"]:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "stream prefix Asset identity does not match prefix_hash",
            )
        return value, prefix, contract_hash

    def _current_attempt(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        job_id: str,
        step_id: str,
        attempt_id: str,
        lease_epoch: int,
        worker_run_id: str,
        run_snapshot_hash: str,
        expected_result_contract: str,
    ) -> sqlite3.Row:
        for value, label in (
            (workspace_id, "workspace_id"),
            (job_id, "job_id"),
            (step_id, "step_id"),
            (attempt_id, "attempt_id"),
            (worker_run_id, "worker_run_id"),
            (expected_result_contract, "expected_result_contract"),
        ):
            _require_id(value, label)
        _require_positive_int(lease_epoch, "lease_epoch")
        _require_hash(run_snapshot_hash, "run_snapshot_hash")
        job, step, attempt = self.checkpoint_store._fence_attempt(
            connection,
            job_id=job_id,
            step_id=step_id,
            attempt_id=attempt_id,
            lease_epoch=lease_epoch,
            worker_run_id=worker_run_id,
            now=utc_now(),
            require_active=False,
            expected_snapshot_hash=run_snapshot_hash,
        )
        if job["workspace_id"] != workspace_id:
            raise ContractError(
                ErrorCode.CHECKPOINT_INVALID,
                "Attempt checkpoint scope belongs to another Workspace",
            )
        if (
            attempt["expected_result_contract"] != expected_result_contract
            or step["expected_result_contract"] != expected_result_contract
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Attempt checkpoint result contract drifted",
            )
        if attempt["state"] not in _NONTERMINAL_ATTEMPT_STATES:
            raise ContractError(
                ErrorCode.STALE_LEASE, "Attempt is no longer recoverable"
            )
        return attempt

    @staticmethod
    def _checkpoint_asset_id(
        connection: sqlite3.Connection, *, job_id: str, operation_key: str
    ) -> str:
        operation = connection.execute(
            "SELECT request_json FROM execution_checkpoint_operation "
            "WHERE job_id=? AND operation_key=?",
            (job_id, operation_key),
        ).fetchone()
        if operation is None:
            raise ContractError(
                ErrorCode.ASSET_ERROR, "checkpoint operation closure is missing"
            )
        try:
            request = parse_json_bytes(str(operation["request_json"]).encode("utf-8"))
            asset_id = request["checkpoint_asset_id"]
        except Exception as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR, "checkpoint operation Asset binding is invalid"
            ) from exc
        return _require_id(asset_id, "checkpoint_asset_id")

    def _runtime_state(
        self, state_asset_id: str, *, required: bool
    ) -> tuple[dict[str, Any], str] | None:
        try:
            metadata = self.assets.describe(state_asset_id)
        except Exception as exc:
            if required:
                raise ContractError(
                    ErrorCode.ASSET_ERROR, "runtime-state Asset metadata is missing"
                ) from exc
            return None
        owned = metadata.logical_role == "chapter_runtime_state"
        try:
            value, _raw, state_hash = self._json_asset(
                state_asset_id, label="chapter runtime state"
            )
        except ContractError:
            if owned or required:
                raise
            return None
        if value.get("schema") != _RUNTIME_STATE_SCHEMA:
            if required:
                raise ContractError(
                    ErrorCode.CHECKPOINT_INVALID,
                    "checkpoint does not contain chapter runtime state",
                )
            return None
        if set(value) != _RUNTIME_STATE_FIELDS:
            raise ContractError(
                ErrorCode.CHECKPOINT_INVALID,
                "chapter runtime-state fields are not closed",
            )
        return value, state_hash

    def _decode_stream_record(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        expected_workspace_id: str,
        required: bool = True,
        replayed: bool = True,
    ) -> StreamCheckpointRecovery | None:
        record = self.checkpoint_store._decode_row(
            connection, row, expected_workspace_id=expected_workspace_id
        )
        checkpoint = dict(record.checkpoint)
        state_asset_id = checkpoint.get("state_asset_id")
        if not isinstance(state_asset_id, str):
            if required:
                raise ContractError(
                    ErrorCode.CHECKPOINT_INVALID,
                    "stream checkpoint has no runtime-state Asset",
                )
            return None
        loaded = self._runtime_state(state_asset_id, required=required)
        if loaded is None:
            return None
        runtime_state, _state_hash = loaded
        try:
            for value, label in (
                (runtime_state["workspace_id"], "runtime_state.workspace_id"),
                (runtime_state["job_id"], "runtime_state.job_id"),
                (runtime_state["step_id"], "runtime_state.step_id"),
                (runtime_state["attempt_id"], "runtime_state.attempt_id"),
                (runtime_state["worker_run_id"], "runtime_state.worker_run_id"),
                (
                    runtime_state["expected_result_contract"],
                    "runtime_state.expected_result_contract",
                ),
                (runtime_state["stream_id"], "runtime_state.stream_id"),
            ):
                _require_id(value, label)
            _require_positive_int(
                runtime_state["lease_epoch"], "runtime_state.lease_epoch"
            )
            _require_positive_int(
                runtime_state["checkpoint_seq"], "runtime_state.checkpoint_seq"
            )
            _require_hash(
                runtime_state["run_snapshot_hash"], "runtime_state.run_snapshot_hash"
            )
            _require_hash(
                runtime_state["stream_prefix_hash"], "runtime_state.stream_prefix_hash"
            )
            if runtime_state["replay_policy"] not in _REPLAY_POLICIES:
                raise ContractValidationError(
                    "runtime replay_policy is not in the frozen enum"
                )
            _validate_provider_recovery(
                provider_outcome=runtime_state["provider_outcome"],
                provider_replay_policy=runtime_state["provider_replay_policy"],
            )
        except KeyError as exc:
            raise ContractError(
                ErrorCode.CHECKPOINT_INVALID,
                "chapter runtime-state binding is incomplete",
            ) from exc

        expected_runtime = (
            expected_workspace_id,
            checkpoint["job_id"],
            checkpoint["step_id"],
            checkpoint["source_attempt_id"],
            int(checkpoint["lease_epoch"]),
            checkpoint["run_snapshot_hash"],
            checkpoint["checkpoint_id"],
            int(checkpoint["checkpoint_seq"]),
            checkpoint["replay_policy"],
        )
        actual_runtime = (
            runtime_state["workspace_id"],
            runtime_state["job_id"],
            runtime_state["step_id"],
            runtime_state["attempt_id"],
            int(runtime_state["lease_epoch"]),
            runtime_state["run_snapshot_hash"],
            runtime_state["checkpoint_id"],
            int(runtime_state["checkpoint_seq"]),
            runtime_state["replay_policy"],
        )
        if actual_runtime != expected_runtime:
            raise ContractError(
                ErrorCode.CHECKPOINT_INVALID,
                "runtime-state identity drifted from its checkpoint",
            )

        source_attempt = connection.execute(
            "SELECT worker_run_id,expected_result_contract,lease_epoch "
            "FROM execution_attempt WHERE attempt_id=? AND job_id=? AND step_id=?",
            (
                checkpoint["source_attempt_id"],
                checkpoint["job_id"],
                checkpoint["step_id"],
            ),
        ).fetchone()
        if source_attempt is None:
            raise ContractError(
                ErrorCode.ASSET_ERROR, "checkpoint source Attempt is missing"
            )
        if (
            runtime_state["worker_run_id"] != source_attempt["worker_run_id"]
            or runtime_state["expected_result_contract"]
            != source_attempt["expected_result_contract"]
            or int(runtime_state["lease_epoch"]) != int(source_attempt["lease_epoch"])
        ):
            raise ContractError(
                ErrorCode.CHECKPOINT_INVALID,
                "runtime-state Attempt binding drifted",
            )

        stream_prefix_asset_id = _require_id(
            runtime_state["stream_prefix_asset_id"], "stream_prefix_asset_id"
        )
        stream_prefix, prefix, contract_hash = self._load_stream_prefix(
            stream_prefix_asset_id
        )
        if contract_hash != runtime_state["stream_prefix_hash"]:
            raise ContractError(
                ErrorCode.ASSET_ERROR, "runtime-state stream-prefix hash drifted"
            )
        expected_prefix = (
            runtime_state["stream_id"],
            checkpoint["job_id"],
            checkpoint["step_id"],
            checkpoint["source_attempt_id"],
            int(checkpoint["lease_epoch"]),
            expected_workspace_id,
        )
        actual_prefix = (
            stream_prefix["stream_id"],
            stream_prefix["job_id"],
            stream_prefix["step_id"],
            stream_prefix["attempt_id"],
            int(stream_prefix["lease_epoch"]),
            stream_prefix["target"]["workspace_id"],
        )
        if actual_prefix != expected_prefix:
            raise ContractError(
                ErrorCode.CHECKPOINT_INVALID,
                "stream-prefix identity drifted from its checkpoint",
            )
        checkpoint_asset_id = self._checkpoint_asset_id(
            connection,
            job_id=str(checkpoint["job_id"]),
            operation_key=record.operation_key,
        )
        recovery_action, automatic = _recovery_decision(runtime_state)
        return StreamCheckpointRecovery(
            replayed=replayed,
            workspace_id=expected_workspace_id,
            job_id=str(checkpoint["job_id"]),
            step_id=str(checkpoint["step_id"]),
            attempt_id=str(checkpoint["source_attempt_id"]),
            lease_epoch=int(checkpoint["lease_epoch"]),
            worker_run_id=str(runtime_state["worker_run_id"]),
            run_snapshot_hash=str(checkpoint["run_snapshot_hash"]),
            expected_result_contract=str(runtime_state["expected_result_contract"]),
            checkpoint_id=str(checkpoint["checkpoint_id"]),
            checkpoint=checkpoint,
            checkpoint_asset_id=checkpoint_asset_id,
            runtime_state=runtime_state,
            runtime_state_asset_id=state_asset_id,
            stream_prefix=stream_prefix,
            stream_prefix_asset_id=stream_prefix_asset_id,
            prefix=prefix,
            prefix_asset_id=str(stream_prefix["prefix_asset_id"]),
            prefix_hash=str(stream_prefix["prefix_hash"]),
            recovery_action=recovery_action,
            automatic_replay_allowed=automatic,
        )

    def _bind_current_attempt(
        self,
        connection: sqlite3.Connection,
        recovered: StreamCheckpointRecovery,
        current: sqlite3.Row,
    ) -> StreamCheckpointRecovery:
        current_attempt_id = str(current["attempt_id"])
        if current_attempt_id == recovered.attempt_id:
            if int(current["lease_epoch"]) != recovered.lease_epoch:
                raise ContractError(
                    ErrorCode.STALE_LEASE, "checkpoint lease epoch is stale"
                )
        else:
            anchor_id = current["resume_checkpoint_id"]
            if (
                current["resume_of_attempt_id"]
                != recovered.checkpoint["source_attempt_id"]
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "fresh Attempt is not bound to the recovered source Attempt",
                )
            anchor = connection.execute(
                "SELECT job_id,step_id,source_attempt_id,checkpoint_seq "
                "FROM execution_checkpoint WHERE checkpoint_id=?",
                (anchor_id,),
            ).fetchone()
            if (
                anchor is None
                or anchor["job_id"] != recovered.job_id
                or anchor["step_id"] != recovered.step_id
                or anchor["source_attempt_id"]
                != recovered.checkpoint["source_attempt_id"]
                or int(anchor["checkpoint_seq"])
                < int(recovered.checkpoint["checkpoint_seq"])
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "fresh Attempt does not carry an exact resume checkpoint edge",
                )
        return replace(
            recovered,
            attempt_id=current_attempt_id,
            lease_epoch=int(current["lease_epoch"]),
            worker_run_id=str(current["worker_run_id"]),
            expected_result_contract=str(current["expected_result_contract"]),
        )

    @staticmethod
    def _same_commit_request(
        recovered: StreamCheckpointRecovery,
        *,
        stream_prefix: Mapping[str, Any],
        prefix: bytes,
        completed_units: int,
        total_units: int | None,
        replay_policy: str,
        provider_outcome: str,
        provider_replay_policy: str,
    ) -> bool:
        state = recovered.runtime_state
        return (
            dict(recovered.stream_prefix) == dict(stream_prefix)
            and recovered.prefix == prefix
            and recovered.checkpoint["completed_units"] == completed_units
            and recovered.checkpoint["total_units"] == total_units
            and recovered.checkpoint["replay_policy"] == replay_policy
            and state["provider_outcome"] == provider_outcome
            and state["provider_replay_policy"] == provider_replay_policy
        )

    def persist_stream_prefix(
        self,
        *,
        workspace_id: str,
        job_id: str,
        step_id: str,
        attempt_id: str,
        lease_epoch: int,
        worker_run_id: str,
        run_snapshot_hash: str,
        expected_result_contract: str,
        stream_id: str,
        output_role: str,
        target: Mapping[str, Any],
        prefix_seq: int,
        prefix: bytes,
        operation_key: str,
        completed_units: int,
        total_units: int | None,
        replay_policy: str = "checkpoint_resume",
        provider_outcome: str = "confirmed",
        provider_replay_policy: str = "idempotent_auto",
        local_seq: int | None = None,
    ) -> StreamCheckpointRecovery:
        _validate_provider_recovery(
            provider_outcome=provider_outcome,
            provider_replay_policy=provider_replay_policy,
        )
        if not isinstance(prefix, bytes):
            raise ContractValidationError("stream prefix must be raw bytes")
        try:
            prefix.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "stream prefix bytes are not valid UTF-8",
            ) from exc
        _require_positive_int(prefix_seq, "prefix_seq")
        _require_id(stream_id, "stream_id")
        _require_id(output_role, "output_role")
        raw_asset = self.assets.put(
            prefix,
            mime="text/plain; charset=utf-8",
            logical_role="chapter_stream_prefix",
            provenance=f"core:chapter-stream:{job_id}:{step_id}:{attempt_id}",
        )
        stream_prefix = {
            "schema": "stream-prefix/v1",
            "stream_id": stream_id,
            "job_id": job_id,
            "step_id": step_id,
            "output_role": output_role,
            "target": dict(target),
            "attempt_id": attempt_id,
            "lease_epoch": lease_epoch,
            "prefix_seq": prefix_seq,
            "prefix_asset_id": raw_asset.asset_id,
            "prefix_hash": raw_asset.sha256,
            "byte_length": len(prefix),
            "encoding": "utf-8",
        }
        verify_stream_prefix(stream_prefix, content=prefix)
        contract_asset = self.assets.put(
            canonical_bytes(stream_prefix),
            mime="application/json",
            logical_role="stream_prefix_contract",
            provenance=f"core:chapter-stream-contract:{job_id}:{step_id}:{attempt_id}",
        )
        return self.commit_stream_prefix(
            stream_prefix_asset_id=contract_asset.asset_id,
            operation_key=operation_key,
            worker_run_id=worker_run_id,
            workspace_id=workspace_id,
            run_snapshot_hash=run_snapshot_hash,
            expected_result_contract=expected_result_contract,
            completed_units=completed_units,
            total_units=total_units,
            replay_policy=replay_policy,
            provider_outcome=provider_outcome,
            provider_replay_policy=provider_replay_policy,
            local_seq=local_seq,
        )

    def commit_stream_prefix(
        self,
        *,
        stream_prefix_asset_id: str,
        operation_key: str,
        worker_run_id: str,
        workspace_id: str,
        run_snapshot_hash: str,
        expected_result_contract: str,
        completed_units: int,
        total_units: int | None,
        replay_policy: str = "checkpoint_resume",
        provider_outcome: str = "confirmed",
        provider_replay_policy: str = "idempotent_auto",
        local_seq: int | None = None,
    ) -> StreamCheckpointRecovery:
        _require_id(operation_key, "operation_key")
        _require_nonnegative_int(completed_units, "completed_units")
        if total_units is not None:
            _require_nonnegative_int(total_units, "total_units")
            if completed_units > total_units:
                raise ContractError(
                    ErrorCode.CHECKPOINT_INVALID,
                    "completed_units cannot exceed total_units",
                )
        if replay_policy not in _REPLAY_POLICIES:
            raise ContractValidationError("replay_policy is not in the frozen enum")
        _validate_provider_recovery(
            provider_outcome=provider_outcome,
            provider_replay_policy=provider_replay_policy,
        )
        stream_prefix, prefix, stream_prefix_hash = self._load_stream_prefix(
            stream_prefix_asset_id
        )
        if stream_prefix["target"]["workspace_id"] != workspace_id:
            raise ContractError(
                ErrorCode.CHECKPOINT_INVALID,
                "stream-prefix target belongs to another Workspace",
            )

        job_id = str(stream_prefix["job_id"])
        step_id = str(stream_prefix["step_id"])
        attempt_id = str(stream_prefix["attempt_id"])
        lease_epoch = int(stream_prefix["lease_epoch"])
        with self.repository.read_connection() as connection:
            current = self._current_attempt(
                connection,
                workspace_id=workspace_id,
                job_id=job_id,
                step_id=step_id,
                attempt_id=attempt_id,
                lease_epoch=lease_epoch,
                worker_run_id=worker_run_id,
                run_snapshot_hash=run_snapshot_hash,
                expected_result_contract=expected_result_contract,
            )
            existing_operation = connection.execute(
                "SELECT checkpoint_id FROM execution_checkpoint_operation "
                "WHERE job_id=? AND operation_key=?",
                (job_id, operation_key),
            ).fetchone()
            if existing_operation is not None:
                existing_row = connection.execute(
                    "SELECT * FROM execution_checkpoint WHERE job_id=? AND checkpoint_id=?",
                    (job_id, existing_operation["checkpoint_id"]),
                ).fetchone()
                if existing_row is None:
                    raise ContractError(
                        ErrorCode.ASSET_ERROR,
                        "checkpoint operation lost its durable checkpoint",
                    )
                recovered = self._decode_stream_record(
                    connection,
                    existing_row,
                    expected_workspace_id=workspace_id,
                    replayed=True,
                )
                if recovered is None or not self._same_commit_request(
                    recovered,
                    stream_prefix=stream_prefix,
                    prefix=prefix,
                    completed_units=completed_units,
                    total_units=total_units,
                    replay_policy=replay_policy,
                    provider_outcome=provider_outcome,
                    provider_replay_policy=provider_replay_policy,
                ):
                    raise ContractError(
                        ErrorCode.DUPLICATE_REQUEST,
                        "stream checkpoint operation key was reused with different content",
                    )
                committed = self.checkpoint_store.commit_checkpoint(
                    recovered.checkpoint,
                    operation_key=operation_key,
                    checkpoint_asset_id=recovered.checkpoint_asset_id,
                    local_seq=local_seq,
                    worker_run_id=worker_run_id,
                )
                if not committed.replayed:
                    raise ContractError(
                        ErrorCode.ASSET_ERROR,
                        "existing checkpoint operation was not replayed",
                    )
                return self._bind_current_attempt(connection, recovered, current)

            rows = connection.execute(
                "SELECT * FROM execution_checkpoint WHERE job_id=? AND step_id=? "
                "ORDER BY checkpoint_seq DESC,job_event_seq DESC,checkpoint_id DESC",
                (job_id, step_id),
            ).fetchall()
            latest_row = rows[0] if rows else None
            previous_checkpoint = (
                None
                if latest_row is None
                else self.checkpoint_store._decode_row(
                    connection, latest_row, expected_workspace_id=workspace_id
                ).checkpoint
            )
            previous_stream: StreamCheckpointRecovery | None = None
            for row in rows:
                candidate = self._decode_stream_record(
                    connection,
                    row,
                    expected_workspace_id=workspace_id,
                    required=False,
                )
                if (
                    candidate is not None
                    and candidate.stream_prefix["stream_id"]
                    == stream_prefix["stream_id"]
                ):
                    previous_stream = candidate
                    break
            if previous_stream is not None:
                old = previous_stream.stream_prefix
                if old["output_role"] != stream_prefix["output_role"] or dict(
                    old["target"]
                ) != dict(stream_prefix["target"]):
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "stream identity changed at a later high-water",
                    )
                if (
                    old["attempt_id"] == attempt_id
                    and int(old["lease_epoch"]) == lease_epoch
                ):
                    verify_stream_prefix(stream_prefix, previous=old, content=prefix)
                elif current["resume_of_attempt_id"] != old["attempt_id"]:
                    raise ContractError(
                        ErrorCode.STALE_LEASE,
                        "stream prefix changed Attempt without an exact resume edge",
                    )
                elif int(stream_prefix["prefix_seq"]) <= int(old["prefix_seq"]) or len(
                    prefix
                ) < len(previous_stream.prefix):
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "stream prefix moved backwards across resume",
                    )
                if not prefix.startswith(previous_stream.prefix):
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "stream prefix does not extend the durable high-water bytes",
                    )

            checkpoint_seq = (
                1
                if previous_checkpoint is None
                else int(previous_checkpoint["checkpoint_seq"]) + 1
            )
            checkpoint_id = _stable_id(
                "checkpoint",
                job_id,
                step_id,
                checkpoint_seq,
                attempt_id,
                lease_epoch,
                operation_key,
                stream_prefix_hash,
            )
            runtime_state = {
                "schema": _RUNTIME_STATE_SCHEMA,
                "workspace_id": workspace_id,
                "job_id": job_id,
                "step_id": step_id,
                "attempt_id": attempt_id,
                "lease_epoch": lease_epoch,
                "worker_run_id": worker_run_id,
                "run_snapshot_hash": run_snapshot_hash,
                "expected_result_contract": expected_result_contract,
                "checkpoint_id": checkpoint_id,
                "checkpoint_seq": checkpoint_seq,
                "replay_policy": replay_policy,
                "stream_id": str(stream_prefix["stream_id"]),
                "stream_prefix_asset_id": stream_prefix_asset_id,
                "stream_prefix_hash": stream_prefix_hash,
                "provider_outcome": provider_outcome,
                "provider_replay_policy": provider_replay_policy,
            }
            runtime_asset = self.assets.put(
                canonical_bytes(runtime_state),
                mime="application/json",
                logical_role="chapter_runtime_state",
                provenance=f"core:chapter-runtime:{job_id}:{step_id}:{attempt_id}",
            )
            unit_set_hash = (
                previous_checkpoint["unit_set_hash"]
                if previous_checkpoint is not None
                else sha256_hex(
                    canonical_bytes(
                        {
                            "schema": "chapter-checkpoint-unit-set/v1",
                            "job_id": job_id,
                            "step_id": step_id,
                            "expected_result_contract": expected_result_contract,
                        }
                    )
                )
            )
            checkpoint = {
                "schema": "checkpoint/v1",
                "checkpoint_id": checkpoint_id,
                "checkpoint_seq": checkpoint_seq,
                "job_id": job_id,
                "step_id": step_id,
                "source_attempt_id": attempt_id,
                "lease_epoch": lease_epoch,
                "run_snapshot_hash": run_snapshot_hash,
                "replay_policy": replay_policy,
                "completed_units": completed_units,
                "total_units": total_units,
                "unit_set_hash": unit_set_hash,
                "state_asset_id": runtime_asset.asset_id,
                "created_at": utc_now(),
            }
            checkpoint["checkpoint_hash"] = hash_without_field(
                checkpoint, "checkpoint_hash", "checkpoint/v1"
            )
            verify_checkpoint(checkpoint, expected_snapshot_hash=run_snapshot_hash)
            checkpoint_asset = self.assets.put(
                canonical_bytes(checkpoint),
                mime="application/json",
                logical_role="checkpoint",
                provenance=f"core:chapter-checkpoint:{job_id}:{step_id}:{attempt_id}",
            )
            committed = self.checkpoint_store.commit_checkpoint(
                checkpoint,
                operation_key=operation_key,
                checkpoint_asset_id=checkpoint_asset.asset_id,
                local_seq=local_seq,
                worker_run_id=worker_run_id,
            )
            stored_row = connection.execute(
                "SELECT * FROM execution_checkpoint WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
            if stored_row is None:
                raise ContractError(
                    ErrorCode.ASSET_ERROR, "committed stream checkpoint disappeared"
                )
            recovered = self._decode_stream_record(
                connection,
                stored_row,
                expected_workspace_id=workspace_id,
                replayed=committed.replayed,
            )
            if recovered is None:
                raise ContractError(
                    ErrorCode.ASSET_ERROR, "committed checkpoint lost runtime state"
                )
            return self._bind_current_attempt(connection, recovered, current)

    def recover_stream(
        self,
        *,
        workspace_id: str,
        job_id: str,
        step_id: str,
        attempt_id: str,
        lease_epoch: int,
        worker_run_id: str,
        stream_id: str,
        run_snapshot_hash: str,
        expected_result_contract: str,
    ) -> StreamCheckpointRecovery | None:
        _require_id(stream_id, "stream_id")
        with self.repository.read_connection() as connection:
            current = self._current_attempt(
                connection,
                workspace_id=workspace_id,
                job_id=job_id,
                step_id=step_id,
                attempt_id=attempt_id,
                lease_epoch=lease_epoch,
                worker_run_id=worker_run_id,
                run_snapshot_hash=run_snapshot_hash,
                expected_result_contract=expected_result_contract,
            )
            rows = connection.execute(
                "SELECT * FROM execution_checkpoint WHERE job_id=? AND step_id=? "
                "ORDER BY checkpoint_seq DESC,job_event_seq DESC,checkpoint_id DESC",
                (job_id, step_id),
            ).fetchall()
            for row in rows:
                recovered = self._decode_stream_record(
                    connection,
                    row,
                    expected_workspace_id=workspace_id,
                    required=False,
                    replayed=True,
                )
                if (
                    recovered is None
                    or recovered.stream_prefix["stream_id"] != stream_id
                ):
                    continue
                if recovered.run_snapshot_hash != run_snapshot_hash:
                    raise ContractError(
                        ErrorCode.CHECKPOINT_INVALID,
                        "recovered stream belongs to another RunSnapshot",
                    )
                if recovered.expected_result_contract != expected_result_contract:
                    raise ContractError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "recovered stream result contract drifted",
                    )
                return self._bind_current_attempt(connection, recovered, current)
            return None

    def read(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        workspace_id: str,
    ) -> JobSnapshotExtensions:
        """Project checkpoint and stream high-waters from the caller's read view."""

        _require_id(job_id, "job_id")
        _require_id(workspace_id, "workspace_id")
        latest_row = self.checkpoint_store._latest_row(connection, job_id)
        checkpoint_binding: JobCheckpointBinding | None = None
        if latest_row is not None:
            latest = self.checkpoint_store._decode_row(
                connection, latest_row, expected_workspace_id=workspace_id
            )
            checkpoint_binding = JobCheckpointBinding(
                workspace_id=workspace_id,
                job_id=job_id,
                step_id=str(latest.checkpoint["step_id"]),
                checkpoint_id=latest.checkpoint_id,
                checkpoint=latest.to_dict(),
            )

        rows = connection.execute(
            "SELECT * FROM execution_checkpoint WHERE job_id=? "
            "ORDER BY job_event_seq DESC,checkpoint_seq DESC,checkpoint_id DESC",
            (job_id,),
        ).fetchall()
        high_waters: dict[str, StreamCheckpointRecovery] = {}
        for row in rows:
            recovered = self._decode_stream_record(
                connection,
                row,
                expected_workspace_id=workspace_id,
                required=False,
                replayed=True,
            )
            if recovered is None:
                continue
            stream_id = str(recovered.stream_prefix["stream_id"])
            high_waters.setdefault(stream_id, recovered)

        step_rows = connection.execute(
            "SELECT step_id,step_ordinal FROM execution_step WHERE job_id=?",
            (job_id,),
        ).fetchall()
        step_order = {
            str(row["step_id"]): int(row["step_ordinal"]) for row in step_rows
        }
        ordered = sorted(
            high_waters.values(),
            key=lambda value: (
                step_order.get(value.step_id, 2**31),
                str(value.stream_prefix["output_role"]),
                str(value.stream_prefix["stream_id"]),
            ),
        )
        stream_bindings = tuple(
            JobStreamHighWaterBinding(
                workspace_id=workspace_id,
                job_id=job_id,
                step_id=value.step_id,
                high_water={
                    "stream_id": value.stream_prefix["stream_id"],
                    "step_id": value.step_id,
                    "output_role": value.stream_prefix["output_role"],
                    "target": dict(value.stream_prefix["target"]),
                    "acked_prefix_seq": int(value.stream_prefix["prefix_seq"]),
                    "acked_bytes": len(value.prefix),
                    "acked_prefix_hash": value.prefix_hash,
                },
            )
            for value in ordered
        )
        return JobSnapshotExtensions(checkpoint_binding, stream_bindings)


__all__ = [
    "AuthorityAttemptStartAdapter",
    "DurableCheckpointAdapter",
    "StreamCheckpointRecovery",
]
