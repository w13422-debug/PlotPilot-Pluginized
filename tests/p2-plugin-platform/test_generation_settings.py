from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from backend.plotpilot_core.plugins.generation import GenerationState, install_generation, promote_lkg, rollback_once, validate_generation
from backend.plotpilot_core.plugins.settings import accept_draft, apply_validation, require_activatable
from plotpilot_plugin_sdk import ContractError
from plotpilot_plugin_sdk.canonical import hash_jcs


ROOT = Path(__file__).resolve().parents[2]


def fixture(name: str) -> dict:
    return json.loads((ROOT / "contracts" / "examples" / "fixtures" / name).read_text(encoding="utf-8"))


def _qualification_evidence(generation: dict) -> dict[str, str]:
    return {
        "generation_id": generation["generation_id"],
        "health_result_asset_id": generation["health_result_asset_id"],
    }


def test_generation_base_cas_lkg_and_rollback_once() -> None:
    first = fixture("plugin-generation.json")
    state = install_generation(GenerationState(), first, expected_base_generation_id=None)
    state = promote_lkg(state, evidence=_qualification_evidence(first))
    second = copy.deepcopy(first)
    second.update(generation_id="generation-2", parent_generation_id="generation-1", base_generation_id="generation-1")
    state = install_generation(state, second, expected_base_generation_id="generation-1")
    assert state.lkg["generation_id"] == "generation-1"
    rolled = rollback_once(state, failed_generation_id="generation-2")
    assert rolled.current["generation_id"] == "generation-1"
    safe = rollback_once(rolled, failed_generation_id="generation-1")
    assert safe.safe_mode is True


def test_lkg_promotion_is_bound_and_safe_mode_cannot_promote_failed_current() -> None:
    first = fixture("plugin-generation.json")
    state = install_generation(GenerationState(), first, expected_base_generation_id=None)
    with pytest.raises(ContractError):
        promote_lkg(state, evidence={"generation_id": "other", "health_result_asset_id": "asset-health-1"})
    state = promote_lkg(state, qualification_evidence=_qualification_evidence(first))

    second = copy.deepcopy(first)
    second.update(generation_id="generation-2", parent_generation_id="generation-1", base_generation_id="generation-1")
    state = install_generation(state, second, expected_base_generation_id="generation-1")

    # A persisted rollback token can be consumed while the failed current is
    # still present.  The failed rollback must enter safe mode without moving
    # the qualified LKG pointer.
    failed = GenerationState(
        current=state.current,
        lkg=state.lkg,
        rollback_consumed=True,
    )
    safe = rollback_once(failed, failed_generation_id="generation-2")
    assert safe.safe_mode is True
    assert safe.current["generation_id"] == "generation-2"
    assert safe.lkg["generation_id"] == "generation-1"

    with pytest.raises(ContractError):
        promote_lkg(safe, evidence=_qualification_evidence(second))

    third = copy.deepcopy(second)
    third.update(generation_id="generation-3", parent_generation_id="generation-2", base_generation_id="generation-2")
    recovered = install_generation(safe, third, expected_base_generation_id="generation-2")
    assert recovered.safe_mode is False
    assert recovered.current["generation_id"] == "generation-3"
    assert recovered.lkg["generation_id"] == "generation-1"


def test_generation_rejects_stale_base_and_duplicate_plugin() -> None:
    value = fixture("plugin-generation.json")
    state = install_generation(GenerationState(), value, expected_base_generation_id=None)
    with pytest.raises(ContractError):
        install_generation(state, value, expected_base_generation_id=None)
    member = {
        "plugin_id": "com.example.same", "release_id": "a" * 64, "package_hash": "b" * 64,
        "data_generation_id": None, "ui_bundle_hash": None, "global_settings_revision_id": None,
        "settings_schema_hash": None, "data_bundle_asset_id": None,
    }
    value["members"] = [member, copy.deepcopy(member)]
    with pytest.raises(ContractError):
        validate_generation(value)


def test_settings_receipt_binding_and_activation() -> None:
    draft = fixture("settings-revision.json")
    accepted = accept_draft(draft, parent=None)
    receipt = fixture("settings-validation-receipt.json")
    receipt["receipt_hash"] = hash_jcs("settings-validation-receipt/v1", {k: v for k, v in receipt.items() if k != "receipt_hash"})
    validated = apply_validation(accepted, receipt)
    require_activatable(validated, release_id=draft["plugin_release_id"], schema_hash=draft["schema_hash"])
    tampered = copy.deepcopy(receipt)
    tampered["payload_hash"] = "b" * 64
    tampered["receipt_hash"] = hash_jcs("settings-validation-receipt/v1", {k: v for k, v in tampered.items() if k != "receipt_hash"})
    with pytest.raises(ContractError):
        apply_validation(accepted, tampered)


def test_settings_history_is_immutable_and_parent_bound() -> None:
    parent = fixture("settings-revision.json")
    parent["state"] = "validated"
    child = fixture("settings-revision.json")
    child["settings_revision_id"] = "settings-2"
    child["parent_revision_id"] = "wrong"
    with pytest.raises(ContractError):
        accept_draft(child, parent=parent)
