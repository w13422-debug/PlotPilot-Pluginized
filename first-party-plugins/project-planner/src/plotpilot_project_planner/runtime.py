"""Source-only Project Planner runtime values and Candidate Bundle builder.

The module consumes the accepted SDK contracts but deliberately performs no
Host RPC.  Runtime composition owns Asset creation, model/Skill execution,
Candidate staging and Job completion.  Publication is never a Planner
capability.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Any

try:  # Installed plugin runtime uses the public top-level SDK package.
    from plotpilot_plugin_sdk import (
        canonical_bytes,
        hash_jcs,
        parse_json_bytes,
        sha256_hex,
        verify_result_bundle,
        verify_snapshot,
    )
except ModuleNotFoundError as exc:  # Monorepo tests expose the same SDK below backend/.
    if exc.name != "plotpilot_plugin_sdk":
        raise
    from backend.plotpilot_plugin_sdk import (
        canonical_bytes,
        hash_jcs,
        parse_json_bytes,
        sha256_hex,
        verify_result_bundle,
        verify_snapshot,
    )


PLANNER_PLUGIN_ID = "com.plotpilot.project-planner"
PLANNER_CANDIDATE_ROLES = ("setting", "bible", "outline")

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class PlannerRuntimeError(ValueError):
    """Raised before an effect when a frozen Planner run is inconsistent."""


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise PlannerRuntimeError(f"{field} must be a v1 identifier")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise PlannerRuntimeError(f"{field} must be a lowercase SHA-256")
    return value


def _plain_json(value: Any, path: str = "$") -> Any:
    """Deep-copy strict JSON without key coercion or non-finite numbers."""

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
    plain = _plain_json(value)
    # Round-trip through the accepted canonical parser as a second strictness
    # gate and to detach hostile Mapping implementations.
    return _freeze(parse_json_bytes(canonical_bytes(plain)))


@dataclass(frozen=True, slots=True)
class PlannerBindingSelection:
    """Expected identities supplied by Core for one frozen RunSnapshot."""

    prompt_skill_id: str
    prompt_release_id: str
    planner_release_id: str
    planner_settings_revision_id: str
    plan_revision_id: str
    model_profile_revision_id: str

    def __post_init__(self) -> None:
        for field in self.__dataclass_fields__:
            object.__setattr__(self, field, _identifier(getattr(self, field), field))


@dataclass(frozen=True, slots=True)
class FrozenSkillBinding:
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
class FrozenPlannerRun:
    """Immutable binding projection derived from one verified RunSnapshot."""

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
    skills: tuple[FrozenSkillBinding, ...]
    binding_fingerprint: str

    def binding_dict(self) -> dict[str, Any]:
        return {
            "schema": "project-planner-frozen-binding/v1",
            "snapshot_hash": self.snapshot_hash,
            "plan_revision_id": self.plan_revision_id,
            "model_profile_revision_id": self.model_profile_revision_id,
            "prompt": {
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
            },
            "skills": [skill.as_dict() for skill in self.skills],
        }


def freeze_planner_run(
    snapshot: Mapping[str, Any], selection: PlannerBindingSelection
) -> FrozenPlannerRun:
    """Verify a RunSnapshot and bind the exact Planner/Prompt/Skill selection."""

    plain = _plain_json(snapshot)
    verify_snapshot(plain)

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
    _digest(planner_settings["schema_hash"], "planner settings schema_hash")

    skills: list[FrozenSkillBinding] = []
    for item in plain["skill_releases"]:
        skills.append(FrozenSkillBinding(
            order=item["order"],
            skill_id=_identifier(item["skill_id"], "skill_id"),
            release_id=_identifier(item["release_id"], "skill release_id"),
            package_hash=_digest(item["package_hash"], "skill package_hash"),
            parameters_asset_id=(
                None if item["parameters_asset_id"] is None
                else _identifier(item["parameters_asset_id"], "skill parameters_asset_id")
            ),
        ))
    prompt = [
        skill for skill in skills
        if skill.skill_id == selection.prompt_skill_id
        and skill.release_id == selection.prompt_release_id
    ]
    if len(prompt) != 1:
        raise PlannerRuntimeError("Prompt package must be one exact frozen Skill release")

    binding_material = {
        "schema": "project-planner-frozen-binding/v1",
        "snapshot_hash": plain["snapshot_hash"],
        "plan_revision_id": selection.plan_revision_id,
        "model_profile_revision_id": selection.model_profile_revision_id,
        "prompt": {
            "skill_id": selection.prompt_skill_id,
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
        },
        "skills": [skill.as_dict() for skill in skills],
    }
    fingerprint = hash_jcs("project-planner-frozen-binding/v1", binding_material)
    return FrozenPlannerRun(
        snapshot=_frozen_json(plain),
        snapshot_id=plain["snapshot_id"],
        snapshot_hash=plain["snapshot_hash"],
        workspace_id=plain["workspace_id"],
        operation=plain["scope"]["operation"],
        run_intent_id=plain["run_intent_id"],
        plan_revision_id=selection.plan_revision_id,
        model_profile_revision_id=selection.model_profile_revision_id,
        prompt_skill_id=selection.prompt_skill_id,
        prompt_release_id=selection.prompt_release_id,
        planner_release_id=selection.planner_release_id,
        planner_package_hash=planner_release["package_hash"],
        planner_settings_revision_id=selection.planner_settings_revision_id,
        planner_settings_schema_hash=planner_settings["schema_hash"],
        skills=tuple(skills),
        binding_fingerprint=fingerprint,
    )


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
        for field in ("workspace_id", "document_id", "revision_id"):
            object.__setattr__(self, field, _identifier(getattr(self, field), field))
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
    role: str
    item_id: str
    payload: bytes
    payload_hash: str
    mime: str
    target: PlannerDocumentTarget
    parent_candidate_ids: tuple[str, ...]
    source_refs: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PreparedPlannerRun:
    """Append-only run version whose payloads await Host Asset materialization."""

    frozen_run: FrozenPlannerRun
    version_id: str
    ordinal: int
    attempt_id: str
    previous_version_id: str | None
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
        plain = _plain_json(value, f"$.{role}")
        return canonical_bytes(plain), "application/json"
    raise PlannerRuntimeError(f"generated {role} must be text or JSON")


def _closed_roles(value: Mapping[str, Any], field: str) -> None:
    actual = set(value)
    expected = set(PLANNER_CANDIDATE_ROLES)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise PlannerRuntimeError(
            f"{field} must contain exactly setting, bible and outline"
            f" (missing={missing}, unknown={unknown})"
        )


def prepare_planner_run(
    frozen_run: FrozenPlannerRun,
    generated: Mapping[str, Any],
    targets: Mapping[str, PlannerDocumentTarget],
    *,
    attempt_id: str,
    ordinal: int = 1,
    previous_version: PreparedPlannerRun | None = None,
    parent_candidate_ids: Mapping[str, Sequence[str]] | None = None,
    source_refs: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> PreparedPlannerRun:
    """Prepare one immutable run version without writing Assets or Core truth."""

    if not isinstance(generated, Mapping) or not isinstance(targets, Mapping):
        raise PlannerRuntimeError("generated and targets must be mappings")
    _closed_roles(generated, "generated")
    _closed_roles(targets, "targets")
    attempt_id = _identifier(attempt_id, "attempt_id")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
        raise PlannerRuntimeError("ordinal must be a positive integer")
    if previous_version is None:
        if ordinal != 1:
            raise PlannerRuntimeError("the first Planner run version must have ordinal 1")
        previous_version_id = None
    else:
        if ordinal != previous_version.ordinal + 1:
            raise PlannerRuntimeError("rerun ordinal must immediately follow the previous version")
        if attempt_id == previous_version.attempt_id:
            raise PlannerRuntimeError("an intentional rerun requires a new attempt_id")
        if previous_version.frozen_run.workspace_id != frozen_run.workspace_id:
            raise PlannerRuntimeError("rerun history cannot cross Workspace identity")
        previous_version_id = previous_version.version_id

    parents = parent_candidate_ids or {}
    refs = source_refs or {}
    if set(parents) - set(PLANNER_CANDIDATE_ROLES):
        raise PlannerRuntimeError("parent_candidate_ids contains an unsupported role")
    if set(refs) - set(PLANNER_CANDIDATE_ROLES):
        raise PlannerRuntimeError("source_refs contains an unsupported role")

    input_revisions = {
        (item["document_id"], item["revision_id"], item["content_hash"])
        for item in frozen_run.snapshot["input_revisions"]
    }
    candidates: list[PreparedPlannerCandidate] = []
    for role in PLANNER_CANDIDATE_ROLES:
        target = targets[role]
        if not isinstance(target, PlannerDocumentTarget) or target.role != role:
            raise PlannerRuntimeError(f"target role mismatch for {role}")
        if target.workspace_id != frozen_run.workspace_id:
            raise PlannerRuntimeError(f"{role} target crosses the frozen Workspace")
        if (target.document_id, target.revision_id, target.content_hash) not in input_revisions:
            raise PlannerRuntimeError(f"{role} target base is absent from RunSnapshot inputs")
        role_parents = tuple(_identifier(item, "parent_candidate_id") for item in parents.get(role, ()))
        if len(set(role_parents)) != len(role_parents):
            raise PlannerRuntimeError(f"{role} parent Candidate IDs must be unique")
        role_refs = tuple(_frozen_json(item) for item in refs.get(role, ()))
        payload, mime = _payload_bytes(generated[role], role)
        payload_hash = sha256_hex(payload)
        item_material = {
            "attempt_id": attempt_id,
            "ordinal": ordinal,
            "previous_version_id": previous_version_id,
            "snapshot_hash": frozen_run.snapshot_hash,
            "binding_fingerprint": frozen_run.binding_fingerprint,
            "role": role,
            "payload_hash": payload_hash,
            "target": target.target_dict(),
            "base": target.base_dict(),
            "parent_candidate_ids": list(role_parents),
            "source_refs": [_thaw(item) for item in role_refs],
        }
        item_id = f"planner-{role}-{hash_jcs('project-planner-item/v1', item_material)[:40]}"
        candidates.append(PreparedPlannerCandidate(
            role=role,
            item_id=item_id,
            payload=payload,
            payload_hash=payload_hash,
            mime=mime,
            target=target,
            parent_candidate_ids=role_parents,
            source_refs=role_refs,
        ))

    version_material = {
        "attempt_id": attempt_id,
        "ordinal": ordinal,
        "previous_version_id": previous_version_id,
        "snapshot_hash": frozen_run.snapshot_hash,
        "binding_fingerprint": frozen_run.binding_fingerprint,
        "items": [
            {"role": item.role, "item_id": item.item_id, "payload_hash": item.payload_hash}
            for item in candidates
        ],
    }
    version_id = f"planner-version-{hash_jcs('project-planner-run-version/v1', version_material)}"
    return PreparedPlannerRun(
        frozen_run=frozen_run,
        version_id=version_id,
        ordinal=ordinal,
        attempt_id=attempt_id,
        previous_version_id=previous_version_id,
        candidates=tuple(candidates),
    )


@dataclass(frozen=True, slots=True)
class SkillChainAttachment:
    chain_result_id: str
    asset_id: str
    asset_hash: str
    candidate_role: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "chain_result_id", _identifier(self.chain_result_id, "chain_result_id"))
        object.__setattr__(self, "asset_id", _identifier(self.asset_id, "asset_id"))
        object.__setattr__(self, "asset_hash", _digest(self.asset_hash, "asset_hash"))
        if self.candidate_role not in PLANNER_CANDIDATE_ROLES:
            raise PlannerRuntimeError("Skill chain attachment has an unsupported role")


@dataclass(frozen=True, slots=True)
class PlannerCandidateBatch:
    run_version: PreparedPlannerRun
    bundle: Mapping[str, Any]
    json_bytes: bytes
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self.bundle)


def materialize_candidate_batch(
    run_version: PreparedPlannerRun,
    payload_asset_ids: Mapping[str, str],
    *,
    producer: Mapping[str, Any],
    provenance_receipt_id: str,
    skill_chain_attachments: Sequence[SkillChainAttachment] = (),
    warnings: Sequence[Mapping[str, Any]] = (),
    known_parent_ids: set[str] | None = None,
) -> PlannerCandidateBatch:
    """Bind Host-created Assets and emit one verified candidate-batch/v1."""

    if not isinstance(payload_asset_ids, Mapping):
        raise PlannerRuntimeError("payload_asset_ids must be a mapping")
    _closed_roles(payload_asset_ids, "payload_asset_ids")
    asset_ids = {
        role: _identifier(payload_asset_ids[role], f"{role} payload_asset_id")
        for role in PLANNER_CANDIDATE_ROLES
    }
    if len(set(asset_ids.values())) != len(asset_ids):
        raise PlannerRuntimeError("each Planner Candidate requires a distinct payload Asset")
    producer_plain = _plain_json(producer, "$.producer")
    provenance_receipt_id = _identifier(provenance_receipt_id, "provenance_receipt_id")
    warning_values = [_plain_json(item, "$.warnings") for item in warnings]

    items: list[dict[str, Any]] = []
    for candidate in run_version.candidates:
        target = candidate.target
        items.append({
            "schema": "candidate-item/v1",
            "item_id": candidate.item_id,
            "item_kind": "document",
            "target": target.target_dict(),
            "mutation": {
                "mode": "replace",
                "payload_schema": "core/document-text/v1",
                "payload_hash": candidate.payload_hash,
            },
            "payload_asset_id": asset_ids[candidate.role],
            "base": target.base_dict(),
            "write_set": [{**target.target_dict(), **target.base_dict()}],
            "parent_candidate_ids": list(candidate.parent_candidate_ids),
            "source_refs": [_thaw(item) for item in candidate.source_refs],
            "status": "complete",
        })

    attachment_identity = [
        {
            "chain_result_id": attachment.chain_result_id,
            "asset_id": attachment.asset_id,
            "asset_hash": attachment.asset_hash,
            "candidate_role": attachment.candidate_role,
            "result_item_id": next(
                item.item_id for item in run_version.candidates
                if item.role == attachment.candidate_role
            ),
        }
        for attachment in skill_chain_attachments
    ]
    bundle_identity = {
        "run_version_id": run_version.version_id,
        "input_snapshot_hash": run_version.frozen_run.snapshot_hash,
        "items": items,
        "producer": producer_plain,
        "provenance_receipt_id": provenance_receipt_id,
        "skill_chain_attachments": attachment_identity,
        "warnings": warning_values,
    }
    bundle_id = f"planner-bundle-{hash_jcs('project-planner-result-bundle/v1', bundle_identity)}"
    item_by_role = {item.role: item.item_id for item in run_version.candidates}
    refs = [
        {
            "schema": "skill-chain-ref/v1",
            "chain_result_id": attachment.chain_result_id,
            "asset_id": attachment.asset_id,
            "asset_hash": attachment.asset_hash,
            "result_bundle_id": bundle_id,
            "result_item_id": item_by_role[attachment.candidate_role],
            "stream_id": None,
            "acked_prefix_hash": None,
        }
        for attachment in skill_chain_attachments
    ]
    bundle = {
        "schema": "result-bundle/v1",
        "contract_id": "candidate-batch/v1",
        "bundle_id": bundle_id,
        "bundle_type": "candidate_batch",
        "producer": producer_plain,
        "input_snapshot_hash": run_version.frozen_run.snapshot_hash,
        "items": items,
        "warnings": warning_values,
        "partial": False,
        "provenance_receipt_id": provenance_receipt_id,
        "skill_chain_result_refs": refs,
    }
    verify_result_bundle(
        bundle,
        snapshot_workspace_id=run_version.frozen_run.workspace_id,
        snapshot_hash_value=run_version.frozen_run.snapshot_hash,
        known_parent_ids=known_parent_ids,
    )
    raw = canonical_bytes(bundle)
    return PlannerCandidateBatch(
        run_version=run_version,
        bundle=_freeze(parse_json_bytes(raw)),
        json_bytes=raw,
        sha256=sha256_hex(raw),
    )


__all__ = [
    "PLANNER_CANDIDATE_ROLES",
    "PLANNER_PLUGIN_ID",
    "FrozenPlannerRun",
    "FrozenSkillBinding",
    "PlannerBindingSelection",
    "PlannerCandidateBatch",
    "PlannerDocumentTarget",
    "PlannerRuntimeError",
    "PreparedPlannerCandidate",
    "PreparedPlannerRun",
    "SkillChainAttachment",
    "freeze_planner_run",
    "materialize_candidate_batch",
    "prepare_planner_run",
]
