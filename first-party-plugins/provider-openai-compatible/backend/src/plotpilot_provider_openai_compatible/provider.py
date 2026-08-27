"""OpenAI-compatible model Provider implementation.

This module is intentionally a small, SDK-only adapter.  It accepts a
generic ModelProfileRevision-like mapping/dataclass, creates an immutable
OpenAI chat/completions request, and delegates all I/O to an injected
transport.  It has no Core database, Job, Candidate or Publication imports.

The important boundary is the send uncertainty window:

* an error before the transport explicitly sends is ``failed``;
* an error after send but before a response is ``uncertain``;
* only an explicitly query-capable transport may recover that window, and it
  is queried with the same invocation key rather than sending a second
  completion request.
"""
from __future__ import annotations

import dataclasses
import inspect
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

try:  # Host/plugin installs expose the SDK as a top-level package.
    from plotpilot_plugin_sdk import canonical_bytes, hash_jcs, parse_json_bytes, sha256_hex
except ModuleNotFoundError:  # Repository-root tests expose it under ``backend``.
    from backend.plotpilot_plugin_sdk import canonical_bytes, hash_jcs, parse_json_bytes, sha256_hex

from .transport import (
    OpenAICompatibleTransport,
    ProviderTransportError,
    TransportError,
    TransportRequest,
    TransportResponse,
    freeze_value,
    thaw_value,
)


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_MAX_ERROR_LENGTH = 512
_REPLAY_POLICIES = frozenset({"idempotent_auto", "manual_if_unknown", "never_replay"})
_NO_RECEIPT_SINK = object()
_SENSITIVE_KEY_PARTS = ("secret_value", "api_key", "authorization", "password", "credential")
_SENSITIVE_EXACT_KEYS = frozenset({"token", "access_token", "secret", "key"})
_RAW_SECRET_PREFIXES = ("sk-", "sk_", "bearer ", "ghp_", "github_pat_", "xoxb-", "xoxp-", "aiza")
_MODEL_PARAMETER_FIELDS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "seed",
    "response_format",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "logprobs",
    "top_logprobs",
    "n",
    "user",
    "modalities",
    "reasoning_effort",
)
_FORBIDDEN_PARAMETER_KEYS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "endpoint",
        "headers",
        "authorization",
        "api_key",
        "api_key_ref",
        "api_key_reference",
        "secret_ref",
        "secret",
        "password",
        "token",
    }
)


class ProviderError(ValueError):
    """A provider-side input, response or lifecycle error."""


class InvalidProfileError(ProviderError):
    """The generic profile cannot be converted to a safe provider request."""


class MalformedResponseError(ProviderError):
    """The transport response is not a bounded OpenAI-compatible response."""


class UnsupportedReplayError(ProviderError):
    """A caller requested recovery without an explicitly query-capable transport."""


class _CancellationRequested(Exception):
    def __init__(self, parsed: "_ParsedResponse") -> None:
        self.parsed = parsed
        super().__init__("provider invocation cancelled")


@runtime_checkable
class SecretProvider(Protocol):
    """Host secret lookup protocol; returned values are used once and not stored."""

    def get_secret(self, secret_ref: str) -> str | None:
        ...


class CancellationToken:
    """Thread-safe cancellation token accepted by sync deterministic streams."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def __repr__(self) -> str:
        return f"CancellationToken(cancelled={self.cancelled!r})"


def _json_value(value: Any) -> Any:
    """Make a plain JSON value while preserving strict type failures."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            result[key] = _json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _immutable_json(value: Any) -> Any:
    return freeze_value(_json_value(value))


def _copy_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    for method_name in ("to_mapping", "as_mapping", "to_dict", "as_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            candidate = method()
            if isinstance(candidate, Mapping):
                return dict(candidate)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        candidate = dataclasses.asdict(value)
        if isinstance(candidate, Mapping):
            return dict(candidate)
    try:
        candidate = vars(value)
    except TypeError as exc:
        raise InvalidProfileError(f"{label} must be a mapping or dataclass") from exc
    if isinstance(candidate, Mapping):
        return dict(candidate)
    raise InvalidProfileError(f"{label} must be a mapping or dataclass")


def _nested(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = mapping.get(key)
    return value if isinstance(value, Mapping) else None


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _is_sensitive_key(key: str) -> bool:
    lowered = key.casefold()
    if lowered.endswith("_ref") or lowered.endswith("_id"):
        return False
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS) or lowered in _SENSITIVE_EXACT_KEYS


def _scrub(value: Any, secrets: Sequence[str] = (), *, key: str | None = None) -> Any:
    """Redact known and conventionally named secret values for receipt data."""

    if key is not None and _is_sensitive_key(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): _scrub(item, secrets, key=str(k)) for k, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item, secrets) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "<redacted>")
        return result
    return value


def _bounded_error(prefix: str, error: Any, secrets: Sequence[str] = ()) -> str:
    detail = _scrub(str(error) if error is not None else "unknown error", secrets)
    detail = " ".join(str(detail).split())
    value = f"{prefix}: {detail}" if prefix else detail
    return value[:_MAX_ERROR_LENGTH]


def _valid_text(value: Any, label: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidProfileError(f"{label} must be a non-empty string")
    if len(value) > max_length or any(ord(char) < 32 for char in value):
        raise InvalidProfileError(f"{label} is invalid")
    return value


def _validate_endpoint(value: Any) -> str:
    endpoint = _valid_text(value, "endpoint", max_length=2048)
    if any(char.isspace() for char in endpoint):
        raise InvalidProfileError("endpoint must not contain whitespace")
    try:
        parsed = urlsplit(endpoint)
    except ValueError as exc:
        raise InvalidProfileError("endpoint is not a valid URL") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        raise InvalidProfileError("endpoint must use an HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidProfileError("endpoint credentials are forbidden")
    if parsed.query or parsed.fragment:
        raise InvalidProfileError("endpoint query and fragment are forbidden")
    return endpoint


def _validate_identity(value: Any, label: str, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    text = _valid_text(value, label)
    if not _ID.fullmatch(text):
        raise InvalidProfileError(f"{label} contains unsupported characters")
    return text


def _validate_secret_ref(value: Any) -> str:
    """Validate an opaque reference without accepting common raw-key forms."""

    text = _validate_identity(value, "api_key_ref", optional=False)
    assert text is not None
    if text.casefold() in {"none", "null", "undefined"} or text.casefold().startswith(_RAW_SECRET_PREFIXES):
        raise InvalidProfileError("api_key_ref must be a reference, not a secret value")
    return text


def _reject_raw_secret_fields(value: Any, *, path: str = "profile") -> None:
    """Reject secret-bearing fields before a generic profile is copied."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidProfileError(f"{path} keys must be strings")
            lowered = key.casefold()
            if _is_sensitive_key(key) and not (lowered.endswith("_ref") or lowered.endswith("_id")):
                raise InvalidProfileError(f"{path} contains a secret-bearing field: {key}")
            _reject_raw_secret_fields(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_raw_secret_fields(item, path=f"{path}[{index}]")


def _normalize_replay_policy(value: Any) -> str:
    if value is None:
        return "never_replay"
    if not isinstance(value, str):
        raise ProviderError("replay_policy must be a string")
    normalized = value.strip().casefold()
    if normalized not in _REPLAY_POLICIES:
        raise ProviderError("unsupported replay_policy")
    return normalized


def _normalize_messages(messages: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(messages, str):
        messages = ({"role": "user", "content": messages},)
    elif isinstance(messages, Mapping):
        messages = (messages,)
    if not isinstance(messages, Sequence) or isinstance(messages, (bytes, bytearray)):
        raise ProviderError("messages must be a sequence of message mappings")
    normalized: list[Mapping[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise ProviderError(f"message {index} must be a mapping")
        item = dict(message)
        role = item.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ProviderError(f"message {index} role is required")
        if "content" not in item and "tool_calls" not in item and "function_call" not in item:
            raise ProviderError(f"message {index} content is required")
        normalized.append(_immutable_json(item))
    return tuple(normalized)


def _normalize_parameters(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = _nested(profile, "provider") or {}
    parameters: dict[str, Any] = {}
    for source in (nested, profile):
        candidate = _first(source, "frozen_parameters", "frozen_params", "parameters", "params", default=None)
        if candidate is not None:
            if not isinstance(candidate, Mapping):
                raise InvalidProfileError("frozen parameters must be a mapping")
            parameters.update(candidate)
        extra = source.get("extra_parameters")
        if extra is not None:
            if not isinstance(extra, Mapping):
                raise InvalidProfileError("extra_parameters must be a mapping")
            parameters.update(extra)
        for field_name in _MODEL_PARAMETER_FIELDS:
            if field_name in source:
                parameters[field_name] = source[field_name]
    for key in parameters:
        if not isinstance(key, str):
            raise InvalidProfileError("model parameter keys must be strings")
        lowered = key.casefold()
        if lowered in _FORBIDDEN_PARAMETER_KEYS or _is_sensitive_key(key) or lowered in {"authorization", "x-api-key"}:
            raise InvalidProfileError(f"forbidden or secret model parameter: {key}")
    try:
        return _immutable_json(parameters)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidProfileError("frozen parameters must be JSON values") from exc


@dataclass(frozen=True, slots=True, repr=False)
class ProviderStreamChunk:
    """Monotonic text prefix emitted from an OpenAI SSE stream."""

    seq: int
    prefix: str
    prefix_hash: str
    delta: str = ""

    def __post_init__(self) -> None:
        if type(self.seq) is not int or self.seq < 1:
            raise ValueError("stream chunk seq must be a positive integer")
        if not isinstance(self.prefix, str) or not isinstance(self.delta, str):
            raise TypeError("stream chunk text must be strings")
        expected = sha256_hex(self.prefix.encode("utf-8"))
        if self.prefix_hash != expected:
            raise ValueError("stream chunk prefix_hash does not match prefix")

    @property
    def text(self) -> str:
        """Compatibility spelling; it is the complete prefix, not only delta."""

        return self.prefix

    @property
    def content(self) -> str:
        return self.prefix

    @property
    def text_delta(self) -> str:
        return self.delta

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "prefix": self.prefix, "prefix_hash": self.prefix_hash, "delta": self.delta}

    def __repr__(self) -> str:
        return f"ProviderStreamChunk(seq={self.seq}, prefix_hash={self.prefix_hash!r}, delta_length={len(self.delta)})"


@dataclass(frozen=True, slots=True, repr=False)
class ProviderInvocation:
    """Immutable provider invocation and its hashed wire request."""

    invocation_id: str
    invocation_key: str
    endpoint: str
    model: str
    messages: tuple[Mapping[str, Any], ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)
    profile_revision_id: str | None = None
    provider_plugin_id: str | None = None
    provider_release_id: str | None = None
    stream: bool = True
    replay_policy: str = "never_replay"
    max_retries: int = 0
    input_context: Mapping[str, Any] = field(default_factory=dict)
    profile_revision: Mapping[str, Any] = field(default_factory=dict)
    body: Mapping[str, Any] = field(default_factory=dict)
    request_hash: str = ""

    def __post_init__(self) -> None:
        _validate_identity(self.invocation_id, "invocation_id", optional=False)
        _validate_identity(self.invocation_key, "invocation_key", optional=False)
        _validate_endpoint(self.endpoint)
        model = _valid_text(self.model, "model", max_length=256)
        if any(char.isspace() for char in model):
            raise ValueError("model must not contain whitespace")
        if type(self.stream) is not bool:
            raise TypeError("stream must be a bool")
        if not isinstance(self.input_context, Mapping) or not isinstance(self.profile_revision, Mapping):
            raise TypeError("input_context and profile_revision must be mappings")
        object.__setattr__(self, "messages", tuple(_immutable_json(item) for item in self.messages))
        if any(not isinstance(item, Mapping) for item in self.messages):
            raise TypeError("messages must contain mappings")
        object.__setattr__(self, "parameters", _immutable_json(self.parameters))
        _reject_raw_secret_fields(self.input_context, path="input_context")
        _reject_raw_secret_fields(self.profile_revision, path="profile_revision")
        object.__setattr__(self, "input_context", _immutable_json(self.input_context))
        object.__setattr__(self, "profile_revision", _immutable_json(self.profile_revision))
        body = self.body
        if not body:
            body = {"model": self.model, "messages": self.messages, **thaw_value(self.parameters), "stream": self.stream}
        if not isinstance(body, Mapping):
            raise TypeError("body must be a mapping")
        _reject_raw_secret_fields(body, path="body")
        object.__setattr__(self, "body", _immutable_json(body))
        replay = _normalize_replay_policy(self.replay_policy)
        object.__setattr__(self, "replay_policy", replay)
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 16:
            raise ValueError("max_retries must be an integer from 0 through 16")
        calculated_hash = hash_jcs(
            "openai-compatible-request/v1",
            {
                "endpoint": self.endpoint,
                "model": self.model,
                "body": thaw_value(self.body),
                "profile_revision_id": self.profile_revision_id,
                "provider_plugin_id": self.provider_plugin_id,
                "provider_release_id": self.provider_release_id,
            },
        )
        if self.request_hash:
            if not isinstance(self.request_hash, str) or not _HEX64.fullmatch(self.request_hash):
                raise ValueError("request_hash must be lowercase SHA-256")
            if self.request_hash != calculated_hash:
                raise ValueError("request_hash does not match the immutable provider request")
        else:
            object.__setattr__(self, "request_hash", calculated_hash)

    @property
    def profile_id(self) -> str | None:
        return self.profile_revision_id

    @property
    def request_key(self) -> str:
        return self.invocation_key

    @property
    def frozen_parameters(self) -> Mapping[str, Any]:
        return self.parameters

    def as_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "invocation_key": self.invocation_key,
            "endpoint": self.endpoint,
            "model": self.model,
            "messages": thaw_value(self.messages),
            "parameters": thaw_value(self.parameters),
            "profile_revision_id": self.profile_revision_id,
            "provider_plugin_id": self.provider_plugin_id,
            "provider_release_id": self.provider_release_id,
            "stream": self.stream,
            "replay_policy": self.replay_policy,
            "max_retries": self.max_retries,
            "input_context": thaw_value(self.input_context),
            "profile_revision": thaw_value(self.profile_revision),
            "body": thaw_value(self.body),
            "request_hash": self.request_hash,
        }

    def __repr__(self) -> str:
        return (
            "ProviderInvocation("
            f"invocation_id={self.invocation_id!r}, invocation_key={self.invocation_key!r}, "
            f"model={self.model!r}, request_hash={self.request_hash!r}, stream={self.stream!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ModelReceipt(Mapping[str, Any]):
    """Provider-owned receipt data suitable for Host binding.

    This is deliberately not the Core ``provenance-receipt/v1``.  Core may
    bind this data into its own receipt, but the provider never fabricates or
    writes that Core authority.
    """

    receipt_id: str
    invocation_id: str
    invocation_key: str
    state: str
    request_hash: str
    response_hash: str | None = None
    profile_revision_id: str | None = None
    provider_plugin_id: str | None = None
    provider_release_id: str | None = None
    endpoint: str | None = None
    model: str | None = None
    lifecycle: tuple[str, ...] = ()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost: int | float | str | None = None
    retry_count: int = 0
    stream_termination: str | None = None
    error: str | None = None
    input_context: Mapping[str, Any] = field(default_factory=dict)
    profile_revision: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    response_asset_id: str | None = None
    recovered: bool = False
    uncertain: bool = False
    receipt_hash: str = ""

    def __post_init__(self) -> None:
        if self.state not in {"receipted", "failed", "cancelled", "uncertain"}:
            raise ValueError(f"unsupported receipt state: {self.state}")
        if not isinstance(self.request_hash, str) or not _HEX64.fullmatch(self.request_hash):
            raise ValueError("request_hash must be lowercase SHA-256")
        if self.response_hash is not None and (
            not isinstance(self.response_hash, str) or not _HEX64.fullmatch(self.response_hash)
        ):
            raise ValueError("response_hash must be lowercase SHA-256 or null")
        if type(self.retry_count) is not int or self.retry_count < 0:
            raise ValueError("retry_count must be a non-negative integer")
        if type(self.recovered) is not bool or type(self.uncertain) is not bool:
            raise TypeError("recovered and uncertain must be booleans")
        if not isinstance(self.lifecycle, (tuple, list)) or any(
            not isinstance(step, str) or not step for step in self.lifecycle
        ):
            raise TypeError("lifecycle must be a sequence of non-empty strings")
        object.__setattr__(self, "lifecycle", tuple(self.lifecycle))
        object.__setattr__(self, "input_context", _immutable_json(_scrub(self.input_context)))
        object.__setattr__(self, "profile_revision", _immutable_json(_scrub(self.profile_revision)))
        object.__setattr__(self, "metadata", _immutable_json(_scrub(self.metadata)))
        if self.error is not None:
            object.__setattr__(self, "error", _bounded_error("", self.error))
        object.__setattr__(self, "uncertain", bool(self.uncertain or self.state == "uncertain"))
        calculated_hash = hash_jcs("model-receipt/v1", self._without_hash())
        if self.receipt_hash:
            if not _HEX64.fullmatch(self.receipt_hash):
                raise ValueError("receipt_hash must be lowercase SHA-256")
            if self.receipt_hash != calculated_hash:
                raise ValueError("receipt_hash does not match the immutable receipt content")
        else:
            object.__setattr__(self, "receipt_hash", calculated_hash)

    def _without_hash(self) -> dict[str, Any]:
        return self._as_dict(include_hash=False)

    def _as_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": "model-receipt/v1",
            "receipt_id": self.receipt_id,
            "invocation_id": self.invocation_id,
            "invocation_key": self.invocation_key,
            "state": self.state,
            "request_hash": self.request_hash,
            "response_hash": self.response_hash,
            "profile_revision_id": self.profile_revision_id,
            "provider_plugin_id": self.provider_plugin_id,
            "provider_release_id": self.provider_release_id,
            "endpoint": self.endpoint,
            "model": self.model,
            "lifecycle": list(self.lifecycle),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost": self.cost,
            "retry_count": self.retry_count,
            "stream_termination": self.stream_termination,
            "error": self.error,
            "input_context": thaw_value(self.input_context),
            "profile_revision": thaw_value(self.profile_revision),
            "metadata": thaw_value(self.metadata),
            "response_asset_id": self.response_asset_id,
            "recovered": self.recovered,
            "uncertain": self.uncertain,
        }
        if include_hash:
            value["receipt_hash"] = self.receipt_hash
        return value

    def as_dict(self) -> dict[str, Any]:
        return self._as_dict()

    def to_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(self.as_dict())

    @property
    def uncertainty(self) -> bool:
        return self.uncertain

    @property
    def retry(self) -> int:
        return self.retry_count

    @property
    def termination(self) -> str | None:
        return self.stream_termination

    @property
    def provider_release(self) -> str | None:
        return self.provider_release_id

    @property
    def tokens(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            }
        )

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())

    def __repr__(self) -> str:
        return (
            "ModelReceipt("
            f"receipt_id={self.receipt_id!r}, state={self.state!r}, "
            f"request_hash={self.request_hash!r}, response_hash={self.response_hash!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ProviderResult:
    """Provider result plus the provider-owned receipt and safe stream view."""

    invocation: ProviderInvocation
    state: str
    text: str
    chunks: tuple[ProviderStreamChunk, ...]
    receipt: ModelReceipt
    response: Mapping[str, Any] | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"receipted", "failed", "cancelled", "uncertain"}:
            raise ValueError(f"unsupported provider result state: {self.state}")
        if self.receipt.invocation_id != self.invocation.invocation_id:
            raise ValueError("provider receipt invocation_id does not match invocation")
        if self.receipt.invocation_key != self.invocation.invocation_key:
            raise ValueError("provider receipt invocation_key does not match invocation")
        if self.receipt.request_hash != self.invocation.request_hash:
            raise ValueError("provider receipt request_hash does not match invocation")
        if self.receipt.state != self.state:
            raise ValueError("provider receipt state does not match result")
        object.__setattr__(self, "chunks", tuple(self.chunks))
        if self.response is not None:
            object.__setattr__(self, "response", _immutable_json(self.response))
        if self.error is not None:
            object.__setattr__(self, "error", _bounded_error("", self.error))

    @property
    def output(self) -> str:
        return self.text

    @property
    def content(self) -> str:
        return self.text

    @property
    def stream(self) -> tuple[ProviderStreamChunk, ...]:
        return self.chunks

    @property
    def stream_chunks(self) -> tuple[ProviderStreamChunk, ...]:
        return self.chunks

    @property
    def model_receipt(self) -> ModelReceipt:
        return self.receipt

    @property
    def provider_receipt(self) -> ModelReceipt:
        return self.receipt

    @property
    def cancelled(self) -> bool:
        return self.state == "cancelled"

    @property
    def uncertain(self) -> bool:
        return self.state == "uncertain"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "text": self.text,
            "chunks": [chunk.as_dict() for chunk in self.chunks],
            "receipt": self.receipt.as_dict(),
            "response": thaw_value(self.response) if self.response is not None else None,
            "error": self.error,
        }

    def __repr__(self) -> str:
        return f"ProviderResult(state={self.state!r}, text_length={len(self.text)}, receipt={self.receipt!r})"


@dataclass
class _ParsedResponse:
    text: str = ""
    chunks: list[ProviderStreamChunk] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    termination: str | None = None
    finish_reason: str | None = None
    response_id: str | None = None
    event_count: int = 0
    saw_valid_event: bool = False
    saw_done: bool = False
    safe_response: dict[str, Any] = field(default_factory=dict)
    response_hash: str | None = None

    def hash(self) -> str:
        return hash_jcs(
            "model-response/v1",
            {
                "text": self.text,
                "chunks": [chunk.as_dict() for chunk in self.chunks],
                "usage": self.usage,
                "termination": self.termination,
                "finish_reason": self.finish_reason,
                "response_id": self.response_id,
            },
        )


def _extract_profile(profile: Any) -> tuple[dict[str, Any], dict[str, Any], str, str, str | None, str | None, str | None, Mapping[str, Any]]:
    source = _copy_mapping(profile, label="model profile")
    _reject_raw_secret_fields(source)
    provider_value = source.get("provider")
    provider_mapping = provider_value if isinstance(provider_value, Mapping) else {}
    plugin_id = _validate_identity(
        _first(source, "provider_plugin_id", "provider_plugin", "plugin_id", default=_first(provider_mapping, "plugin_id", "id")),
        "provider_plugin_id",
    )
    release_id = _validate_identity(
        _first(source, "provider_release_id", "provider_release", "release_id", default=_first(provider_mapping, "release_id", "version")),
        "provider_release_id",
    )
    revision_id = _validate_identity(
        _first(source, "profile_revision_id", "model_profile_revision_id", "revision_id", "id"),
        "profile_revision_id",
    )
    endpoint = _validate_endpoint(
        _first(
            source,
            "endpoint",
            "base_url",
            "base_endpoint",
            "url",
            default=_first(provider_mapping, "endpoint", "base_url", "base_endpoint", "url"),
        )
    )
    model = _valid_text(
        _first(
            source,
            "model",
            "model_name",
            "model_id",
            default=_first(provider_mapping, "model", "model_name", "model_id"),
        ),
        "model",
        max_length=256,
    )
    if any(ord(char) < 32 for char in model):
        raise InvalidProfileError("model is invalid")
    if any(char.isspace() for char in model):
        raise InvalidProfileError("model must not contain whitespace")
    api_key_ref = _first(
        source,
        "api_key_ref",
        "secret_ref",
        "api_key_secret_ref",
        default=_first(provider_mapping, "api_key_ref", "secret_ref", "api_key_secret_ref"),
    )
    if api_key_ref is not None:
        api_key_ref = _validate_secret_ref(api_key_ref)
    # An actual key in a profile is never accepted as a persisted profile
    # representation.  The only accepted values are a one-shot invoke arg or
    # a SecretProvider lookup by reference.
    if any(key in source for key in ("api_key", "api_key_value", "secret_value", "secret")):
        raise InvalidProfileError("profile must contain only api_key_ref, not a secret value")
    parameters = _normalize_parameters(source)
    safe_profile = _scrub(source)
    if isinstance(safe_profile, Mapping):
        safe_profile = dict(safe_profile)
        safe_profile.pop("api_key", None)
        safe_profile.pop("api_key_value", None)
        safe_profile.pop("secret_value", None)
        if api_key_ref is not None:
            safe_profile["api_key_ref"] = api_key_ref
    return source, safe_profile, endpoint, model, revision_id, plugin_id, release_id, parameters


def _secret_value(provider: Any, secret_ref: str) -> str | None:
    if provider is None:
        return None
    if isinstance(provider, Mapping):
        value = provider.get(secret_ref)
    else:
        value = None
        for method_name in ("get_secret", "resolve", "get", "read"):
            method = getattr(provider, method_name, None)
            if callable(method):
                value = method(secret_ref)
                break
        else:
            if callable(provider):
                value = provider(secret_ref)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProviderError("secret provider returned an invalid secret")
    return value


def _is_cancelled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    cancelled = getattr(value, "cancelled", None)
    if isinstance(cancelled, bool):
        return cancelled
    method = getattr(value, "is_cancelled", None)
    if callable(method):
        return bool(method())
    method = getattr(value, "is_set", None)
    if callable(method):
        return bool(method())
    if callable(value):
        return bool(value())
    return bool(value)


def _call_signature_aware(method: Callable[..., Any], positional: tuple[Any, ...], optional: Mapping[str, Any]) -> Any:
    """Call a host double without catching TypeError raised by its body."""

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(*positional)
    candidates: list[dict[str, Any]] = []
    keys = list(optional)
    for count in range(len(keys), -1, -1):
        candidates.append({key: optional[key] for key in keys[:count]})
    for kwargs in candidates:
        try:
            signature.bind(*positional, **kwargs)
        except TypeError:
            continue
        return method(*positional, **kwargs)
    for kwargs in candidates:
        try:
            signature.bind(**kwargs)
        except TypeError:
            continue
        return method(**kwargs)
    return method(*positional)


def _transport_send(transport: Any, request: TransportRequest) -> Any:
    method = getattr(transport, "send", None)
    if not callable(method):
        raise ProviderError("injected transport must implement send(request)")
    return _call_signature_aware(method, (request,), {"invocation_key": request.invocation_key, "cancel_token": request.cancel_token})


def _transport_cancel(transport: Any, key: str) -> None:
    method = getattr(transport, "cancel", None)
    if not callable(method):
        return
    _call_signature_aware(method, (key,), {"invocation_key": key})


def _transport_query(transport: Any, key: str) -> Any:
    method = getattr(transport, "query", None)
    if not callable(method):
        raise UnsupportedReplayError("transport does not expose query")
    return _call_signature_aware(method, (key,), {"invocation_key": key})


def _explicit_query_supports(transport: Any) -> bool:
    return type(getattr(transport, "supports_query", False)) is bool and bool(getattr(transport, "supports_query", False)) and callable(getattr(transport, "query", None))


def _send_state(error: BaseException, transport: Any, key: str) -> str:
    """Resolve explicit sent/not_sent/unknown evidence without bool inference."""

    evidence: list[str] = []
    error_state = getattr(error, "send_state", None)
    if error_state in {"sent", "not_sent", "unknown"}:
        evidence.append(error_state)

    transport_state = getattr(transport, "send_state", None)
    if callable(transport_state):
        try:
            transport_state = transport_state(key)
        except (KeyError, LookupError, TypeError):
            transport_state = None
    elif isinstance(transport_state, Mapping):
        transport_state = transport_state.get(key)
    if transport_state in {"sent", "not_sent", "unknown"}:
        evidence.append(transport_state)

    for name in ("sent_keys", "submitted_keys"):
        values = getattr(transport, name, None)
        if values is not None:
            try:
                if key in values:
                    evidence.append("sent")
            except TypeError:
                pass

    if "sent" in evidence:
        return "sent"
    decisive = {value for value in evidence if value != "unknown"}
    if decisive == {"not_sent"} and "unknown" not in evidence:
        return "not_sent"
    return "unknown"


def _explicit_transient(error: BaseException) -> bool:
    """Only an explicit transport classification can authorize a resend."""

    return type(getattr(error, "transient", False)) is bool and bool(
        getattr(error, "transient", False)
    )


def _transport_reports_cancelled(transport: Any, key: str) -> bool:
    values = getattr(transport, "cancelled_keys", None)
    if values is None:
        return False
    try:
        return key in values
    except TypeError:
        return False


@dataclass(frozen=True, slots=True)
class _LineIteratorBody:
    lines: Iterable[Any]


def _coerce_response(value: Any) -> TransportResponse:
    if isinstance(value, TransportResponse):
        return value
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], int):
        return TransportResponse(status_code=value[0], body=value[1])
    if isinstance(value, Mapping):
        if any(key in value for key in ("status_code", "status", "body", "headers", "json")):
            status = value.get("status_code", value.get("status", 200))
            body = value.get("body", value.get("json", value.get("content", value.get("data"))))
            headers = value.get("headers", {})
            if type(status) is not int:
                raise MalformedResponseError("response status_code must be an integer")
            return TransportResponse(status_code=status, headers=headers if isinstance(headers, Mapping) else {}, body=body)
        return TransportResponse(status_code=200, body=value)
    status = getattr(value, "status_code", getattr(value, "status", None))
    if status is not None:
        if type(status) is not int:
            raise MalformedResponseError("response status_code must be an integer")
        headers = getattr(value, "headers", {})
        body = getattr(value, "body", None)
        if body is None:
            iterator = getattr(value, "iter_lines", None)
            if callable(iterator):
                body = _LineIteratorBody(iterator())
            else:
                content = getattr(value, "content", None)
                if content is not None:
                    body = content
                else:
                    json_method = getattr(value, "json", None)
                    if callable(json_method):
                        body = json_method()
                    else:
                        body = getattr(value, "text", None)
        return TransportResponse(status_code=status, headers=headers if isinstance(headers, Mapping) else {}, body=body)
    return TransportResponse(status_code=200, body=value)


def _decode_json(text: bytes | str) -> Mapping[str, Any]:
    raw = text if isinstance(text, bytes) else text.encode("utf-8")
    try:
        value = parse_json_bytes(raw)
    except Exception as exc:
        raise MalformedResponseError("response body is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise MalformedResponseError("response JSON must be an object")
    return value


def _decode_sse_text(text: str) -> list[Mapping[str, Any] | object]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.strip() == "[DONE]":
        return [_DONE]
    lines = normalized.split("\n")
    data_lines: list[str] = []
    events: list[Mapping[str, Any] | object] = []
    saw_sse = False

    def flush() -> None:
        nonlocal data_lines
        if not data_lines:
            return
        payload = "\n".join(data_lines)
        data_lines = []
        if payload.strip() == "[DONE]":
            events.append(_DONE)
        else:
            events.append(_decode_json(payload))

    for line in lines:
        if line == "":
            flush()
            continue
        if line.startswith(":"):
            saw_sse = True
            continue
        if line.startswith("data:"):
            saw_sse = True
            data_lines.append(line[5:].lstrip(" "))
            continue
        if line.startswith("event:") or line.startswith("id:") or line.startswith("retry:"):
            saw_sse = True
            continue
        if saw_sse:
            # Unknown SSE fields are ignored according to the SSE framing
            # rule; only data fields carry provider response content.
            continue
    flush()
    if saw_sse:
        return events
    return [_decode_json(text)]


def _decode_item(item: Any) -> list[Mapping[str, Any] | object]:
    if item is _DONE:
        return [_DONE]
    if isinstance(item, Mapping):
        if "data" in item and not any(key in item for key in ("choices", "usage", "error", "text", "content")):
            data = item["data"]
            if isinstance(data, bytes):
                return _decode_item(data)
            if isinstance(data, str):
                return _decode_sse_text(data)
        if item.get("done") is True and len(item) <= 2:
            return [_DONE]
        return [item]
    if isinstance(item, bytes):
        try:
            text = item.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MalformedResponseError("SSE response is not valid UTF-8") from exc
        return _decode_sse_text(text)
    if isinstance(item, str):
        return _decode_sse_text(item)
    raise MalformedResponseError("response stream item has an unsupported type")


_DONE = object()


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text", item.get("content"))
                if text is not None and not isinstance(text, str):
                    raise MalformedResponseError("response content part text must be a string")
                if text:
                    parts.append(text)
            else:
                raise MalformedResponseError("response content part has an unsupported type")
        return "".join(parts)
    raise MalformedResponseError("response content must be a string or content-part list")


def _usage(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MalformedResponseError("response usage must be an object")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, (int, float, str, bool)) or item is None:
            result[str(key)] = item
    return result


def _extract_event(event: Mapping[str, Any], parsed: _ParsedResponse) -> None:
    if "error" in event:
        error = event["error"]
        if isinstance(error, Mapping):
            message = error.get("message", error.get("type", "provider response error"))
        else:
            message = error
        raise MalformedResponseError(_bounded_error("provider response error", message))
    recognized = False
    content = ""
    append_content = False
    finish_reason: Any = None
    if "choices" in event:
        recognized = True
        choices = event["choices"]
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
            raise MalformedResponseError("response choices must be an array")
        if choices:
            choice = choices[0]
            if not isinstance(choice, Mapping):
                raise MalformedResponseError("response choice must be an object")
            finish_reason = choice.get("finish_reason")
            delta = choice.get("delta")
            if isinstance(delta, Mapping):
                content = _content_text(delta.get("content"))
                append_content = True
            elif delta is not None:
                raise MalformedResponseError("response delta must be an object")
            if not content and "text" in choice:
                content = _content_text(choice.get("text"))
                append_content = True
            if not content and isinstance(choice.get("message"), Mapping):
                content = _content_text(choice["message"].get("content"))
    elif "message" in event and isinstance(event["message"], Mapping):
        recognized = True
        content = _content_text(event["message"].get("content"))
    elif any(key in event for key in ("text", "content", "output")):
        recognized = True
        content = _content_text(event.get("text", event.get("content", event.get("output"))))
        finish_reason = event.get("finish_reason")
    elif "usage" in event or "id" in event or "object" in event or "finish_reason" in event:
        recognized = True
        finish_reason = event.get("finish_reason")
    if not recognized:
        raise MalformedResponseError("response object is not OpenAI-compatible")
    parsed.saw_valid_event = True
    parsed.event_count += 1
    if event.get("id") is not None:
        parsed.response_id = str(event["id"])
    if "usage" in event:
        parsed.usage.update(_usage(event["usage"]))
    if finish_reason is not None:
        parsed.finish_reason = str(finish_reason)
    if content:
        if append_content:
            delta = content
            prefix = parsed.text + content
        else:
            delta = content if content != parsed.text else ""
            prefix = content
        if delta:
            parsed.text = prefix
            parsed.chunks.append(
                ProviderStreamChunk(
                    seq=len(parsed.chunks) + 1,
                    prefix=parsed.text,
                    prefix_hash=sha256_hex(parsed.text.encode("utf-8")),
                    delta=delta,
                )
            )


def _check_cancel(token: Any, parsed: _ParsedResponse) -> None:
    if _is_cancelled(token):
        parsed.termination = "cancelled"
        parsed.response_hash = parsed.hash()
        raise _CancellationRequested(parsed)


def _parse_response(response: TransportResponse, *, stream: bool, cancellation: Any = None) -> _ParsedResponse:
    parsed = _ParsedResponse()
    body = response.body
    if body is None:
        raise MalformedResponseError("response body is empty")
    line_iterator = isinstance(body, _LineIteratorBody)
    stream_body = body.lines if line_iterator else body
    is_iterable_stream = line_iterator or (
        isinstance(stream_body, Iterable)
        and not isinstance(stream_body, (Mapping, bytes, bytearray, str))
    )
    if is_iterable_stream:
        fragments: list[bytes] = []

        def flush_fragments() -> None:
            if not fragments:
                return
            combined = b"".join(fragments)
            fragments.clear()
            for event in _decode_item(combined):
                _check_cancel(cancellation, parsed)
                if event is _DONE:
                    parsed.saw_done = True
                    parsed.saw_valid_event = True
                    continue
                if not isinstance(event, Mapping):
                    raise MalformedResponseError("decoded response event must be an object")
                _extract_event(event, parsed)

        for item in stream_body:
            _check_cancel(cancellation, parsed)
            if isinstance(item, (str, bytes, bytearray)):
                fragment = item.encode("utf-8") if isinstance(item, str) else bytes(item)
                if line_iterator and not fragment.endswith((b"\n", b"\r")):
                    fragment += b"\n"
                fragments.append(fragment)
                continue
            flush_fragments()
            for event in _decode_item(item):
                _check_cancel(cancellation, parsed)
                if event is _DONE:
                    parsed.saw_done = True
                    parsed.saw_valid_event = True
                    continue
                if not isinstance(event, Mapping):
                    raise MalformedResponseError("decoded response event must be an object")
                _extract_event(event, parsed)
        flush_fragments()
        _check_cancel(cancellation, parsed)
        parsed.termination = "done" if parsed.saw_done else "eof"
    else:
        items = _decode_item(body)
        for event in items:
            _check_cancel(cancellation, parsed)
            if event is _DONE:
                parsed.saw_done = True
                parsed.saw_valid_event = True
                continue
            if not isinstance(event, Mapping):
                raise MalformedResponseError("decoded response event must be an object")
            _extract_event(event, parsed)
        parsed.termination = "done" if parsed.saw_done and stream else "response"
    if not parsed.saw_valid_event:
        raise MalformedResponseError("response contains no usable completion data")
    parsed.safe_response = {
        "text": parsed.text,
        "chunks": [chunk.as_dict() for chunk in parsed.chunks],
        "usage": dict(parsed.usage),
        "finish_reason": parsed.finish_reason,
        "response_id": parsed.response_id,
        "stream_termination": parsed.termination,
        "event_count": parsed.event_count,
    }
    parsed.response_hash = parsed.hash()
    return parsed


def _error_response_hash(response: TransportResponse, secrets: Sequence[str] = ()) -> str:
    body = response.body
    if isinstance(body, Mapping):
        safe_body = _scrub(body, secrets)
    elif isinstance(body, (bytes, str)):
        safe_body = _scrub(str(body)[:_MAX_ERROR_LENGTH], secrets)
    else:
        safe_body = type(body).__name__
    return hash_jcs("model-response/v1", {"status_code": response.status_code, "body": safe_body})


def _http_error(response: TransportResponse, secrets: Sequence[str] = ()) -> str:
    body = response.body
    message: Any = None
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            message = error.get("message", error.get("type"))
        elif error is not None:
            message = error
        if message is None:
            message = body.get("message")
    elif isinstance(body, bytes):
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        message = text[:_MAX_ERROR_LENGTH]
    elif isinstance(body, str):
        message = body[:_MAX_ERROR_LENGTH]
    return _bounded_error(f"HTTP {response.status_code}", message or "non-success response", secrets)


def _receipt_id(invocation: ProviderInvocation, state: str, response_hash: str | None, recovered: bool) -> str:
    digest = hash_jcs(
        "model-receipt-id/v1",
        {
            "invocation_key": invocation.invocation_key,
            "request_hash": invocation.request_hash,
            "response_hash": response_hash,
            "state": state,
            "recovered": recovered,
        },
    )
    return f"model-receipt-{digest[:32]}"


def _token_int(usage: Mapping[str, Any], *keys: str) -> int | None:
    value = next((usage[key] for key in keys if key in usage), None)
    if type(value) is int and value >= 0:
        return value
    return None


def _cost(usage: Mapping[str, Any]) -> int | float | str | None:
    for key in ("cost", "total_cost", "price"):
        value = usage.get(key)
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            return value
    return None


class OpenAICompatibleProvider:
    """SDK-only provider with a mandatory injected transport."""

    def __init__(
        self,
        transport: OpenAICompatibleTransport,
        *,
        secret_provider: SecretProvider | Mapping[str, str] | Callable[[str], str | None] | None = None,
        response_persister: Callable[..., Any] | None = None,
        receipt_sink: Callable[..., Any] | None = None,
        max_error_length: int = _MAX_ERROR_LENGTH,
    ) -> None:
        if transport is None:
            raise TypeError("transport is mandatory; no real-network default exists")
        if not callable(getattr(transport, "send", None)):
            raise TypeError("transport must implement send(request)")
        if type(max_error_length) is not int or not 64 <= max_error_length <= 4096:
            raise ValueError("max_error_length must be an integer between 64 and 4096")
        self._transport = transport
        self._secret_provider = secret_provider
        self._response_persister = response_persister
        self._receipt_sink = receipt_sink
        self._max_error_length = max_error_length
        self._active: dict[str, tuple[Any, Any]] = {}
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return f"OpenAICompatibleProvider(transport={type(self._transport).__name__})"

    def prepare(
        self,
        profile: Any = None,
        messages: Any = None,
        *,
        model_profile: Any = None,
        invocation_id: str | None = None,
        invocation_key: str | None = None,
        input_context: Mapping[str, Any] | None = None,
        replay_policy: str | bool | None = None,
        stream: bool = True,
    ) -> ProviderInvocation:
        if profile is None:
            profile = model_profile
        if profile is None:
            raise InvalidProfileError("model profile is required")
        source, safe_profile, endpoint, model, revision_id, plugin_id, release_id, parameters = _extract_profile(profile)
        provider_mapping = source.get("provider") if isinstance(source.get("provider"), Mapping) else {}
        raw_max_retries = source.get("max_retries", provider_mapping.get("max_retries", 0))
        if type(raw_max_retries) is not int or not 0 <= raw_max_retries <= 16:
            raise InvalidProfileError("max_retries must be an integer from 0 through 16")
        normalized_messages = _normalize_messages(messages)
        if type(stream) is not bool:
            raise ProviderError("stream must be a bool")
        body = {"model": model, "messages": thaw_value(normalized_messages), **thaw_value(parameters), "stream": stream}
        provisional = {
            "endpoint": endpoint,
            "model": model,
            "body": body,
            "profile_revision_id": revision_id,
            "provider_plugin_id": plugin_id,
            "provider_release_id": release_id,
        }
        request_hash = hash_jcs("openai-compatible-request/v1", provisional)
        stable_id = invocation_id or invocation_key or f"invocation-{request_hash[:32]}"
        stable_key = invocation_key or stable_id
        _valid_text(stable_id, "invocation_id", max_length=256)
        _valid_text(stable_key, "invocation_key", max_length=256)
        context = {} if input_context is None else input_context
        if not isinstance(context, Mapping):
            raise ProviderError("input_context must be a mapping")
        return ProviderInvocation(
            invocation_id=stable_id,
            invocation_key=stable_key,
            endpoint=endpoint,
            model=model,
            messages=normalized_messages,
            parameters=parameters,
            profile_revision_id=revision_id,
            provider_plugin_id=plugin_id,
            provider_release_id=release_id,
            stream=stream,
            replay_policy=_normalize_replay_policy(replay_policy),
            max_retries=raw_max_retries,
            input_context=context,
            profile_revision=safe_profile,
            body=body,
            request_hash=request_hash,
        )

    def _headers(self, invocation: ProviderInvocation, api_key: str | None, secret_provider: Any) -> tuple[dict[str, str], tuple[str, ...]]:
        ref = invocation.profile_revision.get("api_key_ref")
        secret: str | None = None
        if api_key is not None:
            if not isinstance(api_key, str) or not api_key:
                raise ProviderError("api_key must be a non-empty one-shot string")
            secret = api_key
        elif ref is not None:
            secret = _secret_value(secret_provider, str(ref))
            if secret is None:
                raise ProviderError("api key reference could not be resolved")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if invocation.stream else "application/json"}
        if secret is not None:
            headers["Authorization"] = f"Bearer {secret}"
        return headers, (secret,) if secret is not None else ()

    def cancel(self, invocation_key: str) -> bool:
        """Cancel an active invocation through its injected transport."""

        with self._lock:
            active = self._active.get(invocation_key)
        if active is None:
            _transport_cancel(self._transport, invocation_key)
            return False
        transport, token = active
        if hasattr(token, "cancel") and callable(token.cancel):
            token.cancel()
        cancelled_keys = getattr(transport, "cancelled_keys", None)
        if cancelled_keys is None or invocation_key not in cancelled_keys:
            _transport_cancel(transport, invocation_key)
        return True

    def _finalize(
        self,
        invocation: ProviderInvocation,
        *,
        state: str,
        lifecycle: Sequence[str],
        parsed: _ParsedResponse | None,
        error: str | None,
        secrets: Sequence[str],
        response_asset_id: str | None = None,
        recovered: bool = False,
        receipt_sink: Callable[..., Any] | object | None = None,
        retry_count: int = 0,
    ) -> ProviderResult:
        parsed = parsed or _ParsedResponse()
        usage = parsed.usage
        response_hash = parsed.response_hash
        if response_hash is None and parsed.text:
            response_hash = parsed.hash()
        clean_error = _bounded_error("", error, secrets) if error else None
        steps = list(lifecycle)
        if state not in steps:
            steps.append(state)
        safe_metadata = {
            "finish_reason": parsed.finish_reason,
            "response_id": parsed.response_id,
            "event_count": parsed.event_count,
            "stream_termination": parsed.termination,
        }
        def make_receipt(
            receipt_state: str,
            receipt_steps: Sequence[str],
            receipt_error: str | None,
        ) -> ModelReceipt:
            return ModelReceipt(
                receipt_id=_receipt_id(invocation, receipt_state, response_hash, recovered),
                invocation_id=invocation.invocation_id,
                invocation_key=invocation.invocation_key,
                state=receipt_state,
                request_hash=invocation.request_hash,
                response_hash=response_hash,
                profile_revision_id=invocation.profile_revision_id,
                provider_plugin_id=invocation.provider_plugin_id,
                provider_release_id=invocation.provider_release_id,
                endpoint=invocation.endpoint,
                model=invocation.model,
                lifecycle=tuple(receipt_steps),
                prompt_tokens=_token_int(usage, "prompt_tokens", "input_tokens"),
                completion_tokens=_token_int(usage, "completion_tokens", "output_tokens"),
                total_tokens=_token_int(usage, "total_tokens"),
                cost=_cost(usage),
                retry_count=retry_count,
                stream_termination=parsed.termination,
                error=receipt_error,
                input_context=_scrub(thaw_value(invocation.input_context), secrets),
                profile_revision=thaw_value(invocation.profile_revision),
                metadata=_scrub(safe_metadata, secrets),
                response_asset_id=response_asset_id,
                recovered=recovered,
                uncertain=receipt_state == "uncertain",
            )

        receipt = make_receipt(state, steps, clean_error)
        sink = None if receipt_sink is _NO_RECEIPT_SINK else (
            receipt_sink if receipt_sink is not None else self._receipt_sink
        )
        if sink is not None:
            try:
                _call_signature_aware(sink, (receipt,), {"receipt": receipt, "invocation": invocation})
            except Exception as exc:
                uncertain_steps = [step for step in steps if step != "receipted"]
                uncertain_steps.extend(
                    step for step in ("receipt_persistence_failed", "uncertain")
                    if step not in uncertain_steps
                )
                state = "uncertain"
                clean_error = _bounded_error("receipt persistence failed", exc, secrets)
                receipt = make_receipt(state, uncertain_steps, clean_error)
        return ProviderResult(
            invocation=invocation,
            state=state,
            text=parsed.text,
            chunks=tuple(parsed.chunks),
            receipt=receipt,
            response=parsed.safe_response or None,
            error=clean_error,
        )

    @staticmethod
    def _asset_id(value: Any) -> str | None:
        if isinstance(value, str) and value:
            return value
        if isinstance(value, Mapping):
            for key in ("response_asset_id", "asset_id", "id"):
                item = value.get(key)
                if isinstance(item, str) and item:
                    return item
        return None

    def _persist_response(
        self,
        persister: Callable[..., Any] | None,
        parsed: _ParsedResponse,
        invocation: ProviderInvocation,
    ) -> str | None:
        if persister is None:
            return None
        value = _call_signature_aware(persister, (parsed.safe_response,), {"response": parsed.safe_response, "invocation": invocation})
        return self._asset_id(value)

    def _recover_query(
        self,
        invocation: ProviderInvocation,
        *,
        lifecycle: list[str],
        cancellation: Any,
        secrets: Sequence[str],
        response_persister: Callable[..., Any] | None,
        receipt_sink: Callable[..., Any] | None,
        cause: BaseException,
        retry_count: int,
    ) -> ProviderResult:
        if not _explicit_query_supports(self._transport):
            return self._finalize(
                invocation,
                state="uncertain",
                lifecycle=(*lifecycle, "uncertain"),
                parsed=None,
                error=_bounded_error("transport result is uncertain", cause, secrets),
                secrets=secrets,
                receipt_sink=receipt_sink,
                retry_count=retry_count,
            )
        lifecycle = [*lifecycle, "uncertain", "querying"]
        try:
            queried = _transport_query(self._transport, invocation.invocation_key)
            response = _coerce_response(queried)
            if not response.ok:
                return self._finalize(
                    invocation,
                    state="uncertain",
                    lifecycle=(*lifecycle, "failed"),
                    parsed=None,
                    error=_http_error(response, secrets),
                    secrets=secrets,
                    receipt_sink=receipt_sink,
                    retry_count=retry_count,
                )
            parsed = _parse_response(response, stream=invocation.stream, cancellation=cancellation)
            lifecycle.append("received")
            response_asset_id = self._persist_response(response_persister, parsed, invocation)
            return self._finalize(
                invocation,
                state="receipted",
                lifecycle=(*lifecycle, "receipted"),
                parsed=parsed,
                error=None,
                secrets=secrets,
                response_asset_id=response_asset_id,
                recovered=True,
                receipt_sink=receipt_sink,
                retry_count=retry_count,
            )
        except _CancellationRequested as exc:
            self.cancel(invocation.invocation_key)
            return self._finalize(
                invocation,
                state="cancelled",
                lifecycle=(*lifecycle, "cancelled"),
                parsed=exc.parsed,
                error="cancelled",
                secrets=secrets,
                receipt_sink=receipt_sink,
                retry_count=retry_count,
            )
        except Exception as exc:
            return self._finalize(
                invocation,
                state="uncertain",
                lifecycle=(*lifecycle, "uncertain"),
                parsed=None,
                error=_bounded_error("query recovery failed", exc, secrets),
                secrets=secrets,
                receipt_sink=receipt_sink,
                retry_count=retry_count,
            )

    def invoke(
        self,
        profile: Any = None,
        messages: Any = None,
        *,
        model_profile: Any = None,
        invocation: ProviderInvocation | None = None,
        invocation_id: str | None = None,
        invocation_key: str | None = None,
        api_key: str | None = None,
        secret_provider: Any = None,
        input_context: Mapping[str, Any] | None = None,
        replay_policy: str | bool | None = None,
        stream: bool | None = None,
        cancellation: Any = None,
        cancel_token: Any = None,
        cancel_event: Any = None,
        cancelled: Any = None,
        response_persister: Callable[..., Any] | None = None,
        persist_response: Callable[..., Any] | None = None,
        receipt_sink: Callable[..., Any] | None = None,
        persist_receipt: Callable[..., Any] | None = None,
    ) -> ProviderResult:
        if cancellation is not None and cancel_token is not None:
            raise ProviderError("use only one of cancellation and cancel_token")
        if cancellation is None:
            cancellation = cancel_token
        if cancellation is None:
            cancellation = cancel_event
        if cancellation is None:
            cancellation = cancelled
        if invocation is None and isinstance(profile, ProviderInvocation):
            invocation = profile
        if invocation is None:
            if profile is None:
                profile = model_profile
            invocation = self.prepare(
                profile,
                messages,
                invocation_id=invocation_id,
                invocation_key=invocation_key,
                input_context=input_context,
                replay_policy=replay_policy,
                stream=True if stream is None else stream,
            )
        elif replay_policy is not None or stream is not None:
            # Rebinding an already-prepared invocation is explicit and local;
            # no profile/model/provider value can be changed at this point.
            if replay_policy is not None and _normalize_replay_policy(replay_policy) != invocation.replay_policy:
                invocation = dataclasses.replace(invocation, replay_policy=_normalize_replay_policy(replay_policy))
            if stream is not None and stream != invocation.stream:
                raise ProviderError("prepared invocation stream mode cannot drift")

        response_writer = response_persister if response_persister is not None else persist_response
        if response_writer is None:
            response_writer = self._response_persister
        receipt_writer = receipt_sink if receipt_sink is not None else persist_receipt
        lifecycle: list[str] = ["prepared"]
        secrets: tuple[str, ...] = ()
        retry_count = 0
        if _is_cancelled(cancellation):
            return self._finalize(
                invocation,
                state="cancelled",
                lifecycle=(*lifecycle, "cancelled"),
                parsed=None,
                error="cancelled",
                secrets=secrets,
                receipt_sink=receipt_writer,
                retry_count=retry_count,
            )
        try:
            headers, secrets = self._headers(invocation, api_key, secret_provider or self._secret_provider)
        except Exception as exc:
            return self._finalize(
                invocation,
                state="failed",
                lifecycle=(*lifecycle, "failed"),
                parsed=None,
                error=_bounded_error("provider preparation failed", exc, secrets),
                secrets=secrets,
                receipt_sink=receipt_writer,
                retry_count=retry_count,
            )
        request = TransportRequest(
            endpoint=invocation.endpoint,
            model=invocation.model,
            messages=invocation.messages,
            parameters=invocation.parameters,
            headers=headers,
            stream=invocation.stream,
            invocation_key=invocation.invocation_key,
            request_hash=invocation.request_hash,
            body=invocation.body,
            cancel_token=cancellation,
        )
        with self._lock:
            self._active[invocation.invocation_key] = (self._transport, cancellation)
        send_returned = False
        try:
            _check_cancel(cancellation, _ParsedResponse())
            while True:
                try:
                    raw_response = _transport_send(self._transport, request)
                    break
                except Exception as exc:
                    if _send_state(exc, self._transport, invocation.invocation_key) != "not_sent":
                        raise
                    if (
                        invocation.replay_policy == "idempotent_auto"
                        and _explicit_transient(exc)
                        and retry_count < invocation.max_retries
                    ):
                        retry_count += 1
                        lifecycle.append("retrying")
                        continue
                    raise
            send_returned = True
            lifecycle.append("sent")
            _check_cancel(cancellation, _ParsedResponse())
            try:
                response = _coerce_response(raw_response)
            except Exception as exc:
                return self._recover_query(
                    invocation,
                    lifecycle=lifecycle,
                    cancellation=cancellation,
                    secrets=secrets,
                    response_persister=response_writer,
                    receipt_sink=receipt_writer,
                    cause=exc,
                    retry_count=retry_count,
                )
            if not response.ok:
                return self._finalize(
                    invocation,
                    state="failed",
                    lifecycle=(*lifecycle, "received", "failed"),
                    parsed=_ParsedResponse(response_hash=_error_response_hash(response, secrets), termination="http_error"),
                    error=_http_error(response, secrets),
                    secrets=secrets,
                    receipt_sink=receipt_writer,
                    retry_count=retry_count,
                )
            try:
                parsed = _parse_response(response, stream=invocation.stream, cancellation=cancellation)
            except _CancellationRequested as exc:
                self.cancel(invocation.invocation_key)
                return self._finalize(
                    invocation,
                    state="cancelled",
                    lifecycle=(*lifecycle, "cancelled"),
                    parsed=exc.parsed,
                    error="cancelled",
                    secrets=secrets,
                    receipt_sink=receipt_writer,
                    retry_count=retry_count,
                )
            except MalformedResponseError as exc:
                return self._recover_query(
                    invocation,
                    lifecycle=lifecycle,
                    cancellation=cancellation,
                    secrets=secrets,
                    response_persister=response_writer,
                    receipt_sink=receipt_writer,
                    cause=exc,
                    retry_count=retry_count,
                )
            lifecycle.append("received")
            if _is_cancelled(cancellation) or _transport_reports_cancelled(self._transport, invocation.invocation_key):
                self.cancel(invocation.invocation_key)
                parsed.termination = "cancelled"
                parsed.response_hash = parsed.hash()
                return self._finalize(
                    invocation,
                    state="cancelled",
                    lifecycle=(*lifecycle, "cancelled"),
                    parsed=parsed,
                    error="cancelled",
                    secrets=secrets,
                    receipt_sink=receipt_writer,
                    retry_count=retry_count,
                )
            try:
                response_asset_id = self._persist_response(response_writer, parsed, invocation)
            except Exception as exc:
                return self._finalize(
                    invocation,
                    state="uncertain",
                    lifecycle=(*lifecycle, "uncertain"),
                    parsed=parsed,
                    error=_bounded_error("response persistence failed", exc, secrets),
                    secrets=secrets,
                    receipt_sink=receipt_writer,
                    retry_count=retry_count,
                )
            return self._finalize(
                invocation,
                state="receipted",
                lifecycle=(*lifecycle, "receipted"),
                parsed=parsed,
                error=None,
                secrets=secrets,
                response_asset_id=response_asset_id,
                receipt_sink=receipt_writer,
                retry_count=retry_count,
            )
        except _CancellationRequested as exc:
            if send_returned:
                self.cancel(invocation.invocation_key)
            return self._finalize(
                invocation,
                state="cancelled",
                lifecycle=(*lifecycle, "cancelled"),
                parsed=exc.parsed,
                error="cancelled",
                secrets=secrets,
                receipt_sink=receipt_writer,
                retry_count=retry_count,
            )
        except Exception as exc:
            boundary = (
                "sent"
                if send_returned
                else _send_state(exc, self._transport, invocation.invocation_key)
            )
            if _is_cancelled(cancellation) or _transport_reports_cancelled(self._transport, invocation.invocation_key):
                if boundary != "not_sent":
                    self.cancel(invocation.invocation_key)
                return self._finalize(
                    invocation,
                    state="cancelled",
                    lifecycle=(*lifecycle, "cancelled"),
                    parsed=None,
                    error="cancelled",
                    secrets=secrets,
                    receipt_sink=receipt_writer,
                    retry_count=retry_count,
                )
            if boundary in {"sent", "unknown"}:
                if boundary == "sent" and "sent" not in lifecycle:
                    lifecycle.append("sent")
                elif boundary == "unknown" and "send_unknown" not in lifecycle:
                    lifecycle.append("send_unknown")
                return self._recover_query(
                    invocation,
                    lifecycle=lifecycle,
                    cancellation=cancellation,
                    secrets=secrets,
                    response_persister=response_writer,
                    receipt_sink=receipt_writer,
                    cause=exc,
                    retry_count=retry_count,
                )
            return self._finalize(
                invocation,
                state="failed",
                lifecycle=(*lifecycle, "failed"),
                parsed=None,
                error=_bounded_error("transport failed before send", exc, secrets),
                secrets=secrets,
                receipt_sink=receipt_writer,
                retry_count=retry_count,
            )
        finally:
            with self._lock:
                self._active.pop(invocation.invocation_key, None)

    # Natural spellings used by host adapters.
    complete = invoke
    chat = invoke
    run = invoke


# Public natural aliases.  They intentionally point at the same immutable
# implementation so host code cannot accidentally choose a second provider.
OpenAIProvider = OpenAICompatibleProvider
OpenAICompatibleModelProvider = OpenAICompatibleProvider
ProviderInvocationResult = ProviderResult
Invocation = ProviderInvocation
InvocationResult = ProviderResult
CompletionResult = ProviderResult
StreamChunk = ProviderStreamChunk
CompletionChunk = ProviderStreamChunk
ProviderReceipt = ModelReceipt
InvocationReceipt = ModelReceipt
Receipt = ModelReceipt
ProviderTransport = OpenAICompatibleTransport


__all__ = [
    "CancellationToken",
    "CompletionChunk",
    "CompletionResult",
    "InvalidProfileError",
    "Invocation",
    "InvocationReceipt",
    "InvocationResult",
    "MalformedResponseError",
    "ModelReceipt",
    "OpenAICompatibleModelProvider",
    "OpenAICompatibleProvider",
    "OpenAICompatibleTransport",
    "OpenAIProvider",
    "ProviderError",
    "ProviderInvocation",
    "ProviderInvocationResult",
    "ProviderReceipt",
    "ProviderResult",
    "ProviderStreamChunk",
    "ProviderTransport",
    "ProviderTransportError",
    "Receipt",
    "SecretProvider",
    "StreamChunk",
    "TransportError",
    "TransportRequest",
    "TransportResponse",
    "UnsupportedReplayError",
]
