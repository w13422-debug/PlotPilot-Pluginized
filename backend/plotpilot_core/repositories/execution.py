from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Any

from backend.plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    ErrorCode,
    assert_valid,
    canonical_bytes,
    derive_operation_context_identity,
    parse_json_bytes,
    sha256_hex,
    verify_result_bundle,
    verify_snapshot,
)
from backend.plotpilot_plugin_sdk.rpc import encode_frame
from backend.plotpilot_plugin_sdk.verifier import hash_without_field, validate_rpc_result

from ..assets import AssetStore
from ..broker.service import (
    BrokerChildRecord,
    BrokerInvokeResult,
    BrokerLedgerEntry,
    BrokerOperationReservation,
    CallerAttemptContext,
    ChildCreationRequest,
    ChildCreationResult,
    TERMINAL_STATES,
)
from ..candidates import CandidateError, CandidateService
from ..domain.entities import utc_now
from ..jobs.states import ATTEMPT_EDGES, JOB_EDGES, STEP_EDGES, can_transition
from .authority import CoreAuthorityRepository


_INVOKE = "host.capability.invoke/v1"
_COMPLETE = "host.job.complete/v1"
_DEFAULT_RPC_ID = "00000000-0000-4000-8000-000000000001"


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str) -> dict[str, Any]:
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ContractError(ErrorCode.ASSET_ERROR, "stored authority value is not an object")
    return loaded


def _broker_context_identity(job_id: str, step_id: str, attempt_id: str) -> str:
    return sha256_hex(
        b"broker-attempt-context/v1\n"
        + canonical_bytes(
            {
                "parent_job_id": job_id,
                "parent_step_id": step_id,
                "parent_attempt_id": attempt_id,
            }
        )
    )


def _id(prefix: str, seed: str) -> str:
    return f"{prefix}-{sha256_hex(seed.encode('utf-8'))[:48]}"


@dataclass(frozen=True, slots=True)
class TerminalCommit:
    result: Mapping[str, Any]
    response_frame: bytes
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return dict(self.result)


class SQLiteBrokerOperationLedger:
    """P1-owned durable implementation of the frozen P3B reservation port."""

    def __init__(self, repository: CoreAuthorityRepository) -> None:
        self.repository = repository

    @staticmethod
    def _row_reservation(row: sqlite3.Row | None) -> BrokerOperationReservation | None:
        if row is None:
            return None
        child = _load(row["child_creation_json"]) if row["child_creation_json"] else None
        return BrokerOperationReservation(
            row["context_identity"],
            row["method"],
            row["operation_key"],
            row["payload_hash"],
            row["envelope_asset_id"],
            child,
        )

    def lookup(self, *, context_identity: str, method: str, operation_key: str) -> BrokerLedgerEntry | None:
        with self.repository._lock:
            row = self.repository._connection.execute(
                "SELECT * FROM p3_broker_operation WHERE context_identity=? AND method=? AND operation_key=?",
                (context_identity, method, operation_key),
            ).fetchone()
        if row is None or row["response"] is None:
            return None
        return BrokerLedgerEntry(context_identity, method, operation_key, row["payload_hash"], bytes(row["response"]))

    def get_reservation(self, *, context_identity: str, method: str, operation_key: str) -> BrokerOperationReservation | None:
        with self.repository._lock:
            row = self.repository._connection.execute(
                "SELECT * FROM p3_broker_operation WHERE context_identity=? AND method=? AND operation_key=?",
                (context_identity, method, operation_key),
            ).fetchone()
        return self._row_reservation(row)

    def reserve(self, *, context_identity: str, method: str, operation_key: str, payload_hash: str) -> BrokerOperationReservation:
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM p3_broker_operation WHERE context_identity=? AND method=? AND operation_key=?",
                (context_identity, method, operation_key),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO p3_broker_operation(context_identity,method,operation_key,payload_hash) VALUES(?,?,?,?)",
                    (context_identity, method, operation_key, payload_hash),
                )
                row = connection.execute(
                    "SELECT * FROM p3_broker_operation WHERE context_identity=? AND method=? AND operation_key=?",
                    (context_identity, method, operation_key),
                ).fetchone()
            elif row["payload_hash"] != payload_hash:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation reservation payload drift")
            return self._row_reservation(row)  # type: ignore[return-value]

    def attach_envelope(self, *, context_identity: str, method: str, operation_key: str, payload_hash: str, envelope_asset_id: str) -> BrokerOperationReservation:
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM p3_broker_operation WHERE context_identity=? AND method=? AND operation_key=?",
                (context_identity, method, operation_key),
            ).fetchone()
            if row is None or row["payload_hash"] != payload_hash:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "operation must be reserved before envelope binding")
            if row["envelope_asset_id"] not in {None, envelope_asset_id}:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "operation reservation envelope drift")
            connection.execute(
                "UPDATE p3_broker_operation SET envelope_asset_id=? WHERE context_identity=? AND method=? AND operation_key=? AND envelope_asset_id IS NULL",
                (envelope_asset_id, context_identity, method, operation_key),
            )
            row = connection.execute(
                "SELECT * FROM p3_broker_operation WHERE context_identity=? AND method=? AND operation_key=?",
                (context_identity, method, operation_key),
            ).fetchone()
            return self._row_reservation(row)  # type: ignore[return-value]

    def attach_child(self, *, context_identity: str, method: str, operation_key: str, payload_hash: str, child_creation: Mapping[str, Any]) -> BrokerOperationReservation:
        candidate = ChildCreationResult.from_mapping(child_creation).to_dict()
        encoded = _json(candidate)
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM p3_broker_operation WHERE context_identity=? AND method=? AND operation_key=?",
                (context_identity, method, operation_key),
            ).fetchone()
            if row is None or row["payload_hash"] != payload_hash or row["envelope_asset_id"] is None:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "operation envelope must be reserved before child binding")
            if row["child_creation_json"] not in {None, encoded}:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "operation reservation child identity drift")
            connection.execute(
                "UPDATE p3_broker_operation SET child_creation_json=? WHERE context_identity=? AND method=? AND operation_key=? AND child_creation_json IS NULL",
                (encoded, context_identity, method, operation_key),
            )
            row = connection.execute(
                "SELECT * FROM p3_broker_operation WHERE context_identity=? AND method=? AND operation_key=?",
                (context_identity, method, operation_key),
            ).fetchone()
            return self._row_reservation(row)  # type: ignore[return-value]

    def record(self, *, context_identity: str, method: str, operation_key: str, payload_hash: str, response: bytes) -> None:
        if not isinstance(response, bytes):
            raise ContractError(ErrorCode.ASSET_ERROR, "broker ledger response must be bytes")
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM p3_broker_operation WHERE context_identity=? AND method=? AND operation_key=?",
                (context_identity, method, operation_key),
            ).fetchone()
            if row is None or row["payload_hash"] != payload_hash:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation key reused with a different payload")
            if method == _INVOKE and row["child_creation_json"] is None:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "invoke response requires durable child identity")
            if row["response"] is not None and bytes(row["response"]) != response:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation key response drift")
            connection.execute(
                "UPDATE p3_broker_operation SET response=? WHERE context_identity=? AND method=? AND operation_key=? AND response IS NULL",
                (response, context_identity, method, operation_key),
            )


class SQLiteBrokerChildRecordStore:
    """Durable child records with immutable lineage and monotonic projection."""

    def __init__(self, repository: CoreAuthorityRepository) -> None:
        self.repository = repository

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> BrokerChildRecord | None:
        return None if row is None else BrokerChildRecord.from_mapping(_load(row["record_json"]))

    def get_by_operation(self, *, context_identity: str, operation_key: str) -> BrokerChildRecord | None:
        with self.repository._lock:
            return self._decode(self.repository._connection.execute(
                "SELECT record_json FROM p3_broker_child_record WHERE context_identity=? AND operation_key=?",
                (context_identity, operation_key),
            ).fetchone())

    def get_by_child_job(self, child_job_id: str) -> BrokerChildRecord | None:
        with self.repository._lock:
            return self._decode(self.repository._connection.execute(
                "SELECT record_json FROM p3_broker_child_record WHERE child_job_id=?", (child_job_id,)
            ).fetchone())

    def save(self, record: BrokerChildRecord) -> None:
        context_identity = _broker_context_identity(record.parent_job_id, record.parent_step_id, record.parent_attempt_id)
        self.bind_operation(context_identity=context_identity, operation_key=record.invoke_operation_key, record=record)

    def bind_operation(self, *, context_identity: str, operation_key: str, record: BrokerChildRecord) -> None:
        encoded = _json(record.to_dict())
        with self.repository.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM p3_broker_child_record WHERE child_job_id=? OR (context_identity=? AND operation_key=?)",
                (record.child_job_id, context_identity, operation_key),
            ).fetchall()
            if any(row["record_json"] != encoded or row["context_identity"] != context_identity or row["operation_key"] != operation_key for row in rows):
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "child record identity drift")
            if not rows:
                connection.execute(
                    "INSERT INTO p3_broker_child_record(child_job_id,context_identity,operation_key,record_json) VALUES(?,?,?,?)",
                    (record.child_job_id, context_identity, operation_key, encoded),
                )

    def replace(self, record: BrokerChildRecord) -> None:
        encoded = _json(record.to_dict())
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM p3_broker_child_record WHERE child_job_id=?", (record.child_job_id,)
            ).fetchone()
            if row is None:
                raise ContractError(ErrorCode.ASSET_ERROR, "unknown child record")
            current = BrokerChildRecord.from_mapping(_load(row["record_json"]))
            stable = (
                "child_job_id", "parent_job_id", "parent_step_id", "parent_attempt_id",
                "invoke_operation_key", "binding_id", "broker_invocation_asset_id",
                "broker_invocation_hash", "child_run_snapshot_asset_id", "child_run_snapshot_hash",
                "result_contract", "required", "propagate_cancel",
            )
            if any(getattr(current, name) != getattr(record, name) for name in stable):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child record stable identity drift")
            if current.state in TERMINAL_STATES and record.state != current.state:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "terminal child projection cannot change state")
            for name in ("result_bundle_asset_id", "provenance_receipt_id"):
                old, new = getattr(current, name), getattr(record, name)
                if old is not None and new != old:
                    raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child projection anchor drift")
            connection.execute("UPDATE p3_broker_child_record SET record_json=? WHERE child_job_id=?", (encoded, record.child_job_id))


class ExecutionAuthority:
    """P1 authority for Job creation, terminal commit and P3B production ports."""

    def __init__(self, repository: CoreAuthorityRepository, assets: AssetStore) -> None:
        self.repository = repository
        self.assets = assets
        self.operation_ledger = SQLiteBrokerOperationLedger(repository)
        self.child_records = SQLiteBrokerChildRecordStore(repository)
        self.candidates = CandidateService(repository, assets)

    def find_by_request_key(self, workspace_id: str, request_key: str) -> Mapping[str, Any] | None:
        row = self.repository._connection.execute(
            "SELECT * FROM execution_job WHERE workspace_id=? AND request_key=?", (workspace_id, request_key)
        ).fetchone()
        return None if row is None else dict(row)

    def create_from_verified_snapshot(self, job_id: str, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        verify_snapshot(snapshot)
        now = utc_now()
        with self.repository.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM execution_job WHERE workspace_id=? AND request_key=?",
                (snapshot["workspace_id"], snapshot["request_key"]),
            ).fetchone()
            if existing is not None:
                if existing["run_intent_id"] != snapshot["run_intent_id"] or existing["run_snapshot_hash"] != snapshot["snapshot_hash"]:
                    raise ContractError(ErrorCode.DUPLICATE_REQUEST, "request key is bound to a different RunSnapshot or intent")
                return dict(existing)
            connection.execute(
                "INSERT INTO execution_job(job_id,workspace_id,request_key,run_intent_id,run_snapshot_hash,run_snapshot_asset_id,run_snapshot_json,job_state,job_revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'queued',1,?,?)",
                (job_id, snapshot["workspace_id"], snapshot["request_key"], snapshot["run_intent_id"], snapshot["snapshot_hash"], None, _json(snapshot), now, now),
            )
            return dict(connection.execute("SELECT * FROM execution_job WHERE job_id=?", (job_id,)).fetchone())

    def start_attempt(
        self,
        *,
        job_id: str,
        step_id: str,
        attempt_id: str,
        worker_run_id: str,
        plugin_id: str,
        release_id: str,
        package_hash: str,
        capability_id: str,
        generation_id: str | None = None,
        lease_epoch: int = 1,
        preallocated_receipt_id: str | None = None,
    ) -> None:
        now = utc_now()
        receipt_id = preallocated_receipt_id or _id("receipt", attempt_id)
        with self.repository.transaction() as connection:
            job = connection.execute("SELECT * FROM execution_job WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown Job")
            attempt = connection.execute("SELECT * FROM execution_attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt is not None:
                created_identity = (
                    job_id, step_id, plugin_id, release_id, capability_id,
                    generation_id, lease_epoch, receipt_id,
                )
                stored_created_identity = tuple(
                    attempt[name] for name in (
                        "job_id", "step_id", "plugin_id", "release_id", "capability_id",
                        "generation_id", "lease_epoch", "preallocated_receipt_id",
                    )
                )
                if attempt["state"] == "created" and attempt["worker_run_id"] is None and attempt["package_hash"] is None:
                    if stored_created_identity != created_identity or job["job_state"] != "queued":
                        raise ContractError(ErrorCode.DUPLICATE_REQUEST, "created Attempt identity drift")
                    step = connection.execute("SELECT * FROM execution_step WHERE step_id=?", (step_id,)).fetchone()
                    if step is None or step["job_id"] != job_id or step["state"] != "pending":
                        raise ContractError(ErrorCode.INVALID_TRANSITION, "created child Step is not startable")
                    connection.execute(
                        "UPDATE execution_attempt SET state='running',worker_run_id=?,package_hash=?,revision=revision+1,updated_at=? WHERE attempt_id=?",
                        (worker_run_id, package_hash, now, attempt_id),
                    )
                    connection.execute(
                        "UPDATE execution_step SET state='running',revision=revision+1,updated_at=? WHERE step_id=?",
                        (now, step_id),
                    )
                    connection.execute(
                        "UPDATE execution_job SET job_state='running',job_revision=job_revision+1,updated_at=? WHERE job_id=?",
                        (now, job_id),
                    )
                    return
                expected = (job_id, step_id, worker_run_id, plugin_id, release_id, package_hash, capability_id, generation_id, lease_epoch, receipt_id)
                actual = tuple(attempt[name] for name in ("job_id", "step_id", "worker_run_id", "plugin_id", "release_id", "package_hash", "capability_id", "generation_id", "lease_epoch", "preallocated_receipt_id"))
                if actual != expected:
                    raise ContractError(ErrorCode.DUPLICATE_REQUEST, "Attempt identity drift")
                return
            step = connection.execute("SELECT * FROM execution_step WHERE step_id=?", (step_id,)).fetchone()
            if step is None:
                connection.execute(
                    "INSERT INTO execution_step(step_id,job_id,state,revision,created_at,updated_at) VALUES(?,?,'running',1,?,?)",
                    (step_id, job_id, now, now),
                )
            elif step["job_id"] != job_id or step["state"] != "running":
                raise ContractError(ErrorCode.INVALID_TRANSITION, "Step is not startable")
            if job["job_state"] not in {"queued", "running"}:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "Job is not startable")
            connection.execute("UPDATE execution_job SET job_state='running',job_revision=job_revision+1,updated_at=? WHERE job_id=?", (now, job_id))
            connection.execute(
                "INSERT INTO execution_attempt(attempt_id,job_id,step_id,state,lease_epoch,worker_run_id,plugin_id,release_id,package_hash,capability_id,generation_id,preallocated_receipt_id,revision,created_at,updated_at) VALUES(?,?,?,'running',?,?,?,?,?,?,?,?,1,?,?)",
                (attempt_id, job_id, step_id, lease_epoch, worker_run_id, plugin_id, release_id, package_hash, capability_id, generation_id, receipt_id, now, now),
            )

    @staticmethod
    def _validate_attempt_row(connection: sqlite3.Connection, context: CallerAttemptContext, *, allow_terminal: bool = False) -> sqlite3.Row:
        expected_identity = _broker_context_identity(context.parent_job_id, context.parent_step_id, context.parent_attempt_id)
        if context.context_identity is not None and context.context_identity != expected_identity:
            raise ContractError(ErrorCode.DUPLICATE_REQUEST, "caller context identity is not canonical")
        row = connection.execute(
            "SELECT a.*,j.job_state,j.job_revision,j.run_snapshot_hash,j.result_bundle_asset_id,j.provenance_receipt_id,j.job_event_high_water,j.core_event_high_water,s.state AS step_state FROM execution_attempt a JOIN execution_job j ON j.job_id=a.job_id JOIN execution_step s ON s.step_id=a.step_id WHERE a.attempt_id=?",
            (context.parent_attempt_id,),
        ).fetchone()
        if row is None or row["job_id"] != context.parent_job_id or row["step_id"] != context.parent_step_id:
            raise ContractError(ErrorCode.STALE_LEASE, "caller Attempt lineage is stale")
        if row["lease_epoch"] != context.lease_epoch or not context.fresh or not context.lease_valid:
            raise ContractError(ErrorCode.STALE_LEASE, "caller Attempt lease epoch is stale")
        if context.generation_id is not None and row["generation_id"] != context.generation_id:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "caller generation is stale")
        if context.plugin_release_id is not None and row["release_id"] != context.plugin_release_id:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "caller release is stale")
        if not allow_terminal and (row["state"] not in {"running", "cancelling"} or row["job_state"] not in {"running", "cancelling"}):
            raise ContractError(ErrorCode.STALE_LEASE, "caller Attempt is no longer active")
        return row

    def validate_attempt(self, context: CallerAttemptContext) -> None:
        with self.repository.transaction() as connection:
            self._validate_attempt_row(connection, context)

    def create_or_recover_child(self, request: ChildCreationRequest) -> ChildCreationResult:
        context_identity = _broker_context_identity(request.caller.parent_job_id, request.caller.parent_step_id, request.caller.parent_attempt_id)
        if request.caller.context_identity not in {None, context_identity}:
            raise ContractError(ErrorCode.DUPLICATE_REQUEST, "caller context identity is not canonical")
        envelope_bytes = request.envelope.canonical_bytes()
        if (
            request.envelope.parent_job_id != request.caller.parent_job_id
            or request.envelope.parent_step_id != request.caller.parent_step_id
            or request.envelope.parent_attempt_id != request.caller.parent_attempt_id
            or request.envelope.expected_result_contract != request.binding.result_contract
            or request.envelope.required != request.binding.required
            or request.envelope.propagate_cancel != request.binding.propagate_cancel
        ):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child request envelope lineage drift")
        try:
            if self.assets.read(request.envelope_asset_id) != envelope_bytes:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "reserved Broker envelope Asset drifted")
            self.assets.require(request.input_asset_id, sha256=request.envelope.input_hash)
            if request.parameters_asset_id is not None:
                self.assets.require(request.parameters_asset_id, sha256=request.envelope.parameters_hash)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "child request Asset binding is invalid") from exc
        operation_key = request.envelope.invoke_operation_key
        seed = f"{context_identity}\n{operation_key}"
        child_job_id = _id("job", seed)
        child_step_id = _id("step", seed)
        child_attempt_id = _id("attempt", seed)
        attestation: dict[str, Any] = {
            "schema": "broker-child-snapshot-binding/v1",
            "child_job_id": child_job_id,
            "child_step_id": child_step_id,
            "child_attempt_id": child_attempt_id,
            "broker_invocation_asset_id": request.envelope_asset_id,
            "broker_invocation_hash": request.envelope.asset_hash,
            "input_asset_id": request.input_asset_id,
            "input_hash": request.envelope.input_hash,
            "parameters_asset_id": request.envelope_asset_id,
        }
        if request.parameters_asset_id is not None:
            attestation["source_parameters_asset_id"] = request.parameters_asset_id
            attestation["source_parameters_hash"] = request.envelope.parameters_hash
        snapshot_bytes = canonical_bytes(attestation)
        snapshot_hash = sha256_hex(snapshot_bytes)
        snapshot_asset_id = self.assets.create_asset(snapshot_bytes, mime="application/json")
        result = ChildCreationResult(
            child_job_id=child_job_id,
            child_step_id=child_step_id,
            child_attempt_id=child_attempt_id,
            child_lease_epoch=1,
            child_plugin_release_id=request.plugin_release_id,
            child_run_snapshot_asset_id=snapshot_asset_id,
            child_run_snapshot_hash=snapshot_hash,
            child_job_event_seq=0,
            state="queued",
            binding_attestation=attestation,
        )
        result_json = _json(result.to_dict())
        request_hash = sha256_hex(canonical_bytes({
            "context_identity": context_identity,
            "operation_key": operation_key,
            "envelope_asset_id": request.envelope_asset_id,
            "envelope_hash": request.envelope.asset_hash,
            "binding": request.binding.to_dict(),
            "descriptor": None if request.descriptor is None else request.descriptor.to_dict(),
            "plugin_release_id": request.plugin_release_id,
            "generation_id": request.generation_id,
        }))
        record = BrokerChildRecord(
            child_job_id=child_job_id,
            parent_job_id=request.caller.parent_job_id,
            parent_step_id=request.caller.parent_step_id,
            parent_attempt_id=request.caller.parent_attempt_id,
            invoke_operation_key=operation_key,
            binding_id=request.binding.binding_id,
            broker_invocation_asset_id=request.envelope_asset_id,
            broker_invocation_hash=request.envelope.asset_hash,
            child_run_snapshot_asset_id=snapshot_asset_id,
            child_run_snapshot_hash=snapshot_hash,
            result_contract=request.binding.result_contract,
            required=request.binding.required,
            propagate_cancel=request.binding.propagate_cancel,
            state="queued",
            result_bundle_asset_id=None,
            provenance_receipt_id=None,
        )
        invoke = BrokerInvokeResult(True, child_job_id, child_step_id, snapshot_asset_id, snapshot_hash, request.binding.result_contract, 0)
        response = invoke.canonical_bytes()
        now = utc_now()
        with self.repository.transaction() as connection:
            parent = self._validate_attempt_row(connection, request.caller)
            operation = connection.execute(
                "SELECT * FROM p3_broker_operation WHERE context_identity=? AND method=? AND operation_key=?",
                (context_identity, _INVOKE, operation_key),
            ).fetchone()
            if operation is None or operation["payload_hash"] != request.envelope.asset_hash or operation["envelope_asset_id"] != request.envelope_asset_id:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child factory reservation binding mismatch")
            existing = connection.execute(
                "SELECT * FROM execution_child_creation WHERE context_identity=? AND operation_key=?",
                (context_identity, operation_key),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash or existing["result_json"] != result_json:
                    raise ContractError(ErrorCode.DUPLICATE_REQUEST, "child creation request drift")
                if operation["child_creation_json"] != result_json or operation["response"] is None or bytes(operation["response"]) != response:
                    raise ContractError(ErrorCode.ASSET_ERROR, "committed child creation is incomplete")
                stored_record = connection.execute(
                    "SELECT record_json FROM p3_broker_child_record WHERE child_job_id=? AND context_identity=? AND operation_key=?",
                    (child_job_id, context_identity, operation_key),
                ).fetchone()
                stored_lineage = connection.execute(
                    "SELECT 1 FROM execution_job j JOIN execution_step s ON s.job_id=j.job_id JOIN execution_attempt a ON a.job_id=j.job_id AND a.step_id=s.step_id WHERE j.job_id=? AND s.step_id=? AND a.attempt_id=?",
                    (child_job_id, child_step_id, child_attempt_id),
                ).fetchone()
                persisted_record = (
                    None if stored_record is None
                    else BrokerChildRecord.from_mapping(_load(stored_record["record_json"]))
                )
                stable_record_fields = (
                    "child_job_id", "parent_job_id", "parent_step_id", "parent_attempt_id",
                    "invoke_operation_key", "binding_id", "broker_invocation_asset_id",
                    "broker_invocation_hash", "child_run_snapshot_asset_id",
                    "child_run_snapshot_hash", "result_contract", "required", "propagate_cancel",
                )
                if (
                    persisted_record is None or stored_lineage is None
                    or any(getattr(persisted_record, name) != getattr(record, name) for name in stable_record_fields)
                ):
                    raise ContractError(ErrorCode.ASSET_ERROR, "committed child authority lineage is incomplete")
                return ChildCreationResult.from_mapping(_load(existing["result_json"]))
            if operation["child_creation_json"] is not None or operation["response"] is not None:
                raise ContractError(ErrorCode.ASSET_ERROR, "orphaned Broker child reservation")
            workspace_id = parent["job_id"] and connection.execute("SELECT workspace_id FROM execution_job WHERE job_id=?", (parent["job_id"],)).fetchone()[0]
            connection.execute(
                "INSERT INTO execution_job(job_id,workspace_id,request_key,run_intent_id,run_snapshot_hash,run_snapshot_asset_id,run_snapshot_json,job_state,job_revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'queued',1,?,?)",
                (child_job_id, workspace_id, sha256_hex(seed.encode()), _id("intent", seed), snapshot_hash, snapshot_asset_id, _json(attestation), now, now),
            )
            connection.execute("INSERT INTO execution_step VALUES(?,?, 'pending',1,?,?)", (child_step_id, child_job_id, now, now))
            connection.execute(
                "INSERT INTO execution_attempt(attempt_id,job_id,step_id,state,lease_epoch,worker_run_id,plugin_id,release_id,package_hash,capability_id,generation_id,preallocated_receipt_id,revision,created_at,updated_at) VALUES(?,?,?,'created',1,NULL,?,?,?,?,?,?,1,?,?)",
                (child_attempt_id, child_job_id, child_step_id, request.binding.plugin_id, request.plugin_release_id, None, request.binding.capability_id, request.generation_id, _id("receipt", child_attempt_id), now, now),
            )
            connection.execute(
                "INSERT INTO execution_child_creation VALUES(?,?,?,?,?)",
                (context_identity, operation_key, request_hash, child_job_id, result_json),
            )
            updated = connection.execute(
                "UPDATE p3_broker_operation SET child_creation_json=?,response=? WHERE context_identity=? AND method=? AND operation_key=? AND child_creation_json IS NULL AND response IS NULL",
                (result_json, response, context_identity, _INVOKE, operation_key),
            )
            if updated.rowcount != 1:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "Broker child reservation changed during creation")
            connection.execute(
                "INSERT INTO p3_broker_child_record(child_job_id,context_identity,operation_key,record_json) VALUES(?,?,?,?)",
                (child_job_id, context_identity, operation_key, _json(record.to_dict())),
            )
        return result

    def complete_attempt(
        self,
        *,
        job_id: str,
        step_id: str,
        attempt_id: str,
        lease_epoch: int,
        operation_key: str,
        worker_run_id: str,
        outcome: str,
        result_bundle_asset_id: str | None,
        candidate_stage_operation_key: str | None,
        terminal_detail_asset_id: str | None,
        local_seq: int,
        provenance_receipt: Mapping[str, Any] | None,
        operation_meta: Mapping[str, Any],
        rpc_id: str = _DEFAULT_RPC_ID,
    ) -> TerminalCommit:
        if outcome not in {"succeeded", "partial", "failed", "cancelled"}:
            raise ContractValidationError("unsupported terminal outcome")
        payload = {
            "operation_key": operation_key,
            "worker_run_id": worker_run_id,
            "outcome": outcome,
            "result_bundle_asset_id": result_bundle_asset_id,
            "candidate_stage_operation_key": candidate_stage_operation_key,
            "terminal_detail_asset_id": terminal_detail_asset_id,
            "local_seq": local_seq,
        }
        payload_hash = sha256_hex(canonical_bytes(payload))
        now = utc_now()
        with self.repository.transaction() as connection:
            context = CallerAttemptContext(job_id, step_id, attempt_id, lease_epoch)
            attempt = self._validate_attempt_row(connection, context, allow_terminal=True)
            if attempt["worker_run_id"] != worker_run_id:
                raise ContractError(ErrorCode.STALE_LEASE, "worker run is not authoritative")
            expected_meta = {
                "context": "attempt", "job_id": job_id, "step_id": step_id,
                "attempt_id": attempt_id, "lease_epoch": lease_epoch,
                "generation_id": attempt["generation_id"],
                "plugin_release_id": attempt["release_id"],
            }
            if any(operation_meta.get(name) != value for name, value in expected_meta.items()):
                code = ErrorCode.STALE_LEASE if operation_meta.get("lease_epoch") != lease_epoch else ErrorCode.INCOMPATIBLE_GENERATION
                raise ContractError(code, "RPC operation meta is not authoritative for this Attempt")
            context_identity = derive_operation_context_identity(
                operation_meta, expected_lease_epoch=attempt["lease_epoch"]
            )
            previous = connection.execute(
                "SELECT o.*,l.response_frame FROM execution_outcome o JOIN p3_host_operation_ledger l ON l.context_identity=o.context_identity AND l.method=? AND l.operation_key=o.operation_key WHERE o.context_identity=? AND o.operation_key=?",
                (_COMPLETE, context_identity, operation_key),
            ).fetchone()
            if previous is not None:
                if previous["payload_hash"] != payload_hash:
                    raise ContractError(ErrorCode.DUPLICATE_REQUEST, "completion key reused with different payload")
                return TerminalCommit(_load(previous["response_json"]), bytes(previous["response_frame"]), True)
            if attempt["job_state"] in {"succeeded", "partial", "failed", "cancelled"}:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "Job is already terminal")
            if attempt["state"] not in {"running", "cancelling"}:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "Attempt is already terminal")
            if not can_transition(ATTEMPT_EDGES, attempt["state"], outcome):
                raise ContractError(ErrorCode.INVALID_TRANSITION, "invalid Attempt terminal transition")
            if not can_transition(STEP_EDGES, attempt["step_state"], outcome):
                raise ContractError(ErrorCode.INVALID_TRANSITION, "invalid Step terminal transition")
            bundle: dict[str, Any] | None = None
            bundle_hash: str | None = None
            staged: list[tuple[str, str]] = []
            if result_bundle_asset_id is not None:
                try:
                    bundle_bytes = self.assets.read(result_bundle_asset_id)
                    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
                    bundle = parse_json_bytes(bundle_bytes)
                    if not isinstance(bundle, dict):
                        raise ContractValidationError("result Bundle is not an object")
                    verify_result_bundle(bundle, snapshot_hash_value=attempt["run_snapshot_hash"])
                except ContractError:
                    raise
                except Exception as exc:
                    raise ContractError(ErrorCode.ASSET_ERROR, "result Bundle Asset is invalid") from exc
                producer = bundle["producer"]
                if (
                    producer["job_id"] != job_id or producer["step_id"] != step_id
                    or producer["attempt_id"] != attempt_id or producer["lease_epoch"] != lease_epoch
                ):
                    code = ErrorCode.STALE_LEASE if producer["lease_epoch"] != lease_epoch else ErrorCode.RESULT_CONTRACT_MISMATCH
                    raise ContractError(code, "result Bundle producer is not the current Attempt")
                if producer["plugin_id"] != attempt["plugin_id"] or producer["release_id"] != attempt["release_id"] or producer["capability_id"] != attempt["capability_id"]:
                    raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "result Bundle producer identity drift")

            statuses = [] if bundle is None else [item["status"] for item in bundle["items"]]
            contract_id = None if bundle is None else bundle["contract_id"]
            if outcome == "succeeded" and (bundle is None or bundle["partial"] or any(value != "complete" for value in statuses)):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "succeeded requires one complete result Bundle")
            if outcome == "partial" and (
                bundle is None or not bundle["partial"]
                or not any(value in {"complete", "partial"} for value in statuses)
                or not any(value != "complete" for value in statuses)
            ):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "partial requires a usable partial result Bundle")
            if outcome in {"failed", "cancelled"} and contract_id not in {None, "diagnostic-bundle/v1"}:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "failed/cancelled cannot expose publishable results")
            if contract_id == "candidate-batch/v1" and candidate_stage_operation_key is None:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate Bundle requires its staging operation key")
            if contract_id != "candidate-batch/v1" and candidate_stage_operation_key is not None:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "non-candidate outcome cannot bind a staging operation")

            if provenance_receipt is None:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "terminal completion requires its provenance receipt")
            receipt = dict(provenance_receipt)
            assert_valid("provenance-receipt/v1", receipt)
            if receipt["receipt_hash"] != hash_without_field(receipt, "receipt_hash", "provenance-receipt/v1"):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "provenance receipt hash mismatch")
            receipt_id = receipt["receipt_id"]
            if receipt_id != attempt["preallocated_receipt_id"]:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "receipt identity is not preallocated")
            expected = (job_id, step_id, attempt_id, lease_epoch, attempt["run_snapshot_hash"], attempt["plugin_id"], attempt["release_id"], attempt["package_hash"], attempt["capability_id"])
            actual = tuple(receipt[name] for name in ("job_id", "step_id", "attempt_id", "lease_epoch", "run_snapshot_hash", "plugin_id", "release_id", "package_hash", "capability_id"))
            if actual != expected:
                code = ErrorCode.STALE_LEASE if receipt["lease_epoch"] != lease_epoch else ErrorCode.RESULT_CONTRACT_MISMATCH
                raise ContractError(code, "provenance receipt lineage drift")
            if bundle is None:
                if receipt["bundle_id"] is not None or receipt["bundle_hash"] is not None:
                    raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "bundleless receipt must have null Bundle anchors")
            elif (
                bundle["provenance_receipt_id"] != receipt_id
                or receipt["bundle_id"] != bundle["bundle_id"]
                or receipt["bundle_hash"] != bundle_hash
            ):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "provenance receipt Bundle anchor drift")

            if contract_id == "candidate-batch/v1":
                for item in bundle["items"]:  # type: ignore[index]
                    try:
                        item_result = self.candidates.stage_in_transaction(
                            connection, candidate_stage_operation_key, item, initial_status="prepared"  # type: ignore[arg-type]
                        )
                    except CandidateError as exc:
                        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, str(exc)) from exc
                    if item_result.candidate_id is not None:
                        existing_pub = connection.execute("SELECT 1 FROM publication_receipt WHERE candidate_id=?", (item_result.candidate_id,)).fetchone()
                        if existing_pub:
                            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate was published before terminal commit")
                        staged.append((item_result.item_id, item_result.candidate_id))
                if list(receipt["staged_items"]) != [item_id for item_id, _ in staged]:
                    raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "receipt staged item mapping is incomplete")
            elif receipt["staged_items"]:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "non-candidate receipt cannot claim staged items")

            if terminal_detail_asset_id is not None:
                detail = self.assets.require(terminal_detail_asset_id)
                detail_hash = detail.sha256
            else:
                detail_hash = None
            receipt_json = _json(receipt)
            row = connection.execute("SELECT receipt_hash,receipt_json FROM execution_receipt WHERE receipt_id=?", (receipt_id,)).fetchone()
            if row is not None and (row["receipt_hash"] != receipt["receipt_hash"] or row["receipt_json"] != receipt_json):
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "receipt identity drift")
            if row is None:
                connection.execute(
                    "INSERT INTO execution_receipt VALUES(?,?,?,?,?,?,?)",
                    (receipt_id, receipt["receipt_hash"], receipt_json, job_id, step_id, attempt_id, receipt["created_at"]),
                )
            for item_id, candidate_id in staged:
                connection.execute("UPDATE candidate SET status='staged' WHERE candidate_id=? AND status IN ('prepared','staged')", (candidate_id,))
                connection.execute(
                    "INSERT INTO execution_candidate_binding(job_id,item_id,candidate_id,stage_operation_key) VALUES(?,?,?,?)",
                    (job_id, item_id, candidate_id, candidate_stage_operation_key),
                )

            job_event_seq = attempt["job_event_high_water"] + 1
            event_id = _id("job-event", f"{context_identity}\n{operation_key}")
            job_event = {
                "schema": "plugin-job-event/v1", "event_id": event_id, "job_id": job_id,
                "step_id": step_id, "attempt_id": attempt_id, "job_event_seq": job_event_seq,
                "event_type": f"plugin.job.{outcome}", "plugin_id": attempt["plugin_id"],
                "release_id": attempt["release_id"], "local_seq": local_seq,
                "payload_asset_id": terminal_detail_asset_id, "payload_hash": detail_hash, "occurred_at": now,
            }
            assert_valid("plugin-job-event/v1", job_event)
            connection.execute("INSERT INTO execution_job_event VALUES(?,?,?,?,?,?)", (job_id, job_event_seq, event_id, attempt_id, local_seq, _json(job_event)))
            step_states = {
                row["step_id"]: (outcome if row["step_id"] == step_id else row["state"])
                for row in connection.execute(
                    "SELECT step_id,state FROM execution_step WHERE job_id=?", (job_id,)
                ).fetchall()
            }
            terminal_step_states = {"succeeded", "partial", "failed", "cancelled"}
            job_is_terminal = bool(step_states) and all(
                state in terminal_step_states for state in step_states.values()
            )
            if not job_is_terminal:
                aggregate_job_state = "cancelling" if attempt["job_state"] == "cancelling" else "running"
            elif attempt["job_state"] == "cancelling":
                aggregate_job_state = "cancelled"
            elif any(state == "failed" for state in step_states.values()):
                aggregate_job_state = "failed"
            elif any(state in {"partial", "cancelled"} for state in step_states.values()):
                aggregate_job_state = "partial"
            else:
                aggregate_job_state = "succeeded"
            if (
                aggregate_job_state != attempt["job_state"]
                and not can_transition(JOB_EDGES, attempt["job_state"], aggregate_job_state)
            ):
                raise ContractError(ErrorCode.INVALID_TRANSITION, "invalid aggregated Job transition")
            aggregate_revision = attempt["job_revision"] + 1
            workspace_id = connection.execute(
                "SELECT workspace_id FROM execution_job WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            core_event_seq = 0
            event_types = ["job.state.changed"]
            if job_is_terminal:
                event_types.append("job.terminal")
            for event_type in event_types:
                core_event_id = _id(
                    "core-event", f"{context_identity}\n{operation_key}\n{event_type}"
                )
                core_event = {
                    "schema": "core-event/v1", "event_id": core_event_id,
                    "workspace_id": workspace_id, "core_event_seq": 0,
                    "aggregate_id": job_id, "aggregate_revision": aggregate_revision,
                    "event_type": event_type,
                    "producer": {"producer_type": "core", "producer_id": "execution-authority", "release_id": None},
                    "correlation_id": operation_key, "causation_id": attempt_id,
                    "payload_asset_id": terminal_detail_asset_id,
                    "payload_hash": detail_hash, "occurred_at": now,
                }
                connection.execute(
                    "INSERT INTO execution_core_event(event_id,workspace_id,aggregate_id,aggregate_revision,event_json) VALUES(?,?,?,?,?)",
                    (core_event_id, workspace_id, job_id, aggregate_revision, _json(core_event)),
                )
                core_event_seq = connection.execute(
                    "SELECT core_event_seq FROM execution_core_event WHERE event_id=?", (core_event_id,)
                ).fetchone()[0]
                core_event["core_event_seq"] = core_event_seq
                assert_valid("core-event-v1", core_event)
                connection.execute(
                    "UPDATE execution_core_event SET event_json=? WHERE event_id=?",
                    (_json(core_event), core_event_id),
                )

            connection.execute("UPDATE execution_attempt SET state=?,revision=revision+1,updated_at=? WHERE attempt_id=?", (outcome, now, attempt_id))
            connection.execute("UPDATE execution_step SET state=?,revision=revision+1,updated_at=? WHERE step_id=?", (outcome, now, step_id))
            connection.execute(
                "UPDATE execution_job SET job_state=?,job_revision=?,result_bundle_asset_id=?,provenance_receipt_id=?,job_event_high_water=?,core_event_high_water=?,updated_at=? WHERE job_id=?",
                (
                    aggregate_job_state, aggregate_revision,
                    result_bundle_asset_id if job_is_terminal else attempt["result_bundle_asset_id"],
                    receipt_id if job_is_terminal else attempt["provenance_receipt_id"],
                    job_event_seq, core_event_seq, now, job_id,
                ),
            )
            result = {
                "accepted": True, "attempt_state": outcome, "step_state": outcome, "job_state": aggregate_job_state,
                "provenance_receipt_id": receipt_id, "job_event_seq": job_event_seq,
                "core_event_high_water": core_event_seq,
            }
            validate_rpc_result(_COMPLETE, result)
            response_frame = encode_frame({"jsonrpc": "2.0", "id": rpc_id, "result": result})
            connection.execute(
                "INSERT INTO execution_outcome VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, step_id, attempt_id, context_identity, operation_key, payload_hash, outcome, result_bundle_asset_id, receipt_id, _json(result), now),
            )
            connection.execute(
                "INSERT INTO p3_host_operation_ledger VALUES(?,?,?,?,?)",
                (context_identity, _COMPLETE, operation_key, payload_hash, response_frame),
            )
            return TerminalCommit(result, response_frame, False)


SQLiteExecutionAuthority = ExecutionAuthority
