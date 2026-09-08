from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from plotpilot_story_state import ChapterSettlement, ChapterSettlementError

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.candidates.application import CandidateApplication
from backend.plotpilot_core.candidates.service import CandidateService
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.publication.application.service import (
    PublicationApplication,
)
from backend.plotpilot_core.publication.service import PublicationService
from backend.plotpilot_core.repositories import CoreAuthorityRepository

HASH = "a" * 64


def _candidate_item(
    *,
    workspace_id: str,
    document_id: str,
    base,
    payload,
    item_id: str,
    payload_schema: str,
    parent_candidate_ids: list[str] | None = None,
    source_refs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    target = {
        "workspace_id": workspace_id,
        "entity_kind": "document",
        "entity_id": document_id,
    }
    return {
        "schema": "candidate-item/v1",
        "item_id": item_id,
        "item_kind": "document",
        "target": target,
        "mutation": {
            "mode": "replace",
            "payload_schema": payload_schema,
            "payload_hash": payload.sha256,
        },
        "payload_asset_id": payload.asset_id,
        "base": {
            "revision_id": base.revision_id,
            "content_hash": base.content_hash,
        },
        "write_set": [
            {
                **target,
                "revision_id": base.revision_id,
                "content_hash": base.content_hash,
            }
        ],
        "parent_candidate_ids": parent_candidate_ids or [],
        "source_refs": source_refs or [],
        "status": "complete",
    }


@pytest.fixture
def core_stack(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    workspace_id = "workspace-1"
    chapter_document_id = "chapter-1"
    state_document_id = "story-state-character-1"
    repository.create_workspace(Workspace(workspace_id, "Novel"))
    repository.create_document(Document(chapter_document_id, workspace_id, "Chapter"))
    repository.create_document(Document(state_document_id, workspace_id, "Character"))
    chapter_base = repository.publish_revision(
        document_id=chapter_document_id,
        content="chapter draft",
        expected_revision_id=None,
        created_by="editor-1",
        revision_id="chapter-base-1",
    )
    state_base = repository.publish_revision(
        document_id=state_document_id,
        content="old state",
        expected_revision_id=None,
        created_by="editor-1",
        revision_id="story-state-base-1",
    )
    service = CandidateService(repository, assets)
    chapter_payload = assets.put(
        b"published chapter",
        mime="text/plain; charset=utf-8",
        logical_role="chapter_payload",
        provenance="test:chapter-settlement",
    )
    chapter_candidate = service.stage(
        "chapter-stage-1",
        _candidate_item(
            workspace_id=workspace_id,
            document_id=chapter_document_id,
            base=chapter_base,
            payload=chapter_payload,
            item_id="chapter-item-1",
            payload_schema="core/document-text/v1",
        ),
    )
    publication = PublicationApplication(PublicationService(repository, assets)).accept_v2(
        {
            "schema": "publication-command/v2",
            "publication_operation_key": "chapter-publication-1",
            "workspace_id": workspace_id,
            "candidate_id": chapter_candidate.candidate_id,
            "accepted_by": "editor-1",
        }
    )
    story_state_payload = assets.put(
        b'{"state":"updated"}',
        mime="application/json",
        logical_role="story_state_payload",
        provenance="test:chapter-settlement",
    )
    try:
        yield {
            "repository": repository,
            "assets": assets,
            "application": CandidateApplication(service),
            "workspace_id": workspace_id,
            "chapter_candidate_id": chapter_candidate.candidate_id,
            "chapter_publication": publication,
            "chapter_document_id": chapter_document_id,
            "state_document_id": state_document_id,
            "state_base": state_base,
            "story_state_payload": story_state_payload,
        }
    finally:
        repository.close()


def _request(
    core: dict[str, Any],
    *,
    operation_key: str = "caller-settlement-1",
    content_hash: str | None = None,
) -> dict[str, str]:
    chapter = core["chapter_publication"]
    return {
        "schema": "post-chapter-story-state-request/v1",
        "operation_key": operation_key,
        "workspace_id": core["workspace_id"],
        "chapter_candidate_id": core["chapter_candidate_id"],
        "chapter_publication_id": chapter["publication_id"],
        "chapter_document_id": core["chapter_document_id"],
        "chapter_revision_id": chapter["revision_id"],
        "chapter_content_hash": content_hash or chapter["content_hash"],
    }


def _bundle(request: dict[str, str], core: dict[str, Any]) -> dict[str, Any]:
    payload = core["story_state_payload"]
    base = core["state_base"]
    item = _candidate_item(
        workspace_id=request["workspace_id"],
        document_id=core["state_document_id"],
        base=base,
        payload=payload,
        item_id="story-settlement-item-1",
        payload_schema="story-state/character/v1",
        parent_candidate_ids=[request["chapter_candidate_id"]],
        source_refs=[
            {
                "workspace_id": request["workspace_id"],
                "source_type": "revision",
                "source_id": request["chapter_revision_id"],
                "revision_or_hash": request["chapter_content_hash"],
            }
        ],
    )
    return {
        "schema": "result-bundle/v1",
        "contract_id": "candidate-batch/v1",
        "bundle_id": "story-settlement-bundle-1",
        "bundle_type": "candidate_batch",
        "producer": {
            "plugin_id": "com.plotpilot.story-state",
            "release_id": "b" * 64,
            "capability_id": "planning.story-state.settle/v1",
            "job_id": "story-job-1",
            "step_id": "story-step-1",
            "attempt_id": "story-attempt-1",
            "lease_epoch": 1,
        },
        "input_snapshot_hash": "c" * 64,
        "items": [item],
        "warnings": [],
        "partial": False,
        "provenance_receipt_id": "story-settlement-receipt-1",
        "skill_chain_result_refs": [],
    }


def _core_staging_factory(core: dict[str, Any], calls, stage_results):
    def factory(request):
        canonical_request = dict(request)
        calls.append(canonical_request)
        bundle = _bundle(canonical_request, core)
        staged = core["application"].stage_story_state_batch(
            operation_key=canonical_request["operation_key"],
            workspace_id=canonical_request["workspace_id"],
            items=bundle["items"],
        )
        stage_results.append(staged)
        return [bundle]

    return factory


def _candidate_count(core: dict[str, Any]) -> int:
    return core["repository"]._connection.execute(
        "SELECT count(*) FROM candidate"
    ).fetchone()[0]


def test_published_chapter_settlement_uses_core_durable_key_across_instances(
    core_stack,
):
    calls = []
    stage_results = []
    factory = _core_staging_factory(core_stack, calls, stage_results)
    first = ChapterSettlement(factory).settle(
        _request(core_stack, operation_key="caller-settlement-first")
    )
    replay = ChapterSettlement(factory).settle(
        _request(core_stack, operation_key="caller-settlement-second")
    )

    assert first.bundles == replay.bundles
    assert first.replayed is False and replay.replayed is False
    assert len(calls) == 2
    assert calls[0]["operation_key"] == calls[1]["operation_key"]
    assert calls[0]["operation_key"] not in {
        "caller-settlement-first",
        "caller-settlement-second",
    }
    assert [entry[0].stage_status for entry in stage_results] == [
        "created",
        "idempotent",
    ]
    assert stage_results[0][0].candidate_id == stage_results[1][0].candidate_id
    assert _candidate_count(core_stack) == 2  # published chapter + one settlement
    item = first.bundles[0]["items"][0]
    assert item["source_refs"] == [
        {
            "workspace_id": core_stack["workspace_id"],
            "source_type": "revision",
            "source_id": core_stack["chapter_publication"]["revision_id"],
            "revision_or_hash": core_stack["chapter_publication"]["content_hash"],
        }
    ]
    assert not hasattr(ChapterSettlement(factory), "publish")


def test_settlement_drift_uses_same_durable_key_and_rejects_without_second_candidate(
    core_stack,
):
    calls = []
    stage_results = []
    factory = _core_staging_factory(core_stack, calls, stage_results)
    ChapterSettlement(factory).settle(_request(core_stack))
    before = _candidate_count(core_stack)

    with pytest.raises(ChapterSettlementError, match="different payload"):
        ChapterSettlement(factory).settle(
            _request(
                core_stack,
                operation_key="caller-settlement-drift",
                content_hash="f" * 64,
            )
        )

    assert len(calls) == 2
    assert calls[0]["operation_key"] == calls[1]["operation_key"]
    assert len(stage_results) == 1
    assert _candidate_count(core_stack) == before


def test_settlement_rejects_nonpublication_or_unbound_bundle_before_return(
    core_stack,
):
    bad = deepcopy(_bundle(_request(core_stack), core_stack))
    bad["items"][0]["source_refs"] = []
    with pytest.raises(ChapterSettlementError, match="source-bound"):
        ChapterSettlement(lambda _request: [bad]).settle(_request(core_stack))

    partial = deepcopy(_bundle(_request(core_stack), core_stack))
    partial["items"][0]["status"] = "failed"
    partial["partial"] = True
    with pytest.raises(ChapterSettlementError, match="complete"):
        ChapterSettlement(lambda _request: [partial]).settle(_request(core_stack))

    with pytest.raises(ChapterSettlementError, match="requires an injected"):
        ChapterSettlement().settle(_request(core_stack))
