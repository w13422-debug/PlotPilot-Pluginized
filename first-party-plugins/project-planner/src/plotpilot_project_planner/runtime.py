"""Fail-closed, source-only Project Planner runtime values.

The module verifies frozen inputs and prepares deterministic payload proposals.
It intentionally has no Result Bundle, Candidate staging, Skill execution or
Publication surface while the accepted Host/CAS composition is deferred.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Any

try:
    from plotpilot_plugin_sdk import (
        canonical_bytes,
        hash_jcs,
        parse_json_bytes,
        sha256_hex,
        verify_snapshot,
    )
except ModuleNotFoundError as exc:
    if exc.name != "plotpilot_plugin_sdk":
        raise
    from backend.plotpilot_plugin_sdk import (
        canonical_bytes,
        hash_jcs,
        parse_json_bytes,
        sha256_hex,
        verify_snapshot,
    )


PLANNER_PLUGIN_ID = "com.plotpilot.project-planner"
PLANNER_OPERATION = "planning.project.generate/v1"
PLANNER_CAPABILITY_ID = PLANNER_OPERATION
PLANNER_PROMPT_SKILL_ID = "com.plotpilot.prompt.project-setting"
PLANNER_CANDIDATE_ROLES = ("setting", "bible", "outline")

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_RUN_SEAL = object()


class PlannerRuntimeError(ValueError):
    """Raised before an effect when a Planner proposal is inconsistent."""


class PlannerRuntimeIntegrationDeferred(PlannerRuntimeError):
    """Raised when a request crosses the accepted source-only boundary."""


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise PlannerRuntimeError(f"{field_name} must be a v1 identifier")
    return value


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise PlannerRuntimeError(f"{field_name} must be a lowercase SHA-256")
    return value


def _plain_json(value: Any, path: str = "$") -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PlannerRuntimeError(f"{path} contains a non-string JSON key")
            result[key] = _plain_json(item, f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_plain_json(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, float) and not isfinite(value):
        raise PlannerRuntimeError(f"{path} contains a non-finite number")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise PlannerRuntimeError(f"{path} contains invalid Unicode") from exc
        return value
    raise PlannerRuntimeError(f"{path} contains unsupported JSON value {type(value).__name__}")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _frozen_json(value: Any) -> Any:
    return _freeze(parse_json_bytes(canonical_bytes(_plain_json(value))))


@dataclass(frozen=True, slots=True)
class PlannerBindingSelection:
    prompt_skill_id: str
    prompt_release_id: str
    planner_release_id: str
    planner_settings_revision_id: str
    plan_revision_id: str
    model_profile_revision_id: str

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            object.__setattr__(self, field_name, _identifier(getattr(self, field_name), field_name))
        if self.prompt_skill_id != PLANNER_PROMPT_SKILL_ID:
            raise PlannerRuntimeError("Prompt selection is not the Planner compatibility designation")


@dataclass(frozen=True, slots=True)
class _FrozenSkillBinding:
    order: int
    skill_id: str
    release_id: str
    package_hash: str
    parameters_asset_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "skill_id": self.skill_id,
            "release_id": self.release_id,
            "package_hash": self.package_hash,
            "parameters_asset_id": self.parameters_asset_id,
        }


@dataclass(frozen=True, slots=True)
class _FrozenPlannerRun:
    snapshot: Mapping[str, Any]
    snapshot_id: str
    snapshot_hash: str
    workspace_id: str
    operation: str
    run_intent_id: str
    plan_revision_id: str
    model_profile_revision_id: str
    prompt_skill_id: str
    prompt_release_id: str
    planner_release_id: str
    planner_package_hash: str
    planner_settings_revision_id: str
    planner_settings_schema_hash: str
    planner_settings_scope: str
    planner_settings_scope_id: str | None
    skills: tuple[_FrozenSkillBinding, ...]
    binding_fingerprint: str
    _seal: object = field(repr=False, compare=False)

    def binding_dict(self) -> dict[str, Any]:
        return {
            "schema": "project-planner-frozen-binding/v1",
            "snapshot_hash": self.snapshot_hash,
            "workspace_id": self.workspace_id,
            "operation": self.operation,
            "plan_revision_id": self.plan_revision_id,
            "model_profile_revision_id": self.model_profile_revision_id,
            "prompt": {
                "designation": "planner-compatibility-prompt/v1",
                "skill_id": self.prompt_skill_id,
                "release_id": self.prompt_release_id,
            },
            "planner_release": {
                "plugin_id": PLANNER_PLUGIN_ID,
                "release_id": self.planner_release_id,
                "package_hash": self.planner_package_hash,
            },
            "planner_settings": {
                "settings_revision_id": self.planner_settings_revision_id,
                "schema_hash": self.planner_settings_schema_hash,
                "scope": self.planner_settings_scope,
                "scope_id": self.planner_settings_scope_id,
            },
            "skills": [skill.as_dict() for skill in self.skills],
        }


def _freeze_planner_run_impl(
    snapshot: Mapping[str, Any], selection: PlannerBindingSelection
) -> _FrozenPlannerRun:
    plain = _plain_json(snapshot)
    verify_snapshot(plain)
    if plain["scope"]["operation"] != PLANNER_OPERATION:
        raise PlannerRuntimeError("RunSnapshot operation is not the Project Planner operation")
    if plain["plan_revision_id"] != selection.plan_revision_id:
        raise PlannerRuntimeError("selected Plan revision is not frozen in RunSnapshot")
    if plain["model_profile_revision_id"] != selection.model_profile_revision_id:
        raise PlannerRuntimeError("selected Model Profile revision is not frozen in RunSnapshot")

    releases = [
        item for item in plain["plugin_releases"]
        if item["plugin_id"] == PLANNER_PLUGIN_ID
    ]
    if len(releases) != 1 or releases[0]["release_id"] != selection.planner_release_id:
        raise PlannerRuntimeError("Planner release is absent from or differs from RunSnapshot")
    planner_release = releases[0]
    _digest(planner_release["package_hash"], "planner package_hash")

    settings = [
        item for item in plain["plugin_settings_revisions"]
        if item["plugin_id"] == PLANNER_PLUGIN_ID
        and item["settings_revision_id"] == selection.planner_settings_revision_id
    ]
    if len(settings) != 1:
        raise PlannerRuntimeError("Planner settings revision is absent from RunSnapshot")
    planner_settings = settings[0]
    if planner_settings["validated_by_release_id"] != selection.planner_release_id:
        raise PlannerRuntimeError("Planner settings were not validated by the frozen release")
    if planner_settings["scope"] == "workspace":
        if planner_settings["scope_id"] != plain["workspace_id"]:
            raise PlannerRuntimeError("Planner workspace settings cross the frozen Workspace")
    elif planner_settings["scope_id"] is not None:
        raise PlannerRuntimeError("global Planner settings must have a null scope_id")
    _digest(planner_settings["schema_hash"], "planner settings schema_hash")

    skills = tuple(
        _FrozenSkillBinding(
            order=item["order"],
            skill_id=_identifier(item["skill_id"], "skill_id"),
            release_id=_identifier(item["release_id"], "skill release_id"),
            package_hash=_digest(item["package_hash"], "skill package_hash"),
            parameters_asset_id=(
                None if item["parameters_asset_id"] is None
                else _identifier(item["parameters_asset_id"], "skill parameters_asset_id")
            ),
        )
        for item in plain["skill_releases"]
    )
    prompt = [
        skill for skill in skills
        if skill.skill_id == PLANNER_PROMPT_SKILL_ID
        and skill.release_id == selection.prompt_release_id
    ]
    if len(prompt) != 1:
        raise PlannerRuntimeError("Planner Prompt must be one exact frozen compatibility Skill release")

    binding_material = {
        "schema": "project-planner-frozen-binding/v1",
        "snapshot_hash": plain["snapshot_hash"],
        "workspace_id": plain["workspace_id"],
        "operation": PLANNER_OPERATION,
        "plan_revision_id": selection.plan_revision_id,
        "model_profile_revision_id": selection.model_profile_revision_id,
        "prompt": {
            "designation": "planner-compatibility-prompt/v1",
            "skill_id": PLANNER_PROMPT_SKILL_ID,
            "release_id": selection.prompt_release_id,
        },
        "planner_release": {
            "plugin_id": PLANNER_PLUGIN_ID,
            "release_id": selection.planner_release_id,
            "package_hash": planner_release["package_hash"],
        },
        "planner_settings": {
            "settings_revision_id": selection.planner_settings_revision_id,
            "schema_hash": planner_settings["schema_hash"],
            "scope": planner_settings["scope"],
            "scope_id": planner_settings["scope_id"],
        },
        "skills": [skill.as_dict() for skill in skills],
    }
    return _FrozenPlannerRun(
        snapshot=_frozen_json(plain),
        snapshot_id=plain["snapshot_id"],
        snapshot_hash=plain["snapshot_hash"],
        workspace_id=plain["workspace_id"],
        operation=PLANNER_OPERATION,
        run_intent_id=plain["run_intent_id"],
        plan_revision_id=selection.plan_revision_id,
        model_profile_revision_id=selection.model_profile_revision_id,
        prompt_skill_id=PLANNER_PROMPT_SKILL_ID,
        prompt_release_id=selection.prompt_release_id,
        planner_release_id=selection.planner_release_id,
        planner_package_hash=planner_release["package_hash"],
        planner_settings_revision_id=selection.planner_settings_revision_id,
        planner_settings_schema_hash=planner_settings["schema_hash"],
        planner_settings_scope=planner_settings["scope"],
        planner_settings_scope_id=planner_settings["scope_id"],
        skills=skills,
        binding_fingerprint=hash_jcs("project-planner-frozen-binding/v1", binding_material),
        _seal=_RUN_SEAL,
    )


def freeze_planner_run(
    snapshot: Mapping[str, Any], selection: PlannerBindingSelection
) -> _FrozenPlannerRun:
    return _freeze_planner_run_impl(snapshot, selection)


def _verified_frozen_run(value: object) -> _FrozenPlannerRun:
    if not isinstance(value, _FrozenPlannerRun) or value._seal is not _RUN_SEAL:
        raise PlannerRuntimeError("frozen Planner run was not minted by freeze_planner_run")
    expected = _freeze_planner_run_impl(
        _thaw(value.snapshot),
        PlannerBindingSelection(
            prompt_skill_id=value.prompt_skill_id,
            prompt_release_id=value.prompt_release_id,
            planner_release_id=value.planner_release_id,
            planner_settings_revision_id=value.planner_settings_revision_id,
            plan_revision_id=value.plan_revision_id,
            model_profile_revision_id=value.model_profile_revision_id,
        ),
    )
    if expected != value:
        raise PlannerRuntimeError("frozen Planner run derived fields were forged or drifted")
    return expected


@dataclass(frozen=True, slots=True)
class PlannerDocumentTarget:
    role: str
    workspace_id: str
    document_id: str
    revision_id: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.role not in PLANNER_CANDIDATE_ROLES:
            raise PlannerRuntimeError(f"unsupported Planner Candidate role: {self.role}")
        for field_name in ("workspace_id", "document_id", "revision_id"):
            object.__setattr__(self, field_name, _identifier(getattr(self, field_name), field_name))
        object.__setattr__(self, "content_hash", _digest(self.content_hash, "content_hash"))

    def target_dict(self) -> dict[str, str]:
        return {
            "workspace_id": self.workspace_id,
            "entity_kind": "document",
            "entity_id": self.document_id,
        }

    def base_dict(self) -> dict[str, str]:
        return {"revision_id": self.revision_id, "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class PreparedPlannerCandidate:
    """Deterministic payload proposal; not a Core Candidate."""

    role: str
    item_id: str
    payload: bytes
    payload_hash: str
    mime: str
    target: PlannerDocumentTarget
    source_refs: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PreparedPlannerProposal:
    frozen_run: _FrozenPlannerRun
    proposal_id: str
    operation_key: str
    operation_payload_hash: str
    attempt_id: str
    candidates: tuple[PreparedPlannerCandidate, ...]

    def candidate(self, role: str) -> PreparedPlannerCandidate:
        for candidate in self.candidates:
            if candidate.role == role:
                return candidate
        raise KeyError(role)


def _payload_bytes(value: Any, role: str) -> tuple[bytes, str]:
    if isinstance(value, str):
        if not value.strip():
            raise PlannerRuntimeError(f"generated {role} text must not be blank")
        if value.startswith("\ufeff"):
            raise PlannerRuntimeError(f"generated {role} text must not start with a BOM")
        try:
            return value.encode("utf-8"), "text/plain; charset=utf-8"
        except UnicodeEncodeError as exc:
            raise PlannerRuntimeError(f"generated {role} text contains invalid Unicode") from exc
    if isinstance(value, (Mapping, list, tuple)):
        return canonical_bytes(_plain_json(value, f"$.{role}")), "application/json"
    raise PlannerRuntimeError(f"generated {role} must be text or JSON")


def _closed_roles(value: Mapping[str, Any], field_name: str) -> None:
    actual = set(value)
    expected = set(PLANNER_CANDIDATE_ROLES)
    if actual != expected:
        raise PlannerRuntimeError(
            f"{field_name} must contain exactly setting, bible and outline"
            f" (missing={sorted(expected - actual)}, unknown={sorted(actual - expected)})"
        )


def _validated_source_refs(
    frozen_run: _FrozenPlannerRun,
    values: Sequence[Mapping[str, Any]],
    *,
    role: str,
) -> tuple[Mapping[str, Any], ...]:
    revisions = {
        (item["revision_id"], item["content_hash"])
        for item in frozen_run.snapshot["input_revisions"]
    }
    assets = {
        item["asset_id"]: item["sha256"]
        for item in frozen_run.snapshot["asset_hashes"]
    }
    result: list[Mapping[str, Any]] = []
    identities: set[tuple[str, str, str, str]] = set()
    for index, value in enumerate(values):
        plain = _plain_json(value, f"$.source_refs.{role}[{index}]")
        fields = {"workspace_id", "source_type", "source_id", "revision_or_hash"}
        if set(plain) != fields:
            raise PlannerRuntimeError(f"{role} source ref fields are not closed")
        if plain["workspace_id"] != frozen_run.workspace_id:
            raise PlannerRuntimeError(f"{role} source ref crosses or lacks the frozen Workspace")
        source_type = plain["source_type"]
        source_id = _identifier(plain["source_id"], "source_id")
        revision_or_hash = _digest(plain["revision_or_hash"], "source revision_or_hash")
        if source_type == "core.revision":
            if (source_id, revision_or_hash) not in revisions:
                raise PlannerRuntimeError(f"{role} revision source is not backed by RunSnapshot inputs")
        elif source_type == "core.asset":
            if assets.get(source_id) != revision_or_hash:
                raise PlannerRuntimeError(f"{role} Asset source is not hash-backed by RunSnapshot")
        else:
            raise PlannerRuntimeError(f"{role} source type lacks an accepted frozen provenance contract")
        identity = (plain["workspace_id"], source_type, source_id, revision_or_hash)
        if identity in identities:
            raise PlannerRuntimeError(f"{role} source refs contain a duplicate identity")
        identities.add(identity)
        result.append(_frozen_json(plain))
    return tuple(result)


def _proposal_material(
    frozen_run: object,
    generated: Mapping[str, Any],
    targets: Mapping[str, PlannerDocumentTarget],
    *,
    attempt_id: str,
    ordinal: int,
    previous_version: object | None,
    parent_candidate_ids: Mapping[str, Sequence[str]] | None,
    source_refs: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> tuple[_FrozenPlannerRun, tuple[PreparedPlannerCandidate, ...], dict[str, Any], str]:
    checked = _verified_frozen_run(frozen_run)
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
        raise PlannerRuntimeError("ordinal must be a positive integer")
    if ordinal != 1 or previous_version is not None:
        raise PlannerRuntimeIntegrationDeferred(
            "durable rerun versions require the P1/P3 operation ledger and previous-version CAS"
        )
    if not isinstance(generated, Mapping) or not isinstance(targets, Mapping):
        raise PlannerRuntimeError("generated and targets must be mappings")
    _closed_roles(generated, "generated")
    _closed_roles(targets, "targets")
    attempt_id = _identifier(attempt_id, "attempt_id")

    parents = parent_candidate_ids or {}
    refs = source_refs or {}
    if set(parents) - set(PLANNER_CANDIDATE_ROLES):
        raise PlannerRuntimeError("parent_candidate_ids contains an unsupported role")
    if any(tuple(values) for values in parents.values()):
        raise PlannerRuntimeIntegrationDeferred(
            "parent Candidate lineage requires authoritative lookup and rerun CAS"
        )
    if set(refs) - set(PLANNER_CANDIDATE_ROLES):
        raise PlannerRuntimeError("source_refs contains an unsupported role")

    input_revisions = {
        (item["document_id"], item["revision_id"], item["content_hash"])
        for item in checked.snapshot["input_revisions"]
    }
    target_identities: set[tuple[str, str]] = set()
    candidates: list[PreparedPlannerCandidate] = []
    item_materials: list[dict[str, Any]] = []
    for role in PLANNER_CANDIDATE_ROLES:
        target = targets[role]
        if not isinstance(target, PlannerDocumentTarget) or target.role != role:
            raise PlannerRuntimeError(f"target role mismatch for {role}")
        if target.workspace_id != checked.workspace_id:
            raise PlannerRuntimeError(f"{role} target crosses the frozen Workspace")
        if (target.document_id, target.revision_id, target.content_hash) not in input_revisions:
            raise PlannerRuntimeError(f"{role} target base is absent from RunSnapshot inputs")
        target_identity = (target.workspace_id, target.document_id)
        if target_identity in target_identities:
            raise PlannerRuntimeError("setting, bible and outline targets must be distinct documents")
        target_identities.add(target_identity)

        role_refs = _validated_source_refs(checked, refs.get(role, ()), role=role)
        payload, mime = _payload_bytes(generated[role], role)
        payload_hash = sha256_hex(payload)
        item_material = {
            "attempt_id": attempt_id,
            "snapshot_hash": checked.snapshot_hash,
            "binding_fingerprint": checked.binding_fingerprint,
            "role": role,
            "payload_hash": payload_hash,
            "target": target.target_dict(),
            "base": target.base_dict(),
            "source_refs": [_thaw(item) for item in role_refs],
        }
        item_id = f"planner-{role}-{hash_jcs('project-planner-item/v2', item_material)[:40]}"
        candidates.append(PreparedPlannerCandidate(
            role=role,
            item_id=item_id,
            payload=payload,
            payload_hash=payload_hash,
            mime=mime,
            target=target,
            source_refs=role_refs,
        ))
        item_materials.append({**item_material, "item_id": item_id})

    request_material = {
        "schema": "project-planner-prepare-operation/v1",
        "attempt_id": attempt_id,
        "snapshot_hash": checked.snapshot_hash,
        "binding_fingerprint": checked.binding_fingerprint,
        "items": item_materials,
    }
    operation_payload_hash = hash_jcs(
        "project-planner-prepare-operation/v1", request_material
    )
    return checked, tuple(candidates), request_material, operation_payload_hash


def planner_prepare_operation_key(
    frozen_run: object,
    generated: Mapping[str, Any],
    targets: Mapping[str, PlannerDocumentTarget],
    *,
    attempt_id: str,
    ordinal: int = 1,
    previous_version: object | None = None,
    parent_candidate_ids: Mapping[str, Sequence[str]] | None = None,
    source_refs: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> str:
    """Derive the only valid operation key for an exact proposal payload."""

    _, _, _, payload_hash = _proposal_material(
        frozen_run,
        generated,
        targets,
        attempt_id=attempt_id,
        ordinal=ordinal,
        previous_version=previous_version,
        parent_candidate_ids=parent_candidate_ids,
        source_refs=source_refs,
    )
    return f"planner-prepare-{payload_hash}"


def prepare_planner_run(
    frozen_run: object,
    generated: Mapping[str, Any],
    targets: Mapping[str, PlannerDocumentTarget],
    *,
    attempt_id: str,
    operation_key: str,
    ordinal: int = 1,
    previous_version: object | None = None,
    parent_candidate_ids: Mapping[str, Sequence[str]] | None = None,
    source_refs: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> PreparedPlannerProposal:
    """Prepare a proposal without claiming a durable Candidate version."""

    checked, candidates, request_material, payload_hash = _proposal_material(
        frozen_run,
        generated,
        targets,
        attempt_id=attempt_id,
        ordinal=ordinal,
        previous_version=previous_version,
        parent_candidate_ids=parent_candidate_ids,
        source_refs=source_refs,
    )
    operation_key = _identifier(operation_key, "operation_key")
    if operation_key != f"planner-prepare-{payload_hash}":
        raise PlannerRuntimeError("operation key was reused with a different Planner payload")
    proposal_material = {
        "operation_key": operation_key,
        "operation_payload_hash": payload_hash,
        "request": request_material,
    }
    return PreparedPlannerProposal(
        frozen_run=checked,
        proposal_id=f"planner-proposal-{hash_jcs('project-planner-proposal/v1', proposal_material)}",
        operation_key=operation_key,
        operation_payload_hash=payload_hash,
        attempt_id=_identifier(attempt_id, "attempt_id"),
        candidates=candidates,
    )


__all__ = [
    "PLANNER_CANDIDATE_ROLES",
    "PLANNER_CAPABILITY_ID",
    "PLANNER_OPERATION",
    "PLANNER_PLUGIN_ID",
    "PLANNER_PROMPT_SKILL_ID",
    "PlannerBindingSelection",
    "PlannerDocumentTarget",
    "PlannerRuntimeError",
    "PlannerRuntimeIntegrationDeferred",
    "PreparedPlannerCandidate",
    "PreparedPlannerProposal",
    "freeze_planner_run",
    "planner_prepare_operation_key",
    "prepare_planner_run",
]
