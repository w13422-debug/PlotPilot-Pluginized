"""Attempt-scoped Core Model Broker and the P0B receipt/Asset bridge."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from backend.plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    ErrorCode,
    canonical_bytes,
    parse_json_bytes,
    parse_model_profile_revision_v1,
    verify_snapshot,
)
from backend.plotpilot_plugin_sdk.model_provider_rpc_v2 import (
    ModelProviderInvokeResult,
    parse_model_provider_invoke_request_v2,
    parse_model_provider_invoke_result_v2,
    parse_model_provider_rpc_error_v2,
    validate_model_provider_rpc_success_v2,
)
from backend.plotpilot_plugin_sdk.verifier import (
    validate_rpc_request,
    validate_rpc_result,
)

from ..assets import AssetStore
from ..broker.service import CallerAttemptContext
from ..plugins.generation import validate_generation
from ..repositories.authority import CoreAuthorityRepository
from ..repositories.execution import ExecutionAuthority
from .invocation import (
    HOST_MODEL_METHOD,
    ModelInvocationLedger,
    ModelInvocationRecord,
    canonical_json_text,
    receiptless_uncertainty,
)

_PROVIDER_REQUEST_ASSET_SCHEMA = "model-provider-request-asset/v1"
_PROVIDER_REQUEST_ASSET_FIELDS = frozenset(
    {"schema", "profile_revision_id", "messages", "options"}
)
_PROVIDER_OPTION_FIELDS = frozenset(
    {
        "stream", "temperature", "top_p", "max_tokens",
        "max_completion_tokens", "max_output_tokens", "frequency_penalty",
        "presence_penalty", "stop", "seed", "response_format", "tools",
        "tool_choice", "parallel_tool_calls", "logprobs", "top_logprobs",
        "n", "user", "modalities", "reasoning_effort",
    }
)
_SENSITIVE_PARTS = ("secret_value", "api_key", "authorization", "password", "credential")
_SENSITIVE_EXACT = frozenset({"token", "access_token", "secret", "key"})
_MODEL_ASSET_MIME = "application/json"


def _copy_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{label} must be an object")
    try:
        decoded = parse_json_bytes(canonical_bytes(dict(value)))
    except Exception as exc:
        raise ContractValidationError(f"{label} must contain finite JSON values") from exc
    if not isinstance(decoded, dict):
        raise ContractValidationError(f"{label} must be an object")
    return decoded


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _assert_no_secret_fields(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ContractValidationError(f"{path} contains a non-text key")
            lowered = key.casefold()
            sensitive = lowered in _SENSITIVE_EXACT or any(
                part in lowered for part in _SENSITIVE_PARTS
            )
            if sensitive and not lowered.endswith(("_ref", "_id")):
                raise ContractValidationError(f"{path} contains a secret-bearing field")
            _assert_no_secret_fields(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_no_secret_fields(child, path=f"{path}[{index}]")


def _receipt_mapping(proof: Any) -> Mapping[str, Any]:
    receipt = getattr(proof, "receipt", proof)
    if not isinstance(receipt, Mapping):
        raise ContractValidationError("receipt proof did not expose a decoded receipt")
    return receipt


def _uncertain_contract_error() -> ContractError:
    rpc_error, _ = receiptless_uncertainty(recovered=False)
    return ContractError(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT, str(rpc_error["message"]))


@dataclass(frozen=True, slots=True, repr=False)
class ProviderInvocationCommand:
    """Safe, secret-free authority passed to a Provider adapter's prepare phase."""

    host_request: Mapping[str, Any]
    context_identity: str
    workspace_id: str
    job_id: str
    step_id: str
    attempt_id: str
    lease_epoch: int
    caller_plugin_id: str
    caller_plugin_release_id: str
    caller_plugin_package_hash: str
    generation_id: str
    operation_key: str
    invocation_id: str
    invocation_key: str
    replay_policy: str
    run_snapshot_id: str
    run_snapshot_asset_id: str
    run_snapshot_hash: str
    plan_revision_id: str
    plan_revision_hash: str
    model_profile_revision: Mapping[str, Any]
    model_request_asset: Mapping[str, str]
    run_snapshot: Mapping[str, Any]
    generation: Mapping[str, Any]
    model_request_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "host_request", MappingProxyType(_copy_mapping(self.host_request, "Host request")))
        object.__setattr__(
            self, "model_profile_revision",
            MappingProxyType(_copy_mapping(self.model_profile_revision, "Model Profile revision")),
        )
        object.__setattr__(
            self, "model_request_asset",
            MappingProxyType(_copy_mapping(self.model_request_asset, "model request Asset identity")),
        )
        object.__setattr__(self, "run_snapshot", MappingProxyType(_copy_mapping(self.run_snapshot, "RunSnapshot")))
        object.__setattr__(self, "generation", MappingProxyType(_copy_mapping(self.generation, "Generation")))
        object.__setattr__(self, "model_request_bytes", bytes(self.model_request_bytes))


@dataclass(frozen=True, slots=True, repr=False)
class ProviderInvocationPreparation:
    """Validated P0B request plus adapter-private, secret-free prepared state."""

    provider_request: Mapping[str, Any]
    opaque: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_request",
            MappingProxyType(_copy_mapping(self.provider_request, "Provider request")),
        )


@dataclass(frozen=True, slots=True, repr=False)
class ProviderInvocationExchange:
    """One Provider response and the exact Asset bytes it identifies."""

    response: Mapping[str, Any]
    receipt_asset_bytes: bytes | None
    response_asset_bytes: bytes | None = None
    receipt_verifier: Callable[[Mapping[str, Any]], Any] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "response", MappingProxyType(_copy_mapping(self.response, "Provider response")))
        if self.receipt_asset_bytes is not None:
            object.__setattr__(self, "receipt_asset_bytes", bytes(self.receipt_asset_bytes))
        if self.response_asset_bytes is not None:
            object.__setattr__(self, "response_asset_bytes", bytes(self.response_asset_bytes))


class HostProviderAdapterPort(Protocol):
    """Secret-free Core view of the trusted Host Provider adapter."""

    repository: CoreAuthorityRepository

    def prepare(self, command: ProviderInvocationCommand) -> Any: ...

    def provider_request(self, preparation: Any) -> Mapping[str, Any]: ...

    def discard_preparation(self, preparation: Any) -> None: ...

    def invoke(self, preparation: Any) -> Any: ...

    def open_verified_exchange(self, exchange: Any) -> ProviderInvocationExchange: ...

    def receipt_verifier_for(
        self, command: ProviderInvocationCommand
    ) -> Callable[[Mapping[str, Any]], Any]: ...


ReceiptProofVerifier = Callable[[Mapping[str, Any]], Any]
PhaseHook = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class PreparedModelInvocation:
    result: Mapping[str, Any]
    commit: Callable[[], None]
    abort: Callable[[], None]
    replayed: bool


@dataclass(frozen=True, slots=True, repr=False)
class _CurrentAuthority:
    context: CallerAttemptContext
    context_identity: str
    workspace_id: str
    job_id: str
    step_id: str
    attempt_id: str
    lease_epoch: int
    caller_plugin_id: str
    caller_plugin_release_id: str
    caller_plugin_package_hash: str
    generation_id: str
    run_snapshot_id: str
    run_snapshot_asset_id: str
    run_snapshot_hash: str
    plan_revision_id: str
    plan_revision_hash: str
    model_profile_revision_id: str
    model_profile_revision_hash: str
    provider_plugin_id: str
    provider_release_id: str
    profile: Mapping[str, Any]
    snapshot: Mapping[str, Any]
    generation: Mapping[str, Any]
    request_asset_id: str
    request_asset_hash: str
    request_asset_bytes: bytes = field(repr=False)

    def reservation(self, request: Mapping[str, Any]) -> dict[str, Any]:
        params = request["params"]
        host_request_json = canonical_json_text(request)
        return {
            "context_identity": self.context_identity,
            "method": HOST_MODEL_METHOD,
            "operation_key": params["operation_key"],
            "workspace_id": self.workspace_id,
            "job_id": self.job_id,
            "step_id": self.step_id,
            "attempt_id": self.attempt_id,
            "lease_epoch": self.lease_epoch,
            "caller_plugin_id": self.caller_plugin_id,
            "caller_plugin_release_id": self.caller_plugin_release_id,
            "caller_plugin_package_hash": self.caller_plugin_package_hash,
            "generation_id": self.generation_id,
            "run_snapshot_id": self.run_snapshot_id,
            "run_snapshot_asset_id": self.run_snapshot_asset_id,
            "run_snapshot_hash": self.run_snapshot_hash,
            "plan_revision_id": self.plan_revision_id,
            "plan_revision_hash": self.plan_revision_hash,
            "model_profile_revision_id": self.model_profile_revision_id,
            "model_profile_revision_hash": self.model_profile_revision_hash,
            "provider_plugin_id": self.provider_plugin_id,
            "provider_release_id": self.provider_release_id,
            "invocation_id": params["invocation_id"],
            "invocation_key": params["invocation_key"],
            "replay_policy": params["replay_policy"],
            "host_request_hash": _hash_bytes(host_request_json.encode("utf-8")),
            "host_request_json": host_request_json,
            "source_request_asset_id": self.request_asset_id,
            "source_request_asset_hash": self.request_asset_hash,
        }

    def command(self, request: Mapping[str, Any]) -> ProviderInvocationCommand:
        params = request["params"]
        return ProviderInvocationCommand(
            host_request=request, context_identity=self.context_identity,
            workspace_id=self.workspace_id, job_id=self.job_id, step_id=self.step_id,
            attempt_id=self.attempt_id, lease_epoch=self.lease_epoch,
            caller_plugin_id=self.caller_plugin_id,
            caller_plugin_release_id=self.caller_plugin_release_id,
            caller_plugin_package_hash=self.caller_plugin_package_hash,
            generation_id=self.generation_id, operation_key=params["operation_key"],
            invocation_id=params["invocation_id"], invocation_key=params["invocation_key"],
            replay_policy=params["replay_policy"], run_snapshot_id=self.run_snapshot_id,
            run_snapshot_asset_id=self.run_snapshot_asset_id,
            run_snapshot_hash=self.run_snapshot_hash, plan_revision_id=self.plan_revision_id,
            plan_revision_hash=self.plan_revision_hash,
            model_profile_revision=self.profile,
            model_request_asset={"asset_id": self.request_asset_id, "content_hash": self.request_asset_hash},
            run_snapshot=self.snapshot, generation=self.generation,
            model_request_bytes=self.request_asset_bytes,
        )


@dataclass(frozen=True, slots=True, repr=False)
class _TerminalEvidence:
    authority: _CurrentAuthority
    reservation: Mapping[str, Any]
    provider_request: Mapping[str, Any]
    provider_success: Mapping[str, Any]
    provider_result: Mapping[str, Any]
    host_result: Mapping[str, Any]
    terminal: Mapping[str, Any]
    receipt_verifier: ReceiptProofVerifier = field(repr=False, compare=False)


class _TerminalCommit:
    def __init__(
        self,
        broker: ModelBroker,
        evidence: _TerminalEvidence,
        result: Mapping[str, Any],
    ) -> None:
        self._broker = broker
        self._evidence = evidence
        self._result = result
        self._lock = threading.Lock()
        self._committed = False
        self._closed = False

    def __call__(self) -> None:
        with self._lock:
            if self._committed:
                return
            if _copy_mapping(self._result, "prepared Host result") != dict(
                self._evidence.host_result
            ):
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "prepared Host result drifted before T3",
                )
            self._broker._commit_terminal(self._evidence)
            self._broker._phase("after_t3")
            self._committed = True
            self._closed = True

    def abort(self) -> None:
        with self._lock:
            if self._committed or self._closed:
                return
            self._broker._close_receiptless(self._evidence.reservation, recovered=False)
            self._closed = True


class ModelBroker:
    """Core-owned T1/T2/T3 broker with no automatic Provider fallback."""

    def __init__(
        self,
        authority: ExecutionAuthority,
        provider_port: HostProviderAdapterPort | None,
        *,
        phase_hook: PhaseHook | None = None,
    ) -> None:
        if not isinstance(authority, ExecutionAuthority):
            raise TypeError("ModelBroker requires the accepted ExecutionAuthority")
        if provider_port is not None:
            required = (
                "prepare",
                "provider_request",
                "discard_preparation",
                "invoke",
                "open_verified_exchange",
                "receipt_verifier_for",
            )
            if any(not callable(getattr(provider_port, name, None)) for name in required):
                raise TypeError("Host Provider adapter does not expose its sealed ports")
            if getattr(provider_port, "repository", None) is not authority.repository:
                raise TypeError("Host Provider adapter belongs to another Core repository")
        self.authority = authority
        self.repository: CoreAuthorityRepository = authority.repository
        self.assets: AssetStore = authority.assets
        self.provider_port = provider_port
        self.ledger = ModelInvocationLedger(self.repository)
        self._phase_hook = phase_hook

    def _phase(self, name: str) -> None:
        if self._phase_hook is not None:
            self._phase_hook(name)

    @staticmethod
    def _host_parts(request: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        validate_rpc_request(request)
        if request.get("method") != HOST_MODEL_METHOD:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "ModelBroker received another Host method",
            )
        copied = _copy_mapping(request, "Host model request")
        return copied, dict(copied["meta"]), dict(copied["params"])

    def _read_json_asset(self, asset_id: str, *, label: str) -> tuple[bytes, dict[str, Any], str]:
        try:
            metadata = self.assets.require(asset_id, mime="application/json")
            raw = self.assets.read(asset_id)
            value = parse_json_bytes(raw)
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, f"{label} is missing or corrupt") from exc
        if not isinstance(value, Mapping) or canonical_bytes(value) != raw:
            raise ContractError(ErrorCode.ASSET_ERROR, f"{label} is not canonical JSON")
        return raw, dict(value), metadata.sha256

    @staticmethod
    def _validate_safe_json_asset(raw: bytes, *, label: str) -> dict[str, Any]:
        """Apply structural no-reflection checks without value-substring scans."""

        try:
            value = parse_json_bytes(raw)
        except Exception as exc:
            raise ContractValidationError(f"{label} must be canonical UTF-8 JSON") from exc
        if not isinstance(value, Mapping) or canonical_bytes(value) != raw:
            raise ContractValidationError(f"{label} must be a canonical JSON object")
        _assert_no_secret_fields(value, path=label)
        return dict(value)

    @staticmethod
    def _validate_request_asset(
        value: Mapping[str, Any], *, profile_revision_id: str
    ) -> None:
        if set(value) != _PROVIDER_REQUEST_ASSET_FIELDS:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "model request Asset has an unrecognized shape",
            )
        if (
            value.get("schema") != _PROVIDER_REQUEST_ASSET_SCHEMA
            or value.get("profile_revision_id") != profile_revision_id
            or not isinstance(value.get("messages"), list)
            or not isinstance(value.get("options"), Mapping)
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "model request Asset is not bound to the pinned Model Profile",
            )
        options = value["options"]
        if not set(options).issubset(_PROVIDER_OPTION_FIELDS):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "model request Asset attempts to override Host Provider authority",
            )
        if "stream" in options and type(options["stream"]) is not bool:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "model request stream option must be boolean",
            )
        _assert_no_secret_fields(options, path="model_request.options")

    def _current_authority(
        self,
        connection: Any,
        request: Mapping[str, Any],
        meta: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> _CurrentAuthority:
        context = CallerAttemptContext(
            str(meta["job_id"]), str(meta["step_id"]), str(meta["attempt_id"]),
            int(meta["lease_epoch"]), generation_id=str(meta["generation_id"]),
            plugin_release_id=str(meta["plugin_release_id"]),
        )
        row = self.authority._validate_attempt_row(connection, context)
        context_identity = context.identity()
        try:
            snapshot = parse_json_bytes(str(row["run_snapshot_json"]).encode("utf-8"))
            if not isinstance(snapshot, Mapping):
                raise TypeError("RunSnapshot is not an object")
            verify_snapshot(snapshot)
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "Attempt RunSnapshot is invalid") from exc
        if snapshot["snapshot_hash"] != row["run_snapshot_hash"]:
            raise ContractError(ErrorCode.ASSET_ERROR, "Attempt RunSnapshot hash drifted")
        run_snapshot_asset_id = row["run_snapshot_asset_id"]
        if not isinstance(run_snapshot_asset_id, str):
            raise ContractError(ErrorCode.ASSET_ERROR, "Attempt RunSnapshot Asset is missing")
        _snapshot_bytes, snapshot_asset, _snapshot_asset_hash = self._read_json_asset(
            run_snapshot_asset_id, label="RunSnapshot Asset"
        )
        if snapshot_asset != dict(snapshot):
            raise ContractError(ErrorCode.ASSET_ERROR, "RunSnapshot row and Asset differ")

        caller_releases = [
            member for member in snapshot["plugin_releases"]
            if member["plugin_id"] == row["plugin_id"]
        ]
        if len(caller_releases) != 1 or (
            caller_releases[0]["release_id"], caller_releases[0]["package_hash"],
        ) != (row["release_id"], row["package_hash"]):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "Attempt caller is not pinned by its RunSnapshot",
            )
        profile_revision_id = snapshot.get("model_profile_revision_id")
        plan_revision_id = snapshot.get("plan_revision_id")
        if (
            not isinstance(profile_revision_id, str) or not profile_revision_id
            or not isinstance(plan_revision_id, str) or not plan_revision_id
            or params["model_profile_revision_id"] != profile_revision_id
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "Host request is not bound to the RunSnapshot Model Profile and Plan",
            )

        selections = connection.execute(
            "SELECT * FROM p1_workspace_plan_selection WHERE workspace_id=? "
            "AND plan_revision_id=? AND model_profile_revision_id=? "
            "AND active_generation_id=? ORDER BY workspace_revision DESC",
            (row["workspace_id"], plan_revision_id, profile_revision_id, row["generation_id"]),
        ).fetchall()
        if not selections:
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "RunSnapshot Plan/Profile/Generation authority is unavailable",
            )
        selection = selections[0]
        if any(
            (item["plan_revision_hash"], item["model_profile_revision_hash"])
            != (selection["plan_revision_hash"], selection["model_profile_revision_hash"])
            for item in selections
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "historical Plan selection authority is ambiguous",
            )
        plan = connection.execute(
            "SELECT plan_revision_hash FROM p1_plan_revision_reference "
            "WHERE generation_id=? AND plan_revision_id=?",
            (row["generation_id"], plan_revision_id),
        ).fetchone()
        if plan is None or plan["plan_revision_hash"] != selection["plan_revision_hash"]:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "pinned Plan authority drifted")

        profile_row = connection.execute(
            "SELECT payload_json,revision_hash FROM p1_model_profile_revision WHERE revision_id=?",
            (profile_revision_id,),
        ).fetchone()
        if profile_row is None or profile_row["revision_hash"] != selection["model_profile_revision_hash"]:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "pinned Model Profile authority drifted")
        try:
            profile = parse_model_profile_revision_v1(
                parse_json_bytes(str(profile_row["payload_json"]).encode("utf-8"))
            )
        except Exception as exc:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "pinned Model Profile is invalid") from exc
        if profile["revision_hash"] != profile_row["revision_hash"]:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "Model Profile self-hash drifted")

        generation_row = connection.execute(
            "SELECT payload_json,payload_hash FROM p2_plugin_generation WHERE generation_id=?",
            (row["generation_id"],),
        ).fetchone()
        if generation_row is None:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "pinned Generation is missing")
        try:
            generation = validate_generation(
                parse_json_bytes(str(generation_row["payload_json"]).encode("utf-8"))
            )
        except Exception as exc:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "pinned Generation is invalid") from exc
        if (
            generation["generation_id"] != row["generation_id"]
            or _hash_bytes(canonical_bytes(generation)) != generation_row["payload_hash"]
        ):
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "pinned Generation hash drifted")
        generation_callers = [
            member for member in generation["members"] if member["plugin_id"] == row["plugin_id"]
        ]
        provider = profile["provider"]
        provider_members = [
            member for member in generation["members"]
            if member["plugin_id"] == provider["plugin_id"]
        ]
        if (
            len(generation_callers) != 1
            or generation_callers[0]["release_id"] != row["release_id"]
            or generation_callers[0]["package_hash"] != row["package_hash"]
            or len(provider_members) != 1
            or provider_members[0]["release_id"] != provider["release_id"]
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "caller or Provider release is outside the pinned Generation",
            )

        request_asset_id = str(params["request_asset_id"])
        request_bytes, request_asset, request_hash = self._read_json_asset(
            request_asset_id, label="model request Asset"
        )
        self._validate_request_asset(request_asset, profile_revision_id=profile_revision_id)
        return _CurrentAuthority(
            context=context, context_identity=context_identity,
            workspace_id=str(row["workspace_id"]), job_id=str(row["job_id"]),
            step_id=str(row["step_id"]), attempt_id=str(row["attempt_id"]),
            lease_epoch=int(row["lease_epoch"]), caller_plugin_id=str(row["plugin_id"]),
            caller_plugin_release_id=str(row["release_id"]),
            caller_plugin_package_hash=str(row["package_hash"]),
            generation_id=str(row["generation_id"]),
            run_snapshot_id=str(snapshot["snapshot_id"]),
            run_snapshot_asset_id=run_snapshot_asset_id,
            run_snapshot_hash=str(snapshot["snapshot_hash"]),
            plan_revision_id=plan_revision_id,
            plan_revision_hash=str(selection["plan_revision_hash"]),
            model_profile_revision_id=profile_revision_id,
            model_profile_revision_hash=str(profile_row["revision_hash"]),
            provider_plugin_id=str(provider["plugin_id"]),
            provider_release_id=str(provider["release_id"]),
            profile=profile, snapshot=dict(snapshot), generation=generation,
            request_asset_id=request_asset_id, request_asset_hash=request_hash,
            request_asset_bytes=request_bytes,
        )

    def _bind_provider_request(
        self,
        authority: _CurrentAuthority,
        request: Mapping[str, Any],
        provider_request: Mapping[str, Any],
    ) -> dict[str, Any]:
        parsed = parse_model_provider_invoke_request_v2(provider_request)
        params = request["params"]
        context = parsed["params"]["planner_context"]
        expected = {
            "operation_key": params["operation_key"],
            "workspace_id": authority.workspace_id,
            "plugin_id": authority.caller_plugin_id,
            "plugin_release_id": authority.caller_plugin_release_id,
            "plugin_package_hash": authority.caller_plugin_package_hash,
            "generation_id": authority.generation_id,
            "job_id": authority.job_id,
            "step_id": authority.step_id,
            "attempt_id": authority.attempt_id,
            "lease_epoch": authority.lease_epoch,
            "run_snapshot_id": authority.run_snapshot_id,
            "run_snapshot_asset_id": authority.run_snapshot_asset_id,
            "run_snapshot_hash": authority.run_snapshot_hash,
            "model_profile_revision_id": authority.model_profile_revision_id,
        }
        drift = [field for field, value in expected.items() if context[field] != value]
        if drift:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Provider planner context drifted from Host/Attempt authority",
                details={"fields": drift},
            )
        if parsed["params"]["model_request_asset"] != {
            "asset_id": authority.request_asset_id,
            "content_hash": authority.request_asset_hash,
        }:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Provider request does not identify the immutable Host request Asset",
            )
        snapshot_assets = {
            item["asset_id"]: item["sha256"] for item in authority.snapshot["asset_hashes"]
        }
        for asset_id_field, hash_field in (
            ("chain_asset_id", "chain_content_hash"),
            ("input_asset_id", "input_content_hash"),
        ):
            asset_id = context[asset_id_field]
            digest = context[hash_field]
            if snapshot_assets.get(asset_id) != digest:
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    f"Provider {asset_id_field} is outside the frozen RunSnapshot",
                )
            try:
                self.assets.require(asset_id, sha256=digest)
            except Exception as exc:
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    f"Provider {asset_id_field} Asset is missing or corrupt",
                ) from exc
        try:
            chain_raw = self.assets.read(context["chain_asset_id"])
            chain = parse_json_bytes(chain_raw)
            if (
                isinstance(chain, Mapping) and "chain_id" in chain
                and chain["chain_id"] != context["chain_id"]
            ):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "Provider chain identity drifted from its Asset",
                )
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "Provider chain Asset is invalid") from exc
        _assert_no_secret_fields(parsed, path="provider_request")
        return parsed

    @staticmethod
    def _assert_same_authority(
        first: _CurrentAuthority, second: _CurrentAuthority, request: Mapping[str, Any]
    ) -> None:
        if first.reservation(request) != second.reservation(request):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "model invocation authority changed before the dispatch barrier",
            )

    def _asset_verifier(self, identity: Mapping[str, Any], role: str) -> None:
        if role not in {"model_request_asset", "response_asset"}:
            raise ContractError(ErrorCode.ASSET_ERROR, "unknown P0B Asset role")
        try:
            self.assets.require(str(identity["asset_id"]), sha256=str(identity["content_hash"]))
        except Exception as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, f"{role} failed Core Asset verification") from exc

    def _validate_receipt_backed(
        self,
        authority: _CurrentAuthority,
        reservation: Mapping[str, Any],
        provider_request: Mapping[str, Any],
        provider_success: Mapping[str, Any],
        receipt_verifier: ReceiptProofVerifier,
    ) -> tuple[ModelProviderInvokeResult, Mapping[str, Any]]:
        if not callable(receipt_verifier):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "receipt proof callback is unavailable")
        captured: list[Any] = []

        def verify(anchor: Mapping[str, Any]) -> Any:
            proof = receipt_verifier(dict(anchor))
            captured.append(proof)
            return proof

        result = validate_model_provider_rpc_success_v2(
            provider_success, request=provider_request,
            receipt_verifier=verify, asset_verifier=self._asset_verifier,
        )
        if len(captured) != 1:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "receipt proof callback was not exact")
        proof = captured[0]
        receipt = _receipt_mapping(proof)
        anchor = result["model_receipt_anchor"]
        if (
            getattr(proof, "receipt", None) is not receipt
            or getattr(proof, "asset_id", None) != anchor["asset_id"]
            or getattr(proof, "asset_hash", None) != anchor["content_hash"]
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "receipt callback did not return authoritative Asset proof",
            )
        context = provider_request["params"]["planner_context"]
        receipt_context = receipt.get("input_context")
        if not isinstance(receipt_context, Mapping):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "ModelReceipt input context is missing")
        direct_fields = (
            "operation_key", "workspace_id", "plugin_id", "plugin_release_id",
            "plugin_package_hash", "generation_id", "job_id", "step_id",
            "attempt_id", "lease_epoch", "chain_id", "chain_asset_id",
            "chain_content_hash", "run_snapshot_id", "run_snapshot_asset_id",
            "run_snapshot_hash", "input_asset_id",
        )
        if any(receipt_context.get(field) != context[field] for field in direct_fields) or (
            receipt_context.get("input_hash") != context["input_content_hash"]
        ):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "ModelReceipt input context drifted from the P0B planner context",
            )
        profile_provider = authority.profile["provider"]
        expected_receipt = {
            "invocation_id": reservation["invocation_id"],
            "invocation_key": reservation["invocation_key"],
            "profile_revision_id": authority.model_profile_revision_id,
            "provider_plugin_id": authority.provider_plugin_id,
            "provider_release_id": authority.provider_release_id,
            "endpoint": profile_provider["endpoint"],
            "model": profile_provider["model_name"],
        }
        if any(receipt.get(field) != value for field, value in expected_receipt.items()):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "ModelReceipt drifted from the outer Host invocation or pinned Profile",
            )
        max_retries = profile_provider["options"]["max_retries"]
        if type(receipt.get("retry_count")) is not int or receipt["retry_count"] > max_retries:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "ModelReceipt retry count exceeds the pinned Profile",
            )
        _assert_no_secret_fields(receipt.get("profile_revision"), path="model_receipt.profile_revision")
        _assert_no_secret_fields(provider_success, path="provider_success")
        return result, receipt

    @staticmethod
    def _host_result(
        provider_result: Mapping[str, Any], receipt_asset_id: str
    ) -> dict[str, Any]:
        provider_state = provider_result["provider_terminal_state"]
        state = {
            "receipted": "received", "failed": "failed",
            "cancelled": "failed", "uncertain": "uncertain",
        }[provider_state]
        response = provider_result["response_asset"]
        uncertainty = None
        if provider_state == "uncertain":
            uncertainty = {
                "code": "uncertain_external_effect",
                "message": "Provider returned a canonical uncertain ModelReceipt",
                "details_asset_id": None,
            }
        return {
            "state": state,
            "response_asset_id": None if response is None else response["asset_id"],
            "receipt_id": receipt_asset_id,
            "uncertainty": uncertainty,
        }

    def _close_receiptless(
        self, reservation: Mapping[str, Any], *, recovered: bool
    ) -> ModelInvocationRecord:
        with self.repository.transaction() as connection:
            record, _ = self.ledger.mark_receiptless_uncertain(
                connection, reservation, recovered=recovered
            )
        return record

    def _terminal_evidence(
        self,
        authority: _CurrentAuthority,
        reservation: Mapping[str, Any],
        provider_request: Mapping[str, Any],
        provider_success: Mapping[str, Any],
        receipt_verifier: ReceiptProofVerifier,
    ) -> _TerminalEvidence:
        provider_result, receipt = self._validate_receipt_backed(
            authority,
            reservation,
            provider_request,
            provider_success,
            receipt_verifier,
        )
        anchor = provider_result["model_receipt_anchor"]
        host_result = self._host_result(provider_result, anchor["asset_id"])
        host_request = parse_json_bytes(str(reservation["host_request_json"]).encode("utf-8"))
        if not isinstance(host_request, Mapping):
            raise ContractError(ErrorCode.ASSET_ERROR, "stored Host request is invalid")
        validate_rpc_result(HOST_MODEL_METHOD, host_result, request=host_request)
        terminal = {
            "state": host_result["state"],
            "provider_success_json": dict(provider_success),
            "provider_transport_request_hash": provider_result["provider_transport_request_hash"],
            "provider_transport_response_hash": provider_result["provider_transport_response_hash"],
            "response_asset_id": host_result["response_asset_id"],
            "response_asset_hash": (
                None if provider_result["response_asset"] is None
                else provider_result["response_asset"]["content_hash"]
            ),
            "model_receipt_id": receipt["receipt_id"],
            "receipt_asset_id": anchor["asset_id"],
            "receipt_asset_hash": anchor["content_hash"],
            "receipt_hash": anchor["receipt_hash"],
            "host_result_json": host_result,
            "rpc_error_json": None,
            "uncertainty_json": host_result["uncertainty"],
        }
        return _TerminalEvidence(
            authority=authority, reservation=dict(reservation),
            provider_request=dict(provider_request), provider_success=dict(provider_success),
            provider_result=dict(provider_result), host_result=host_result, terminal=terminal,
            receipt_verifier=receipt_verifier,
        )

    def _commit_terminal(self, evidence: _TerminalEvidence) -> None:
        provider_result, _receipt = self._validate_receipt_backed(
            evidence.authority, evidence.reservation,
            evidence.provider_request, evidence.provider_success,
            evidence.receipt_verifier,
        )
        if dict(provider_result) != dict(evidence.provider_result):
            raise ContractError(ErrorCode.ASSET_ERROR, "terminal Provider evidence drifted before T3")
        with self.repository.transaction() as connection:
            self.ledger.commit_terminal(connection, evidence.reservation, evidence.terminal)

    def _replay_terminal(
        self,
        authority: _CurrentAuthority,
        reservation: Mapping[str, Any],
        record: ModelInvocationRecord,
    ) -> PreparedModelInvocation:
        if record.state == "uncertain" and not record.receipt_backed:
            raise _uncertain_contract_error()
        provider_request = record.provider_request
        provider_success = record.provider_success
        host_result = record.host_result
        if provider_request is None or provider_success is None or host_result is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "terminal model invocation closure is incomplete")
        host_request = parse_json_bytes(str(reservation["host_request_json"]).encode("utf-8"))
        if not isinstance(host_request, Mapping):
            raise ContractError(ErrorCode.ASSET_ERROR, "stored Host request is invalid")
        self._bind_provider_request(authority, host_request, provider_request)
        if self.provider_port is None:
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "verified Host Provider adapter is unavailable",
            )
        receipt_verifier = self.provider_port.receipt_verifier_for(
            authority.command(host_request)
        )
        evidence = self._terminal_evidence(
            authority,
            reservation,
            provider_request,
            provider_success,
            receipt_verifier,
        )
        if dict(evidence.host_result) != host_result or evidence.terminal["state"] != record.state:
            raise ContractError(ErrorCode.ASSET_ERROR, "terminal Host result drifted from its ledger")
        for terminal_field, value in evidence.terminal.items():
            expected = None if value is None else (
                canonical_json_text(value)
                if terminal_field
                in {
                    "provider_success_json",
                    "host_result_json",
                    "rpc_error_json",
                    "uncertainty_json",
                }
                else value
            )
            if record.values.get(terminal_field) != expected:
                raise ContractError(ErrorCode.ASSET_ERROR, "terminal model invocation locator drifted")
        validate_rpc_result(HOST_MODEL_METHOD, host_result, request=host_request)
        return PreparedModelInvocation(
            MappingProxyType(dict(host_result)), lambda: None, lambda: None, True
        )

    def prepare_host_invocation(
        self, request: Mapping[str, Any]
    ) -> PreparedModelInvocation:
        """Run T1/T2 and one Provider call; return a T3-before-ACK callback."""

        host_request, meta, params = self._host_parts(request)
        if self.provider_port is None:
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "verified Host Provider adapter is unavailable",
            )

        # Read and validate the full secret-free authority before preparing a
        # factory binding.  No durable T1 row exists until that binding, its
        # matching receipt decoder, and its P0B request have all been checked.
        with self.repository.transaction() as connection:
            current = self._current_authority(connection, host_request, meta, params)
            reservation = current.reservation(host_request)
            record = self.ledger.lookup(
                connection,
                context_identity=current.context_identity,
                operation_key=str(params["operation_key"]),
            )
            if record is not None:
                record.assert_reservation(reservation)
        if record is not None and record.terminal:
            return self._replay_terminal(current, reservation, record)
        if record is not None and record.state == "dispatching":
            raise _uncertain_contract_error()

        command = current.command(host_request)
        prepared = self.provider_port.prepare(command)
        try:
            provider_request = self._bind_provider_request(
                current,
                host_request,
                self.provider_port.provider_request(prepared),
            )
            with self.repository.transaction() as connection:
                current_at_t1 = self._current_authority(
                    connection, host_request, meta, params
                )
                self._assert_same_authority(current, current_at_t1, host_request)
                self._bind_provider_request(
                    current_at_t1, host_request, provider_request
                )
                record = self.ledger.lookup(
                    connection,
                    context_identity=current.context_identity,
                    operation_key=str(params["operation_key"]),
                )
                if record is None:
                    record, _created = self.ledger.reserve(connection, reservation)
                else:
                    record.assert_reservation(reservation)
            self._phase("after_t1")
            if record.terminal:
                self.provider_port.discard_preparation(prepared)
                return self._replay_terminal(current, reservation, record)
            if record.state == "dispatching":
                self.provider_port.discard_preparation(prepared)
                raise _uncertain_contract_error()

            with self.repository.transaction() as connection:
                current_at_barrier = self._current_authority(
                    connection, host_request, meta, params
                )
                self._assert_same_authority(current, current_at_barrier, host_request)
                self._bind_provider_request(
                    current_at_barrier, host_request, provider_request
                )
                record, dispatch_owner = self.ledger.mark_dispatching(
                    connection, reservation, provider_request
                )
            if not dispatch_owner:
                self.provider_port.discard_preparation(prepared)
                if record.terminal:
                    return self._replay_terminal(current, reservation, record)
                raise _uncertain_contract_error()
            self._phase("after_t2")
        except BaseException:
            self.provider_port.discard_preparation(prepared)
            raise

        try:
            sealed_exchange = self.provider_port.invoke(prepared)
            exchange = self.provider_port.open_verified_exchange(sealed_exchange)
            if (
                not isinstance(exchange, ProviderInvocationExchange)
                or not callable(exchange.receipt_verifier)
            ):
                raise ContractValidationError(
                    "Host Provider adapter opened an invalid exchange"
                )
        except Exception:  # noqa: BLE001 - any adapter failure is post-barrier uncertainty
            self._close_receiptless(reservation, recovered=False)
            raise _uncertain_contract_error() from None
        self._phase("after_provider")

        response = dict(exchange.response)
        if "error" in response:
            try:
                parse_model_provider_rpc_error_v2(response, request=provider_request)
            except Exception:  # noqa: BLE001 - malformed and valid errors are both receipt-less
                self._close_receiptless(reservation, recovered=False)
                raise _uncertain_contract_error() from None
            self._close_receiptless(reservation, recovered=False)
            raise _uncertain_contract_error()
        try:
            if set(response) != {"jsonrpc", "id", "result"}:
                raise ContractValidationError("Provider success envelope shape drifted")
            provider_result = parse_model_provider_invoke_result_v2(response["result"])
            response_identity = provider_result["response_asset"]
            if (response_identity is None) != (exchange.response_asset_bytes is None):
                raise ContractValidationError("Provider response Asset bytes/identity drifted")
            if exchange.receipt_asset_bytes is None:
                raise ContractValidationError("Provider success lacks canonical receipt Asset bytes")
            self._validate_safe_json_asset(
                exchange.receipt_asset_bytes, label="model_receipt"
            )
            if exchange.response_asset_bytes is not None:
                self._validate_safe_json_asset(
                    exchange.response_asset_bytes, label="model_response"
                )
        except Exception:  # noqa: BLE001 - invalid post-barrier evidence is uncertain
            self._close_receiptless(reservation, recovered=False)
            raise _uncertain_contract_error() from None

        if response_identity is not None:
            try:
                response_asset_id = self.assets.create_asset(
                    exchange.response_asset_bytes or b"",
                    mime=_MODEL_ASSET_MIME,
                )
                response_asset = self.assets.require(
                    response_asset_id, mime=_MODEL_ASSET_MIME
                )
                if (
                    response_asset.asset_id != response_identity["asset_id"]
                    or response_asset.sha256 != response_identity["content_hash"]
                ):
                    raise ContractValidationError("Provider response Asset identity drifted")
            except Exception:  # noqa: BLE001 - failed Asset publication is uncertain
                self._close_receiptless(reservation, recovered=False)
                raise _uncertain_contract_error() from None
        self._phase("after_response_asset")

        anchor = provider_result["model_receipt_anchor"]
        try:
            receipt_asset_id = self.assets.create_asset(
                exchange.receipt_asset_bytes,
                mime=_MODEL_ASSET_MIME,
            )
            receipt_asset = self.assets.require(
                receipt_asset_id, mime=_MODEL_ASSET_MIME
            )
            if (
                receipt_asset.asset_id != anchor["asset_id"]
                or receipt_asset.sha256 != anchor["content_hash"]
            ):
                raise ContractValidationError("ModelReceipt Asset identity drifted")
        except Exception:  # noqa: BLE001 - invalid post-barrier evidence is uncertain
            self._close_receiptless(reservation, recovered=False)
            raise _uncertain_contract_error() from None
        self._phase("after_receipt_asset")

        try:
            evidence = self._terminal_evidence(
                current,
                reservation,
                provider_request,
                response,
                exchange.receipt_verifier,
            )
        except Exception:  # noqa: BLE001 - invalid post-barrier evidence is uncertain
            self._close_receiptless(reservation, recovered=False)
            raise _uncertain_contract_error() from None
        self._phase("before_t3")
        result = _copy_mapping(evidence.host_result, "prepared Host result")
        commit = _TerminalCommit(self, evidence, result)
        return PreparedModelInvocation(
            MappingProxyType(result), commit, commit.abort, False
        )

    invoke = prepare_host_invocation

    def recover_dispatching(self) -> int:
        return self.ledger.recover_dispatching()


__all__ = [
    "HostProviderAdapterPort",
    "ModelBroker",
    "PhaseHook",
    "PreparedModelInvocation",
    "ProviderInvocationCommand",
    "ProviderInvocationExchange",
    "ProviderInvocationPreparation",
    "ReceiptProofVerifier",
]
