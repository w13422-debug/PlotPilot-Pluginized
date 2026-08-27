"""Core authority domain entities.

The entities in this module deliberately contain no persistence concerns.  A
``Revision`` is immutable, while ``Workspace``, ``Document`` and ``Node`` are
stable identities whose current revision is maintained by the Core
repository.  Keeping the data objects small makes them useful to the future
Candidate/Publication slices without exposing a database connection.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def utc_now() -> str:
    """Return the frozen-design UTC representation used by Core rows."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _copy_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping")
    # A shallow copy is intentional: repositories serialise the mapping at the
    # transaction boundary and entities never mutate it themselves.
    return dict(value)


@dataclass(frozen=True, slots=True)
class Workspace:
    workspace_id: str
    title: str
    workspace_kind: str = "WritingProject"
    status: str = "active"
    current_plan_revision_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _copy_metadata(self.metadata))

    @property
    def id(self) -> str:
        return self.workspace_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Document:
    document_id: str
    workspace_id: str
    title: str
    document_type: str = "core.document"
    current_revision_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    revision: int = 0
    # ``content`` is a read convenience populated by get_document; it is not
    # stored on the stable document row and never participates in a write.
    content: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _copy_metadata(self.metadata))

    @property
    def id(self) -> str:
        return self.document_id

    @property
    def kind(self) -> str:
        return self.document_type

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Node:
    node_id: str
    workspace_id: str
    document_id: str | None
    title: str
    node_type: str = "section"
    parent_node_id: str | None = None
    position: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    current_revision_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    revision: int = 0
    content: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _copy_metadata(self.metadata))

    @property
    def id(self) -> str:
        return self.node_id

    @property
    def kind(self) -> str:
        return self.node_type

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Revision:
    revision_id: str
    workspace_id: str
    document_id: str | None
    node_id: str | None
    parent_revision_id: str | None
    content: str
    content_hash: str
    created_by: str
    source_candidate_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    revision_number: int = 1
    payload_schema: str | None = None

    @property
    def id(self) -> str:
        return self.revision_id

    @property
    def payload(self) -> str:
        return self.content

    @property
    def target_id(self) -> str:
        target = self.document_id or self.node_id
        if target is None:  # pragma: no cover - guarded by repository/schema
            raise ValueError("revision has no target")
        return target

    @property
    def target_type(self) -> str:
        return "document" if self.document_id is not None else "node"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Relation:
    relation_id: str
    workspace_id: str
    relation_type: str
    source_id: str
    target_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    revision_id: str | None = None
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _copy_metadata(self.metadata))

    @property
    def id(self) -> str:
        return self.relation_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Page:
    """Stable pagination envelope used by query helpers."""

    items: list[Any]
    offset: int
    limit: int
    total: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total

    @property
    def next_offset(self) -> int | None:
        return self.offset + len(self.items) if self.has_more else None

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int):
        return self.items[index]


@dataclass(frozen=True, slots=True)
class TextPage:
    """Metadata for bounded document body reads."""

    text: str
    offset: int
    length: int
    total_length: int

    @property
    def has_more(self) -> bool:
        return self.offset + self.length < self.total_length

    @property
    def next_offset(self) -> int | None:
        return self.offset + self.length if self.has_more else None

    def __str__(self) -> str:
        return self.text

    def __len__(self) -> int:
        return len(self.text)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TextPage):
            return (
                self.text,
                self.offset,
                self.length,
                self.total_length,
            ) == (other.text, other.offset, other.length, other.total_length)
        if isinstance(other, str):
            return self.text == other
        return NotImplemented


__all__ = [
    "Document",
    "Node",
    "Page",
    "Relation",
    "Revision",
    "TextPage",
    "Workspace",
    "utc_now",
]
