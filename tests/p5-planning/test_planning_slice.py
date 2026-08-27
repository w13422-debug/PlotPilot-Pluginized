import json
from pathlib import Path

import jsonschema
import pytest

from plotpilot_project_planner import Binding, PlanningInput, build_candidate_drafts
from plotpilot_story_state import FactRef, Proposal, build_projection, failed_items_for_retry, partition_proposals

ROOT = Path(__file__).resolve().parents[2]


def _plan():
    return PlanningInput("失忆侦探寻找真相", ("悬疑", "都市", "悬疑"), 800000, "三幕式")


def _binding():
    return Binding("prompt@1", "skill@2", "profile@3", "plan@4", "release@5")


def _fact(workspace="ws", kind="character", entity="c1", revision="rev-1", digest="a" * 64):
    return FactRef(workspace, kind, entity, revision, digest)


def test_m0_manifest_schema_dependency_is_mandatory_and_dead_manifests_are_withheld():
    schema = json.loads((ROOT / "contracts/json-schema/plugin-manifest-v1.schema.json").read_text("utf-8"))
    fixture = json.loads((ROOT / "contracts/examples/fixtures/plugin-manifest-code.json").read_text("utf-8"))
    jsonschema.Draft202012Validator(schema).validate(fixture)
    assert not (ROOT / "first-party-plugins/project-planner/plugin.json").exists()
    assert not (ROOT / "first-party-plugins/story-state/plugin.json").exists()


def test_mixed_known_unknown_sections_fail_closed_before_any_draft():
    with pytest.raises(ValueError, match="outline_enhancement"):
        build_candidate_drafts(_plan(), _binding(), {"bible": {}, "outline_enhancement": {}}, run_id="bad")


def test_payload_sources_and_inputs_are_deep_copied_and_frozen():
    content = {"characters": [{"name": "林岚"}]}
    sources = [{"candidate_id": "source-a", "meta": [1]}]
    draft = build_candidate_drafts(_plan(), _binding(), {"bible": content}, run_id="run", source_refs=sources)[0]
    content["characters"][0]["name"] = "篡改"
    sources[0]["meta"].append(2)
    assert draft.payload["content"]["characters"][0]["name"] == "林岚"
    assert draft.source_refs[0]["meta"] == (1,)
    with pytest.raises(TypeError):
        draft.payload["role"] = "world"


def test_ordered_lineage_sources_and_result_mode_are_in_draft_identity():
    kwargs = dict(planning_input=_plan(), binding=_binding(), generated={"world": {}}, run_id="synth")
    first = build_candidate_drafts(**kwargs, parent_candidate_ids=("a", "b"), source_refs=({"id": "s1"},), result_mode="synthesize")[0]
    reversed_parents = build_candidate_drafts(**kwargs, parent_candidate_ids=("b", "a"), source_refs=({"id": "s1"},), result_mode="synthesize")[0]
    changed_source = build_candidate_drafts(**kwargs, parent_candidate_ids=("a", "b"), source_refs=({"id": "s2"},), result_mode="synthesize")[0]
    separate = build_candidate_drafts(**kwargs, source_refs=({"id": "s1"},), result_mode="separate")[0]
    assert len({first.draft_key, reversed_parents.draft_key, changed_source.draft_key, separate.draft_key}) == 4
    assert first.parent_candidate_ids == ("a", "b")


@pytest.mark.parametrize("mode,parents", [("merge", ()), ("synthesize", ("a",))])
def test_invalid_result_modes_and_synthesis_lineage_fail_closed(mode, parents):
    with pytest.raises(ValueError):
        build_candidate_drafts(_plan(), _binding(), {"bible": {}}, run_id="bad", result_mode=mode, parent_candidate_ids=parents)


def test_partial_and_status_are_consistent_immutable_carriers():
    draft = build_candidate_drafts(_plan(), _binding(), {"items": []}, run_id="partial", status="partial", partial=True)[0]
    assert draft.status == "partial" and draft.partial is True
    with pytest.raises(ValueError, match="agree"):
        build_candidate_drafts(_plan(), _binding(), {"items": []}, run_id="bad", status="complete", partial=True)


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64, ""])
def test_fact_ref_requires_exact_lowercase_sha256(digest):
    with pytest.raises(ValueError, match="SHA-256"):
        _fact(digest=digest)


def test_projection_identity_includes_workspace_and_kind_and_edges_use_full_keys():
    facts = (_fact("ws-a", "character", "same", "rev-1", "a" * 64), _fact("ws-b", "character", "same", "rev-2", "b" * 64), _fact("ws-a", "item", "same", "rev-3", "c" * 64))
    source, target = facts[0].key, facts[2].key
    projection = build_projection(facts, ((source, "owns", target),))
    assert projection.by_scope[("ws-a", "character")] == (source,)
    assert projection.by_scope[("ws-a", "item")] == (target,)
    assert projection == build_projection(reversed(facts), ((source, "owns", target),))
    with pytest.raises(ValueError, match="known entities"):
        build_projection(facts, ((("ws-x", "character", "same"), "owns", target),))


@pytest.mark.parametrize("outcome,error", [("success", "unexpected"), ("failure", None), ("failure", ""), ("failure", "   "), ("unknown", None)])
def test_proposal_has_explicit_valid_outcome_and_nonblank_failure(outcome, error):
    with pytest.raises(ValueError):
        Proposal("p", "op", "item", "try-1", _fact(), {}, outcome=outcome, error=error)


def test_partition_and_retry_preserve_identity_and_retry_only_failures():
    ok = Proposal("p-ok", "op-1", "item-ok", "try-1", _fact(), {"status": "advanced"})
    failed = Proposal("p-fail", "op-1", "item-fail", "try-1", _fact(), {}, outcome="failure", error="parent unavailable")
    complete, failures = partition_proposals((ok, failed))
    assert complete == (ok,) and failures == (failed,)
    assert failed_items_for_retry((ok, failed), retry_id="try-2") == (("op-1", "item-fail", "try-2"),)


def test_duplicate_proposal_identity_and_blank_retry_fail_closed():
    first = Proposal("same", "op", "one", "try-1", _fact(), {})
    second = Proposal("same", "op", "two", "try-1", _fact(), {})
    with pytest.raises(ValueError, match="unique"):
        partition_proposals((first, second))
    duplicate_item = Proposal("other", "op", "one", "try-1", _fact(), {})
    with pytest.raises(ValueError, match="operation/item/retry"):
        partition_proposals((first, duplicate_item))
    with pytest.raises(ValueError, match="retry_id"):
        failed_items_for_retry((first,), retry_id=" ")
