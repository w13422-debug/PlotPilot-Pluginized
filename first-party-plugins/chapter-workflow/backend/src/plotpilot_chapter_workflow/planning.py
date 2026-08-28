"""Dependency-free context and Skill freeze rules.

The plan is an immutable input to the real workflow adapter.  It deliberately
contains exact source bytes so an injected Broker never has to re-read mutable
Core state after the workflow starts.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _required(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")


@dataclass(frozen=True, slots=True)
class ContextSource:
    source_id: str
    revision_id: str
    kind: str
    content: str
    content_hash: str

    def __post_init__(self) -> None:
        for name in ("source_id", "revision_id", "kind"):
            _required(getattr(self, name), f"context {name}")
        if not isinstance(self.content, str):
            raise ValueError("context content must be text")
        if not _HASH_RE.fullmatch(self.content_hash):
            raise ValueError("context content_hash must be lowercase SHA-256")
        if sha256(self.content.encode("utf-8")).hexdigest() != self.content_hash:
            raise ValueError("context source hash mismatch")


@dataclass(frozen=True, slots=True)
class SkillRef:
    order: int
    skill_id: str
    release_id: str
    package_hash: str
    parameters_asset_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.order, int) or isinstance(self.order, bool) or self.order < 1:
            raise ValueError("Skill order must be a positive integer")
        for name in ("skill_id", "release_id"):
            _required(getattr(self, name), f"Skill {name}")
        if not _HASH_RE.fullmatch(self.package_hash):
            raise ValueError("Skill package_hash must be lowercase SHA-256")
        if self.parameters_asset_id is not None:
            _required(self.parameters_asset_id, "Skill parameters_asset_id")


@dataclass(frozen=True, slots=True)
class FrozenContextPlan:
    operation: str
    sources: tuple[ContextSource, ...]
    skills: tuple[SkillRef, ...]
    fingerprint: str


def freeze_context_plan(operation: str, sources: tuple[ContextSource, ...], skills: tuple[SkillRef, ...]) -> FrozenContextPlan:
    _required(operation, "operation")
    sources = tuple(sources)
    skills = tuple(skills)
    source_keys = [(source.kind, source.source_id, source.revision_id) for source in sources]
    if len(source_keys) != len(set(source_keys)):
        raise ValueError("duplicate context source revision")
    orders = [skill.order for skill in skills]
    if orders != sorted(orders) or len(set(orders)) != len(orders):
        raise ValueError("Skill chain must have unique ascending order")
    releases = [(skill.skill_id, skill.release_id) for skill in skills]
    if len(set(releases)) != len(releases):
        raise ValueError("duplicate Skill release")
    material = {
        "operation": operation,
        "sources": [
            {"kind": source.kind, "source_id": source.source_id, "revision_id": source.revision_id, "content_hash": source.content_hash}
            for source in sources
        ],
        "skills": [
            {
                "order": skill.order,
                "skill_id": skill.skill_id,
                "release_id": skill.release_id,
                "package_hash": skill.package_hash,
                "parameters_asset_id": skill.parameters_asset_id,
            }
            for skill in skills
        ],
    }
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return FrozenContextPlan(operation, sources, skills, sha256(canonical).hexdigest())
