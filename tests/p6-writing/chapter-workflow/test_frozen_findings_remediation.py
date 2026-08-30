from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from typing import Any, Mapping

import pytest

from plotpilot_chapter_workflow import (
    ChapterOperation,
    ChapterRequest,
    FrozenContextPlan,
    RewriteSelection,
    SessionState,
    SkillRef,
    WorkflowError,
    freeze_context_plan,
)

from conftest import chain_ref, event_script, make_request, publication_result, story_bundle


ROOT = Path(__file__).resolve().parents[3]


def _rewrite_request(selection: RewriteSelection) -> ChapterRequest:
    base = make_request(ChapterOperation.REWRITE)
    return ChapterRequest(
        base.operation_key,
        base.operation,
        base.target,
        base.context_plan,
        base.producer,
        base.instruction,
        selection,
    )


def _selection_receipt(request: ChapterRequest) -> dict[str, Any]:
    selection = request.rewrite_selection
    assert selection is not None
    return {
        "schema": "rewrite-selection-receipt/v1",
        "receipt_id": "selection-receipt-1",
        "workspace_id": request.target.workspace_id,
        "document_id": request.target.document_id,
        "base_revision_id": request.target.base_revision_id,
        "base_content_hash": request.target.base_content_hash,
        "current_revision_id": request.target.base_revision_id,
        "total_codepoints": 100,
        "start_codepoint": selection.start_codepoint,
        "end_codepoint": selection.end_codepoint,
        "selected_text": selection.selected_text,
        "selected_hash": selection.selected_hash,
    }


def _thread_call(target, output: dict[str, Any], key: str) -> None:
    try:
        output[key] = target()
    except BaseException as exc:  # test must surface thread failures
        output[key] = exc


def test_frozen_plan_rejects_mutable_containers_wrong_members_and_forged_identity():
    request = make_request()
    plan = request.context_plan
    with pytest.raises(ValueError, match="sources must be a tuple"):
        FrozenContextPlan(plan.operation, list(plan.sources), plan.skills, plan.fingerprint)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="contain ContextSource"):
        FrozenContextPlan(plan.operation, ("not-a-source",), plan.skills, plan.fingerprint)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fingerprint"):
        FrozenContextPlan(plan.operation, plan.sources, plan.skills, "0" * 64)

    first = SkillRef(20, "skill.second", "1" * 64, "2" * 64)
    second = SkillRef(10, "skill.first", "3" * 64, "4" * 64)
    with pytest.raises(ValueError, match="ascending"):
        FrozenContextPlan(plan.operation, plan.sources, (first, second), "0" * 64)
    duplicate_release = (
        SkillRef(10, "skill.same", "5" * 64, "6" * 64),
        SkillRef(20, "skill.same", "5" * 64, "7" * 64),
    )
    with pytest.raises(ValueError, match="duplicate Skill release"):
        FrozenContextPlan(plan.operation, plan.sources, duplicate_release, "0" * 64)


def test_freeze_snapshots_mutable_inputs_and_start_revalidates_corruption(ports):
    workflow, core, broker, bundles, publication, story = ports
    base = make_request()
    sources = list(base.context_plan.sources)
    skills = [SkillRef(10, "skill.one", "1" * 64, "2" * 64)]
    plan = freeze_context_plan(base.operation.capability_id, sources, skills)
    sources.clear()
    skills.append(SkillRef(20, "skill.two", "3" * 64, "4" * 64))
    assert len(plan.sources) == len(plan.skills) == 1

    request = ChapterRequest(
        base.operation_key,
        base.operation,
        base.target,
        plan,
        base.producer,
        base.instruction,
    )
    object.__setattr__(plan, "fingerprint", "0" * 64)
    with pytest.raises(WorkflowError, match="workflow start"):
        workflow.start(request)
    assert not core.assets and not broker.starts


@pytest.mark.parametrize("operation", list(ChapterOperation))
@pytest.mark.parametrize("output", [b"", b" \r\n\t"])
def test_blank_complete_output_converges_failed_without_candidate(ports, operation, output):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request(operation))
    broker.poll_events[current.invocation_id] = event_script((output,) if output else ())
    failed = workflow.poll(current.session_id)
    assert failed.state is SessionState.FAILED
    assert failed.candidate is None and "non-blank" in failed.error
    assert not bundles.calls and not core.stage_calls and not publication.calls
    with pytest.raises(WorkflowError, match="complete"):
        workflow.accept(failed.session_id, accepted_by="author", publication_operation_key="publish-empty")


def test_complete_and_partial_truncated_utf8_never_stage_document_candidate(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    broker.poll_events[current.invocation_id] = event_script((b"\xf0\x9f",))
    failed = workflow.poll(current.session_id)
    assert failed.state is SessionState.FAILED and "UTF-8" in failed.error
    assert not bundles.calls and not core.stage_calls

    workflow2, core2, broker2, bundles2, publication2, story2 = _make_fresh_ports()
    current2 = workflow2.start(make_request(operation_key="write-partial-utf8"))
    broker2.control_events[("pause", current2.invocation_id)] = event_script((b"\xf0\x9f",), "paused")
    with pytest.raises(WorkflowError, match="matching terminal"):
        workflow2.pause(current2.session_id)
    snapshot = workflow2.poll(current2.session_id)
    assert snapshot.state is SessionState.FAILED and snapshot.candidate is None
    assert not bundles2.calls and not core2.stage_calls
def test_utf8_is_validated_after_full_ack_assembly(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    broker.poll_events[current.invocation_id] = event_script((b"\xf0\x9f", b"\x99\x82"))
    completed = workflow.poll(current.session_id)
    assert completed.state is SessionState.COMPLETED
    assert completed.candidate.content == "🙂".encode("utf-8")


def test_final_skill_blank_output_fails_instead_of_reusing_previous_phase(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request(skills=(SkillRef(1, "skill", "1" * 64, "2" * 64),)))
    broker.poll_events[current.invocation_id] = event_script((b"raw",))
    current = workflow.poll(current.session_id)
    broker.poll_events[current.invocation_id] = event_script((), chain=chain_ref(broker.starts[1], 1))
    failed = workflow.poll(current.session_id)
    assert failed.state is SessionState.FAILED and failed.candidate is None
    assert not bundles.calls and not core.stage_calls


def test_candidate_asset_failure_retries_without_repolling_broker(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    original = core.create_asset
    failures = 1

    def flaky(content: bytes, *, mime: str):
        nonlocal failures
        if mime.startswith("text/plain") and failures:
            failures -= 1
            raise RuntimeError("asset temporarily unavailable")
        return original(content, mime=mime)

    core.create_asset = flaky
    broker.poll_events[current.invocation_id] = event_script((b"complete",))
    with pytest.raises(WorkflowError, match="Asset creation failed"):
        workflow.poll(current.session_id)
    pending = workflow._sessions[current.session_id]
    assert pending.state is SessionState.FINALIZING and pending.candidate is None
    completed = workflow.poll(current.session_id)
    assert completed.state is SessionState.COMPLETED and completed.candidate.content == b"complete"
    assert len(broker.poll_calls) == 1


def test_candidate_persist_and_stage_failures_resume_confirmed_steps(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    original_persist = bundles.persist_result_bundle
    persist_failures = 1

    def flaky_persist(bundle: Mapping[str, Any]):
        nonlocal persist_failures
        if persist_failures:
            persist_failures -= 1
            raise RuntimeError("persist unavailable")
        return original_persist(bundle)

    bundles.persist_result_bundle = flaky_persist
    broker.poll_events[current.invocation_id] = event_script((b"complete",))
    with pytest.raises(WorkflowError, match="persistence failed"):
        workflow.poll(current.session_id)
    asset_count = len(core.assets)
    completed = workflow.poll(current.session_id)
    assert completed.state is SessionState.COMPLETED
    assert len(core.assets) == asset_count and len(bundles.calls) == 1 and len(broker.poll_calls) == 1

    workflow2, core2, broker2, bundles2, publication2, story2 = _make_fresh_ports()
    current2 = workflow2.start(make_request(operation_key="write-stage-retry"))
    original_stage = core2.stage_candidate
    stage_attempts = 0

    def flaky_stage(operation_key: str, bundle_id: str):
        nonlocal stage_attempts
        stage_attempts += 1
        if stage_attempts == 1:
            raise RuntimeError("stage unavailable")
        return original_stage(operation_key, bundle_id)

    core2.stage_candidate = flaky_stage
    broker2.poll_events[current2.invocation_id] = event_script((b"complete",))
    with pytest.raises(WorkflowError, match="staging failed"):
        workflow2.poll(current2.session_id)
    asset_count2 = len(core2.assets)
    assert len(bundles2.calls) == 1
    completed2 = workflow2.poll(current2.session_id)
    assert completed2.state is SessionState.COMPLETED
    assert len(core2.assets) == asset_count2 and len(bundles2.calls) == 1 and stage_attempts == 2
    assert len(broker2.poll_calls) == 1


@pytest.mark.parametrize(("action", "terminal", "state"), [("pause", "paused", SessionState.PAUSED), ("cancel", "cancelled", SessionState.CANCELLED)])
def test_control_finalization_retry_preserves_prefix_and_calls_broker_once(ports, action, terminal, state):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    original_stage = core.stage_candidate
    attempts = 0

    def flaky_stage(operation_key: str, bundle_id: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("stage unavailable")
        return original_stage(operation_key, bundle_id)

    core.stage_candidate = flaky_stage
    broker.control_events[(action, current.invocation_id)] = event_script(("精确".encode(),), terminal)
    with pytest.raises(WorkflowError, match="staging failed"):
        getattr(workflow, action)(current.session_id)
    pending = workflow._sessions[current.session_id]
    assert pending.state is SessionState.FINALIZING and bytes(pending.acked) == "精确".encode()
    result = getattr(workflow, action)(current.session_id)
    assert result.state is state and result.candidate.content == "精确".encode()
    assert len(broker.control_calls) == 1 and attempts == 2


def test_skill_start_failure_retries_same_invocation_without_early_phase_advance(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request(skills=(SkillRef(10, "skill", "1" * 64, "2" * 64),)))
    original_start = broker.start
    attempted_keys: list[str] = []
    failures = 1

    def flaky_start(invocation):
        nonlocal failures
        attempted_keys.append(invocation.invocation_key)
        if invocation.phase == "skill" and failures:
            failures -= 1
            raise RuntimeError("Broker start unavailable")
        return original_start(invocation)

    broker.start = flaky_start
    broker.poll_events[current.invocation_id] = event_script((b"raw",))
    with pytest.raises(WorkflowError, match="Broker start failed"):
        workflow.poll(current.session_id)
    pending = workflow._sessions[current.session_id]
    assert pending.state is SessionState.FINALIZING
    assert pending.phase == "raw" and pending.skill_index == -1 and pending.invocation_id == current.invocation_id
    asset_count = len(core.assets)
    resumed = workflow.poll(current.session_id)
    assert resumed.state is SessionState.RUNNING and resumed.phase == "skill" and resumed.skill_index == 0
    assert len(core.assets) == asset_count and len(broker.poll_calls) == 1
    assert attempted_keys == ["write-1:skill:10", "write-1:skill:10"]


def test_late_poll_after_cancel_is_fenced_by_transition_token(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    entered, release = Event(), Event()

    def blocking_poll(invocation_id: str, after_event_seq: int):
        entered.set()
        assert release.wait(3)
        return event_script((b"late",))

    broker.poll = blocking_poll
    output: dict[str, Any] = {}
    thread = Thread(target=_thread_call, args=(lambda: workflow.poll(current.session_id), output, "poll"))
    thread.start()
    assert entered.wait(3)
    broker.control_events[("cancel", current.invocation_id)] = event_script((b"winner",), "cancelled")
    cancelled = workflow.cancel(current.session_id)
    release.set()
    thread.join(3)
    assert not thread.is_alive() and not isinstance(output.get("poll"), BaseException)
    assert cancelled.state is SessionState.CANCELLED and cancelled.candidate.content == b"winner"
    assert output["poll"].state is SessionState.CANCELLED
    assert len(core.stage_calls) == 1


def test_concurrent_pause_cancel_commits_only_one_terminal_candidate(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    barrier = Barrier(3)

    def pause_call(operation_key: str, invocation_id: str):
        barrier.wait(3)
        return event_script((b"pause",), "paused")

    def cancel_call(operation_key: str, invocation_id: str):
        barrier.wait(3)
        return event_script((b"cancel",), "cancelled")

    broker.pause, broker.cancel = pause_call, cancel_call
    output: dict[str, Any] = {}
    threads = [
        Thread(target=_thread_call, args=(lambda: workflow.pause(current.session_id), output, "pause")),
        Thread(target=_thread_call, args=(lambda: workflow.cancel(current.session_id), output, "cancel")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(3)
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    final = workflow.poll(current.session_id)
    assert final.state in {SessionState.PAUSED, SessionState.CANCELLED}
    assert final.candidate.content in {b"pause", b"cancel"}
    assert len(core.stage_calls) == len(bundles.calls) == 1


def test_late_old_skill_response_cannot_advance_or_append_evidence_twice(ports):
    workflow, core, broker, bundles, publication, story = ports
    skills = (
        SkillRef(10, "skill.one", "1" * 64, "2" * 64),
        SkillRef(20, "skill.two", "3" * 64, "4" * 64),
    )
    current = workflow.start(make_request(skills=skills))
    broker.poll_events[current.invocation_id] = event_script((b"raw",))
    current = workflow.poll(current.session_id)
    old_invocation = broker.starts[1]
    original_poll = broker.poll
    barrier, counter_lock = Barrier(3), Lock()
    counter = 0

    def racing_poll(invocation_id: str, after_event_seq: int):
        nonlocal counter
        with counter_lock:
            counter += 1
            index = counter
        barrier.wait(3)
        return event_script((f"skill-{index}".encode(),), chain=chain_ref(old_invocation, index))

    broker.poll = racing_poll
    output: dict[str, Any] = {}
    threads = [Thread(target=_thread_call, args=(lambda: workflow.poll(current.session_id), output, str(i))) for i in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(3)
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    session = workflow._sessions[current.session_id]
    assert session.phase == "skill" and session.skill_index == 1
    assert len(session.skill_chain_refs) == 1 and len(broker.starts) == 3

    broker.poll = original_poll
    broker.poll_events[session.invocation_id] = event_script((b"final",), chain=chain_ref(broker.starts[2], 3))
    completed = workflow.poll(current.session_id)
    assert completed.state is SessionState.COMPLETED
    refs = bundles.bundles[completed.candidate.bundle_id]["skill_chain_result_refs"]
    assert len(refs) == 2 and len({ref["chain_result_id"] for ref in refs}) == 2


def test_synchronous_broker_reentry_fences_outer_poll_without_deadlock(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    broker.control_events[("cancel", current.invocation_id)] = event_script((b"reentrant",), "cancelled")

    def reentrant_poll(invocation_id: str, after_event_seq: int):
        inner = workflow.cancel(current.session_id)
        assert inner.state is SessionState.CANCELLED
        return event_script((b"late",))

    broker.poll = reentrant_poll
    result = workflow.poll(current.session_id)
    assert result.state is SessionState.CANCELLED and result.candidate.content == b"reentrant"
    assert len(core.stage_calls) == 1


def test_rewrite_receipt_binds_unicode_range_base_and_context_before_writes(ports):
    workflow, core, broker, bundles, publication, story = ports
    text = "🙂"
    request = _rewrite_request(RewriteSelection(1, 2, text, sha256(text.encode()).hexdigest()))
    started = workflow.start(request)
    context = json.loads(core.assets[broker.starts[0].input_asset_id])
    assert context["rewrite_selection_receipt"]["receipt_id"] == "selection-receipt-1"
    assert context["rewrite_selection_receipt"]["current_revision_id"] == request.target.base_revision_id
    assert len(core.selection_calls) == 1 and started.state is SessionState.RUNNING
    with pytest.raises(WorkflowError, match="range length"):
        RewriteSelection(1, 3, text, sha256(text.encode()).hexdigest())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipt: receipt.update(total_codepoints=3),
        lambda receipt: receipt.update(current_revision_id="revision-stale"),
        lambda receipt: receipt.update(base_content_hash="f" * 64),
        lambda receipt: receipt.update(selected_text="新句"),
        lambda receipt: receipt.update(start_codepoint=1),
    ],
)
def test_rewrite_receipt_mismatch_or_out_of_range_fails_before_asset_and_broker(ports, mutate):
    workflow, core, broker, bundles, publication, story = ports
    request = make_request(ChapterOperation.REWRITE)
    core.selection_response = _selection_receipt(request)
    mutate(core.selection_response)
    with pytest.raises(WorkflowError, match="bind|outside"):
        workflow.start(request)
    assert not core.assets and not broker.starts and len(core.selection_calls) == 1


def test_publication_command_is_immutable_and_result_binds_original_candidate(complete_session):
    completed, ports = complete_session
    workflow, core, broker, bundles, publication, story = ports

    class MutatingPublisher:
        def publish(self, command: Mapping[str, Any]):
            command["candidate_id"] = "candidate-other"  # type: ignore[index]

    workflow._publication = MutatingPublisher()
    with pytest.raises(WorkflowError, match="Publication port failed"):
        workflow.accept(completed.session_id, accepted_by="author", publication_operation_key="publish-mutating")
    assert not workflow._accepted_publications and not story.calls

    class SelfConsistentWrongPublisher:
        def publish(self, command: Mapping[str, Any]):
            try:
                command["workspace_id"] = "ws-2"  # type: ignore[index]
            except TypeError:
                pass
            return publication_result("candidate-other", "ws-2")

    workflow._publication = SelfConsistentWrongPublisher()
    with pytest.raises(WorkflowError, match="bound"):
        workflow.accept(completed.session_id, accepted_by="author", publication_operation_key="publish-wrong")
    assert not workflow._accepted_publications and not story.calls


def test_story_settlement_preflights_every_bundle_before_persist_or_batch(complete_session):
    completed, ports = complete_session
    workflow, core, broker, bundles, publication, story = ports
    receipt = workflow.accept(completed.session_id, accepted_by="author", publication_operation_key="publish-1")
    first = story_bundle(receipt, bundle_id="story-bundle-1")
    second = story_bundle(receipt, bundle_id="story-bundle-2")
    second["items"][0]["target"]["workspace_id"] = "ws-2"
    story.bundles = [first, second]
    before = len(bundles.calls)
    with pytest.raises(WorkflowError, match="Workspace"):
        workflow.settle_story_state(receipt, operation_key="settle-late-invalid")
    assert len(bundles.calls) == before and not core.batch_calls

    second = story_bundle(receipt, bundle_id="story-bundle-2")
    second["items"][0]["item_id"] = first["items"][0]["item_id"]
    story.bundles = [first, second]
    with pytest.raises(WorkflowError, match="duplicate item_id"):
        workflow.settle_story_state(receipt, operation_key="settle-duplicate-item")
    assert len(bundles.calls) == before and not core.batch_calls


def test_story_settlement_persist_failure_retries_without_partial_candidate_or_reproposal(complete_session):
    completed, ports = complete_session
    workflow, core, broker, bundles, publication, story = ports
    receipt = workflow.accept(completed.session_id, accepted_by="author", publication_operation_key="publish-1")
    story.bundles = [story_bundle(receipt, bundle_id="story-bundle-1"), story_bundle(receipt, bundle_id="story-bundle-2")]
    original = bundles.persist_result_bundle
    failures = 1

    def flaky(bundle: Mapping[str, Any]):
        nonlocal failures
        if bundle["bundle_id"] == "story-bundle-2" and failures:
            failures -= 1
            raise RuntimeError("second persist unavailable")
        return original(bundle)

    bundles.persist_result_bundle = flaky
    before = len(bundles.calls)
    with pytest.raises(WorkflowError, match="persistence failed"):
        workflow.settle_story_state(receipt, operation_key="settle-persist-retry")
    assert len(bundles.calls) == before + 1 and not core.batch_calls and len(story.calls) == 1
    result = workflow.settle_story_state(receipt, operation_key="settle-persist-retry")
    assert result.bundle_ids == ("story-bundle-1", "story-bundle-2")
    assert result.story_state_candidate_ids == ("candidate-2", "candidate-3")
    assert result.settlement_receipt_id == "settlement-receipt-1"
    assert bundles.calls.count("story-bundle-1") == 1 and len(story.calls) == 1 and len(core.batch_calls) == 1


def test_atomic_settlement_batch_recovers_ambiguous_commit_by_durable_receipt(complete_session):
    completed, ports = complete_session
    workflow, core, broker, bundles, publication, story = ports
    receipt = workflow.accept(completed.session_id, accepted_by="author", publication_operation_key="publish-1")
    story.bundles = [story_bundle(receipt)]

    class AmbiguousBatch:
        def __init__(self):
            self.fail = True

        def stage_story_state_batch(self, command: Mapping[str, Any]):
            result = core.stage_story_state_batch(command)
            if self.fail:
                self.fail = False
                raise RuntimeError("response lost after atomic commit")
            return result

    workflow._settlement_batch = AmbiguousBatch()
    before = len(bundles.calls)
    with pytest.raises(WorkflowError, match="atomic Candidate batch failed"):
        workflow.settle_story_state(receipt, operation_key="settle-ambiguous")
    assert len(core.batch_receipts) == 1 and len(story.calls) == 1 and len(bundles.calls) == before + 1
    result = workflow.settle_story_state(receipt, operation_key="settle-ambiguous")
    assert result.settlement_receipt_id == "settlement-receipt-1"
    assert result.story_state_candidate_ids == ("candidate-2",)
    assert len(core.batch_receipts) == 1 and len(core.batch_calls) == 2
    assert len(story.calls) == 1 and len(bundles.calls) == before + 1


@pytest.mark.parametrize("mismatch_index", [0, 1])
def test_story_bundle_persisted_identity_mismatch_never_reaches_atomic_stage(complete_session, mismatch_index):
    completed, ports = complete_session
    workflow, core, broker, bundles, publication, story = ports
    receipt = workflow.accept(completed.session_id, accepted_by="author", publication_operation_key="publish-1")
    story.bundles = [story_bundle(receipt, bundle_id="story-bundle-1"), story_bundle(receipt, bundle_id="story-bundle-2")]
    original = bundles.persist_result_bundle
    story_call_index = 0

    def substitute(bundle: Mapping[str, Any]):
        nonlocal story_call_index
        if str(bundle["bundle_id"]).startswith("story-bundle"):
            current = story_call_index
            story_call_index += 1
            if current == mismatch_index:
                return "chapter-bundle-substitute"
        return original(bundle)

    bundles.persist_result_bundle = substitute
    with pytest.raises(WorkflowError, match="changed the declared bundle identity"):
        workflow.settle_story_state(receipt, operation_key=f"settle-substitute-{mismatch_index}")
    assert not core.batch_calls and not core.batch_receipts


def test_runtime_delta_declares_exact_dependency_closure_and_contract_owners():
    path = ROOT / "coordination" / "PPA-06" / "chapter-workflow" / "runtime-integration-delta-v1.json"
    value = json.loads(path.read_text("utf-8"))
    assert value["integration_dependencies"] == [
        "NW-P2-PROMPT-SKILL-RUNTIME-02",
        "NW-P5-STORY-STATE-RUNTIME-03",
        "NW-P3-JOB-RPC-02",
        "NW-P3-EVENT-SSE-03",
        "NW-P0-RUNTIME-COMPOSITION-02",
    ]
    expected_owners = {
        "P6-CHAPTER-P3-CANCEL-PARTIAL-001": ["P3", "P0"],
        "P6-CHAPTER-P2-SKILL-EVIDENCE-001": ["P2", "P3", "P0"],
        "P6-CHAPTER-PUBLICATION-HTTP-001": ["P0"],
        "P6-CHAPTER-P5-SETTLEMENT-001": ["P5", "P3", "P0"],
        "P6-CHAPTER-P0-REWRITE-SELECTION-001": ["P0"],
    }
    assert {delta["delta_id"]: delta["contract_owners"] for delta in value["deltas"]} == expected_owners
    assert not any("owners" in delta for delta in value["deltas"])
    assert {owner for owners in expected_owners.values() for owner in owners} == {"P0", "P2", "P3", "P5"}
    assert value["source_readiness"] == "source_ready_integration_deferred"
    assert value["production_integration_claimed"] is False


def _make_fresh_ports():
    from conftest import FakeBroker, FakeBundles, FakeCore, FakePublication, FakeStoryState
    from plotpilot_chapter_workflow import ChapterWorkflow

    core, broker, bundles = FakeCore(), FakeBroker(), FakeBundles()
    publication, story = FakePublication(), FakeStoryState()
    workflow = ChapterWorkflow(
        core=core,
        broker=broker,
        result_bundles=bundles,
        publication=publication,
        story_state=story,
        rewrite_selections=core,
        settlement_batch=core,
    )
    return workflow, core, broker, bundles, publication, story
