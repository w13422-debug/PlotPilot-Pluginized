import copy
import json
from pathlib import Path

import pytest
from plotpilot_project_planner import (
    PlannerBindingSelection,
    PlannerDocumentTarget,
    PlannerRuntimeError,
    PlanningInput,
    SkillChainAttachment,
    freeze_planner_run,
    materialize_candidate_batch,
    prepare_planner_run,
)

from backend.plotpilot_plugin_sdk import ContractError, verify_result_bundle
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

ROOT = Path(__file__).resolve().parents[3]
HASHES = [character * 64 for character in "abcdef123456789"]


def _snapshot():
    value = json.loads((ROOT / "contracts/examples/run-snapshot.json").read_text("utf-8"))
    value.update({
        "snapshot_id": "snapshot-planner-1",
        "workspace_id": "workspace-planner",
        "scope": {
            "document_id": "document-setting",
            "node_id": None,
            "operation": "planning.project.generate/v1",
        },
        "input_revisions": [
            {"document_id": "document-setting", "revision_id": "revision-setting-1", "content_hash": HASHES[0]},
            {"document_id": "document-bible", "revision_id": "revision-bible-1", "content_hash": HASHES[1]},
            {"document_id": "document-outline", "revision_id": "revision-outline-1", "content_hash": HASHES[2]},
        ],
        "plan_revision_id": "plan-revision-7",
        "plugin_releases": [{
            "plugin_id": "com.plotpilot.project-planner",
            "release_id": HASHES[3],
            "package_hash": HASHES[4],
            "data_generation_id": None,
        }],
        "plugin_settings_revisions": [{
            "plugin_id": "com.plotpilot.project-planner",
            "settings_revision_id": "planner-settings-3",
            "scope": "workspace",
            "scope_id": "workspace-planner",
            "schema_hash": HASHES[5],
            "validated_by_release_id": HASHES[3],
        }],
        "data_bindings": [],
        "skill_releases": [
            {
                "skill_id": "com.plotpilot.prompt.project-setting",
                "release_id": HASHES[6],
                "package_hash": HASHES[7],
                "parameters_asset_id": "asset-prompt-parameters",
                "order": 10,
            },
            {
                "skill_id": "com.plotpilot.skill.story-structure",
                "release_id": HASHES[8],
                "package_hash": HASHES[9],
                "parameters_asset_id": None,
                "order": 20,
            },
        ],
        "model_profile_revision_id": "model-profile-revision-9",
        "parameters_asset_id": "asset-run-parameters",
        "asset_hashes": [
            {"asset_id": "asset-prompt-parameters", "sha256": HASHES[10]},
            {"asset_id": "asset-run-parameters", "sha256": HASHES[11]},
        ],
        "run_intent_id": "planner-intent-1",
        "created_at": "2026-08-28T00:00:00Z",
    })
    value["request_key"] = request_key(value)
    value["snapshot_hash"] = snapshot_hash(value)
    return value


def _selection(**changes):
    values = {
        "prompt_skill_id": "com.plotpilot.prompt.project-setting",
        "prompt_release_id": HASHES[6],
        "planner_release_id": HASHES[3],
        "planner_settings_revision_id": "planner-settings-3",
        "plan_revision_id": "plan-revision-7",
        "model_profile_revision_id": "model-profile-revision-9",
    }
    values.update(changes)
    return PlannerBindingSelection(**values)


def _targets(workspace="workspace-planner"):
    return {
        "setting": PlannerDocumentTarget("setting", workspace, "document-setting", "revision-setting-1", HASHES[0]),
        "bible": PlannerDocumentTarget("bible", workspace, "document-bible", "revision-bible-1", HASHES[1]),
        "outline": PlannerDocumentTarget("outline", workspace, "document-outline", "revision-outline-1", HASHES[2]),
    }


def _generated():
    return {
        "setting": {"premise": "失忆侦探寻找真相", "genres": ["悬疑", "都市"]},
        "bible": "人物：林岚\n世界：临海城",
        "outline": {"acts": [{"act": 1, "goal": "发现线索"}, {"act": 2, "goal": "揭露真相"}]},
    }


def _producer(attempt_id="attempt-planner-1"):
    return {
        "plugin_id": "com.plotpilot.project-planner",
        "release_id": HASHES[3],
        "capability_id": "planning.project.generate/v1",
        "job_id": "job-planner-1",
        "step_id": "step-planner-1",
        "attempt_id": attempt_id,
        "lease_epoch": 1,
    }


def _prepared(*, attempt_id="attempt-planner-1", **kwargs):
    return prepare_planner_run(
        freeze_planner_run(_snapshot(), _selection()),
        _generated(),
        _targets(),
        attempt_id=attempt_id,
        **kwargs,
    )


def _assets():
    return {"setting": "asset-setting-1", "bible": "asset-bible-1", "outline": "asset-outline-1"}


def test_freeze_binds_prompt_skill_model_profile_plan_release_and_settings():
    snapshot = _snapshot()
    frozen = freeze_planner_run(snapshot, _selection())
    original_fingerprint = frozen.binding_fingerprint
    snapshot["skill_releases"][0]["package_hash"] = HASHES[12]
    assert frozen.skills[0].package_hash == HASHES[7]
    assert frozen.binding_dict()["skills"][0]["package_hash"] == HASHES[7]

    changed = _snapshot()
    changed["skill_releases"][0]["package_hash"] = HASHES[12]
    changed["snapshot_hash"] = snapshot_hash(changed)
    assert freeze_planner_run(changed, _selection()).binding_fingerprint != original_fingerprint


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("plan_revision_id", "plan-revision-other", "Plan revision"),
        ("model_profile_revision_id", "model-profile-other", "Model Profile"),
        ("prompt_release_id", HASHES[12], "Prompt package"),
        ("planner_settings_revision_id", "settings-other", "settings revision"),
    ],
)
def test_freeze_rejects_binding_drift(field, value, error):
    with pytest.raises(PlannerRuntimeError, match=error):
        freeze_planner_run(_snapshot(), _selection(**{field: value}))


def test_prepare_exact_setting_bible_outline_in_fixed_order_and_strict_bytes():
    prepared = _prepared()
    assert [item.role for item in prepared.candidates] == ["setting", "bible", "outline"]
    assert prepared.candidate("setting").payload == (
        '{"genres":["悬疑","都市"],"premise":"失忆侦探寻找真相"}'.encode()
    )
    assert prepared.candidate("bible").mime == "text/plain; charset=utf-8"
    assert prepared.candidate("outline").mime == "application/json"


def test_materialized_batch_is_sdk_valid_immutable_and_has_no_publication_surface():
    prepared = _prepared()
    batch = materialize_candidate_batch(
        prepared,
        _assets(),
        producer=_producer(),
        provenance_receipt_id="receipt-planner-1",
    )
    verify_result_bundle(
        batch.to_dict(),
        snapshot_workspace_id="workspace-planner",
        snapshot_hash_value=prepared.frozen_run.snapshot_hash,
    )
    assert [item["target"]["entity_id"] for item in batch.bundle["items"]] == [
        "document-setting", "document-bible", "document-outline"
    ]
    assert all(item["mutation"]["payload_schema"] == "core/document-text/v1" for item in batch.bundle["items"])
    with pytest.raises(TypeError):
        batch.bundle["bundle_id"] = "mutated"
    for forbidden in ("publish", "accept", "stage_candidate", "invoke", "execute"):
        assert not hasattr(batch, forbidden)


def test_deterministic_rebuild_and_intentional_rerun_preserve_old_version():
    first = _prepared()
    reordered = {"outline": _generated()["outline"], "setting": _generated()["setting"], "bible": _generated()["bible"]}
    rebuilt = prepare_planner_run(
        freeze_planner_run(_snapshot(), _selection()),
        reordered,
        _targets(),
        attempt_id="attempt-planner-1",
    )
    assert rebuilt.version_id == first.version_id
    assert [item.item_id for item in rebuilt.candidates] == [item.item_id for item in first.candidates]

    second = prepare_planner_run(
        first.frozen_run,
        _generated(),
        _targets(),
        attempt_id="attempt-planner-2",
        ordinal=2,
        previous_version=first,
        parent_candidate_ids={
            "setting": ("candidate-setting-v1",),
            "bible": ("candidate-bible-v1",),
            "outline": ("candidate-outline-v1",),
        },
    )
    assert second.previous_version_id == first.version_id
    assert second.version_id != first.version_id
    assert first.previous_version_id is None
    assert first.candidate("setting").parent_candidate_ids == ()


def test_rerun_requires_new_attempt_and_contiguous_ordinal():
    first = _prepared()
    with pytest.raises(PlannerRuntimeError, match="new attempt_id"):
        prepare_planner_run(first.frozen_run, _generated(), _targets(), attempt_id=first.attempt_id, ordinal=2, previous_version=first)
    with pytest.raises(PlannerRuntimeError, match="immediately follow"):
        prepare_planner_run(first.frozen_run, _generated(), _targets(), attempt_id="attempt-other", ordinal=3, previous_version=first)


def test_parent_lineage_must_be_confirmed_by_core_before_bundle_materialization():
    first = _prepared()
    parents = {role: (f"candidate-{role}-v1",) for role in ("setting", "bible", "outline")}
    rerun = prepare_planner_run(
        first.frozen_run,
        _generated(),
        _targets(),
        attempt_id="attempt-planner-2",
        ordinal=2,
        previous_version=first,
        parent_candidate_ids=parents,
    )
    with pytest.raises(ContractError, match="parent does not exist"):
        materialize_candidate_batch(rerun, _assets(), producer=_producer("attempt-planner-2"), provenance_receipt_id="receipt-2")
    known = {parent for values in parents.values() for parent in values}
    batch = materialize_candidate_batch(
        rerun,
        _assets(),
        producer=_producer("attempt-planner-2"),
        provenance_receipt_id="receipt-2",
        known_parent_ids=known,
    )
    assert batch.bundle["items"][0]["parent_candidate_ids"] == ("candidate-setting-v1",)


def test_skill_chain_attachments_are_bound_to_exact_bundle_items():
    prepared = _prepared()
    attachment = SkillChainAttachment("chain-setting-1", "asset-chain-setting", HASHES[12], "setting")
    batch = materialize_candidate_batch(
        prepared,
        _assets(),
        producer=_producer(),
        provenance_receipt_id="receipt-planner-1",
        skill_chain_attachments=(attachment,),
    )
    ref = batch.bundle["skill_chain_result_refs"][0]
    assert ref["result_bundle_id"] == batch.bundle["bundle_id"]
    assert ref["result_item_id"] == prepared.candidate("setting").item_id


@pytest.mark.parametrize(
    "generated,targets,error",
    [
        ({"setting": {}, "bible": {}}, _targets(), "exactly setting"),
        ({**_generated(), "unknown": {}}, _targets(), "unknown"),
        ({**_generated(), "setting": float("nan")}, _targets(), "text or JSON"),
        ({**_generated(), "setting": {1: "collision", "1": "value"}}, _targets(), "non-string JSON key"),
        (_generated(), _targets("workspace-other"), "crosses the frozen Workspace"),
    ],
)
def test_invalid_output_and_target_fail_closed_before_materialization(generated, targets, error):
    frozen = freeze_planner_run(_snapshot(), _selection())
    with pytest.raises(PlannerRuntimeError, match=error):
        prepare_planner_run(frozen, generated, targets, attempt_id="attempt-invalid")


def test_payload_asset_roles_are_closed_unique_and_affect_bundle_identity():
    prepared = _prepared()
    with pytest.raises(PlannerRuntimeError, match="exactly setting"):
        materialize_candidate_batch(prepared, {"setting": "asset-1"}, producer=_producer(), provenance_receipt_id="receipt-1")
    duplicate = {role: "asset-same" for role in ("setting", "bible", "outline")}
    with pytest.raises(PlannerRuntimeError, match="distinct"):
        materialize_candidate_batch(prepared, duplicate, producer=_producer(), provenance_receipt_id="receipt-1")
    first = materialize_candidate_batch(prepared, _assets(), producer=_producer(), provenance_receipt_id="receipt-1")
    changed_assets = _assets()
    changed_assets["outline"] = "asset-outline-2"
    second = materialize_candidate_batch(prepared, changed_assets, producer=_producer(), provenance_receipt_id="receipt-1")
    assert first.bundle["bundle_id"] != second.bundle["bundle_id"]


@pytest.mark.parametrize("target_words", [True, 1.5, 0, -1])
def test_legacy_planning_input_rejects_non_integer_or_non_positive_word_counts(target_words):
    with pytest.raises(ValueError, match="positive integer"):
        PlanningInput("premise", ("genre",), target_words, "structure")


def test_frozen_snapshot_is_detached_from_mutable_input():
    snapshot = _snapshot()
    frozen = freeze_planner_run(snapshot, _selection())
    before = copy.deepcopy(dict(frozen.snapshot["scope"]))
    snapshot["scope"]["operation"] = "mutated.operation/v1"
    assert dict(frozen.snapshot["scope"]) == before
