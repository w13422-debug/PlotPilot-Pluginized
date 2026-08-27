"""Story State proposal and rebuildable-projection rules."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


ALLOWED_KINDS = frozenset({"bible", "character", "relationship", "world", "location", "organization", "rule", "item", "foreshadowing", "story_evolution"})


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
        for name in ("workspace_id", "entity_id", "revision_id", "content_hash"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")


@dataclass(frozen=True, slots=True)
class Proposal:
    proposal_id: str
    target: FactRef
    payload: Mapping[str, Any]
    parent_candidate_ids: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Projection:
    source_revisions: tuple[str, ...]
    by_kind: Mapping[str, tuple[str, ...]]
    edges: tuple[tuple[str, str, str], ...]


def partition_proposals(proposals: Iterable[Proposal]) -> tuple[tuple[Proposal, ...], tuple[Proposal, ...]]:
    """Keep partial failures explicit so callers retry only failed items."""
    complete, failed = [], []
    seen: set[str] = set()
    for proposal in proposals:
        if not proposal.proposal_id.strip() or proposal.proposal_id in seen:
            raise ValueError("proposal_id must be non-blank and unique")
        seen.add(proposal.proposal_id)
        (failed if proposal.error else complete).append(proposal)
    return tuple(complete), tuple(failed)


def build_projection(facts: Iterable[FactRef], relations: Iterable[tuple[str, str, str]]) -> Projection:
    """Build a disposable index solely from published Core revision references."""
    facts = tuple(facts)
    ids = {fact.entity_id for fact in facts}
    if len(ids) != len(facts):
        raise ValueError("entity_id must be unique in a projection snapshot")
    by_kind: dict[str, list[str]] = {}
    for fact in facts:
        by_kind.setdefault(fact.entity_kind, []).append(fact.entity_id)
    edges: list[tuple[str, str, str]] = []
    for source, relation, target in relations:
        if source not in ids or target not in ids or not relation.strip():
            raise ValueError("projection relation must reference known entities")
        edges.append((source, relation, target))
    return Projection(
        tuple(sorted(fact.revision_id for fact in facts)),
        {kind: tuple(sorted(values)) for kind, values in sorted(by_kind.items())},
        tuple(sorted(edges)),
    )
