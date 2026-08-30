"""Closed, versioned Story State payload contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .domain import ALLOWED_KINDS, FactRef

_BODY_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "bible": (frozenset({"title", "premise", "themes", "status"}), frozenset()),
    "character": (frozenset({"name", "role", "traits", "status"}), frozenset()),
    "relationship": (
        frozenset({"source_entity_id", "target_entity_id", "relation_type", "status"}),
        frozenset(),
    ),
    "world": (frozenset({"name", "description", "rules"}), frozenset()),
    "location": (frozenset({"name", "description"}), frozenset({"parent_entity_id"})),
    "organization": (frozenset({"name", "purpose", "member_entity_ids"}), frozenset()),
    "rule": (frozenset({"title", "statement", "exceptions"}), frozenset()),
    "item": (
        frozenset({"name", "description", "status"}),
        frozenset({"owner_entity_id"}),
    ),
    "foreshadowing": (
        frozenset({"description", "introduced_revision_id", "status"}),
        frozenset({"resolution_revision_id"}),
    ),
    "story_evolution": (
        frozenset({"summary", "affected_entity_ids"}),
        frozenset({"predecessor_entity_id"}),
    ),
}

_ENUMS = {
    ("bible", "status"): frozenset({"active", "archived"}),
    ("character", "status"): frozenset({"active", "inactive", "deceased"}),
    ("relationship", "status"): frozenset({"active", "ended"}),
    ("item", "status"): frozenset({"active", "lost", "destroyed"}),
    ("foreshadowing", "status"): frozenset({"planted", "advanced", "resolved", "abandoned"}),
}

_LIST_FIELDS = {
    ("bible", "themes"),
    ("character", "traits"),
    ("world", "rules"),
    ("organization", "member_entity_ids"),
    ("rule", "exceptions"),
    ("story_evolution", "affected_entity_ids"),
}

_REFERENCE_FIELDS = {
    ("relationship", "source_entity_id"),
    ("relationship", "target_entity_id"),
    ("location", "parent_entity_id"),
    ("item", "owner_entity_id"),
    ("story_evolution", "predecessor_entity_id"),
}


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-blank text")
    return value


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field} must be a list of non-blank strings")
    result = tuple(_text(item, field) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} must contain unique values")
    return result


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class StatePayload:
    """One closed Story State semantic value and its authoritative references."""

    state_kind: str
    entity_id: str
    body: Mapping[str, Any]
    references: tuple[FactRef, ...] = ()
    schema: str = "story-state-payload/v1"

    def __post_init__(self) -> None:
        if self.schema != "story-state-payload/v1":
            raise ValueError("unsupported Story State payload schema")
        if self.state_kind not in ALLOWED_KINDS:
            raise ValueError(f"unsupported state_kind: {self.state_kind}")
        _text(self.entity_id, "entity_id")
        if not isinstance(self.body, Mapping):
            raise TypeError("body must be an object")
        required, optional = _BODY_FIELDS[self.state_kind]
        fields = set(self.body)
        if fields != required | (fields & optional):
            missing = sorted(required - fields)
            unknown = sorted(fields - required - optional)
            raise ValueError(f"{self.state_kind} body fields are not closed: missing={missing}, unknown={unknown}")
        normalized: dict[str, Any] = {}
        for field, value in self.body.items():
            if (self.state_kind, field) in _LIST_FIELDS:
                normalized[field] = _string_list(value, field)
            elif field in {"parent_entity_id", "owner_entity_id", "predecessor_entity_id", "resolution_revision_id"}:
                normalized[field] = None if value is None else _text(value, field)
            else:
                normalized[field] = _text(value, field)
            allowed = _ENUMS.get((self.state_kind, field))
            if allowed is not None and normalized[field] not in allowed:
                raise ValueError(f"invalid {self.state_kind} {field}")
        if self.state_kind == "relationship" and normalized["source_entity_id"] == normalized["target_entity_id"]:
            raise ValueError("relationship endpoints must be distinct")
        if self.state_kind == "foreshadowing":
            resolved = normalized["status"] == "resolved"
            if resolved != (normalized.get("resolution_revision_id") is not None):
                raise ValueError("resolved foreshadowing must have exactly one resolution_revision_id")
        refs = tuple(self.references)
        if len({ref.key for ref in refs}) != len(refs):
            raise ValueError("Story State references must be unique")
        object.__setattr__(self, "body", MappingProxyType(dict(sorted(normalized.items()))))
        object.__setattr__(self, "references", refs)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> StatePayload:
        if set(value) != {"schema", "state_kind", "entity_id", "body", "references"}:
            raise ValueError("Story State payload top-level fields are not closed")
        raw_refs = value["references"]
        if isinstance(raw_refs, (str, bytes)) or not isinstance(raw_refs, Sequence):
            raise TypeError("references must be a list")
        refs: list[FactRef] = []
        for item in raw_refs:
            if not isinstance(item, Mapping) or set(item) != {
                "workspace_id",
                "entity_kind",
                "entity_id",
                "revision_id",
                "content_hash",
            }:
                raise ValueError("Story State reference fields are not closed")
            refs.append(FactRef(**dict(item)))
        return cls(
            state_kind=value["state_kind"],
            entity_id=value["entity_id"],
            body=value["body"],
            references=tuple(refs),
            schema=value["schema"],
        )

    @classmethod
    def coerce(cls, value: StatePayload | Mapping[str, Any]) -> StatePayload:
        return value if isinstance(value, cls) else cls.from_mapping(value)

    def validate_reference_closure(
        self,
        *,
        workspace_id: str,
        known_entities: frozenset[str],
        batch_entity_ids: frozenset[str],
    ) -> None:
        for ref in self.references:
            if ref.workspace_id != workspace_id:
                raise ValueError("Story State reference crosses workspace")
            if ref.entity_id not in known_entities:
                raise ValueError("Story State reference is not in the preflight closure")
        referenced_ids = {ref.entity_id for ref in self.references}
        body_ids: set[str] = set()
        for field, value in self.body.items():
            if (self.state_kind, field) in _REFERENCE_FIELDS and value is not None:
                body_ids.add(value)
            if field in {"member_entity_ids", "affected_entity_ids"}:
                body_ids.update(value)
        if not body_ids.issubset(referenced_ids | batch_entity_ids):
            raise ValueError("Story State body contains an unbound entity reference")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "state_kind": self.state_kind,
            "entity_id": self.entity_id,
            "body": _thaw(self.body),
            "references": [
                {
                    "workspace_id": ref.workspace_id,
                    "entity_kind": ref.entity_kind,
                    "entity_id": ref.entity_id,
                    "revision_id": ref.revision_id,
                    "content_hash": ref.content_hash,
                }
                for ref in self.references
            ],
        }
