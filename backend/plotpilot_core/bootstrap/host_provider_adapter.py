"""Trusted Host boundary for one durable Model Provider dispatch.

The Core Model Broker deliberately never receives a local configuration value.
This module owns the only resolver call, the exact installed-factory binding,
Provider invocation, dynamic-output reflection gate, and runtime-local seal.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from backend.plotpilot_core.configuration import ModelConfigurationAuthority
from backend.plotpilot_core.model.broker import (
    ProviderInvocationCommand,
    ProviderInvocationExchange,
    ProviderInvocationPreparation,
)
from backend.plotpilot_core.model.invocation import HOST_MODEL_METHOD, canonical_json_text
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    ErrorCode,
    canonical_bytes,
    parse_json_bytes,
)
from backend.plotpilot_plugin_sdk.model_provider_rpc_v2 import (
    parse_model_provider_invoke_request_v2,
    parse_model_provider_invoke_result_v2,
    parse_model_provider_rpc_error_v2,
    validate_model_provider_rpc_success_v2,
)

_MODEL_RESPONSE_SCHEMA = "model-provider-response-asset/v1"
_MODEL_RECEIPT_SCHEMA = "model-receipt/v1"
_MODEL_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "receipt_id",
        "invocation_id",
        "invocation_key",
        "state",
        "request_hash",
        "response_hash",
        "profile_revision_id",
        "provider_plugin_id",
        "provider_release_id",
        "endpoint",
        "model",
        "lifecycle",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost",
        "retry_count",
        "stream_termination",
        "error",
        "input_context",
        "profile_revision",
        "metadata",
        "response_asset_id",
        "recovered",
        "uncertain",
        "receipt_hash",
    }
)
_SENSITIVE_PARTS = (
    "secret_value",
    "api_key",
    "authorization",
    "password",
    "credential",
)
_SENSITIVE_EXACT = frozenset({"token", "access_token", "secret", "key"})
_TOKEN_OPTION_NAMES = frozenset(
    {"max_tokens", "max_completion_tokens", "max_output_tokens"}
)


def _copy_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{label} must be an object")
    try:
        decoded = parse_json_bytes(canonical_bytes(dict(value)))
    except Exception as exc:
        raise ContractValidationError(
            f"{label} must contain finite JSON values"
        ) from exc
    if not isinstance(decoded, dict):
        raise ContractValidationError(f"{label} must be an object")
    return decoded


def _canonical_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = parse_json_bytes(raw)
    except Exception as exc:
        raise ContractValidationError(f"{label} is not canonical UTF-8 JSON") from exc
    if not isinstance(value, Mapping) or canonical_bytes(value) != raw:
        raise ContractValidationError(f"{label} is not a canonical JSON object")
    return dict(value)


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
                raise ContractValidationError(
                    f"{path} contains a secret-bearing field"
                )
            _assert_no_secret_fields(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_no_secret_fields(child, path=f"{path}[{index}]")


def _contains_reflection(value: Any, one_shot_value: str) -> bool:
    if isinstance(value, str):
        return one_shot_value in value
    if isinstance(value, Mapping):
        return any(
            one_shot_value in key or _contains_reflection(child, one_shot_value)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_reflection(child, one_shot_value) for child in value)
    return False


def _asset_digest(raw: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(raw).hexdigest()
    return f"asset-sha256-{digest}", digest


def _member(
    generation: Mapping[str, Any], plugin_id: str
) -> Mapping[str, Any] | None:
    matches = [
        item
        for item in generation.get("members", ())
        if isinstance(item, Mapping) and item.get("plugin_id") == plugin_id
    ]
    return matches[0] if len(matches) == 1 else None


def _profile_projection(
    command: ProviderInvocationCommand,
    chat_options: Mapping[str, Any],
) -> dict[str, Any]:
    profile = command.model_profile_revision
    provider = profile["provider"]
    return {
        "profile_revision_id": profile["revision_id"],
        "provider_plugin_id": provider["plugin_id"],
        "provider_release_id": provider["release_id"],
        "endpoint": provider["endpoint"],
        "model_name": provider["model_name"],
        "api_key_ref": provider["api_key_ref"],
        "max_retries": provider["options"]["max_retries"],
        "parameters": _copy_mapping(chat_options, "Chat Completions options"),
    }


def project_chat_completions_options(
    command: ProviderInvocationCommand,
) -> tuple[bool, Mapping[str, Any]]:
    """Project P0A/request options onto one fixed Chat Completions body.

    ``timeout_seconds`` is intentionally absent (transport-only),
    ``max_retries`` is intentionally absent (Provider policy-only), and every
    accepted token spelling is normalized to the one ``max_tokens`` field.
    """

    request_asset = _canonical_object(command.model_request_bytes, "model request Asset")
    request_options = _copy_mapping(request_asset.get("options", {}), "model request options")
    profile_options = _copy_mapping(
        command.model_profile_revision["provider"]["options"],
        "Model Profile options",
    )
    max_output_tokens = profile_options["max_output_tokens"]
    supplied_token_values = {
        request_options[name]
        for name in _TOKEN_OPTION_NAMES
        if name in request_options
    }
    if supplied_token_values and supplied_token_values != {max_output_tokens}:
        raise ContractValidationError(
            "model request token limit conflicts with the pinned Model Profile"
        )
    stream = request_options.pop("stream", False)
    if type(stream) is not bool:
        raise ContractValidationError("model request stream option must be boolean")
    for name in _TOKEN_OPTION_NAMES:
        request_options.pop(name, None)
    if "timeout_seconds" in request_options or "max_retries" in request_options:
        raise ContractValidationError(
            "model request cannot override transport or Provider retry authority"
        )
    parameters: dict[str, Any] = {
        "temperature": profile_options["temperature"],
        "top_p": profile_options["top_p"],
        "max_tokens": max_output_tokens,
    }
    parameters.update(request_options)
    _assert_no_secret_fields(parameters, path="chat_completions.options")
    return stream, MappingProxyType(_copy_mapping(parameters, "Chat Completions options"))


@dataclass(frozen=True, slots=True, repr=False)
class ProviderFactoryRequest:
    """Exact Generation identities that an installed factory must bind."""

    generation_id: str
    provider_plugin_id: str
    provider_release_id: str
    provider_package_hash: str
    verifier_plugin_id: str
    verifier_release_id: str
    verifier_package_hash: str


@dataclass(frozen=True, slots=True, repr=False)
class ProviderFactoryBinding:
    """Verified installed Provider factory plus its matching receipt decoder."""

    generation_id: str
    provider_plugin_id: str
    provider_release_id: str
    provider_package_hash: str
    verifier_plugin_id: str
    verifier_release_id: str
    verifier_package_hash: str
    provider_factory: Callable[[Any], Any] = field(repr=False, compare=False)
    receipt_verifier: Callable[[Mapping[str, Any]], Any] = field(
        repr=False, compare=False
    )


class ProviderFactoryPort(Protocol):
    """Host registry for already-installed, package-hash-verified factories."""

    def bind(self, request: ProviderFactoryRequest) -> ProviderFactoryBinding | None: ...


class ProviderInvocationPort(Protocol):
    """Factory-produced adapter around the accepted Provider implementation."""

    def prepare(
        self,
        command: ProviderInvocationCommand,
        *,
        provider_profile: Mapping[str, Any],
        stream: bool,
    ) -> ProviderInvocationPreparation: ...

    def invoke(
        self, preparation: ProviderInvocationPreparation, *, api_key: str
    ) -> ProviderInvocationExchange: ...


class HostProviderPreparation:
    """Opaque runtime-owned preparation handle."""

    __slots__ = ()

    def __new__(cls) -> HostProviderPreparation:
        raise TypeError("Host Provider preparations are minted by their runtime")


class HostVerifiedProviderExchange:
    """Marker for a runtime-local sealed and already-screened exchange."""

    __slots__ = ()

    def __new__(cls) -> HostVerifiedProviderExchange:
        raise TypeError("Host verified exchanges are minted by their runtime")


class HostProviderAdapterError(RuntimeError):
    """Non-sensitive post-dispatch adapter failure."""


class HostTransportError(RuntimeError):
    """Transport failure with the accepted explicit send-state evidence."""

    def __init__(
        self,
        message: str,
        *,
        send_state: str = "unknown",
        transient: bool = False,
    ) -> None:
        if send_state not in {"sent", "not_sent", "unknown"}:
            raise ValueError("send_state must be sent, not_sent, or unknown")
        self.send_state = send_state
        self.transient = bool(transient)
        super().__init__(message)


class HttpxChatCompletionsTransport:
    """One direct httpx Chat Completions POST with no fallback or retry."""

    supports_query = False

    def __init__(
        self,
        timeout_seconds: int,
        *,
        client_factory: Callable[..., Any] = httpx.Client,
    ) -> None:
        if type(timeout_seconds) is not int or timeout_seconds < 1:
            raise ValueError("timeout_seconds must be a positive integer")
        if not callable(client_factory):
            raise TypeError("client_factory must be callable")
        self.timeout_seconds = timeout_seconds
        self._client_factory = client_factory
        self._states: dict[str, str] = {}
        self.sent_keys: set[str] = set()
        self.cancelled_keys: set[str] = set()

    @staticmethod
    def _endpoint(value: Any) -> str:
        if not isinstance(value, str):
            raise HostTransportError("Provider endpoint is invalid", send_state="not_sent")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HostTransportError("Provider endpoint is invalid", send_state="not_sent")
        if parsed.query or parsed.fragment:
            raise HostTransportError(
                "Provider endpoint cannot contain query or fragment",
                send_state="not_sent",
            )
        path = parsed.path
        if not path.endswith("/chat/completions"):
            path = f"{path.rstrip('/')}/chat/completions"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    @staticmethod
    def _value(request: Any, name: str) -> Any:
        if isinstance(request, Mapping):
            return request[name]
        return getattr(request, name)

    @staticmethod
    def _classify(error: BaseException) -> tuple[str, bool]:
        explicit_state = getattr(error, "send_state", None)
        explicit_transient = getattr(error, "transient", None)
        if explicit_state in {"sent", "not_sent", "unknown"}:
            return explicit_state, type(explicit_transient) is bool and explicit_transient
        if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return "not_sent", True
        return "unknown", isinstance(error, httpx.TimeoutException)

    def send(self, request: Any) -> Any:
        key = self._value(request, "invocation_key")
        if not isinstance(key, str) or not key:
            raise HostTransportError("Provider invocation key is invalid", send_state="not_sent")
        endpoint = self._endpoint(self._value(request, "endpoint"))
        body = self._value(request, "body")
        headers = self._value(request, "headers")
        if not isinstance(body, Mapping) or not isinstance(headers, Mapping):
            raise HostTransportError("Provider request is invalid", send_state="not_sent")
        self._states[key] = "unknown"
        client = self._client_factory(
            trust_env=False,
            follow_redirects=False,
            timeout=self.timeout_seconds,
        )
        try:
            response = client.post(
                endpoint,
                json=_copy_mapping(body, "Provider transport body"),
                headers={str(name): str(value) for name, value in headers.items()},
            )
        except BaseException as exc:
            state, transient = self._classify(exc)
            self._states[key] = state
            raise HostTransportError(
                "Chat Completions transport failed",
                send_state=state,
                transient=transient,
            ) from None
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        self._states[key] = "sent"
        self.sent_keys.add(key)
        return response

    def send_state(self, invocation_key: str) -> str:
        return self._states.get(invocation_key, "unknown")

    def cancel(self, invocation_key: str) -> None:
        self.cancelled_keys.add(str(invocation_key))


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedRecord:
    command: ProviderInvocationCommand
    factory_request: ProviderFactoryRequest
    binding: ProviderFactoryBinding = field(repr=False)
    provider_port: Any = field(repr=False)
    provider_preparation: ProviderInvocationPreparation = field(repr=False)
    provider_request: Mapping[str, Any]
    profile_projection: Mapping[str, Any]


def _payload_digest(payload: ProviderInvocationExchange) -> str:
    material = bytearray(canonical_bytes(dict(payload.response)))
    for raw in (payload.receipt_asset_bytes, payload.response_asset_bytes):
        if raw is None:
            material.extend(b"\xff")
        else:
            material.extend(len(raw).to_bytes(8, "big"))
            material.extend(raw)
    return hashlib.sha256(material).hexdigest()


def _exchange_seal() -> tuple[
    Callable[[ProviderInvocationExchange], HostVerifiedProviderExchange],
    Callable[[Any], ProviderInvocationExchange],
]:
    runtime_token = object()
    pending: dict[object, tuple[Any, str, int]] = {}
    lock = threading.Lock()

    @dataclass(frozen=True, slots=True, repr=False)
    class _RuntimeExchange(HostVerifiedProviderExchange):
        token: object = field(repr=False, compare=False)
        nonce: object = field(repr=False, compare=False)
        payload: ProviderInvocationExchange = field(repr=False, compare=False)
        digest: str

    def mint(payload: ProviderInvocationExchange) -> HostVerifiedProviderExchange:
        nonce = object()
        digest = _payload_digest(payload)
        sealed = object.__new__(_RuntimeExchange)
        object.__setattr__(sealed, "token", runtime_token)
        object.__setattr__(sealed, "nonce", nonce)
        object.__setattr__(sealed, "payload", payload)
        object.__setattr__(sealed, "digest", digest)
        with lock:
            pending[nonce] = (sealed, digest, id(payload.receipt_verifier))
        return sealed

    def open_exchange(value: Any) -> ProviderInvocationExchange:
        if type(value) is not _RuntimeExchange or value.token is not runtime_token:
            raise TypeError("Host verified exchange belongs to another runtime")
        with lock:
            expected = pending.pop(value.nonce, None)
        if expected is None or expected[0] is not value:
            raise TypeError("Host verified exchange was forged, copied, or already consumed")
        payload = value.payload
        if (
            not isinstance(payload, ProviderInvocationExchange)
            or value.digest != expected[1]
            or _payload_digest(payload) != expected[1]
            or id(payload.receipt_verifier) != expected[2]
        ):
            raise TypeError("Host verified exchange payload was replaced")
        return payload

    return mint, open_exchange


class HostProviderAdapter:
    """Resolve, invoke, screen, and seal outside the Core Broker boundary."""

    def __init__(
        self,
        configuration: ModelConfigurationAuthority,
        factory_port: ProviderFactoryPort,
        *,
        transport_factory: Callable[[int], Any] | None = None,
    ) -> None:
        if not isinstance(configuration, ModelConfigurationAuthority):
            raise TypeError("HostProviderAdapter requires ModelConfigurationAuthority")
        if not callable(getattr(factory_port, "bind", None)):
            raise TypeError("ProviderFactoryPort must expose bind")
        if transport_factory is not None and not callable(transport_factory):
            raise TypeError("transport_factory must be callable")
        self.repository = configuration.repository
        self._configuration = configuration
        self._factory_port = factory_port
        self._transport_factory = transport_factory or HttpxChatCompletionsTransport
        self._prepared: dict[HostProviderPreparation, _PreparedRecord] = {}
        self._prepared_lock = threading.Lock()
        self._mint_exchange, self._open_exchange = _exchange_seal()

    @staticmethod
    def _factory_request(command: ProviderInvocationCommand) -> ProviderFactoryRequest:
        provider = command.model_profile_revision["provider"]
        provider_member = _member(command.generation, str(provider["plugin_id"]))
        verifier_member = _member(command.generation, command.caller_plugin_id)
        if (
            command.generation.get("generation_id") != command.generation_id
            or provider_member is None
            or verifier_member is None
            or provider_member.get("release_id") != provider["release_id"]
            or verifier_member.get("release_id") != command.caller_plugin_release_id
            or verifier_member.get("package_hash")
            != command.caller_plugin_package_hash
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "Host Provider factory cannot bind the pinned Generation",
            )
        return ProviderFactoryRequest(
            generation_id=command.generation_id,
            provider_plugin_id=str(provider["plugin_id"]),
            provider_release_id=str(provider["release_id"]),
            provider_package_hash=str(provider_member["package_hash"]),
            verifier_plugin_id=command.caller_plugin_id,
            verifier_release_id=command.caller_plugin_release_id,
            verifier_package_hash=command.caller_plugin_package_hash,
        )

    def _binding(
        self, request: ProviderFactoryRequest
    ) -> ProviderFactoryBinding:
        try:
            binding = self._factory_port.bind(request)
        except Exception:
            binding = None
        if (
            type(binding) is not ProviderFactoryBinding
            or any(
                getattr(binding, name) != getattr(request, name)
                for name in (
                    "generation_id",
                    "provider_plugin_id",
                    "provider_release_id",
                    "provider_package_hash",
                    "verifier_plugin_id",
                    "verifier_release_id",
                    "verifier_package_hash",
                )
            )
            or not callable(binding.provider_factory)
            or not callable(binding.receipt_verifier)
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "verified Provider factory and receipt decoder are unavailable",
            )
        return binding

    @staticmethod
    def _bind_provider_request(
        command: ProviderInvocationCommand,
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        request = parse_model_provider_invoke_request_v2(value)
        context = request["params"]["planner_context"]
        expected = {
            "operation_key": command.operation_key,
            "workspace_id": command.workspace_id,
            "plugin_id": command.caller_plugin_id,
            "plugin_release_id": command.caller_plugin_release_id,
            "plugin_package_hash": command.caller_plugin_package_hash,
            "generation_id": command.generation_id,
            "job_id": command.job_id,
            "step_id": command.step_id,
            "attempt_id": command.attempt_id,
            "lease_epoch": command.lease_epoch,
            "run_snapshot_id": command.run_snapshot_id,
            "run_snapshot_asset_id": command.run_snapshot_asset_id,
            "run_snapshot_hash": command.run_snapshot_hash,
            "model_profile_revision_id": command.model_profile_revision["revision_id"],
        }
        if any(context.get(name) != expected_value for name, expected_value in expected.items()):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Provider preparation drifted from Host authority",
            )
        hashes = {
            item["asset_id"]: item["sha256"]
            for item in command.run_snapshot["asset_hashes"]
        }
        if (
            hashes.get(context.get("chain_asset_id"))
            != context.get("chain_content_hash")
            or hashes.get(context.get("input_asset_id"))
            != context.get("input_content_hash")
            or request["params"]["model_request_asset"]
            != dict(command.model_request_asset)
        ):
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "Provider preparation escaped frozen Host Assets",
            )
        _assert_no_secret_fields(request, path="provider_request")
        return request

    def prepare(self, command: ProviderInvocationCommand) -> HostProviderPreparation:
        if not isinstance(command, ProviderInvocationCommand):
            raise TypeError("HostProviderAdapter requires ProviderInvocationCommand")
        factory_request = self._factory_request(command)
        binding = self._binding(factory_request)
        provider_options = command.model_profile_revision["provider"]["options"]
        timeout_seconds = provider_options["timeout_seconds"]
        try:
            stream, chat_options = project_chat_completions_options(command)
            profile_projection = _profile_projection(command, chat_options)
            transport = self._transport_factory(timeout_seconds)
            provider_port = binding.provider_factory(transport)
        except Exception:
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "verified Provider factory could not be instantiated",
            ) from None
        if not callable(getattr(provider_port, "prepare", None)) or not callable(
            getattr(provider_port, "invoke", None)
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "verified Provider factory returned no invocation adapter",
            )
        try:
            provider_preparation = provider_port.prepare(
                command,
                provider_profile=profile_projection,
                stream=stream,
            )
            if not isinstance(provider_preparation, ProviderInvocationPreparation):
                raise TypeError("invalid Provider preparation")
            provider_request = self._bind_provider_request(
                command, provider_preparation.provider_request
            )
        except ContractError:
            raise
        except Exception:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Provider preparation is invalid",
            ) from None
        record = _PreparedRecord(
            command=command,
            factory_request=factory_request,
            binding=binding,
            provider_port=provider_port,
            provider_preparation=provider_preparation,
            provider_request=MappingProxyType(provider_request),
            profile_projection=MappingProxyType(profile_projection),
        )
        handle = object.__new__(HostProviderPreparation)
        with self._prepared_lock:
            self._prepared[handle] = record
        return handle

    def provider_request(
        self, preparation: HostProviderPreparation
    ) -> Mapping[str, Any]:
        with self._prepared_lock:
            record = self._prepared.get(preparation)
        if record is None:
            raise TypeError("Host Provider preparation is forged or already consumed")
        return MappingProxyType(_copy_mapping(record.provider_request, "Provider request"))

    def discard_preparation(self, preparation: HostProviderPreparation) -> None:
        with self._prepared_lock:
            self._prepared.pop(preparation, None)

    def _assert_durable_t2(self, record: _PreparedRecord) -> None:
        command = record.command
        with self.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT state,provider_request_json,invocation_id,invocation_key "
                "FROM model_invocation WHERE context_identity=? AND method=? "
                "AND operation_key=?",
                (command.context_identity, HOST_MODEL_METHOD, command.operation_key),
            ).fetchone()
        if (
            row is None
            or row["state"] != "dispatching"
            or row["provider_request_json"]
            != canonical_json_text(record.provider_request)
            or row["invocation_id"] != command.invocation_id
            or row["invocation_key"] != command.invocation_key
        ):
            raise HostProviderAdapterError(
                "durable Model Provider dispatch barrier is unavailable"
            )

    @staticmethod
    def _expected_receipt_context(
        provider_request: Mapping[str, Any],
    ) -> dict[str, Any]:
        context = dict(provider_request["params"]["planner_context"])
        context["input_hash"] = context.pop("input_content_hash")
        return context

    @staticmethod
    def _validate_dynamic_output(
        record: _PreparedRecord,
        exchange: ProviderInvocationExchange,
        one_shot_value: str,
    ) -> None:
        response = _copy_mapping(exchange.response, "Provider response")
        if "error" in response:
            parsed_error = parse_model_provider_rpc_error_v2(
                response, request=record.provider_request
            )
            if _contains_reflection(parsed_error["error"], one_shot_value):
                raise ContractValidationError("Provider error reflected Host-local data")
            return
        if set(response) != {"jsonrpc", "id", "result"}:
            raise ContractValidationError("Provider success envelope shape drifted")
        result = parse_model_provider_invoke_result_v2(response["result"])
        if exchange.receipt_asset_bytes is None:
            raise ContractValidationError("Provider success lacks receipt bytes")
        receipt = _canonical_object(exchange.receipt_asset_bytes, "ModelReceipt")
        response_asset = None
        if exchange.response_asset_bytes is not None:
            response_asset = _canonical_object(
                exchange.response_asset_bytes, "model response Asset"
            )
        response_identity = result["response_asset"]
        if (response_identity is None) != (response_asset is None):
            raise ContractValidationError("Provider response Asset identity drifted")
        if response_asset is not None:
            asset_id, digest = _asset_digest(exchange.response_asset_bytes or b"")
            if response_identity != {"asset_id": asset_id, "content_hash": digest}:
                raise ContractValidationError("Provider response Asset hash drifted")
            dynamic_response = dict(response_asset)
            schema = dynamic_response.pop("schema", None)
            if schema not in {_MODEL_RESPONSE_SCHEMA, _MODEL_RECEIPT_SCHEMA}:
                if _contains_reflection(schema, one_shot_value):
                    raise ContractValidationError("Provider response schema reflected Host-local data")
            if _contains_reflection(dynamic_response, one_shot_value):
                raise ContractValidationError("Provider response reflected Host-local data")
        if set(receipt) != _MODEL_RECEIPT_FIELDS or receipt.get("schema") != _MODEL_RECEIPT_SCHEMA:
            raise ContractValidationError("ModelReceipt shape drifted")
        expected_context = HostProviderAdapter._expected_receipt_context(
            record.provider_request
        )
        expected_profile = dict(record.profile_projection)
        fixed = {
            "invocation_id": record.command.invocation_id,
            "invocation_key": record.command.invocation_key,
            "state": result["provider_terminal_state"],
            "request_hash": result["provider_transport_request_hash"],
            "response_hash": result["provider_transport_response_hash"],
            "profile_revision_id": record.command.model_profile_revision["revision_id"],
            "provider_plugin_id": record.factory_request.provider_plugin_id,
            "provider_release_id": record.factory_request.provider_release_id,
            "endpoint": record.command.model_profile_revision["provider"]["endpoint"],
            "model": record.command.model_profile_revision["provider"]["model_name"],
            "input_context": expected_context,
            "profile_revision": expected_profile,
            "response_asset_id": (
                None if response_identity is None else response_identity["asset_id"]
            ),
            "uncertain": result["provider_terminal_state"] == "uncertain",
        }
        if any(receipt.get(name) != expected for name, expected in fixed.items()):
            raise ContractValidationError("ModelReceipt drifted from Host authority")
        max_retries = record.command.model_profile_revision["provider"]["options"][
            "max_retries"
        ]
        if (
            type(receipt.get("retry_count")) is not int
            or receipt["retry_count"] < 0
            or receipt["retry_count"] > max_retries
        ):
            raise ContractValidationError("ModelReceipt retry count drifted")
        dynamic_receipt = {
            name: receipt[name]
            for name in (
                "lifecycle",
                "cost",
                "stream_termination",
                "error",
                "metadata",
            )
        }
        if _contains_reflection(dynamic_receipt, one_shot_value):
            raise ContractValidationError("ModelReceipt reflected Host-local data")
        _assert_no_secret_fields(receipt, path="model_receipt")
        validate_model_provider_rpc_success_v2(
            response,
            request=record.provider_request,
            canonical_receipt=receipt,
        )

    def invoke(
        self, preparation: HostProviderPreparation
    ) -> HostVerifiedProviderExchange:
        with self._prepared_lock:
            record = self._prepared.pop(preparation, None)
        if record is None:
            raise HostProviderAdapterError(
                "Host Provider preparation was forged, crossed runtimes, or was reused"
            )
        self._assert_durable_t2(record)
        one_shot_value: str | None = None
        try:
            one_shot_value = self._configuration.resolve_secret_value(
                str(record.command.model_profile_revision["provider"]["api_key_ref"])
            )
            if not isinstance(one_shot_value, str) or not one_shot_value:
                raise TypeError("invalid local value")
            exchange = record.provider_port.invoke(
                record.provider_preparation,
                api_key=one_shot_value,
            )
            if not isinstance(exchange, ProviderInvocationExchange):
                raise TypeError("invalid Provider exchange")
            self._validate_dynamic_output(record, exchange, one_shot_value)
            verified = ProviderInvocationExchange(
                exchange.response,
                exchange.receipt_asset_bytes,
                exchange.response_asset_bytes,
                receipt_verifier=record.binding.receipt_verifier,
            )
            return self._mint_exchange(verified)
        except BaseException:
            raise HostProviderAdapterError(
                "Host Provider invocation could not produce a verified exchange"
            ) from None
        finally:
            one_shot_value = None

    def open_verified_exchange(self, value: Any) -> ProviderInvocationExchange:
        return self._open_exchange(value)

    def receipt_verifier_for(
        self, command: ProviderInvocationCommand
    ) -> Callable[[Mapping[str, Any]], Any]:
        request = self._factory_request(command)
        return self._binding(request).receipt_verifier


def build_host_provider_adapter(
    configuration: ModelConfigurationAuthority,
    factory_port: ProviderFactoryPort | None,
    *,
    transport_factory: Callable[[int], Any] | None = None,
) -> HostProviderAdapter | None:
    """Return no adapter until an installed factory registry is supplied."""

    if factory_port is None or not callable(getattr(factory_port, "bind", None)):
        return None
    return HostProviderAdapter(
        configuration,
        factory_port,
        transport_factory=transport_factory,
    )


__all__ = [
    "HostProviderAdapter",
    "HostProviderAdapterError",
    "HostProviderPreparation",
    "HostTransportError",
    "HostVerifiedProviderExchange",
    "HttpxChatCompletionsTransport",
    "ProviderFactoryBinding",
    "ProviderFactoryPort",
    "ProviderFactoryRequest",
    "ProviderInvocationPort",
    "build_host_provider_adapter",
    "project_chat_completions_options",
]
