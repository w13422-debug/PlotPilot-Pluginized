from __future__ import annotations

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.bootstrap.m4_authority_adapters import (
    build_m4_authority_adapters,
)
from backend.plotpilot_core.candidates.service import CandidateService
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.repositories import CoreAuthorityRepository


def _item(base, payload):
    target = {
        "workspace_id": "ws-1",
        "entity_kind": "document",
        "entity_id": "doc-1",
    }
    return {
        "schema": "candidate-item/v1",
        "item_id": "item-http",
        "item_kind": "document",
        "target": target,
        "mutation": {
            "mode": "replace",
            "payload_schema": "core/document-text/v1",
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
        "parent_candidate_ids": [],
        "source_refs": [],
        "status": "complete",
    }


@pytest.fixture
def http_stack(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    repository.create_workspace(Workspace("ws-1", "Novel"))
    repository.create_workspace(Workspace("ws-2", "Other"))
    repository.create_document(Document("doc-1", "ws-1", "Chapter"))
    base = repository.publish_revision(
        document_id="doc-1",
        content="old",
        expected_revision_id=None,
        created_by="user",
        revision_id="rev-base",
    )
    payload = assets.put(
        b"new chapter",
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:core-http",
    )
    staged = CandidateService(repository, assets).stage("stage-http", _item(base, payload))
    try:
        yield {
            "repository": repository,
            "adapter": build_m4_authority_adapters(repository, assets).http,
            "candidate_id": staged.candidate_id,
        }
    finally:
        repository.close()


def test_core_http_adapter_binds_all_approved_candidate_publication_routes(http_stack):
    adapter = http_stack["adapter"]
    candidate_id = http_stack["candidate_id"]
    list_status, listing = adapter.handle(
        "candidate.list",
        {
            "schema": "candidate-list-query/v2",
            "workspace_id": "ws-1",
            "cursor": None,
            "limit": 50,
        },
        path_identity={"workspace_id": "ws-1"},
    )
    get_status, fetched = adapter.handle(
        "candidate.get",
        {
            "schema": "candidate-get-query/v2",
            "workspace_id": "ws-1",
            "candidate_id": candidate_id,
        },
        path_identity={"workspace_id": "ws-1", "candidate_id": candidate_id},
    )
    preview_status, preview = adapter.handle(
        "candidate.preview",
        {
            "schema": "candidate-preview-query/v2",
            "workspace_id": "ws-1",
            "candidate_id": candidate_id,
            "offset": 0,
            "length": 64,
        },
        path_identity={"workspace_id": "ws-1", "candidate_id": candidate_id},
    )
    review_status, review = adapter.handle(
        "candidate.review",
        {
            "schema": "candidate-review-command/v2",
            "operation_key": "review-http",
            "workspace_id": "ws-1",
            "candidate_id": candidate_id,
            "decision": "approve",
            "decided_by": "editor-1",
            "expected_status": "complete",
        },
        path_identity={"workspace_id": "ws-1", "candidate_id": candidate_id},
    )
    publication_status, publication = adapter.handle(
        "publication.accept",
        {
            "schema": "publication-command/v2",
            "publication_operation_key": "publish-http",
            "workspace_id": "ws-1",
            "candidate_id": candidate_id,
            "accepted_by": "editor-1",
        },
        path_identity={"workspace_id": "ws-1"},
    )
    projection_status, projection = adapter.handle(
        "story-state.projection-input",
        {
            "schema": "core-authority-query/v2",
            "workspace_id": "ws-1",
            "entity_kind": "document",
            "entity_id": "doc-1",
            "include_assets": True,
        },
        path_identity={"workspace_id": "ws-1"},
    )

    assert (list_status, get_status, preview_status) == (200, 200, 200)
    assert (review_status, publication_status, projection_status) == (200, 200, 200)
    assert listing["items"][0]["candidate_id"] == candidate_id
    assert fetched["candidate"]["candidate_id"] == candidate_id
    assert preview["payload_hash"] == fetched["candidate"]["mutation"]["payload_hash"]
    assert review["publication_eligibility"] == "eligible"
    assert publication["candidate_id"] == candidate_id
    assert projection["publication"]["publication_id"] == publication["publication_id"]


def test_core_http_adapter_rejects_cross_workspace_and_path_body_mismatch(http_stack):
    adapter = http_stack["adapter"]
    candidate_id = http_stack["candidate_id"]
    cross_status, cross = adapter.handle(
        "candidate.get",
        {
            "schema": "candidate-get-query/v2",
            "workspace_id": "ws-2",
            "candidate_id": candidate_id,
        },
        path_identity={"workspace_id": "ws-2", "candidate_id": candidate_id},
    )
    mismatch_status, mismatch = adapter.handle(
        "candidate.get",
        {
            "schema": "candidate-get-query/v2",
            "workspace_id": "ws-1",
            "candidate_id": candidate_id,
        },
        path_identity={"workspace_id": "ws-2", "candidate_id": candidate_id},
    )

    assert (cross_status, cross["error_code"]) == (404, "cross_workspace")
    assert (mismatch_status, mismatch["error_code"]) == (400, "malformed_request")
