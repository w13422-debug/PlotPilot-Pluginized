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
        if not isinstance(self.content_hash, str) or _HASH_RE.fullmatch(self.content_hash) is None:
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
        if not isinstance(self.package_hash, str) or _HASH_RE.fullmatch(self.package_hash) is None:
            raise ValueError("Skill package_hash must be lowercase SHA-256")
        if self.parameters_asset_id is not None:
            _required(self.parameters_asset_id, "Skill parameters_asset_id")


@dataclass(frozen=True, slots=True)
class FrozenContextPlan:
    operation: str
    sources: tuple[ContextSource, ...]
    skills: tuple[SkillRef, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Recheck the complete freeze boundary without trusting the factory.

        ``ChapterWorkflow.start`` calls this again so a request corrupted after
        construction is rejected before any injected port observes it.
        """

        _required(self.operation, "operation")
        if type(self.sources) is not tuple:  # exact immutable container
            raise ValueError("frozen context sources must be a tuple")
        if type(self.skills) is not tuple:  # exact immutable container
            raise ValueError("frozen Skill chain must be a tuple")
        if any(type(source) is not ContextSource for source in self.sources):
            raise ValueError("frozen context sources must contain ContextSource values")
        if any(type(skill) is not SkillRef for skill in self.skills):
            raise ValueError("frozen Skill chain must contain SkillRef values")

        # Re-run leaf invariants as a defense against post-construction
        # ``object.__setattr__`` corruption of a frozen dataclass.
        for source in self.sources:
            source.__post_init__()
        for skill in self.skills:
            skill.__post_init__()

        source_keys = [(source.kind, source.source_id, source.revision_id) for source in self.sources]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("duplicate context source revision")
        orders = [skill.order for skill in self.skills]
        if orders != sorted(orders) or len(set(orders)) != len(orders):
            raise ValueError("Skill chain must have unique ascending order")
        releases = [(skill.skill_id, skill.release_id) for skill in self.skills]
        if len(set(releases)) != len(releases):
            raise ValueError("duplicate Skill release")
        if not isinstance(self.fingerprint, str) or _HASH_RE.fullmatch(self.fingerprint) is None:
            raise ValueError("context fingerprint must be lowercase SHA-256")
        if self.fingerprint != _plan_fingerprint(self.operation, self.sources, self.skills):
            raise ValueError("context fingerprint does not match frozen plan")


def _plan_fingerprint(operation: str, sources: tuple[ContextSource, ...], skills: tuple[SkillRef, ...]) -> str:
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
    return sha256(canonical).hexdigest()


def freeze_context_plan(operation: str, sources: tuple[ContextSource, ...], skills: tuple[SkillRef, ...]) -> FrozenContextPlan:
    _required(operation, "operation")
    sources = tuple(sources)
    skills = tuple(skills)
    return FrozenContextPlan(operation, sources, skills, _plan_fingerprint(operation, sources, skills))
