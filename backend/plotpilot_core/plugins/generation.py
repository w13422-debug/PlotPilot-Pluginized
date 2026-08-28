"""Deterministic, persistence-agnostic Generation domain rules.

Persistence adapters may commit the returned snapshots atomically; this module owns no
database and therefore cannot bypass P1's authority boundary.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from plotpilot_plugin_sdk.errors import ContractError, ErrorCode
from plotpilot_plugin_sdk.verifier import assert_valid, verify_data_bundle, verify_plan

DataBundleResolver = Callable[[str], Mapping[str, Any] | None]


@dataclass(frozen=True)
class GenerationState:
    current: Mapping[str, Any] | None = None
    lkg: Mapping[str, Any] | None = None
    safe_mode: bool = False
    rollback_consumed: bool = False


def validate_generation(generation: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(generation))
    assert_valid("plugin-generation/v1", value)
    plugin_ids = [member["plugin_id"] for member in value["members"]]
    if len(plugin_ids) != len(set(plugin_ids)):
        raise ContractError(
            ErrorCode.INCOMPATIBLE_GENERATION,
            "a Generation may contain only one active release per plugin_id",
        )
    if plugin_ids != sorted(plugin_ids, key=lambda item: item.encode("utf-8")):
        raise ContractError(
            ErrorCode.INCOMPATIBLE_GENERATION,
            "Generation members must be sorted by plugin_id UTF-8 bytes",
        )
    return value


def validate_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the complete Plan semantics, including ordered uniqueness."""
    value = copy.deepcopy(dict(plan))
    verify_plan(value)
    return value


def install_generation(
    state: GenerationState,
    candidate: Mapping[str, Any],
    *,
    expected_base_generation_id: str | None,
) -> GenerationState:
    """Apply a successful health-checked Generation using base CAS semantics."""
    value = validate_generation(candidate)
    actual = None if state.current is None else state.current["generation_id"]
    if (
        actual != expected_base_generation_id
        or value["base_generation_id"] != expected_base_generation_id
    ):
        raise ContractError(
            ErrorCode.INCOMPATIBLE_GENERATION,
            "Generation base compare-and-swap failed",
            details={"expected": expected_base_generation_id, "actual": actual},
        )
    if value["parent_generation_id"] != actual:
        raise ContractError(
            ErrorCode.INCOMPATIBLE_GENERATION,
            "Generation parent must be the current Generation",
        )
    # LKG is a qualification pointer, not an install side effect.  In
    # particular, a current Generation may be the failed generation that put
    # the host into safe mode.  Never derive LKG from ``state.current`` here;
    # only ``promote_lkg`` may advance it after an explicit evidence binding.
    return GenerationState(
        current=value,
        lkg=copy.deepcopy(state.lkg),
        safe_mode=False,
        rollback_consumed=False,
    )


def promote_lkg(
    state: GenerationState,
    *,
    evidence: Mapping[str, Any] | None = None,
    qualification_evidence: Mapping[str, Any] | None = None,
) -> GenerationState:
    """Promote the current Generation to LKG using bound health evidence.

    ``install_generation`` changes the current pointer, but the LKG pointer is
    a qualification result, not an install side effect.  The caller must
    provide an evidence record that binds both the current generation and its
    ``health_result_asset_id``.  The asset's authoritative contents are owned
    by Core/P1; this domain gate prevents a receipt for another generation
    from being attached here.

    ``qualification_evidence`` is a descriptive alias for ``evidence``.  It
    keeps call sites explicit while allowing callers to use the terminology
    used by the install transition contract.  Supplying both is rejected.
    """
    if evidence is not None and qualification_evidence is not None:
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "provide only one LKG qualification evidence record",
        )
    bound_evidence = evidence if evidence is not None else qualification_evidence
    if state.current is None:
        raise ContractError(
            ErrorCode.INCOMPATIBLE_GENERATION,
            "cannot promote LKG without a current Generation",
        )
    if state.safe_mode:
        raise ContractError(
            ErrorCode.INVALID_TRANSITION,
            "cannot promote a safe-mode current Generation to LKG",
        )
    if not isinstance(bound_evidence, Mapping):
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "LKG promotion requires a qualification evidence mapping",
        )

    current = validate_generation(state.current)
    evidence_generation_id = bound_evidence.get("generation_id")
    evidence_health_asset_id = bound_evidence.get("health_result_asset_id")
    if evidence_generation_id != current["generation_id"]:
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "LKG qualification evidence is bound to a different Generation",
            details={
                "expected": current["generation_id"],
                "actual": evidence_generation_id,
            },
        )
    if evidence_health_asset_id != current["health_result_asset_id"]:
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            "LKG qualification evidence is bound to a different health result",
            details={
                "expected": current["health_result_asset_id"],
                "actual": evidence_health_asset_id,
            },
        )

    return GenerationState(
        current=copy.deepcopy(current),
        lkg=copy.deepcopy(current),
        safe_mode=False,
        rollback_consumed=state.rollback_consumed,
    )


def rollback_once(
    state: GenerationState, *, failed_generation_id: str
) -> GenerationState:
    if state.current is None or state.current["generation_id"] != failed_generation_id:
        raise ContractError(
            ErrorCode.INCOMPATIBLE_GENERATION, "rollback target is not current"
        )
    if state.rollback_consumed or state.lkg is None:
        return GenerationState(
            current=copy.deepcopy(state.current),
            lkg=copy.deepcopy(state.lkg),
            safe_mode=True,
            rollback_consumed=True,
        )
    return GenerationState(
        current=copy.deepcopy(state.lkg),
        lkg=copy.deepcopy(state.lkg),
        safe_mode=False,
        rollback_consumed=True,
    )


def resolve_plan_generation(
    plan: Mapping[str, Any],
    generation: Mapping[str, Any],
    *,
    data_bundle_resolver: DataBundleResolver,
    release_catalog: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Resolve enabled Plan bindings without inventing a semver range language.

    Contract v1 freezes ``release_requirement`` as an exact SemVer string. The installed
    manifest version is supplied by the caller in ``release_version`` only in this internal
    projection, not serialized as a public contract.
    """
    plan_value = validate_plan(plan)
    value = validate_generation(generation)
    members = {member["plugin_id"]: member for member in value["members"]}
    resolved: list[dict[str, Any]] = []
    enabled_by_id: dict[str, Mapping[str, Any]] = {}
    for binding in plan_value["bindings"]:
        if not binding["enabled"]:
            continue
        member = members.get(binding["plugin_id"])
        if member is None:
            if binding["required"]:
                raise ContractError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "required Plan binding is absent",
                    details={"binding_id": binding["binding_id"]},
                )
            continue
        if release_catalog is not None:
            descriptor = release_catalog.get(binding["plugin_id"])
            if not isinstance(descriptor, Mapping):
                raise ContractError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "Plan release identity is absent from the verified catalog",
                    details={"binding_id": binding["binding_id"]},
                )
            if (
                descriptor.get("release_id") != member["release_id"]
                or descriptor.get("version") != binding["release_requirement"]
            ):
                raise ContractError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "Plan exact release requirement conflicts with current Generation",
                    details={"binding_id": binding["binding_id"]},
                )
            capabilities = descriptor.get("capabilities")
            if capabilities is None or binding["capability_id"] not in capabilities:
                raise ContractError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "Plan capability is not exported by the selected release",
                    details={"binding_id": binding["binding_id"]},
                )
        enabled_by_id[binding["binding_id"]] = binding
        resolved.append(
            {"binding": copy.deepcopy(binding), "member": copy.deepcopy(member)}
        )

    # Data bindings preserve their own user order and must resolve both the
    # exact data release and an enabled interpreter binding.  They remain an
    # internal projection so the public Plan/Generation schemas stay closed.
    for data_binding in plan_value["data_bindings"]:
        if not data_binding["enabled"]:
            continue
        member = members.get(data_binding["data_plugin_id"])
        interpreter = enabled_by_id.get(data_binding["interpreter_binding_id"])
        if (
            member is None
            or interpreter is None
            or member["data_generation_id"] is None
            or member["data_bundle_asset_id"] is None
        ):
            raise ContractError(
                ErrorCode.DATA_INTERPRETER_UNAVAILABLE,
                "enabled Data binding cannot resolve its exact data generation/bundle/interpreter",
                details={"data_binding_id": data_binding["data_binding_id"]},
            )
        if release_catalog is not None:
            data_descriptor = release_catalog.get(data_binding["data_plugin_id"])
            interpreter_descriptor = release_catalog.get(interpreter["plugin_id"])
            if (
                not isinstance(data_descriptor, Mapping)
                or data_descriptor.get("release_id") != member["release_id"]
                or data_descriptor.get("version") != data_binding["release_requirement"]
                or data_descriptor.get("data_bundle_asset_id")
                != member["data_bundle_asset_id"]
            ):
                raise ContractError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "Data binding exact release conflicts with current Generation",
                    details={"data_binding_id": data_binding["data_binding_id"]},
                )
            accepted = (
                None
                if not isinstance(interpreter_descriptor, Mapping)
                else interpreter_descriptor.get("accepted_data_formats")
            )
            if accepted is None or data_binding["format_id"] not in accepted:
                raise ContractError(
                    ErrorCode.DATA_INTERPRETER_UNAVAILABLE,
                    "interpreter does not accept the Data binding format",
                    details={"data_binding_id": data_binding["data_binding_id"]},
                )
        try:
            bundle = data_bundle_resolver(member["data_bundle_asset_id"])
        except (KeyError, LookupError) as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "Data bundle Asset is absent",
                details={
                    "data_binding_id": data_binding["data_binding_id"],
                    "asset_id": member["data_bundle_asset_id"],
                },
            ) from exc
        if not isinstance(bundle, Mapping):
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "Data bundle resolver did not return an immutable Asset payload",
                details={"data_binding_id": data_binding["data_binding_id"]},
            )
        bundle_value = copy.deepcopy(dict(bundle))
        verify_data_bundle(bundle_value)
        if (
            bundle_value["data_plugin_id"] != data_binding["data_plugin_id"]
            or bundle_value["data_release_id"] != member["release_id"]
            or bundle_value["package_hash"] != member["package_hash"]
            or bundle_value["format_id"] != data_binding["format_id"]
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "Data bundle Asset identity conflicts with Plan/Generation",
                details={"data_binding_id": data_binding["data_binding_id"]},
            )
        resolved.append(
            {
                "data_binding": copy.deepcopy(data_binding),
                "member": copy.deepcopy(member),
                "data_bundle_asset_id": member["data_bundle_asset_id"],
                "bundle_hash": bundle_value["bundle_hash"],
                "interpreter_binding": copy.deepcopy(interpreter),
            }
        )
    return tuple(resolved)
