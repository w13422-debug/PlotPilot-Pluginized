"""Pure DTOs for the model-profile API boundary.

There is intentionally no FastAPI/Pydantic dependency here.  The DTOs are
closed, frozen values and all serialization is performed through the domain's
secret-reference-only projection.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
from typing import Any

try:
    from backend.plotpilot_plugin_sdk.canonical import parse_json_bytes
except ModuleNotFoundError:  # pragma: no cover - alternate runner
    from plotpilot_plugin_sdk.canonical import parse_json_bytes

from ....model import ModelProfileRevision, ProviderConfig


CREATE_SCHEMA = "model-profile-create/v1"

_CREATE_FIELDS = {
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
}
_CREATE_ALIASES = {
    "model_profile_id": "profile_id",
    "model_profile_revision_id": "revision_id",
    "plugin_id": "provider_plugin_id",
    "release_id": "provider_release_id",
    "model": "model_name",
    "secret_ref": "api_key_ref",
    "api_key_reference": "api_key_ref",
    "timeout": "timeout_seconds",
    "retry_count": "max_retries",
    "retries": "max_retries",
    "context_window": "context_limit",
    "context_length": "context_limit",
    "max_output_tokens": "output_limit",
    "output_tokens": "output_limit",
    "note": "notes",
    "user_note": "notes",
    "labels": "tags",
}


def _mapping(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _decode(value: Mapping[str, Any] | str | bytes, *, name: str) -> Mapping[str, Any]:
    if isinstance(value, bytes):
        parsed = parse_json_bytes(value)
    elif isinstance(value, str):
        parsed = parse_json_bytes(value.encode("utf-8"))
    else:
        parsed = value
    return _mapping(parsed, name=name)


def _normalize_create(value: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(value)
    schema = data.pop("schema", None)
    if schema is not None and schema != CREATE_SCHEMA:
        raise ValueError("model profile create schema is not supported")
    nested = data.pop("provider", None)
    if nested is not None:
        provider = _mapping(nested, name="provider")
        # Nested provider is a compatibility input, not an additional output
        # field.  Its accepted names are the ProviderConfig closed names.
        provider_allowed = {
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
        unknown_provider = set(provider) - provider_allowed
        if unknown_provider:
            raise ValueError(f"provider contains unknown fields: {', '.join(sorted(unknown_provider))}")
        provider = {
            "provider_plugin_id" if key == "plugin_id" else "provider_release_id" if key == "release_id" else key: item
            for key, item in provider.items()
        }
        for key, item in provider.items():
            if key in data and data[key] != item:
                raise ValueError(f"model profile contains conflicting provider field {key}")
            data.setdefault(key, item)
    normalized: dict[str, Any] = {}
    for key, item in data.items():
        target = _CREATE_ALIASES.get(key, key)
        if target not in _CREATE_FIELDS:
            # Explicitly reject raw secret-looking request fields rather than
            # silently dropping them at an API boundary.
            raise ValueError(f"model profile create contains unknown field: {key}")
        if target in normalized:
            raise ValueError(f"model profile create contains duplicate field: {target}")
        normalized[target] = item
    return normalized


@dataclass(frozen=True, slots=True)
class ModelProfileCreateRequest:
    """Validated create request containing a secret reference only."""

    profile_id: str
    provider_plugin_id: str
    provider_release_id: str
    endpoint: str
    model_name: str
    api_key_ref: str
    temperature: float = 0.7
    parameters: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 60.0
    max_retries: int = 0
    context_limit: int = 128_000
    output_limit: int = 4_096
    notes: str = ""
    tags: Sequence[str] = field(default_factory=tuple)
    revision_id: str | None = None
    revision_number: int | None = None
    parent_revision_id: str | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        config = ProviderConfig(
            plugin_id=self.provider_plugin_id,
            release_id=self.provider_release_id,
            endpoint=self.endpoint,
            model_name=self.model_name,
            api_key_ref=self.api_key_ref,
            temperature=self.temperature,
            parameters=self.parameters,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            context_limit=self.context_limit,
            output_limit=self.output_limit,
            notes=self.notes,
            tags=self.tags,
        )
        object.__setattr__(self, "provider_plugin_id", config.plugin_id)
        object.__setattr__(self, "provider_release_id", config.release_id)
        object.__setattr__(self, "endpoint", config.endpoint)
        object.__setattr__(self, "model_name", config.model_name)
        object.__setattr__(self, "api_key_ref", config.api_key_ref)
        object.__setattr__(self, "temperature", config.temperature)
        object.__setattr__(self, "parameters", config.parameters)
        object.__setattr__(self, "timeout_seconds", config.timeout_seconds)
        object.__setattr__(self, "max_retries", config.max_retries)
        object.__setattr__(self, "context_limit", config.context_limit)
        object.__setattr__(self, "output_limit", config.output_limit)
        object.__setattr__(self, "notes", config.notes)
        object.__setattr__(self, "tags", config.tags)
        if self.revision_id is not None and not isinstance(self.revision_id, str):
            raise ValueError("revision_id must be a string")
        if self.parent_revision_id is not None and not isinstance(self.parent_revision_id, str):
            raise ValueError("parent_revision_id must be a string")
        if self.revision_number is not None and (isinstance(self.revision_number, bool) or not isinstance(self.revision_number, int)):
            raise ValueError("revision_number must be an integer")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | str | bytes) -> "ModelProfileCreateRequest":
        return cls(**_normalize_create(_decode(value, name="model_profile_create")))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ModelProfileCreateRequest":
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "profile_id": self.profile_id,
            "provider_plugin_id": self.provider_plugin_id,
            "provider_release_id": self.provider_release_id,
            "endpoint": self.endpoint,
            "model_name": self.model_name,
            "api_key_ref": self.api_key_ref,
            "temperature": self.temperature,
            "parameters": _thaw(self.parameters),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "context_limit": self.context_limit,
            "output_limit": self.output_limit,
            "notes": self.notes,
            "tags": list(self.tags),
        }
        if self.revision_id is not None:
            result["revision_id"] = self.revision_id
        if self.revision_number is not None:
            result["revision_number"] = self.revision_number
        if self.parent_revision_id is not None:
            result["parent_revision_id"] = self.parent_revision_id
        if self.created_at is not None:
            result["created_at"] = self.created_at
        return result

    def to_revision(self, *, revision_id: str | None = None, revision_number: int | None = None) -> ModelProfileRevision:
        return ModelProfileRevision(
            profile_id=self.profile_id,
            revision_id=revision_id if revision_id is not None else self.revision_id,
            provider=ProviderConfig(
                plugin_id=self.provider_plugin_id,
                release_id=self.provider_release_id,
                endpoint=self.endpoint,
                model_name=self.model_name,
                api_key_ref=self.api_key_ref,
                temperature=self.temperature,
                parameters=self.parameters,
                timeout_seconds=self.timeout_seconds,
                max_retries=self.max_retries,
                context_limit=self.context_limit,
                output_limit=self.output_limit,
                notes=self.notes,
                tags=self.tags,
            ),
            revision_number=revision_number if revision_number is not None else (self.revision_number or 1),
            parent_revision_id=self.parent_revision_id,
            created_at=self.created_at,
        )


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ModelProfileResponse:
    """Read DTO projecting one immutable revision."""

    revision: ModelProfileRevision

    @classmethod
    def from_domain(cls, value: ModelProfileRevision) -> "ModelProfileResponse":
        if not isinstance(value, ModelProfileRevision):
            raise TypeError("revision must be ModelProfileRevision")
        return cls(value)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | str | bytes) -> "ModelProfileResponse":
        return cls(ModelProfileRevision.from_dict(_decode(value, name="model_profile_revision")))

    @property
    def profile_id(self) -> str:
        return self.revision.profile_id

    @property
    def revision_id(self) -> str:
        return self.revision.revision_id

    @property
    def revision_hash(self) -> str:
        return self.revision.revision_hash

    @property
    def api_key_ref(self) -> str:
        return self.revision.api_key_ref

    def to_dict(self) -> dict[str, Any]:
        return self.revision.to_public_dict()


@dataclass(frozen=True, slots=True)
class ModelProfileListResponse:
    """Read-only list façade DTO."""

    items: tuple[ModelProfileResponse, ...]
    total: int

    def __post_init__(self) -> None:
        values = tuple(item if isinstance(item, ModelProfileResponse) else ModelProfileResponse.from_domain(item) for item in self.items)
        object.__setattr__(self, "items", values)
        if self.total != len(values) or isinstance(self.total, bool) or self.total < 0:
            raise ValueError("model profile list total must equal item count")

    def to_dict(self) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in self.items], "total": self.total}

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


# Compatibility names used by integration callers.
ModelProfileRevisionDTO = ModelProfileResponse
ModelProfileDTO = ModelProfileResponse
CreateModelProfileRequest = ModelProfileCreateRequest
ModelProfileCreateDTO = ModelProfileCreateRequest
ModelProfileListDTO = ModelProfileListResponse


__all__ = [
    "CREATE_SCHEMA",
    "CreateModelProfileRequest",
    "ModelProfileCreateDTO",
    "ModelProfileCreateRequest",
    "ModelProfileDTO",
    "ModelProfileListDTO",
    "ModelProfileListResponse",
    "ModelProfileResponse",
    "ModelProfileRevisionDTO",
]
