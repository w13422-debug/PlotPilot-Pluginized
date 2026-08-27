"""Deterministic Project Planner domain logic.

This module deliberately stops before Core Candidate materialization.  It returns
immutable payload drafts which a future P1/P2/P3 adapter can stage through the
public SDK; it never writes a Story State authority of its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence


def _required(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be blank")
    return normalized


@dataclass(frozen=True, slots=True)
class PlanningInput:
    premise: str
    genres: tuple[str, ...]
    target_words: int
    structure: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "premise", _required(self.premise, "premise"))
        object.__setattr__(self, "structure", _required(self.structure, "structure"))
        genres = tuple(dict.fromkeys(_required(item, "genre") for item in self.genres))
        if not genres:
            raise ValueError("genres must not be empty")
        if self.target_words <= 0:
            raise ValueError("target_words must be positive")
        object.__setattr__(self, "genres", genres)


@dataclass(frozen=True, slots=True)
class Binding:
    prompt_package_id: str
    skill_chain_id: str
    model_profile_id: str
    plan_id: str
    release_id: str

    def __post_init__(self) -> None:
        for field in self.__dataclass_fields__:
            object.__setattr__(self, field, _required(getattr(self, field), field))


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    draft_key: str
    target_role: str
    payload: Mapping[str, Any]
    parent_candidate_ids: tuple[str, ...] = ()


_ROLES = ("bible", "characters", "world", "items", "foreshadowing", "story_evolution")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def build_candidate_drafts(
    planning_input: PlanningInput,
    binding: Binding,
    generated: Mapping[str, Any],
    *,
    run_id: str,
    parent_candidate_ids: Sequence[str] = (),
) -> tuple[CandidateDraft, ...]:
    """Normalize one generation attempt without overwriting earlier attempts."""
    run_id = _required(run_id, "run_id")
    parents = tuple(dict.fromkeys(_required(x, "parent_candidate_id") for x in parent_candidate_ids))
    common = {
        "planning_input": {
            "premise": planning_input.premise,
            "genres": list(planning_input.genres),
            "target_words": planning_input.target_words,
            "structure": planning_input.structure,
        },
        "binding": {name: getattr(binding, name) for name in binding.__dataclass_fields__},
    }
    drafts: list[CandidateDraft] = []
    for role in _ROLES:
        if role not in generated:
            continue
        payload = {**common, "role": role, "content": generated[role]}
        drafts.append(CandidateDraft(f"{run_id}:{role}:{_canonical_hash(payload)}", role, payload, parents))
    if not drafts:
        raise ValueError("generated output has no supported planning sections")
    return tuple(drafts)
