"""Dependency-free context and Skill freeze rules for a future P2/P3 run."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True, slots=True)
class ContextSource:
    source_id: str
    revision_id: str
    kind: str
    content: str
    content_hash: str

    def __post_init__(self) -> None:
        if sha256(self.content.encode("utf-8")).hexdigest() != self.content_hash:
            raise ValueError("context source hash mismatch")


@dataclass(frozen=True, slots=True)
class SkillRef:
    order: int
    skill_id: str
    release_id: str
    package_hash: str
    parameters_asset_id: str | None = None


@dataclass(frozen=True, slots=True)
class FrozenContextPlan:
    operation: str
    sources: tuple[ContextSource, ...]
    skills: tuple[SkillRef, ...]
    fingerprint: str


def freeze_context_plan(operation: str, sources: tuple[ContextSource, ...], skills: tuple[SkillRef, ...]) -> FrozenContextPlan:
    if not operation.strip():
        raise ValueError("operation is required")
    orders = [skill.order for skill in skills]
    if orders != sorted(orders) or len(set(orders)) != len(orders):
        raise ValueError("Skill chain must have unique ascending order")
    releases = [(skill.skill_id, skill.release_id) for skill in skills]
    if len(set(releases)) != len(releases):
        raise ValueError("duplicate Skill release")
    material = [operation]
    material.extend(f"{source.kind}:{source.source_id}:{source.revision_id}:{source.content_hash}" for source in sources)
    material.extend(f"{skill.order}:{skill.skill_id}:{skill.release_id}:{skill.package_hash}:{skill.parameters_asset_id or ''}" for skill in skills)
    return FrozenContextPlan(operation, sources, skills, sha256("\n".join(material).encode("utf-8")).hexdigest())
