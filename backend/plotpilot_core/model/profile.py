"""Immutable model-provider profile domain objects.

This module is deliberately independent from Core persistence.  A model
profile is a configuration boundary: it contains provider *references* and
never a secret value.  Repositories in :mod:`repository` are injectable
adapters for this domain and do not participate in the Core authority schema.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import re
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import urlsplit
import uuid

try:  # Works both from the repository root and with ``backend`` on sys.path.
    from backend.plotpilot_plugin_sdk.canonical import canonical_bytes as _canonical_bytes
    from backend.plotpilot_plugin_sdk.canonical import hash_jcs
    from backend.plotpilot_plugin_sdk.errors import ContractError, ContractValidationError, ErrorCode
except ModuleNotFoundError:  # pragma: no cover - exercised by alternate runners
    from plotpilot_plugin_sdk.canonical import canonical_bytes as _canonical_bytes
    from plotpilot_plugin_sdk.canonical import hash_jcs
    from plotpilot_plugin_sdk.errors import ContractError, ContractValidationError, ErrorCode


MODEL_PROFILE_SCHEMA: Final[str] = "model-profile-revision/v1"
PROVIDER_CONFIG_SCHEMA: Final[str] = "provider-config/v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s")
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "api_key_value",
        "apikey",
        "authorization",
        "password",
        "secret",
        "secret_value",
        "token",
        "access_token",
        "bearer_token",
    }
)
_RAW_SECRET_PREFIXES = (
    "sk-",
    "sk_",
    "bearer ",
    "ghp_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "aiza",
)


def utc_now() -> str:
    """Return the same compact UTC representation used by Core."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ModelProfileError(ValueError):
    """Base error for model-profile domain failures."""


class ModelProfileValidationError(ContractValidationError, ModelProfileError):
    """Input failed the closed model-profile boundary."""


class ModelProfileConflictError(ContractError, ModelProfileError):
    """An immutable identity was reused with different content."""

    def __init__(self, message: str = "model profile revision identity already has different content") -> None:
        super().__init__(ErrorCode.DUPLICATE_REQUEST, message)


class ModelProfileNotFoundError(KeyError, ModelProfileError):
    """Requested model-profile revision does not exist."""


def _invalid(message: str, *, path: str | None = None) -> ModelProfileValidationError:
    return ModelProfileValidationError(message, path=path)


def _require_mapping(value: Any, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(f"{name} must be an object", path=name)
    return value


def _validate_identifier(value: Any, *, name: str, max_length: int = 256) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{name} must be a string", path=name)
    if not value or len(value) > max_length:
        raise _invalid(f"{name} must be 1..{max_length} characters", path=name)
    if value.strip() != value or _CONTROL_RE.search(value) or _WHITESPACE_RE.search(value):
        raise _invalid(f"{name} must not contain whitespace or control characters", path=name)
    if value in {".", ".."}:
        raise _invalid(f"{name} is not a valid identifier", path=name)
    return value


def _validate_text(value: Any, *, name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{name} must be a string", path=name)
    if len(value) > max_length or _CONTROL_RE.search(value):
        raise _invalid(f"{name} is too long or contains control characters", path=name)
    return value


def _validate_endpoint(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise _invalid("endpoint must be a non-empty URL", path="endpoint")
    if value.strip() != value or _CONTROL_RE.search(value) or _WHITESPACE_RE.search(value):
        raise _invalid("endpoint must not contain whitespace or control characters", path="endpoint")
    try:
        parsed = urlsplit(value)
        # Accessing port also validates malformed numeric ports.
        _ = parsed.port
    except ValueError as exc:
        raise _invalid(f"endpoint is not a valid URL: {exc}", path="endpoint") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        raise _invalid("endpoint must use http or https and include a host", path="endpoint")
    if parsed.username is not None or parsed.password is not None:
        raise _invalid("endpoint must not contain userinfo", path="endpoint")
    if parsed.query or parsed.fragment:
        raise _invalid("endpoint must not contain a query or fragment", path="endpoint")
    return value


def _validate_model_name(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise _invalid("model_name must be a non-empty string", path="model_name")
    if value.strip() != value or _CONTROL_RE.search(value) or _WHITESPACE_RE.search(value):
        raise _invalid("model_name must not contain whitespace or control characters", path="model_name")
    return value


def _validate_secret_ref(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise _invalid("api_key_ref must be a non-empty secret reference", path="api_key_ref")
    if value.strip() != value or _CONTROL_RE.search(value) or _WHITESPACE_RE.search(value):
        raise _invalid("api_key_ref must not contain whitespace or control characters", path="api_key_ref")
    lowered = value.lower()
    if lowered in {"none", "null", "undefined"} or lowered.startswith(_RAW_SECRET_PREFIXES):
        raise _invalid("api_key_ref must be a reference, not a secret value", path="api_key_ref")
    return value


def _validate_float(value: Any, *, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"{name} must be a number", path=name)
    converted = float(value)
    if not math.isfinite(converted) or not minimum <= converted <= maximum:
        raise _invalid(f"{name} must be finite and in [{minimum}, {maximum}]", path=name)
    return converted


def _validate_positive_int(value: Any, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{name} must be an integer", path=name)
    if value < 1 or value > maximum:
        raise _invalid(f"{name} must be in [1, {maximum}]", path=name)
    return value


def _validate_nonnegative_int(value: Any, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{name} must be an integer", path=name)
    if value < 0 or value > maximum:
        raise _invalid(f"{name} must be in [0, {maximum}]", path=name)
    return value


def _freeze_json(value: Any, *, path: str = "parameters") -> Any:
    """Recursively copy JSON values into immutable Python values."""

    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            if _CONTROL_RE.search(value):
                raise _invalid(f"{path} contains a control character", path=path)
            return value
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid(f"{path} contains a non-finite number", path=path)
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise _invalid(f"{path} keys must be strings", path=path)
            if key.lower() in _SENSITIVE_KEYS:
                raise _invalid(f"{path}.{key} must not contain a secret value", path=f"{path}.{key}")
            if _CONTROL_RE.search(key):
                raise _invalid(f"{path} contains a control-character key", path=path)
            frozen[key] = _freeze_json(child, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(child, path=f"{path}[{index}]") for index, child in enumerate(value))
    raise _invalid(f"{path} must contain only JSON values", path=path)


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _freeze_parameters(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if value is None:
        value = {}
    mapping = _require_mapping(value, name="parameters")
    return _freeze_json(mapping, path="parameters")


def _freeze_tags(value: Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _invalid("tags must be an array of strings", path="tags")
    if len(value) > 64:
        raise _invalid("tags cannot contain more than 64 entries", path="tags")
    result: set[str] = set()
    for index, tag in enumerate(value):
        if not isinstance(tag, str) or not tag or len(tag) > 128 or _CONTROL_RE.search(tag):
            raise _invalid(f"tags[{index}] must be a non-empty string", path=f"tags[{index}]")
        if tag.strip() != tag:
            raise _invalid(f"tags[{index}] must not have surrounding whitespace", path=f"tags[{index}]")
        result.add(tag)
    return tuple(sorted(result))


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise _invalid("created_at must be an ISO-8601 timestamp", path="created_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _invalid("created_at must be an ISO-8601 timestamp", path="created_at") from exc
    if parsed.tzinfo is None:
        raise _invalid("created_at must include a timezone", path="created_at")
    return value


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """Immutable provider plugin/release identity."""

    plugin_id: str
    release_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_id", _validate_identifier(self.plugin_id, name="plugin_id"))
        object.__setattr__(self, "release_id", _validate_identifier(self.release_id, name="release_id"))

    @property
    def provider_plugin_id(self) -> str:
        return self.plugin_id

    @property
    def provider_release_id(self) -> str:
        return self.release_id

    def to_dict(self) -> dict[str, str]:
        return {"plugin_id": self.plugin_id, "release_id": self.release_id}


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Validated HTTP(S) endpoint value object."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_endpoint(self.value))

    @property
    def url(self) -> str:
        return self.value

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ModelName:
    """Validated provider model-name value object."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_model_name(self.value))

    @property
    def name(self) -> str:
        return self.value

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Opaque reference to a secret held outside the model profile."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _validate_secret_ref(self.value))

    @property
    def ref(self) -> str:
        return self.value

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, init=False)
class ProviderConfig:
    """Closed, immutable provider configuration.

    ``parameters`` is recursively frozen at construction time.  The object
    only accepts ``api_key_ref``; there is intentionally no secret-bearing
    field or secret-resolution method on this boundary.
    """

    plugin_id: str
    release_id: str
    endpoint: str
    model_name: str
    api_key_ref: str
    temperature: float
    parameters: Mapping[str, Any]
    timeout_seconds: float
    max_retries: int
    context_limit: int
    output_limit: int
    notes: str
    tags: tuple[str, ...]

    def __init__(
        self,
        plugin_id: str | None = None,
        release_id: str | None = None,
        endpoint: str | None = None,
        model_name: str | None = None,
        api_key_ref: str | None = None,
        temperature: float = 0.7,
        parameters: Mapping[str, Any] | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 0,
        context_limit: int = 128_000,
        output_limit: int = 4_096,
        notes: str = "",
        tags: Sequence[str] | None = None,
        **aliases: Any,
    ) -> None:
        plugin_id = _take_alias(plugin_id, aliases, "provider_plugin_id", "provider_plugin")
        release_id = _take_alias(release_id, aliases, "provider_release_id", "provider_release")
        model_name = _take_alias(model_name, aliases, "model")
        api_key_ref = _take_alias(api_key_ref, aliases, "secret_ref", "api_key_reference")
        timeout_seconds = _take_alias(timeout_seconds, aliases, "timeout", default=60.0)
        max_retries = _take_alias(max_retries, aliases, "retry_count", "retries", default=0)
        context_limit = _take_alias(context_limit, aliases, "context_window", "context_length", default=128_000)
        output_limit = _take_alias(output_limit, aliases, "max_output_tokens", "output_tokens", default=4_096)
        notes = _take_alias(notes, aliases, "note", "user_note", default="")
        tags = _take_alias(tags, aliases, "labels", default=None)
        if aliases:
            unknown = ", ".join(sorted(aliases))
            raise _invalid(f"provider config contains unknown fields: {unknown}")
        if plugin_id is None or release_id is None or endpoint is None or model_name is None or api_key_ref is None:
            raise _invalid("plugin_id, release_id, endpoint, model_name and api_key_ref are required")
        object.__setattr__(self, "plugin_id", _validate_identifier(plugin_id, name="plugin_id"))
        object.__setattr__(self, "release_id", _validate_identifier(release_id, name="release_id"))
        object.__setattr__(self, "endpoint", _validate_endpoint(endpoint))
        object.__setattr__(self, "model_name", _validate_model_name(model_name))
        object.__setattr__(self, "api_key_ref", _validate_secret_ref(api_key_ref))
        object.__setattr__(self, "temperature", _validate_float(temperature, name="temperature", minimum=0.0, maximum=2.0))
        object.__setattr__(self, "parameters", _freeze_parameters(parameters))
        object.__setattr__(self, "timeout_seconds", _validate_float(timeout_seconds, name="timeout_seconds", minimum=0.001, maximum=86_400.0))
        object.__setattr__(self, "max_retries", _validate_nonnegative_int(max_retries, name="max_retries", maximum=16))
        object.__setattr__(self, "context_limit", _validate_positive_int(context_limit, name="context_limit", maximum=10_000_000))
        object.__setattr__(self, "output_limit", _validate_positive_int(output_limit, name="output_limit", maximum=10_000_000))
        object.__setattr__(self, "notes", _validate_text(notes, name="notes", max_length=4096))
        object.__setattr__(self, "tags", _freeze_tags(tags))

    @property
    def provider_identity(self) -> ProviderIdentity:
        return ProviderIdentity(self.plugin_id, self.release_id)

    @property
    def provider_plugin_id(self) -> str:
        return self.plugin_id

    @property
    def provider_release_id(self) -> str:
        return self.release_id

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def api_key_reference(self) -> str:
        return self.api_key_ref

    @property
    def timeout(self) -> float:
        return self.timeout_seconds

    @property
    def retry_count(self) -> int:
        return self.max_retries

    @property
    def context_window(self) -> int:
        return self.context_limit

    @property
    def max_output_tokens(self) -> int:
        return self.output_limit

    @property
    def note(self) -> str:
        return self.notes

    @property
    def user_note(self) -> str:
        return self.notes

    @property
    def labels(self) -> tuple[str, ...]:
        return self.tags

    def to_dict(self, *, include_schema: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "plugin_id": self.plugin_id,
            "release_id": self.release_id,
            "endpoint": self.endpoint,
            "model_name": self.model_name,
            "api_key_ref": self.api_key_ref,
            "temperature": self.temperature,
            "parameters": _thaw_json(self.parameters),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "context_limit": self.context_limit,
            "output_limit": self.output_limit,
            "notes": self.notes,
            "tags": list(self.tags),
        }
        if include_schema:
            return {"schema": PROVIDER_CONFIG_SCHEMA, **result}
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderConfig":
        data = dict(_require_mapping(value, name="provider"))
        schema = data.pop("schema", None)
        if schema is not None and schema != PROVIDER_CONFIG_SCHEMA:
            raise _invalid("provider schema is not supported", path="schema")
        aliases = {"provider_plugin_id": "plugin_id", "provider_release_id": "release_id", "model": "model_name"}
        for source, target in aliases.items():
            if source in data:
                if target in data:
                    raise _invalid(f"provider contains both {source} and {target}")
                data[target] = data.pop(source)
        allowed = {
            "plugin_id",
            "release_id",
            "endpoint",
            "model_name",
            "api_key_ref",
            "temperature",
            "parameters",
            "timeout_seconds",
            "max_retries",
            "context_limit",
            "output_limit",
            "notes",
            "tags",
        }
        unknown = set(data) - allowed
        if unknown:
            raise _invalid(f"provider contains unknown fields: {', '.join(sorted(unknown))}")
        return cls(**data)

    def __hash__(self) -> int:
        return hash(_canonical_bytes(self.to_dict()))


def _take_alias(value: Any, aliases: dict[str, Any], *names: str, default: Any = None) -> Any:
    present = [name for name in names if name in aliases]
    if len(present) > 1:
        raise _invalid(f"multiple compatibility aliases supplied: {', '.join(present)}")
    if present:
        # Optional constructor arguments have ordinary defaults, so an alias
        # is allowed to replace that untouched default.  A non-default
        # canonical value plus an alias is still ambiguous and rejected.
        if value is not None and default is None:
            raise _invalid(f"value supplied with compatibility alias {present[0]}")
        if default is not None and value != default:
            raise _invalid(f"value supplied with compatibility alias {present[0]}")
        return aliases.pop(present[0])
    if value is None and default is not None:
        return default
    return value


@dataclass(frozen=True, slots=True, init=False)
class ModelProfileRevision:
    """Immutable, content-addressed model profile revision."""

    profile_id: str
    revision_id: str
    revision_number: int
    parent_revision_id: str | None
    provider: ProviderConfig
    created_at: str
    revision_hash: str

    def __init__(
        self,
        profile_id: str | None = None,
        revision_id: str | None = None,
        *,
        provider: ProviderConfig | Mapping[str, Any] | None = None,
        provider_plugin_id: str | None = None,
        provider_release_id: str | None = None,
        plugin_id: str | None = None,
        release_id: str | None = None,
        endpoint: str | None = None,
        model_name: str | None = None,
        api_key_ref: str | None = None,
        temperature: float = 0.7,
        parameters: Mapping[str, Any] | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 0,
        context_limit: int = 128_000,
        output_limit: int = 4_096,
        notes: str = "",
        tags: Sequence[str] | None = None,
        revision_number: int = 1,
        parent_revision_id: str | None = None,
        created_at: str | None = None,
        revision_hash: str | None = None,
        **aliases: Any,
    ) -> None:
        profile_id = _take_alias(profile_id, aliases, "model_profile_id")
        revision_id = _take_alias(revision_id, aliases, "model_profile_revision_id", "id")
        parent_revision_id = _take_alias(parent_revision_id, aliases, "parent_id")
        provider_plugin_id = _take_alias(provider_plugin_id, aliases, "provider_plugin", default=None)
        provider_release_id = _take_alias(provider_release_id, aliases, "provider_release", default=None)
        if aliases:
            unknown = ", ".join(sorted(aliases))
            raise _invalid(f"model profile contains unknown fields: {unknown}")
        if profile_id is None:
            raise _invalid("profile_id is required", path="profile_id")
        profile_id = _validate_identifier(profile_id, name="profile_id")
        revision_id = revision_id or f"model-revision-{uuid.uuid4().hex}"
        revision_id = _validate_identifier(revision_id, name="revision_id")
        revision_number = _validate_positive_int(revision_number, name="revision_number", maximum=10_000_000)
        if parent_revision_id is not None:
            parent_revision_id = _validate_identifier(parent_revision_id, name="parent_revision_id")
        if provider is not None:
            if any(value is not None for value in (provider_plugin_id, provider_release_id, plugin_id, release_id, endpoint, model_name, api_key_ref)):
                raise _invalid("provider cannot be combined with direct provider fields")
            provider_value = ProviderConfig.from_dict(provider) if isinstance(provider, Mapping) else provider
            if not isinstance(provider_value, ProviderConfig):
                raise _invalid("provider must be ProviderConfig or object", path="provider")
        else:
            if provider_plugin_id is not None and plugin_id is not None:
                raise _invalid("provider plugin is specified more than once")
            if provider_release_id is not None and release_id is not None:
                raise _invalid("provider release is specified more than once")
            provider_value = ProviderConfig(
                plugin_id=provider_plugin_id if provider_plugin_id is not None else plugin_id,
                release_id=provider_release_id if provider_release_id is not None else release_id,
                endpoint=endpoint,
                model_name=model_name,
                api_key_ref=api_key_ref,
                temperature=temperature,
                parameters=parameters,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
                context_limit=context_limit,
                output_limit=output_limit,
                notes=notes,
                tags=tags,
            )
        created_at = _validate_timestamp(created_at if created_at is not None else utc_now())
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "revision_number", revision_number)
        object.__setattr__(self, "parent_revision_id", parent_revision_id)
        object.__setattr__(self, "provider", provider_value)
        object.__setattr__(self, "created_at", created_at)
        calculated = hash_jcs(MODEL_PROFILE_SCHEMA, self.hash_payload())
        if revision_hash is not None:
            if not isinstance(revision_hash, str) or not _SHA256_RE.fullmatch(revision_hash):
                raise _invalid("revision_hash must be lowercase SHA-256", path="revision_hash")
            if revision_hash != calculated:
                raise _invalid("revision_hash does not match the immutable profile content", path="revision_hash")
        object.__setattr__(self, "revision_hash", calculated)

    @property
    def id(self) -> str:
        return self.revision_id

    @property
    def model_profile_revision_id(self) -> str:
        return self.revision_id

    @property
    def model_profile_id(self) -> str:
        return self.profile_id

    @property
    def hash(self) -> str:
        return self.revision_hash

    @property
    def content_hash(self) -> str:
        return self.revision_hash

    @property
    def provider_identity(self) -> ProviderIdentity:
        return self.provider.provider_identity

    @property
    def provider_plugin_id(self) -> str:
        return self.provider.plugin_id

    @property
    def provider_release_id(self) -> str:
        return self.provider.release_id

    @property
    def plugin_id(self) -> str:
        return self.provider.plugin_id

    @property
    def release_id(self) -> str:
        return self.provider.release_id

    @property
    def endpoint(self) -> str:
        return self.provider.endpoint

    @property
    def model_name(self) -> str:
        return self.provider.model_name

    @property
    def api_key_ref(self) -> str:
        return self.provider.api_key_ref

    @property
    def temperature(self) -> float:
        return self.provider.temperature

    @property
    def parameters(self) -> Mapping[str, Any]:
        return self.provider.parameters

    @property
    def timeout_seconds(self) -> float:
        return self.provider.timeout_seconds

    @property
    def max_retries(self) -> int:
        return self.provider.max_retries

    @property
    def context_limit(self) -> int:
        return self.provider.context_limit

    @property
    def output_limit(self) -> int:
        return self.provider.output_limit

    @property
    def notes(self) -> str:
        return self.provider.notes

    @property
    def tags(self) -> tuple[str, ...]:
        return self.provider.tags

    def hash_payload(self) -> dict[str, Any]:
        """Return the exact payload covered by ``revision_hash``."""

        return {
            "profile_id": self.profile_id,
            "revision_id": self.revision_id,
            "revision_number": self.revision_number,
            "parent_revision_id": self.parent_revision_id,
            "created_at": self.created_at,
            "provider": self.provider.to_dict(),
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.hash_payload())

    def to_dict(self, *, include_schema: bool = True, include_hash: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "profile_id": self.profile_id,
            "revision_id": self.revision_id,
            "revision_number": self.revision_number,
            "parent_revision_id": self.parent_revision_id,
            "provider_plugin_id": self.provider_plugin_id,
            "provider_release_id": self.provider_release_id,
            "endpoint": self.endpoint,
            "model_name": self.model_name,
            "api_key_ref": self.api_key_ref,
            "temperature": self.temperature,
            "parameters": _thaw_json(self.parameters),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "context_limit": self.context_limit,
            "output_limit": self.output_limit,
            "notes": self.notes,
            "tags": list(self.tags),
            "created_at": self.created_at,
        }
        if include_schema:
            result = {"schema": MODEL_PROFILE_SCHEMA, **result}
        if include_hash:
            result["revision_hash"] = self.revision_hash
        return result

    def to_nested_dict(self, *, include_schema: bool = True, include_hash: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "profile_id": self.profile_id,
            "revision_id": self.revision_id,
            "revision_number": self.revision_number,
            "parent_revision_id": self.parent_revision_id,
            "provider": self.provider.to_dict(),
            "created_at": self.created_at,
        }
        if include_schema:
            result = {"schema": MODEL_PROFILE_SCHEMA, **result}
        if include_hash:
            result["revision_hash"] = self.revision_hash
        return result

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize without any possibility of secret-value inclusion."""

        return self.to_dict()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ModelProfileRevision":
        data = dict(_require_mapping(value, name="model_profile_revision"))
        schema = data.pop("schema", None)
        if schema is not None and schema != MODEL_PROFILE_SCHEMA:
            raise _invalid("model profile schema is not supported", path="schema")
        aliases = {
            "model_profile_id": "profile_id",
            "model_profile_revision_id": "revision_id",
            "plugin_id": "provider_plugin_id",
            "release_id": "provider_release_id",
            "model": "model_name",
            "secret_ref": "api_key_ref",
            "user_note": "notes",
            "labels": "tags",
        }
        for source, target in aliases.items():
            if source in data:
                if target in data:
                    raise _invalid(f"model profile contains both {source} and {target}")
                data[target] = data.pop(source)
        if "provider" in data:
            allowed = {"profile_id", "revision_id", "revision_number", "parent_revision_id", "provider", "created_at", "revision_hash"}
            unknown = set(data) - allowed
            if unknown:
                raise _invalid(f"model profile contains unknown fields: {', '.join(sorted(unknown))}")
            return cls(**data)
        allowed = {
            "profile_id",
            "revision_id",
            "revision_number",
            "parent_revision_id",
            "provider_plugin_id",
            "provider_release_id",
            "endpoint",
            "model_name",
            "api_key_ref",
            "temperature",
            "parameters",
            "timeout_seconds",
            "max_retries",
            "context_limit",
            "output_limit",
            "notes",
            "tags",
            "created_at",
            "revision_hash",
        }
        unknown = set(data) - allowed
        if unknown:
            raise _invalid(f"model profile contains unknown fields: {', '.join(sorted(unknown))}")
        return cls(**data)

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "ModelProfileRevision":
        return cls.from_dict(value)

    def __hash__(self) -> int:
        return hash(self.revision_hash)


# Compatibility names used by callers that call the object a model profile.
ModelProfile = ModelProfileRevision
ProviderConfiguration = ProviderConfig
ApiKeyRef = SecretRef
ModelProfileRevisionHash = str


__all__ = [
    "ApiKeyRef",
    "Endpoint",
    "MODEL_PROFILE_SCHEMA",
    "ModelName",
    "ModelProfile",
    "ModelProfileError",
    "ModelProfileConflictError",
    "ModelProfileNotFoundError",
    "ModelProfileRevision",
    "ModelProfileRevisionHash",
    "ModelProfileValidationError",
    "PROVIDER_CONFIG_SCHEMA",
    "ProviderConfig",
    "ProviderConfiguration",
    "ProviderIdentity",
    "SecretRef",
    "utc_now",
]
