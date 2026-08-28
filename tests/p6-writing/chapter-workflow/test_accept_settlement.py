from __future__ import annotations

from copy import deepcopy

import pytest

from plotpilot_chapter_workflow import PublicationReceipt, WorkflowError

from conftest import event_script, make_request, publication_result, story_bundle


def test_accept_sends_closed_typed_command_and_is_idempotent(complete_session):
    completed, ports = complete_session
    workflow, core, broker, bundles, publication, story = ports
    receipt = workflow.accept(completed.session_id, accepted_by="author-1", publication_operation_key="publish-1")
    assert publication.calls == [
        {
            "schema": "publication-command/v1",
            "publication_operation_key": "publish-1",
            "workspace_id": "ws-1",
            "candidate_id": completed.candidate.candidate_id,
            "accepted_by": "author-1",
        }
    ]
    assert receipt.publication_id == "publication-1" and receipt.revision_id == "revision-new"
    assert workflow.accept(completed.session_id, accepted_by="author-1", publication_operation_key="publish-1") == receipt
    assert len(publication.calls) == 1


def test_partial_candidate_cannot_be_accepted(ports):
    workflow, core, broker, bundles, publication, story = ports
    current = workflow.start(make_request())
    broker.control_events[("pause", current.invocation_id)] = event_script((b"partial",), "paused")
    paused = workflow.pause(current.session_id)
    with pytest.raises(WorkflowError, match="complete"):
        workflow.accept(paused.session_id, accepted_by="author", publication_operation_key="publish-1")
    assert not publication.calls


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda value: value.update(extra=True), "closed"),
        (lambda value: value.update(workspace_id="ws-2"), "bound"),
        (lambda value: value.update(entity_id="chapter-2"), "target"),
        (lambda value: value["resulting_revision"].update(content_hash="x" * 64), "SHA-256"),
        (lambda value: value["resulting_revision"].update(revision_number=0), "positive"),
        (lambda value: value.update(idempotent=1), "boolean"),
    ],
)
def test_malformed_publication_result_fails_closed(complete_session, mutate, match):
    completed, ports = complete_session
    workflow, core, broker, bundles, publication, story = ports
    publication.response = publication_result(completed.candidate.candidate_id)
    mutate(publication.response)
    with pytest.raises(WorkflowError, match=match):
        workflow.accept(completed.session_id, accepted_by="author", publication_operation_key="publish-bad")


def test_story_state_settlement_only_stages_candidates_after_typed_accept(complete_session):
    completed, ports = complete_session
    workflow, core, broker, bundles, publication, story = ports
    receipt = workflow.accept(completed.session_id, accepted_by="author", publication_operation_key="publish-1")
    story.bundles = [story_bundle(receipt)]
    before_assets = dict(core.assets)
    result = workflow.settle_story_state(receipt, operation_key="settle-1")
    assert story.calls == [
        {
            "schema": "post-chapter-story-state-request/v1",
            "operation_key": "settle-1",
            "workspace_id": "ws-1",
            "chapter_candidate_id": receipt.candidate_id,
            "chapter_publication_id": "publication-1",
            "chapter_document_id": "chapter-1",
            "chapter_revision_id": "revision-new",
            "chapter_content_hash": "d" * 64,
        }
    ]
    assert result.story_state_candidate_ids == ("candidate-2",)
    assert result.bundle_ids == ("story-bundle-1",)
    assert core.assets == before_assets  # no direct正文/Story State Asset write
    assert workflow.settle_story_state(receipt, operation_key="settle-1") == result
    assert len(story.calls) == 1


def test_fabricated_publication_cannot_trigger_story_state(ports):
    workflow, core, broker, bundles, publication, story = ports
    fake = PublicationReceipt("publication-fake", "candidate-fake", "ws-1", "chapter-1", "revision-x", "d" * 64, {})
    with pytest.raises(WorkflowError, match="typed Publication"):
        workflow.settle_story_state(fake, operation_key="settle-1")
    assert not story.calls


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda bundle: bundle["items"][0]["target"].update(workspace_id="ws-2"), "Workspace"),
        (lambda bundle: bundle["items"][0]["target"].update(entity_kind="document"), "正文"),
        (lambda bundle: bundle["items"][0].update(status="partial"), "complete"),
        (lambda bundle: bundle["items"][0].update(source_refs=[]), "published chapter"),
    ],
)
def test_invalid_story_state_candidate_fails_closed(complete_session, mutate, match):
    completed, ports = complete_session
    workflow, core, broker, bundles, publication, story = ports
    receipt = workflow.accept(completed.session_id, accepted_by="author", publication_operation_key="publish-1")
    value = story_bundle(receipt)
    mutate(value)
    story.bundles = [value]
    before = len(core.stage_calls)
    with pytest.raises(WorkflowError, match=match):
        workflow.settle_story_state(receipt, operation_key="settle-bad")
    assert len(core.stage_calls) == before


def test_duplicate_story_state_bundle_and_candidate_identity_fail_closed(complete_session):
    completed, ports = complete_session
    workflow, core, broker, bundles, publication, story = ports
    receipt = workflow.accept(completed.session_id, accepted_by="author", publication_operation_key="publish-1")
    value = story_bundle(receipt)
    story.bundles = [value, deepcopy(value)]
    with pytest.raises(WorkflowError, match="duplicate bundle_id"):
        workflow.settle_story_state(receipt, operation_key="settle-dup")
