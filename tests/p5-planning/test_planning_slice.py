import json
from pathlib import Path

import pytest

from plotpilot_project_planner import Binding, PlanningInput, build_candidate_drafts
from plotpilot_story_state import FactRef, Proposal, build_projection, partition_proposals


ROOT = Path(__file__).resolve().parents[2]


def test_plugin_manifests_validate_against_m0_contract():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "contracts/json-schema/plugin-manifest-v1.schema.json").read_text("utf-8"))
    for path in (ROOT / "first-party-plugins/project-planner/plugin.json", ROOT / "first-party-plugins/story-state/plugin.json"):
        jsonschema.Draft202012Validator(schema).validate(json.loads(path.read_text("utf-8")))


def test_planner_freezes_inputs_bindings_and_isolates_reruns():
    plan = PlanningInput("失忆侦探寻找真相", ("悬疑", "都市", "悬疑"), 800000, "三幕式")
    binding = Binding("prompt@1", "skill@2", "profile@3", "plan@4", "release@5")
    generated = {"bible": {"theme": "记忆与身份"}, "characters": [{"name": "林岚"}]}
    first = build_candidate_drafts(plan, binding, generated, run_id="run-1")
    rerun = build_candidate_drafts(plan, binding, generated, run_id="run-2")
    assert [d.target_role for d in first] == ["bible", "characters"]
    assert first[0].payload["binding"]["release_id"] == "release@5"
    assert {d.draft_key for d in first}.isdisjoint(d.draft_key for d in rerun)


def test_synthesis_requires_explicit_parents_and_missing_sections_fail_closed():
    plan = PlanningInput("梗概", ("奇幻",), 1000, "单线")
    binding = Binding("p", "s", "m", "plan", "r")
    draft = build_candidate_drafts(plan, binding, {"world": {}}, run_id="synth-1", parent_candidate_ids=("a", "b"))[0]
    assert draft.parent_candidate_ids == ("a", "b")
    with pytest.raises(ValueError, match="no supported"):
        build_candidate_drafts(plan, binding, {"outline_enhancement": {}}, run_id="bad")


def test_story_state_partial_partition_and_failed_item_retry_identity():
    base = FactRef("ws", "foreshadowing", "f1", "rev-7", "a" * 64)
    ok = Proposal("p-ok", base, {"status": "advanced"})
    failed = Proposal("p-fail", base, {}, error="parent unavailable")
    complete, failures = partition_proposals((ok, failed))
    assert complete == (ok,)
    assert failures == (failed,)


def test_projection_is_deterministic_rebuildable_and_rejects_orphans():
    facts = (
        FactRef("ws", "character", "c2", "rev-2", "b" * 64),
        FactRef("ws", "character", "c1", "rev-1", "a" * 64),
        FactRef("ws", "item", "i1", "rev-3", "c" * 64),
    )
    projection = build_projection(facts, (("c1", "owns", "i1"), ("c2", "knows", "c1")))
    assert projection.source_revisions == ("rev-1", "rev-2", "rev-3")
    assert projection.by_kind["character"] == ("c1", "c2")
    assert projection == build_projection(reversed(facts), reversed((('c1', 'owns', 'i1'), ('c2', 'knows', 'c1'))))
    with pytest.raises(ValueError, match="known entities"):
        build_projection(facts, (("missing", "owns", "i1"),))
