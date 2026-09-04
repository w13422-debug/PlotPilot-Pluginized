from __future__ import annotations

from copy import deepcopy

import pytest
from plotpilot_story_state import ChapterSettlement, ChapterSettlementError

HASH = "a" * 64


def _request(*, operation_key="settle-1", content_hash=HASH):
    return {
        "schema": "post-chapter-story-state-request/v1",
        "operation_key": operation_key,
        "workspace_id": "workspace-1",
        "chapter_candidate_id": "chapter-candidate-1",
        "chapter_publication_id": "chapter-publication-1",
        "chapter_document_id": "chapter-1",
        "chapter_revision_id": "chapter-revision-2",
        "chapter_content_hash": content_hash,
    }


def _bundle(request):
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
        "items": [
            {
                "schema": "candidate-item/v1",
                "item_id": "story-settlement-item-1",
                "item_kind": "node_structure",
                "target": {
                    "workspace_id": request["workspace_id"],
                    "entity_kind": "node_structure",
                    "entity_id": "story-state-node-1",
                },
                "mutation": {
                    "mode": "structure_patch",
                    "payload_schema": "story-state/proposal/v1",
                    "payload_hash": "d" * 64,
                },
                "payload_asset_id": "story-state-payload-1",
                "base": {"revision_id": "story-state-base-1", "content_hash": "e" * 64},
                "write_set": [
                    {
                        "workspace_id": request["workspace_id"],
                        "entity_kind": "node_structure",
                        "entity_id": "story-state-node-1",
                        "revision_id": "story-state-base-1",
                        "content_hash": "e" * 64,
                    }
                ],
                "parent_candidate_ids": [request["chapter_candidate_id"]],
                "source_refs": [
                    {
                        "workspace_id": request["workspace_id"],
                        "source_type": "core.revision",
                        "source_id": request["chapter_document_id"],
                        "revision_or_hash": request["chapter_revision_id"],
                    }
                ],
                "status": "complete",
            }
        ],
        "warnings": [],
        "partial": False,
        "provenance_receipt_id": "story-settlement-receipt-1",
        "skill_chain_result_refs": [],
    }


def test_published_chapter_settlement_is_source_bound_idempotent_and_candidate_only():
    calls = []
    settlement = ChapterSettlement(
        lambda request: calls.append(dict(request)) or [_bundle(request)]
    )
    first = settlement.settle(_request())
    replay = settlement.settle(_request())
    assert first.bundles == replay.bundles and replay.replayed is True
    assert calls == [_request()]
    item = first.bundles[0]["items"][0]
    assert item["source_refs"][0]["revision_or_hash"] == "chapter-revision-2"
    assert not hasattr(settlement, "publish")


def test_settlement_rejects_payload_drift_and_never_returns_partial_candidate():
    settlement = ChapterSettlement(lambda request: [_bundle(request)])
    settlement.settle(_request())
    with pytest.raises(ChapterSettlementError, match="operation_key"):
        settlement.settle(_request(content_hash="f" * 64))
    bad = _bundle(_request())
    bad["items"][0]["status"] = "partial"
    with pytest.raises(ChapterSettlementError, match="complete"):
        ChapterSettlement(lambda _request: [bad]).settle(_request())


def test_settlement_rejects_nonpublication_or_unbound_bundle_before_candidate_handoff():
    bad = deepcopy(_bundle(_request()))
    bad["items"][0]["source_refs"] = []
    with pytest.raises(ChapterSettlementError, match="source-bound"):
        ChapterSettlement(lambda _request: [bad]).settle(_request())
    with pytest.raises(ChapterSettlementError, match="requires an injected"):
        ChapterSettlement().settle(_request())
