"""Deterministic Project Planner domain logic.

This module deliberately stops before Core Candidate materialization.  It returns
immutable payload drafts which a future P1/P2/P3 adapter can stage through the
public SDK; it never writes a Story State authority of its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType
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
    source_refs: tuple[Any, ...] = ()
    result_mode: str = "separate"
    status: str = "complete"
    partial: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))
        object.__setattr__(self, "parent_candidate_ids", tuple(self.parent_candidate_ids))
        object.__setattr__(self, "source_refs", tuple(_freeze(item) for item in self.source_refs))
        if self.result_mode not in {"separate", "synthesize"}:
            raise ValueError("result_mode must be separate or synthesize")
        if self.result_mode == "synthesize" and len(self.parent_candidate_ids) < 2:
            raise ValueError("synthesize requires at least two ordered parents")
        if self.status not in {"complete", "partial"}:
            raise ValueError("status must be complete or partial")
        if self.partial != (self.status == "partial"):
            raise ValueError("partial must agree with status")


_ROLES = ("bible", "characters", "world", "items", "foreshadowing", "story_evolution")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(_thaw(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _freeze(value: Any) -> Any:
    """Canonical deep-copy into recursively immutable, JSON-compatible values."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"unsupported payload value: {type(value).__name__}")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def build_candidate_drafts(
    planning_input: PlanningInput,
    binding: Binding,
    generated: Mapping[str, Any],
    *,
    run_id: str,
    parent_candidate_ids: Sequence[str] = (),
    source_refs: Sequence[Any] = (),
    result_mode: str = "separate",
    status: str = "complete",
    partial: bool = False,
) -> tuple[CandidateDraft, ...]:
    """Normalize one generation attempt without overwriting earlier attempts."""
    run_id = _required(run_id, "run_id")
    parents = tuple(_required(x, "parent_candidate_id") for x in parent_candidate_ids)
    if len(set(parents)) != len(parents):
        raise ValueError("parent_candidate_ids must be unique")
    frozen_sources = tuple(_freeze(item) for item in source_refs)
    unknown = sorted(set(generated) - set(_ROLES))
    if unknown:
        raise ValueError(f"unsupported planning sections: {', '.join(unknown)}")
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
        payload = _freeze({**common, "role": role, "content": generated[role]})
        identity = _freeze({
            "payload": payload,
            "parents": parents,
            "sources": frozen_sources,
            "result_mode": result_mode,
            "status": status,
            "partial": partial,
        })
        drafts.append(CandidateDraft(
            f"{run_id}:{role}:{_canonical_hash(identity)}", role, payload, parents,
            frozen_sources, result_mode, status, partial,
        ))
    if not drafts:
        raise ValueError("generated output has no supported planning sections")
    return tuple(drafts)
