import json
from dataclasses import replace
from pathlib import Path

import plotpilot_project_planner as planner_package
import plotpilot_project_planner.runtime as planner_runtime
import pytest
from plotpilot_project_planner import (
    PLANNER_CAPABILITY_ID,
    PLANNER_OPERATION,
    PLANNER_PROMPT_SKILL_ID,
    PlannerBindingSelection,
    PlannerDocumentTarget,
    PlannerRuntimeError,
    PlannerRuntimeIntegrationDeferred,
    PlanningInput,
    freeze_planner_run,
    planner_prepare_operation_key,
    prepare_planner_run,
)

from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

ROOT = Path(__file__).resolve().parents[3]
HASHES = [character * 64 for character in "abcdef123456789"]
DEPENDENCIES = [
    "NW-P2-PROMPT-SKILL-RUNTIME-02",
    "NW-P1-CORE-HTTP-G2",
    "NW-P2-PLUGIN-API-01",
    "NW-P3-JOB-RPC-02",
    "NW-P0-RUNTIME-COMPOSITION-02",
]


def _snapshot():
    value = json.loads((ROOT / "contracts/examples/run-snapshot.json").read_text("utf-8"))
    value.update({
        "snapshot_id": "snapshot-planner-1",
        "workspace_id": "workspace-planner",
        "scope": {
            "document_id": "document-setting",
            "node_id": None,
            "operation": PLANNER_OPERATION,
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
                "skill_id": PLANNER_PROMPT_SKILL_ID,
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
    return _rehash(value)


def _rehash(value):
    value["request_key"] = request_key(value)
    value["snapshot_hash"] = snapshot_hash(value)
    return value


def _selection(**changes):
    values = {
        "prompt_skill_id": PLANNER_PROMPT_SKILL_ID,
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


def _frozen():
    return freeze_planner_run(_snapshot(), _selection())


def _operation_key(*, frozen=None, generated=None, targets=None, attempt_id="attempt-planner-1", **kwargs):
    return planner_prepare_operation_key(
        frozen or _frozen(),
        generated or _generated(),
        targets or _targets(),
        attempt_id=attempt_id,
        **kwargs,
    )


def _prepared(*, frozen=None, generated=None, targets=None, attempt_id="attempt-planner-1", operation_key=None, **kwargs):
    frozen = frozen or _frozen()
    generated = generated or _generated()
    targets = targets or _targets()
    operation_key = operation_key or _operation_key(
        frozen=frozen,
        generated=generated,
        targets=targets,
        attempt_id=attempt_id,
        **kwargs,
    )
    return prepare_planner_run(
        frozen,
        generated,
        targets,
        attempt_id=attempt_id,
        operation_key=operation_key,
        **kwargs,
    )


def test_freeze_binds_operation_workspace_prompt_skill_model_plan_release_and_settings():
    frozen = _frozen()
    assert frozen.operation == PLANNER_OPERATION == PLANNER_CAPABILITY_ID
    assert frozen.workspace_id == "workspace-planner"
    assert frozen.prompt_skill_id == PLANNER_PROMPT_SKILL_ID
    assert frozen.binding_dict()["planner_settings"]["scope_id"] == "workspace-planner"
    assert [skill.order for skill in frozen.skills] == [10, 20]


@pytest.mark.parametrize(
    "mutate,error",
    [
        (lambda value: value["scope"].update(operation="writing.chapter.generate/v1"), "operation"),
        (lambda value: value["plugin_settings_revisions"][0].update(scope_id="workspace-other"), "cross"),
        (lambda value: value["plugin_settings_revisions"][0].update(scope="global", scope_id="workspace-planner"), "null scope_id"),
    ],
)
def test_freeze_rejects_operation_and_settings_scope_drift(mutate, error):
    value = _snapshot()
    mutate(value)
    _rehash(value)
    with pytest.raises(PlannerRuntimeError, match=error):
        freeze_planner_run(value, _selection())


def test_arbitrary_skill_cannot_be_designated_as_prompt():
    with pytest.raises(PlannerRuntimeError, match="compatibility designation"):
        _selection(prompt_skill_id="com.plotpilot.skill.story-structure")


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": "workspace-forged"},
        {"snapshot_hash": "0" * 64},
        {"binding_fingerprint": "0" * 64},
        {"operation": "writing.chapter.generate/v1"},
    ],
)
def test_prepare_boundary_revalidates_opaque_frozen_run(changes):
    forged = replace(_frozen(), **changes)
    with pytest.raises(PlannerRuntimeError, match="forged|operation"):
        _operation_key(frozen=forged)


def test_prepare_boundary_revalidates_embedded_snapshot():
    frozen = _frozen()
    snapshot = _snapshot()
    snapshot["workspace_id"] = "workspace-forged"
    snapshot["plugin_settings_revisions"][0]["scope_id"] = "workspace-forged"
    _rehash(snapshot)
    forged = replace(frozen, snapshot=snapshot)
    with pytest.raises(PlannerRuntimeError, match="forged|drifted"):
        _operation_key(frozen=forged)


def test_frozen_run_type_and_finalization_values_are_not_public():
    for name in (
        "FrozenPlannerRun",
        "PlannerCandidateBatch",
        "SkillChainAttachment",
        "materialize_candidate_batch",
    ):
        assert not hasattr(planner_package, name)
        assert name not in planner_runtime.__all__


def test_prepare_exact_setting_bible_outline_in_fixed_order_and_strict_bytes():
    prepared = _prepared()
    assert [item.role for item in prepared.candidates] == ["setting", "bible", "outline"]
    assert prepared.candidate("setting").payload == (
        '{"genres":["悬疑","都市"],"premise":"失忆侦探寻找真相"}'.encode()
    )
    assert prepared.candidate("bible").mime == "text/plain; charset=utf-8"
    assert prepared.candidate("outline").mime == "application/json"


def test_exact_retry_converges_on_operation_payload_and_proposal_identity():
    first = _prepared()
    replay = _prepared(operation_key=first.operation_key)
    assert replay.operation_key == first.operation_key
    assert replay.operation_payload_hash == first.operation_payload_hash
    assert replay.proposal_id == first.proposal_id
    assert [item.item_id for item in replay.candidates] == [item.item_id for item in first.candidates]


def test_same_operation_key_with_conflicting_payload_is_rejected():
    first = _prepared()
    changed = _generated()
    changed["outline"] = {"acts": [{"act": 1, "goal": "冲突输出"}]}
    with pytest.raises(PlannerRuntimeError, match="operation key was reused"):
        _prepared(generated=changed, operation_key=first.operation_key)


def test_rerun_and_parent_lineage_stop_without_authoritative_cas():
    first = _prepared()
    with pytest.raises(PlannerRuntimeIntegrationDeferred, match="previous-version CAS"):
        planner_prepare_operation_key(
            first.frozen_run,
            _generated(),
            _targets(),
            attempt_id="attempt-planner-2",
            ordinal=2,
            previous_version=first,
        )
    with pytest.raises(PlannerRuntimeIntegrationDeferred, match="authoritative lookup"):
        _operation_key(parent_candidate_ids={"setting": ("candidate-setting-v1",)})


def test_producer_asset_receipt_and_skill_chain_strings_have_no_finalization_surface():
    prepared = _prepared()
    assert prepared.proposal_id.startswith("planner-proposal-")
    for forbidden in (
        "producer",
        "payload_asset_ids",
        "provenance_receipt_id",
        "skill_chain_attachments",
        "bundle",
        "publish",
        "accept",
        "stage_candidate",
    ):
        assert not hasattr(prepared, forbidden)
    assert not hasattr(planner_runtime, "materialize_candidate_batch")


def test_distinct_role_targets_are_required():
    targets = _targets()
    targets["bible"] = PlannerDocumentTarget(
        "bible", "workspace-planner", "document-setting", "revision-setting-1", HASHES[0]
    )
    with pytest.raises(PlannerRuntimeError, match="distinct documents"):
        _operation_key(targets=targets)


def test_snapshot_backed_revision_and_asset_sources_are_frozen():
    refs = {
        "setting": (
            {
                "workspace_id": "workspace-planner",
                "source_type": "core.revision",
                "source_id": "revision-setting-1",
                "revision_or_hash": HASHES[0],
            },
            {
                "workspace_id": "workspace-planner",
                "source_type": "core.asset",
                "source_id": "asset-run-parameters",
                "revision_or_hash": HASHES[11],
            },
        )
    }
    prepared = _prepared(source_refs=refs)
    assert [item["source_type"] for item in prepared.candidate("setting").source_refs] == [
        "core.revision", "core.asset"
    ]


@pytest.mark.parametrize(
    "source,error",
    [
        ({"workspace_id": "workspace-other", "source_type": "core.revision", "source_id": "revision-setting-1", "revision_or_hash": HASHES[0]}, "crosses"),
        ({"workspace_id": None, "source_type": "external", "source_id": "source-1", "revision_or_hash": HASHES[0]}, "lacks"),
        ({"workspace_id": "workspace-planner", "source_type": "core.revision", "source_id": "revision-setting-1", "revision_or_hash": HASHES[1]}, "not backed"),
        ({"workspace_id": "workspace-planner", "source_type": "core.asset", "source_id": "asset-run-parameters", "revision_or_hash": HASHES[0]}, "not hash-backed"),
        ({"workspace_id": "workspace-planner", "source_type": "unknown", "source_id": "source-1", "revision_or_hash": HASHES[0]}, "lacks an accepted"),
    ],
)
def test_unfrozen_or_cross_workspace_sources_fail_closed(source, error):
    with pytest.raises(PlannerRuntimeError, match=error):
        _operation_key(source_refs={"setting": (source,)})


def test_duplicate_source_identity_is_rejected():
    source = {
        "workspace_id": "workspace-planner",
        "source_type": "core.revision",
        "source_id": "revision-setting-1",
        "revision_or_hash": HASHES[0],
    }
    with pytest.raises(PlannerRuntimeError, match="duplicate identity"):
        _operation_key(source_refs={"setting": (source, source)})


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
def test_invalid_output_and_target_fail_closed(generated, targets, error):
    with pytest.raises(PlannerRuntimeError, match=error):
        _operation_key(generated=generated, targets=targets)


@pytest.mark.parametrize("target_words", [True, 1.5, 0, -1])
def test_legacy_planning_input_rejects_non_integer_or_non_positive_word_counts(target_words):
    with pytest.raises(ValueError, match="positive integer"):
        PlanningInput("premise", ("genre",), target_words, "structure")


def test_runtime_integration_delta_is_exact_and_includes_cancel_impact():
    value = json.loads((
        ROOT / "coordination/PPA-05/planner-runtime/runtime-integration-delta-v1.json"
    ).read_text("utf-8"))
    assert value["source_readiness"] == "source_ready_integration_deferred"
    assert value["production_integration_claimed"] is False
    assert value["integration_dependencies"] == DEPENDENCIES
    assert "host.capability.cancel/v1" in value["host_contract_impacts"]
    assert value["host_contract_impacts"]["host.capability.cancel/v1"]["params"] == [
        "operation_key", "child_job_id", "reason"
    ]
    assert "input_snapshot_hash" not in value["host_contract_impacts"]["host.job.complete/v1"]["params"]
    assert value["affected_slice_stopped"] is True


def test_compileall_raw_evidence_has_auditable_command_streams_and_exit_code():
    text = (
        ROOT / "docs/deliveries/PPA-05/planner-runtime/evidence/raw/compileall.txt"
    ).read_text("utf-8")
    assert "command=python -B -m compileall -q first-party-plugins/project-planner" in text
    assert "stdout_begin" in text and "stdout_end" in text
    assert "stderr_begin" in text and "stderr_end" in text
    assert "exit_code=0" in text
