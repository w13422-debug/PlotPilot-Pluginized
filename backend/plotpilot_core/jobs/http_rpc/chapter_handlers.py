"""Production Host RPC handlers for one authority-bound chapter Attempt.

The frozen Host wire intentionally omits several Core-owned values.  This
module therefore never guesses a worker owner, stream progress, or provenance
receipt.  A handler set is bound to the immutable ``AttemptStartBinding``
returned by the P1 authority and consumes explicit resolvers for the two
values that are not present on the wire.

Handlers prepare and validate the exact success result while the accepted
repository gate is held.  The dispatcher then validates that result against
the request before invoking the returned commit callback.  Durable mutations
remain owned by the existing P1 authority/checkpoint/event ports; this module
owns no database, ledger, SQL, or Supervisor state.
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from backend.plotpilot_core.api.v1.jobs.rpc import (
    AttemptStartBinding,
    JobCommandQueryAdapter,
)
from backend.plotpilot_core.broker.service import CallerAttemptContext
from backend.plotpilot_core.domain.entities import utc_now
from backend.plotpilot_core.events.store import CoreEventStore, JobEventStore
from backend.plotpilot_core.jobs.checkpoint_adapter import DurableCheckpointAdapter
from backend.plotpilot_core.jobs.http_rpc.dispatcher import (
    HostRpcHandler,
    PreparedHostRpcResult,
)
from backend.plotpilot_core.repositories.checkpoints import SQLiteCheckpointStore
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
from backend.plotpilot_plugin_sdk.rpc import decode_frame, encode_frame
from backend.plotpilot_plugin_sdk.verifier import (
    validate_rpc_request,
    validate_rpc_response,
    validate_rpc_result,
    verify_provenance_receipt,
)

CHECKPOINT_COMMIT = "host.checkpoint.commit/v1"
STREAM_COMMIT = "host.stream.commit/v1"
JOB_EVENT = "host.job.event/v1"
AWAIT_USER = "host.job.await_user/v1"
JOB_COMPLETE = "host.job.complete/v1"
CHAPTER_HOST_METHODS = (
    CHECKPOINT_COMMIT,
    STREAM_COMMIT,
    JOB_EVENT,
    AWAIT_USER,
    JOB_COMPLETE,
)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_STEP_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})
_REPLAY_POLICIES = frozenset(
    {"idempotent_auto", "checkpoint_resume", "manual_if_unknown", "never_replay"}
)
_PROVIDER_REPLAY_POLICIES = frozenset(
    {"idempotent_auto", "manual_if_unknown", "never_replay"}
)


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


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\n".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:48]}"


class ProvenanceReceiptResolver(Protocol):
    """Read the authoritative receipt for one exact completion request."""

    def resolve_provenance_receipt(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class StreamCommitPolicy:
    """Core-owned progress/replay semantics omitted from the frozen wire."""

    completed_units: int
    total_units: int | None
    replay_policy: str = "checkpoint_resume"
    provider_outcome: str = "confirmed"
    provider_replay_policy: str = "idempotent_auto"

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.completed_units, "completed_units")
        if self.total_units is not None:
            _require_nonnegative_int(self.total_units, "total_units")
            if self.completed_units > self.total_units:
                raise ContractValidationError(
                    "completed_units cannot exceed total_units"
                )
        if self.replay_policy not in _REPLAY_POLICIES:
            raise ContractValidationError("replay_policy is not in the frozen enum")
        _require_id(self.provider_outcome, "provider_outcome")
        if self.provider_replay_policy not in _PROVIDER_REPLAY_POLICIES:
            raise ContractValidationError(
                "provider_replay_policy is not in the frozen enum"
            )
        if self.provider_outcome == "unknown" and self.provider_replay_policy not in {
            "manual_if_unknown",
            "never_replay",
        }:
            raise ContractError(
                ErrorCode.UNCERTAIN_EXTERNAL_EFFECT,
                "an unknown provider outcome cannot be replayed automatically",
            )


class StreamCommitPolicyResolver(Protocol):
    """Resolve stream progress without deriving it from byte or prefix counts."""

    def resolve_stream_commit_policy(
        self,
        request: Mapping[str, Any],
        stream_prefix: Mapping[str, Any],
        attempt: AttemptStartBinding,
    ) -> StreamCommitPolicy: ...


class JobSnapshotReader(Protocol):
    def get_snapshot(self, *, workspace_id: str, job_id: str) -> dict[str, Any]: ...

    def get_event_page(
        self,
        *,
        workspace_id: str,
        job_id: str,
        after_job_event_seq: int = 0,
    ) -> dict[str, Any]: ...


def _resolve_receipt(
    resolver: ProvenanceReceiptResolver
    | Callable[[Mapping[str, Any]], Mapping[str, Any]],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    method = getattr(resolver, "resolve_provenance_receipt", None)
    if callable(method):
        value = method(request)
    elif callable(resolver):
        value = resolver(request)
    else:
        raise TypeError(
            "provenance_receipt_resolver must be callable or expose "
            "resolve_provenance_receipt"
        )
    if not isinstance(value, Mapping):
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "provenance receipt resolver returned a non-object",
        )
    receipt = dict(value)
    verify_provenance_receipt(receipt)
    return receipt


def _resolve_stream_policy(
    resolver: StreamCommitPolicyResolver
    | Callable[
        [Mapping[str, Any], Mapping[str, Any], AttemptStartBinding],
        StreamCommitPolicy,
    ],
    request: Mapping[str, Any],
    stream_prefix: Mapping[str, Any],
    attempt: AttemptStartBinding,
) -> StreamCommitPolicy:
    method = getattr(resolver, "resolve_stream_commit_policy", None)
    if callable(method):
        value = method(request, stream_prefix, attempt)
    elif callable(resolver):
        value = resolver(request, stream_prefix, attempt)
    else:
        raise TypeError(
            "stream_commit_policy_resolver must be callable or expose "
            "resolve_stream_commit_policy"
        )
    if not isinstance(value, StreamCommitPolicy):
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "stream policy resolver must return StreamCommitPolicy",
        )
    return value


class _CommitOnce:
    """Commit one prepared result and always release its repository gate."""

    def __init__(
        self,
        gate: Any,
        *,
        method: str,
        request: Mapping[str, Any],
        expected: Mapping[str, Any],
        action: Callable[[], Mapping[str, Any]],
    ) -> None:
        self._gate = gate
        self._method = method
        self._request = dict(request)
        self._expected = dict(expected)
        self._action = action
        self._closed = False
        self._committed = False

    def _close(self, exc_info: tuple[Any, Any, Any] = (None, None, None)) -> None:
        if self._closed:
            return
        self._closed = True
        self._gate.__exit__(*exc_info)

    def __call__(self) -> None:
        if self._committed:
            return
        try:
            actual = dict(self._action())
            validate_rpc_result(self._method, actual, request=self._request)
            if actual != self._expected:
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "authoritative Host result differs from its prepared result",
                )
            self._committed = True
        except BaseException:
            self._close(sys.exc_info())
            raise
        self._close()

    def abort(self) -> None:
        """Release the preparation gate without applying its mutation."""

        self._close()


class ChapterDurableReplayResolver:
    """Resolve exact terminal ACKs after the active Attempt fence is consumed.

    The resolver deliberately returns ``None`` while an Attempt is still
    active and for non-terminal Host methods.  Consequently it cannot create a
    first authority decision when used as ``PluginProcessSupervisor``'s
    ``durable_host_replay`` callback.
    """

    def __init__(
        self,
        authority: ExecutionAuthority,
        provenance_receipt_resolver: ProvenanceReceiptResolver
        | Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        repository = getattr(authority, "repository", None)
        if repository is None or not callable(
            getattr(repository, "read_connection", None)
        ):
            raise TypeError("durable replay requires the accepted authority repository")
        control = getattr(authority, "control_port", None)
        if control is None or not callable(getattr(control, "await_user", None)):
            raise TypeError("durable replay requires the accepted control port")
        self._authority = authority
        self._control = control
        self._receipt_resolver = provenance_receipt_resolver

    @staticmethod
    def _parts(
        request: Mapping[str, Any], *, expected_method: str | None = None
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        validate_rpc_request(request)
        method = str(request["method"])
        if expected_method is not None and method != expected_method:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                f"expected {expected_method}, received {method}",
            )
        meta = request["meta"]
        params = request["params"]
        if not isinstance(meta, Mapping) or not isinstance(params, Mapping):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Host request meta/params are not objects",
            )
        return method, dict(meta), dict(params)

    def _is_stale(self, meta: Mapping[str, Any]) -> bool:
        context = CallerAttemptContext(
            str(meta["job_id"]),
            str(meta["step_id"]),
            str(meta["attempt_id"]),
            int(meta["lease_epoch"]),
            generation_id=str(meta["generation_id"]),
            plugin_release_id=str(meta["plugin_release_id"]),
        )
        try:
            self._authority.validate_attempt(context)
        except ContractError as exc:
            if exc.code == int(ErrorCode.STALE_LEASE):
                return True
            raise
        return False

    def resolve_result(self, request: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Return a validated terminal result, or ``None`` if not replayable."""

        method, meta, params = self._parts(request)
        if method not in {AWAIT_USER, JOB_COMPLETE} or not self._is_stale(meta):
            return None
        if method == AWAIT_USER:
            decision = self._control.await_user(
                job_id=meta["job_id"],
                step_id=meta["step_id"],
                attempt_id=meta["attempt_id"],
                lease_epoch=meta["lease_epoch"],
                operation_key=params["operation_key"],
                worker_run_id=params["worker_run_id"],
                checkpoint_asset_id=params["checkpoint_asset_id"],
                prompt_asset_id=params["prompt_asset_id"],
                reason=params["reason"],
            )
            if not bool(getattr(decision, "replayed", False)):
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "durable replay resolver cannot create an await-user decision",
                )
            result = decision.to_dict()
        else:
            receipt = _resolve_receipt(self._receipt_resolver, request)
            terminal = self._authority.complete_attempt(
                job_id=meta["job_id"],
                step_id=meta["step_id"],
                attempt_id=meta["attempt_id"],
                lease_epoch=meta["lease_epoch"],
                operation_key=params["operation_key"],
                worker_run_id=params["worker_run_id"],
                outcome=params["outcome"],
                result_bundle_asset_id=params["result_bundle_asset_id"],
                candidate_stage_operation_key=params["candidate_stage_operation_key"],
                terminal_detail_asset_id=params["terminal_detail_asset_id"],
                local_seq=params["local_seq"],
                provenance_receipt=receipt,
                operation_meta=meta,
                rpc_id=str(request["id"]),
            )
            if not terminal.replayed:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "durable replay resolver cannot create a completion decision",
                )
            response = decode_frame(terminal.response_frame)
            validate_rpc_response(response, request=request)
            if encode_frame(response) != terminal.response_frame:
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "durable completion response frame is not canonical",
                )
            result = dict(response["result"])
        validate_rpc_result(method, result, request=request)
        return MappingProxyType(dict(result))

    def __call__(self, request: Mapping[str, Any]) -> bytes | None:
        method, _meta, _params = self._parts(request)
        if method == JOB_COMPLETE:
            # Preserve the exact frame persisted by ExecutionAuthority rather
            # than reconstructing bytes around an otherwise equivalent result.
            meta = dict(request["meta"])
            if not self._is_stale(meta):
                return None
            receipt = _resolve_receipt(self._receipt_resolver, request)
            params = dict(request["params"])
            terminal = self._authority.complete_attempt(
                job_id=meta["job_id"],
                step_id=meta["step_id"],
                attempt_id=meta["attempt_id"],
                lease_epoch=meta["lease_epoch"],
                operation_key=params["operation_key"],
                worker_run_id=params["worker_run_id"],
                outcome=params["outcome"],
                result_bundle_asset_id=params["result_bundle_asset_id"],
                candidate_stage_operation_key=params["candidate_stage_operation_key"],
                terminal_detail_asset_id=params["terminal_detail_asset_id"],
                local_seq=params["local_seq"],
                provenance_receipt=receipt,
                operation_meta=meta,
                rpc_id=str(request["id"]),
            )
            if not terminal.replayed:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "durable replay resolver cannot create a completion decision",
                )
            response = decode_frame(terminal.response_frame)
            validate_rpc_response(response, request=request)
            if encode_frame(response) != terminal.response_frame:
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "durable completion response frame is not canonical",
                )
            return terminal.response_frame
        result = self.resolve_result(request)
        if result is None:
            return None
        response = {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": dict(result),
        }
        validate_rpc_response(response, request=request)
        return encode_frame(response)


class ChapterHostHandlerSet(Mapping[str, HostRpcHandler]):
    """Five production handlers bound to one immutable running Attempt."""

    def __init__(
        self,
        authority: ExecutionAuthority,
        *,
        workspace_id: str,
        run_snapshot_hash: str,
        attempt: AttemptStartBinding,
        provenance_receipt_resolver: ProvenanceReceiptResolver
        | Callable[[Mapping[str, Any]], Mapping[str, Any]],
        stream_commit_policy_resolver: StreamCommitPolicyResolver
        | Callable[
            [Mapping[str, Any], Mapping[str, Any], AttemptStartBinding],
            StreamCommitPolicy,
        ],
        checkpoints: DurableCheckpointAdapter | None = None,
        snapshot_reader: JobSnapshotReader | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.workspace_id = _require_id(workspace_id, "workspace_id")
        self.run_snapshot_hash = _require_hash(run_snapshot_hash, "run_snapshot_hash")
        if not isinstance(attempt, AttemptStartBinding):
            raise TypeError("attempt must be the P1 AttemptStartBinding")
        self._validate_binding(attempt)
        repository = getattr(authority, "repository", None)
        assets = getattr(authority, "assets", None)
        checkpoint_store = getattr(authority, "checkpoint_store", None)
        control = getattr(authority, "control_port", None)
        if repository is None or not callable(
            getattr(repository, "read_connection", None)
        ):
            raise TypeError(
                "chapter handlers require the accepted authority repository"
            )
        if assets is None or not callable(getattr(assets, "read", None)):
            raise TypeError("chapter handlers require the accepted AssetStore")
        if not isinstance(checkpoint_store, SQLiteCheckpointStore):
            raise TypeError("chapter handlers require the accepted checkpoint store")
        if control is None or not callable(getattr(control, "await_user", None)):
            raise TypeError("chapter handlers require the accepted control port")

        checkpoint_adapter = checkpoints or DurableCheckpointAdapter(authority)
        if checkpoint_adapter.authority is not authority:
            raise ValueError("checkpoint adapter must use the exact ExecutionAuthority")
        reader = snapshot_reader or JobCommandQueryAdapter(
            authority,
            snapshot_extensions=checkpoint_adapter,
        )
        if not callable(getattr(reader, "get_snapshot", None)) or not callable(
            getattr(reader, "get_event_page", None)
        ):
            raise TypeError(
                "snapshot reader must expose Job snapshot and event queries"
            )

        self._authority = authority
        self._repository = repository
        self._assets = assets
        self._checkpoint_store = checkpoint_store
        self._checkpoints = checkpoint_adapter
        self._control = control
        self._snapshot_reader = reader
        self._job_events = JobEventStore(repository)
        self._core_events = CoreEventStore(repository)
        self._attempt = attempt
        self._receipt_resolver = provenance_receipt_resolver
        self._stream_policy_resolver = stream_commit_policy_resolver
        self._clock = clock
        self.durable_replay = ChapterDurableReplayResolver(
            authority, provenance_receipt_resolver
        )
        self._handlers: dict[str, HostRpcHandler] = {
            CHECKPOINT_COMMIT: self.checkpoint_commit,
            STREAM_COMMIT: self.stream_commit,
            JOB_EVENT: self.job_event,
            AWAIT_USER: self.await_user,
            JOB_COMPLETE: self.job_complete,
        }

    @staticmethod
    def _validate_binding(attempt: AttemptStartBinding) -> None:
        for name in (
            "job_id",
            "step_id",
            "attempt_id",
            "worker_run_id",
            "plugin_id",
            "capability_id",
            "generation_id",
            "preallocated_receipt_id",
            "expected_result_contract",
        ):
            _require_id(getattr(attempt, name), f"attempt.{name}")
        _require_hash(attempt.release_id, "attempt.release_id")
        _require_hash(attempt.package_hash, "attempt.package_hash")
        if (
            isinstance(attempt.lease_epoch, bool)
            or not isinstance(attempt.lease_epoch, int)
            or attempt.lease_epoch < 1
        ):
            raise ContractValidationError("attempt.lease_epoch must be positive")

    def __getitem__(self, key: str) -> HostRpcHandler:
        return self._handlers[key]

    def __iter__(self) -> Iterator[str]:
        return iter(CHAPTER_HOST_METHODS)

    def __len__(self) -> int:
        return len(CHAPTER_HOST_METHODS)

    def _parts(
        self, request: Mapping[str, Any], expected_method: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        validate_rpc_request(request)
        if request.get("method") != expected_method:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                f"expected {expected_method}, received {request.get('method')}",
            )
        meta = request.get("meta")
        params = request.get("params")
        if not isinstance(meta, Mapping) or not isinstance(params, Mapping):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Host request meta/params are not objects",
            )
        meta = dict(meta)
        params = dict(params)
        attempt = self._attempt
        lineage = (
            meta.get("job_id"),
            meta.get("step_id"),
            meta.get("attempt_id"),
            meta.get("lease_epoch"),
        )
        expected_lineage = (
            attempt.job_id,
            attempt.step_id,
            attempt.attempt_id,
            attempt.lease_epoch,
        )
        if lineage != expected_lineage:
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "Host request does not match the bound Attempt/epoch",
            )
        if (
            meta.get("generation_id") != attempt.generation_id
            or meta.get("plugin_release_id") != attempt.release_id
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "Host request generation/release is not authoritative",
            )
        worker_run_id = params.get("worker_run_id")
        if worker_run_id is not None and worker_run_id != attempt.worker_run_id:
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "Host request worker_run_id is not authoritative",
            )
        return meta, params

    def _validate_active_attempt(self, meta: Mapping[str, Any]) -> None:
        self._authority.validate_attempt(
            CallerAttemptContext(
                self._attempt.job_id,
                self._attempt.step_id,
                self._attempt.attempt_id,
                self._attempt.lease_epoch,
                generation_id=str(meta["generation_id"]),
                plugin_release_id=str(meta["plugin_release_id"]),
            )
        )

    def _snapshot(self) -> dict[str, Any]:
        snapshot = self._snapshot_reader.get_snapshot(
            workspace_id=self.workspace_id,
            job_id=self._attempt.job_id,
        )
        if (
            snapshot.get("workspace_id") != self.workspace_id
            or snapshot.get("job_id") != self._attempt.job_id
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Job snapshot escaped the bound Workspace/Job",
            )
        return snapshot

    def _event_page(self) -> dict[str, Any]:
        return self._snapshot_reader.get_event_page(
            workspace_id=self.workspace_id,
            job_id=self._attempt.job_id,
            after_job_event_seq=0,
        )

    def _load_json_asset(
        self, asset_id: str, *, label: str
    ) -> tuple[dict[str, Any], bytes, str]:
        _require_id(asset_id, f"{label}_asset_id")
        try:
            raw = self._assets.read(asset_id)
            metadata = self._assets.require(asset_id)
            value = parse_json_bytes(raw)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR, f"{label} Asset is missing or invalid"
            ) from exc
        if not isinstance(value, Mapping):
            raise ContractError(
                ErrorCode.ASSET_ERROR, f"{label} Asset is not an object"
            )
        result = dict(value)
        if raw != canonical_bytes(result) or metadata.sha256 != sha256_hex(raw):
            raise ContractError(
                ErrorCode.ASSET_ERROR, f"{label} Asset is not canonical"
            )
        return result, raw, str(metadata.sha256)

    def _checkpoint_asset(self, asset_id: str) -> dict[str, Any]:
        checkpoint, _raw, _digest = self._load_json_asset(asset_id, label="checkpoint")
        verify_checkpoint(checkpoint, expected_snapshot_hash=self.run_snapshot_hash)
        expected = (
            self._attempt.job_id,
            self._attempt.step_id,
            self._attempt.attempt_id,
            self._attempt.lease_epoch,
            self.run_snapshot_hash,
        )
        actual = (
            checkpoint.get("job_id"),
            checkpoint.get("step_id"),
            checkpoint.get("source_attempt_id"),
            checkpoint.get("lease_epoch"),
            checkpoint.get("run_snapshot_hash"),
        )
        if actual != expected:
            code = (
                ErrorCode.STALE_LEASE
                if checkpoint.get("source_attempt_id") != self._attempt.attempt_id
                or checkpoint.get("lease_epoch") != self._attempt.lease_epoch
                else ErrorCode.CHECKPOINT_INVALID
            )
            raise ContractError(code, "checkpoint is outside the bound Attempt")
        return checkpoint

    def _stream_asset(self, asset_id: str) -> tuple[dict[str, Any], bytes, str]:
        stream, _raw, contract_hash = self._load_json_asset(
            asset_id, label="stream prefix"
        )
        try:
            prefix_asset_id = str(stream["prefix_asset_id"])
            prefix = self._assets.read(prefix_asset_id)
            self._assets.require(prefix_asset_id, sha256=str(stream["prefix_hash"]))
            verify_stream_prefix(stream, content=prefix)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR, "stream prefix Asset closure is missing"
            ) from exc
        expected = (
            self._attempt.job_id,
            self._attempt.step_id,
            self._attempt.attempt_id,
            self._attempt.lease_epoch,
            self.workspace_id,
        )
        target = stream.get("target")
        actual = (
            stream.get("job_id"),
            stream.get("step_id"),
            stream.get("attempt_id"),
            stream.get("lease_epoch"),
            target.get("workspace_id") if isinstance(target, Mapping) else None,
        )
        if actual != expected:
            code = (
                ErrorCode.STALE_LEASE
                if stream.get("attempt_id") != self._attempt.attempt_id
                or stream.get("lease_epoch") != self._attempt.lease_epoch
                else ErrorCode.RESULT_CONTRACT_MISMATCH
            )
            raise ContractError(code, "stream prefix is outside the bound Attempt")
        return stream, prefix, contract_hash

    def _operation_event(self, operation_key: str) -> dict[str, Any] | None:
        event_id = _stable_id("job-event", self._attempt.job_id, operation_key)
        return next(
            (
                dict(value)
                for value in self._event_page()["events"]
                if value.get("event_id") == event_id
            ),
            None,
        )

    def _prepare(
        self,
        request: Mapping[str, Any],
        method: str,
        result_factory: Callable[
            [dict[str, Any], dict[str, Any], dict[str, Any]],
            tuple[Mapping[str, Any], Callable[[], Mapping[str, Any]]],
        ],
    ) -> PreparedHostRpcResult:
        meta, params = self._parts(request, method)
        gate = self._repository.read_connection()
        gate.__enter__()
        try:
            self._validate_active_attempt(meta)
            snapshot = self._snapshot()
            result, action = result_factory(meta, params, snapshot)
            prepared = dict(result)
            validate_rpc_result(method, prepared, request=request)
            commit = _CommitOnce(
                gate,
                method=method,
                request=request,
                expected=prepared,
                action=action,
            )
            return PreparedHostRpcResult(
                MappingProxyType(prepared), commit, commit.abort
            )
        except BaseException:
            gate.__exit__(*sys.exc_info())
            raise

    def _terminal_replay(
        self, request: Mapping[str, Any], method: str
    ) -> PreparedHostRpcResult | None:
        self._parts(request, method)
        result = self.durable_replay.resolve_result(request)
        if result is None:
            return None
        prepared = dict(result)
        validate_rpc_result(method, prepared, request=request)
        return PreparedHostRpcResult(MappingProxyType(prepared), lambda: None)

    def checkpoint_commit(self, request: Mapping[str, Any]) -> PreparedHostRpcResult:
        def factory(
            _meta: dict[str, Any],
            params: dict[str, Any],
            snapshot: dict[str, Any],
        ) -> tuple[Mapping[str, Any], Callable[[], Mapping[str, Any]]]:
            operation_key = str(params["operation_key"])
            asset_id = str(params["checkpoint_asset_id"])
            checkpoint = self._checkpoint_asset(asset_id)
            by_id = self._checkpoint_store.get_checkpoint(
                str(checkpoint["checkpoint_id"])
            )
            operation_event = self._operation_event(operation_key)
            if operation_event is not None:
                if (
                    by_id != checkpoint
                    or operation_event.get("payload_asset_id") != asset_id
                ):
                    raise ContractError(
                        ErrorCode.DUPLICATE_REQUEST,
                        "checkpoint operation key was reused with different content",
                    )
                event_seq = int(operation_event["job_event_seq"])
            elif by_id is not None:
                raise ContractError(
                    ErrorCode.DUPLICATE_REQUEST,
                    "checkpoint ID is already bound to another operation",
                )
            else:
                event_seq = int(snapshot["job_event_high_water"]) + 1
            result = {
                "accepted": True,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "completed_units": checkpoint["completed_units"],
                "total_units": checkpoint["total_units"],
                "job_event_seq": event_seq,
            }

            def commit() -> Mapping[str, Any]:
                return self._checkpoint_store.commit_checkpoint(
                    asset_id,
                    operation_key=operation_key,
                    worker_run_id=self._attempt.worker_run_id,
                ).to_dict()

            return result, commit

        return self._prepare(request, CHECKPOINT_COMMIT, factory)

    def stream_commit(self, request: Mapping[str, Any]) -> PreparedHostRpcResult:
        def factory(
            _meta: dict[str, Any],
            params: dict[str, Any],
            snapshot: dict[str, Any],
        ) -> tuple[Mapping[str, Any], Callable[[], Mapping[str, Any]]]:
            operation_key = str(params["operation_key"])
            asset_id = str(params["stream_prefix_asset_id"])
            stream, prefix, stream_contract_hash = self._stream_asset(asset_id)
            policy = _resolve_stream_policy(
                self._stream_policy_resolver, request, stream, self._attempt
            )
            operation_event = self._operation_event(operation_key)
            committed_checkpoint: dict[str, Any] | None = None
            committed_state: dict[str, Any] | None = None
            for checkpoint in self._checkpoint_store.list_for_job(
                self._attempt.job_id, self._attempt.step_id
            ):
                expected_checkpoint_id = _stable_id(
                    "checkpoint",
                    self._attempt.job_id,
                    self._attempt.step_id,
                    checkpoint["checkpoint_seq"],
                    self._attempt.attempt_id,
                    self._attempt.lease_epoch,
                    operation_key,
                    stream_contract_hash,
                )
                if checkpoint.get("checkpoint_id") == expected_checkpoint_id:
                    state, _raw, _digest = self._load_json_asset(
                        str(checkpoint["state_asset_id"]),
                        label="chapter runtime state",
                    )
                    if state.get("stream_prefix_asset_id") != asset_id:
                        continue
                    committed_checkpoint = checkpoint
                    committed_state = state
                    break
            if operation_event is not None:
                if committed_checkpoint is None or committed_state is None:
                    raise ContractError(
                        ErrorCode.DUPLICATE_REQUEST,
                        "stream operation key is already bound to different content",
                    )
                event_checkpoint, _event_raw, _event_hash = self._load_json_asset(
                    str(operation_event.get("payload_asset_id")),
                    label="stream checkpoint",
                )
                expected_state = (
                    asset_id,
                    self._attempt.worker_run_id,
                    self.run_snapshot_hash,
                    self._attempt.expected_result_contract,
                    policy.replay_policy,
                    policy.provider_outcome,
                    policy.provider_replay_policy,
                )
                actual_state = (
                    committed_state.get("stream_prefix_asset_id"),
                    committed_state.get("worker_run_id"),
                    committed_state.get("run_snapshot_hash"),
                    committed_state.get("expected_result_contract"),
                    committed_state.get("replay_policy"),
                    committed_state.get("provider_outcome"),
                    committed_state.get("provider_replay_policy"),
                )
                if (
                    actual_state != expected_state
                    or event_checkpoint != committed_checkpoint
                    or committed_checkpoint.get("completed_units")
                    != policy.completed_units
                    or committed_checkpoint.get("total_units") != policy.total_units
                ):
                    raise ContractError(
                        ErrorCode.DUPLICATE_REQUEST,
                        "stream operation key was reused with different content",
                    )
                event_seq = int(operation_event["job_event_seq"])
            else:
                if committed_checkpoint is not None:
                    raise ContractError(
                        ErrorCode.ASSET_ERROR,
                        "stream checkpoint lost its durable operation event",
                    )
                event_seq = int(snapshot["job_event_high_water"]) + 1
            result = {
                "accepted": True,
                "stream_id": stream["stream_id"],
                "acked_prefix_seq": stream["prefix_seq"],
                "acked_bytes": len(prefix),
                "acked_prefix_hash": stream["prefix_hash"],
                "job_event_seq": event_seq,
            }

            def commit() -> Mapping[str, Any]:
                recovered = self._checkpoints.commit_stream_prefix(
                    stream_prefix_asset_id=asset_id,
                    operation_key=operation_key,
                    worker_run_id=self._attempt.worker_run_id,
                    workspace_id=self.workspace_id,
                    run_snapshot_hash=self.run_snapshot_hash,
                    expected_result_contract=self._attempt.expected_result_contract,
                    completed_units=policy.completed_units,
                    total_units=policy.total_units,
                    replay_policy=policy.replay_policy,
                    provider_outcome=policy.provider_outcome,
                    provider_replay_policy=policy.provider_replay_policy,
                )
                operation_event = self._operation_event(operation_key)
                if operation_event is None:
                    raise ContractError(
                        ErrorCode.ASSET_ERROR,
                        "stream commit lost its durable checkpoint operation",
                    )
                event_checkpoint, _event_raw, _event_hash = self._load_json_asset(
                    str(operation_event.get("payload_asset_id")),
                    label="stream checkpoint",
                )
                if event_checkpoint != dict(recovered.checkpoint):
                    raise ContractError(
                        ErrorCode.ASSET_ERROR,
                        "stream commit event points to another checkpoint",
                    )
                return {
                    "accepted": True,
                    "stream_id": recovered.stream_prefix["stream_id"],
                    "acked_prefix_seq": recovered.stream_prefix["prefix_seq"],
                    "acked_bytes": len(recovered.prefix),
                    "acked_prefix_hash": recovered.prefix_hash,
                    "job_event_seq": int(operation_event["job_event_seq"]),
                }

            return result, commit

        return self._prepare(request, STREAM_COMMIT, factory)

    def job_event(self, request: Mapping[str, Any]) -> PreparedHostRpcResult:
        def factory(
            _meta: dict[str, Any],
            params: dict[str, Any],
            snapshot: dict[str, Any],
        ) -> tuple[Mapping[str, Any], Callable[[], Mapping[str, Any]]]:
            operation_key = str(params["operation_key"])
            payload_asset_id = params["payload_asset_id"]
            payload_hash = None
            if payload_asset_id is not None:
                payload_hash = self._checkpoint_store.asset_hash(
                    str(payload_asset_id), label="Job event payload Asset"
                )
            event_id = _stable_id("job-event", self._attempt.job_id, operation_key)
            page = self._event_page()
            existing = next(
                (
                    value
                    for value in page["events"]
                    if value.get("event_id") == event_id
                ),
                None,
            )
            expected_identity = {
                "job_id": self._attempt.job_id,
                "step_id": self._attempt.step_id,
                "attempt_id": self._attempt.attempt_id,
                "event_type": params["event_type"],
                "plugin_id": self._attempt.plugin_id,
                "release_id": self._attempt.release_id,
                "local_seq": params["local_seq"],
                "payload_asset_id": payload_asset_id,
                "payload_hash": payload_hash,
            }
            if existing is not None:
                if any(
                    existing.get(name) != value
                    for name, value in expected_identity.items()
                ):
                    raise ContractError(
                        ErrorCode.DUPLICATE_REQUEST,
                        "Job event operation key was reused with different content",
                    )
                event = dict(existing)
                event_seq = int(existing["job_event_seq"])
            else:
                if any(
                    value.get("attempt_id") == self._attempt.attempt_id
                    and value.get("local_seq") == params["local_seq"]
                    for value in page["events"]
                ):
                    raise ContractError(
                        ErrorCode.DUPLICATE_REQUEST,
                        "Job event local_seq is already bound",
                    )
                event_seq = int(snapshot["job_event_high_water"]) + 1
                event = {
                    "schema": "plugin-job-event/v1",
                    "event_id": event_id,
                    **expected_identity,
                    "job_event_seq": event_seq,
                    "occurred_at": self._clock(),
                }
            result = {"accepted": True, "job_event_seq": event_seq}

            def commit() -> Mapping[str, Any]:
                stored = self._job_events.append(event)
                return {
                    "accepted": True,
                    "job_event_seq": int(stored["job_event_seq"]),
                }

            return result, commit

        return self._prepare(request, JOB_EVENT, factory)

    def await_user(self, request: Mapping[str, Any]) -> PreparedHostRpcResult:
        try:
            return self._prepare(request, AWAIT_USER, self._await_user_factory(request))
        except ContractError as exc:
            if exc.code != int(ErrorCode.STALE_LEASE):
                raise
            replay = self._terminal_replay(request, AWAIT_USER)
            if replay is None:
                raise
            return replay

    def _await_user_factory(
        self, request: Mapping[str, Any]
    ) -> Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any]],
        tuple[Mapping[str, Any], Callable[[], Mapping[str, Any]]],
    ]:
        def factory(
            meta: dict[str, Any],
            params: dict[str, Any],
            snapshot: dict[str, Any],
        ) -> tuple[Mapping[str, Any], Callable[[], Mapping[str, Any]]]:
            if self._operation_event(str(params["operation_key"])) is not None:
                raise ContractError(
                    ErrorCode.DUPLICATE_REQUEST,
                    "await-user operation key is already bound to a Job event",
                )
            self._checkpoint_store.asset_hash(
                str(params["prompt_asset_id"]), label="await-user prompt Asset"
            )
            checkpoint = self._checkpoint_asset(str(params["checkpoint_asset_id"]))
            if (
                self._checkpoint_store.get_checkpoint(str(checkpoint["checkpoint_id"]))
                is not None
            ):
                raise ContractError(
                    ErrorCode.DUPLICATE_REQUEST,
                    "await-user checkpoint is already committed by another operation",
                )
            result = {
                "accepted": True,
                "attempt_state": "suspended",
                "step_state": "waiting_user",
                "job_state": "waiting_user",
                # Suspension commits the checkpoint event and then its control
                # event in the same P1 transaction.
                "job_event_seq": int(snapshot["job_event_high_water"]) + 2,
            }

            def commit() -> Mapping[str, Any]:
                return self._control.await_user(
                    job_id=meta["job_id"],
                    step_id=meta["step_id"],
                    attempt_id=meta["attempt_id"],
                    lease_epoch=meta["lease_epoch"],
                    operation_key=params["operation_key"],
                    worker_run_id=params["worker_run_id"],
                    checkpoint_asset_id=params["checkpoint_asset_id"],
                    prompt_asset_id=params["prompt_asset_id"],
                    reason=params["reason"],
                ).to_dict()

            return result, commit

        return factory

    def job_complete(self, request: Mapping[str, Any]) -> PreparedHostRpcResult:
        try:
            return self._prepare(
                request, JOB_COMPLETE, self._job_complete_factory(request)
            )
        except ContractError as exc:
            if exc.code != int(ErrorCode.STALE_LEASE):
                raise
            replay = self._terminal_replay(request, JOB_COMPLETE)
            if replay is None:
                raise
            return replay

    def _job_complete_factory(
        self, request: Mapping[str, Any]
    ) -> Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any]],
        tuple[Mapping[str, Any], Callable[[], Mapping[str, Any]]],
    ]:
        def factory(
            meta: dict[str, Any],
            params: dict[str, Any],
            snapshot: dict[str, Any],
        ) -> tuple[Mapping[str, Any], Callable[[], Mapping[str, Any]]]:
            receipt = _resolve_receipt(self._receipt_resolver, request)
            terminal_detail_asset_id = params["terminal_detail_asset_id"]
            if terminal_detail_asset_id is not None:
                self._checkpoint_store.asset_hash(
                    str(terminal_detail_asset_id), label="terminal detail Asset"
                )
            expected_receipt = (
                self._attempt.preallocated_receipt_id,
                self._attempt.plugin_id,
                self._attempt.release_id,
                self._attempt.package_hash,
                self._attempt.capability_id,
                self._attempt.job_id,
                self._attempt.step_id,
                self._attempt.attempt_id,
                self._attempt.lease_epoch,
                self.run_snapshot_hash,
            )
            actual_receipt = tuple(
                receipt[name]
                for name in (
                    "receipt_id",
                    "plugin_id",
                    "release_id",
                    "package_hash",
                    "capability_id",
                    "job_id",
                    "step_id",
                    "attempt_id",
                    "lease_epoch",
                    "run_snapshot_hash",
                )
            )
            if actual_receipt != expected_receipt:
                code = (
                    ErrorCode.STALE_LEASE
                    if receipt.get("attempt_id") != self._attempt.attempt_id
                    or receipt.get("lease_epoch") != self._attempt.lease_epoch
                    else ErrorCode.RESULT_CONTRACT_MISMATCH
                )
                raise ContractError(
                    code, "provenance receipt is outside the bound Attempt"
                )

            step_states = {
                str(step["step_id"]): str(step["state"]) for step in snapshot["steps"]
            }
            step_states[self._attempt.step_id] = str(params["outcome"])
            job_is_terminal = bool(step_states) and all(
                state in _TERMINAL_STEP_STATES for state in step_states.values()
            )
            if not job_is_terminal:
                job_state = (
                    "cancelling" if snapshot["job_state"] == "cancelling" else "running"
                )
            elif snapshot["job_state"] == "cancelling":
                job_state = "cancelled"
            elif any(state == "failed" for state in step_states.values()):
                job_state = "failed"
            elif any(
                state in {"partial", "cancelled"} for state in step_states.values()
            ):
                job_state = "partial"
            else:
                job_state = "succeeded"

            result = {
                "accepted": True,
                "attempt_state": params["outcome"],
                "step_state": params["outcome"],
                "job_state": job_state,
                "provenance_receipt_id": receipt["receipt_id"],
                "job_event_seq": int(snapshot["job_event_high_water"]) + 1,
                "core_event_high_water": self._core_events.high_water()
                + (2 if job_is_terminal else 1),
            }

            def commit() -> Mapping[str, Any]:
                return self._authority.complete_attempt(
                    job_id=meta["job_id"],
                    step_id=meta["step_id"],
                    attempt_id=meta["attempt_id"],
                    lease_epoch=meta["lease_epoch"],
                    operation_key=params["operation_key"],
                    worker_run_id=params["worker_run_id"],
                    outcome=params["outcome"],
                    result_bundle_asset_id=params["result_bundle_asset_id"],
                    candidate_stage_operation_key=params[
                        "candidate_stage_operation_key"
                    ],
                    terminal_detail_asset_id=params["terminal_detail_asset_id"],
                    local_seq=params["local_seq"],
                    provenance_receipt=receipt,
                    operation_meta=meta,
                    rpc_id=str(request["id"]),
                ).to_dict()

            return result, commit

        return factory


def build_chapter_host_handlers(
    authority: ExecutionAuthority,
    *,
    workspace_id: str,
    run_snapshot_hash: str,
    attempt: AttemptStartBinding,
    provenance_receipt_resolver: ProvenanceReceiptResolver
    | Callable[[Mapping[str, Any]], Mapping[str, Any]],
    stream_commit_policy_resolver: StreamCommitPolicyResolver
    | Callable[
        [Mapping[str, Any], Mapping[str, Any], AttemptStartBinding],
        StreamCommitPolicy,
    ],
    checkpoints: DurableCheckpointAdapter | None = None,
    snapshot_reader: JobSnapshotReader | None = None,
    clock: Callable[[], str] = utc_now,
) -> Mapping[str, HostRpcHandler]:
    """Build the five frozen chapter Host handlers for one active Attempt."""

    return ChapterHostHandlerSet(
        authority,
        workspace_id=workspace_id,
        run_snapshot_hash=run_snapshot_hash,
        attempt=attempt,
        provenance_receipt_resolver=provenance_receipt_resolver,
        stream_commit_policy_resolver=stream_commit_policy_resolver,
        checkpoints=checkpoints,
        snapshot_reader=snapshot_reader,
        clock=clock,
    )


def build_chapter_durable_replay_resolver(
    authority: ExecutionAuthority,
    *,
    provenance_receipt_resolver: ProvenanceReceiptResolver
    | Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> ChapterDurableReplayResolver:
    """Build the public callback consumed by the sole P2 Supervisor."""

    return ChapterDurableReplayResolver(authority, provenance_receipt_resolver)


__all__ = [
    "AWAIT_USER",
    "CHAPTER_HOST_METHODS",
    "CHECKPOINT_COMMIT",
    "JOB_COMPLETE",
    "JOB_EVENT",
    "STREAM_COMMIT",
    "ChapterDurableReplayResolver",
    "ChapterHostHandlerSet",
    "JobSnapshotReader",
    "ProvenanceReceiptResolver",
    "StreamCommitPolicy",
    "StreamCommitPolicyResolver",
    "build_chapter_durable_replay_resolver",
    "build_chapter_host_handlers",
]
