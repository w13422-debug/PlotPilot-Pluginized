from __future__ import annotations

import json
from hashlib import sha256

import pytest

from plotpilot_chapter_workflow import (
    BrokerEvent,
    ChapterOperation,
    ChapterRequest,
    RewriteSelection,
    SessionState,
    SkillRef,
    WorkflowError,
)

from conftest import chain_ref, event_script, make_request


def test_start_assembles_canonical_exact_context_and_is_idempotent(ports):
    workflow, core, broker, bundles, publication, story = ports
    request = make_request()
    first = workflow.start(request)
    second = workflow.start(request)
    assert first == second
    assert len(broker.starts) == 1 and len(core.assets) == 1
    context = json.loads(core.assets[broker.starts[0].input_asset_id])
    assert context["operation"] == "writing.chapter.draft/v1"
    assert context["sources"][0]["content"] == "上一章\r\n林岚推开门。"
    assert broker.starts[0].input_hash == sha256(core.assets[broker.starts[0].input_asset_id]).hexdigest()
    changed = make_request(operation_key=request.operation_key)
    changed = ChapterRequest(
        changed.operation_key,
        changed.operation,
        changed.target,
        changed.context_plan,
        changed.producer,
        "不同指令",
        changed.rewrite_selection,
    )
    with pytest.raises(WorkflowError, match="reused"):
        workflow.start(changed)


def test_generate_runs_raw_then_ordered_skills_and_stages_one_complete_candidate(ports):
    workflow, core, broker, bundles, publication, story = ports
    skills = (
        SkillRef(10, "skill.character", "1" * 64, "2" * 64),
        SkillRef(20, "skill.project", "3" * 64, "4" * 64),
    )
    current = workflow.start(make_request(skills=skills))
    broker.poll_events[current.invocation_id] = event_script(("原始".encode(), "正文".encode()))
    current = workflow.poll(current.session_id)
    assert current.phase == "skill" and current.skill_index == 0
    assert broker.starts[1].skill == skills[0]
    assert core.assets[broker.starts[1].input_asset_id] == "原始正文".encode()

    broker.poll_events[current.invocation_id] = event_script(("人物".encode(),), chain=chain_ref(broker.starts[1], 1))
    current = workflow.poll(current.session_id)
    assert broker.starts[2].skill == skills[1]
    assert core.assets[broker.starts[2].input_asset_id] == "人物".encode()

    broker.poll_events[current.invocation_id] = event_script(("终稿🙂".encode(),), chain=chain_ref(broker.starts[2], 2))
    current = workflow.poll(current.session_id)
    assert current.state is SessionState.COMPLETED
    assert current.candidate.content == "终稿🙂".encode()
    assert current.candidate.status == "complete"
    assert len(bundles.calls) == len(core.stage_calls) == 1
    bundle = bundles.bundles[current.candidate.bundle_id]
    assert [ref["chain_result_id"] for ref in bundle["skill_chain_result_refs"]] == ["chain-1", "chain-2"]
    assert all(ref["result_bundle_id"] == bundle["bundle_id"] for ref in bundle["skill_chain_result_refs"])


@pytest.mark.parametrize(
    ("operation", "capability", "mutation"),
    [
        (ChapterOperation.GENERATE, "writing.chapter.draft/v1", "replace"),
        (ChapterOperation.CONTINUE, "writing.chapter.continue/v1", "append_text"),
        (ChapterOperation.REWRITE, "writing.chapter.rewrite/v1", "replace"),
    ],
)
def test_operation_profiles_are_typed_and_contract_legal(ports, operation, capability, mutation):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request(operation))
    invocation = broker.starts[0]
    assert invocation.capability_id == capability
    context = json.loads(core.assets[invocation.input_asset_id])
    assert (context["rewrite_selection"] is not None) == (operation is ChapterOperation.REWRITE)
    broker.poll_events[current.invocation_id] = event_script((b"result",))
    current = workflow.poll(current.session_id)
    item = bundles.bundles[current.candidate.bundle_id]["items"][0]
    assert item["mutation"] == {"mode": mutation, "payload_schema": "core/document-text/v1", "payload_hash": sha256(b"result").hexdigest()}
    assert item["base"] == {"revision_id": "revision-1", "content_hash": "a" * 64}
    assert item["write_set"] == [{"workspace_id": "ws-1", "entity_kind": "document", "entity_id": "chapter-1", "revision_id": "revision-1", "content_hash": "a" * 64}]


def test_rewrite_selection_and_skill_release_fail_before_any_port_call(ports):
    workflow, core, broker, bundles, publication, story = ports
    base = make_request(ChapterOperation.REWRITE)
    with pytest.raises(WorkflowError, match="requires"):
        ChapterRequest(base.operation_key, base.operation, base.target, base.context_plan, base.producer, base.instruction, None)
    with pytest.raises(WorkflowError, match="hash mismatch"):
        RewriteSelection(0, 1, "字", "f" * 64)
    weak = SkillRef(1, "skill", "rel-1", "1" * 64)
    with pytest.raises(WorkflowError, match="release_id"):
        make_request(skills=(weak,))
    assert not core.assets and not broker.starts


@pytest.mark.parametrize("action,terminal,state", [("pause", "paused", SessionState.PAUSED), ("cancel", "cancelled", SessionState.CANCELLED)])
def test_pause_and_cancel_stage_only_exact_ack_prefix_once(ports, action, terminal, state):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    chunks = ("你".encode(), b"\r\n", "🙂".encode())
    broker.control_events[(action, current.invocation_id)] = event_script(chunks, terminal)
    result = getattr(workflow, action)(current.session_id)
    exact = b"".join(chunks)
    assert result.state is state and result.candidate.partial
    assert result.candidate.content == exact
    assert result.candidate.content_hash == sha256(exact).hexdigest()
    bundle = bundles.bundles[result.candidate.bundle_id]
    assert bundle["partial"] is True
    assert bundle["items"][0]["item_kind"] == "document"
    assert bundle["items"][0]["status"] == "partial"
    assert core.assets[result.candidate.payload_asset_id] == exact
    again = getattr(workflow, action)(current.session_id)
    assert again.candidate == result.candidate
    assert len(bundles.calls) == len(core.stage_calls) == len(broker.control_calls) == 1


def test_cancel_at_skill_boundary_uses_last_complete_phase_not_empty(ports):
    workflow, core, broker, bundles, publication, story = ports
    skill = SkillRef(1, "skill", "1" * 64, "2" * 64)
    current = workflow.start(make_request(skills=(skill,)))
    broker.poll_events[current.invocation_id] = event_script(("原始完成".encode(),))
    current = workflow.poll(current.session_id)
    broker.control_events[("cancel", current.invocation_id)] = event_script((), "cancelled")
    current = workflow.cancel(current.session_id)
    assert current.candidate.content == "原始完成".encode()
    assert current.candidate.partial


def test_pause_cancel_race_has_first_terminal_winner(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    broker.control_events[("pause", current.invocation_id)] = event_script((b"a",), "paused")
    paused = workflow.pause(current.session_id)
    cancelled = workflow.cancel(current.session_id)
    assert cancelled == paused and paused.state is SessionState.PAUSED
    assert len(broker.control_calls) == 1 and len(core.stage_calls) == 1


@pytest.mark.parametrize(
    "events,match",
    [
        ([BrokerEvent(2, "acked", b"a", 1, sha256(b"a").hexdigest())], "sequence"),
        ([BrokerEvent(1, "acked", b"a", 2, sha256(b"a").hexdigest())], "prefix_size"),
        ([BrokerEvent(1, "acked", b"a", 1, "0" * 64)], "prefix_hash"),
        (event_script((b"a",)) + [BrokerEvent(3, "acked", b"b", 2, sha256(b"ab").hexdigest())], "after a terminal"),
    ],
)
def test_malformed_broker_stream_fails_closed_without_candidate(ports, events, match):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    broker.poll_events[current.invocation_id] = events
    with pytest.raises(WorkflowError, match=match):
        workflow.poll(current.session_id)
    assert not bundles.calls and not core.stage_calls


def test_skill_completion_requires_bundle_backed_chain_evidence(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request(skills=(SkillRef(1, "skill", "1" * 64, "2" * 64),)))
    broker.poll_events[current.invocation_id] = event_script((b"raw",))
    current = workflow.poll(current.session_id)
    broker.poll_events[current.invocation_id] = event_script((b"styled",))
    with pytest.raises(WorkflowError, match="chain evidence"):
        workflow.poll(current.session_id)
    assert not bundles.calls and not core.stage_calls


def test_broker_failure_is_terminal_without_candidate(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    broker.poll_events[current.invocation_id] = [BrokerEvent(1, "failed", prefix_hash=sha256(b"").hexdigest(), error="provider failed")]
    current = workflow.poll(current.session_id)
    assert current.state is SessionState.FAILED and current.error == "provider failed"
    assert current.candidate is None and not bundles.calls and not core.stage_calls
