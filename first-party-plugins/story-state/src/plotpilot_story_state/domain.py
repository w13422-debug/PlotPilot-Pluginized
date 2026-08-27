"""Story State proposal and rebuildable-projection rules."""
from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping


ALLOWED_KINDS = frozenset({"bible", "character", "relationship", "world", "location", "organization", "rule", "item", "foreshadowing", "story_evolution"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EntityKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class FactRef:
    workspace_id: str
    entity_kind: str
    entity_id: str
    revision_id: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.entity_kind not in ALLOWED_KINDS:
            raise ValueError(f"unsupported entity_kind: {self.entity_kind}")
        for name in ("workspace_id", "entity_id", "revision_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")
        if not SHA256_RE.fullmatch(self.content_hash):
            raise ValueError("content_hash must be a lowercase SHA-256")

    @property
    def key(self) -> EntityKey:
        return self.workspace_id, self.entity_kind, self.entity_id


@dataclass(frozen=True, slots=True)
class Proposal:
    proposal_id: str
    operation_id: str
    item_id: str
    retry_id: str
    target: FactRef
    payload: Mapping[str, Any]
    parent_candidate_ids: tuple[str, ...] = ()
    outcome: str = "success"
    error: str | None = None

    def __post_init__(self) -> None:
        for name in ("proposal_id", "operation_id", "item_id", "retry_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")
        if self.outcome not in {"success", "failure"}:
            raise ValueError("outcome must be success or failure")
        if self.outcome == "success" and self.error is not None:
            raise ValueError("successful proposal cannot have error")
        if self.outcome == "failure" and (self.error is None or not self.error.strip()):
            raise ValueError("failed proposal requires non-blank error")
        object.__setattr__(self, "payload", _freeze(self.payload))
        object.__setattr__(self, "parent_candidate_ids", tuple(self.parent_candidate_ids))


@dataclass(frozen=True, slots=True)
class Projection:
    source_revisions: tuple[str, ...]
    by_scope: Mapping[tuple[str, str], tuple[EntityKey, ...]]
    edges: tuple[tuple[EntityKey, str, EntityKey], ...]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"unsupported payload value: {type(value).__name__}")


def partition_proposals(proposals: Iterable[Proposal]) -> tuple[tuple[Proposal, ...], tuple[Proposal, ...]]:
    """Keep partial failures explicit so callers retry only failed items."""
    complete, failed = [], []
    seen: set[str] = set()
    seen_items: set[tuple[str, str, str]] = set()
    for proposal in proposals:
        if not proposal.proposal_id.strip() or proposal.proposal_id in seen:
            raise ValueError("proposal_id must be non-blank and unique")
        item_identity = proposal.operation_id, proposal.item_id, proposal.retry_id
        if item_identity in seen_items:
            raise ValueError("operation/item/retry identity must be unique")
        seen.add(proposal.proposal_id)
        seen_items.add(item_identity)
        (failed if proposal.outcome == "failure" else complete).append(proposal)
    return tuple(complete), tuple(failed)


def failed_items_for_retry(proposals: Iterable[Proposal], *, retry_id: str) -> tuple[tuple[str, str, str], ...]:
    """Return only failed operation/item identities under a new retry identity."""
    if not retry_id.strip():
        raise ValueError("retry_id must not be blank")
    _, failed = partition_proposals(proposals)
    return tuple((proposal.operation_id, proposal.item_id, retry_id) for proposal in failed)


def build_projection(facts: Iterable[FactRef], relations: Iterable[tuple[EntityKey, str, EntityKey]]) -> Projection:
    """Build a disposable index solely from published Core revision references."""
    facts = tuple(facts)
    keys = {fact.key for fact in facts}
    if len(keys) != len(facts):
        raise ValueError("fact identity must be unique in a projection snapshot")
    by_scope: dict[tuple[str, str], list[EntityKey]] = {}
    for fact in facts:
        by_scope.setdefault((fact.workspace_id, fact.entity_kind), []).append(fact.key)
    edges: list[tuple[EntityKey, str, EntityKey]] = []
    for source, relation, target in relations:
        if source not in keys or target not in keys or not relation.strip():
            raise ValueError("projection relation must reference known entities")
        edges.append((source, relation, target))
    return Projection(
        tuple(sorted(fact.revision_id for fact in facts)),
        MappingProxyType({scope: tuple(sorted(values)) for scope, values in sorted(by_scope.items())}),
        tuple(sorted(edges)),
    )
