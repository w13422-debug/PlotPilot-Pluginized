from __future__ import annotations

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.candidates.application import (
    CandidateApplication,
    CandidateConflictError,
)
from backend.plotpilot_core.candidates.service import CandidateService
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.publication.application import IncompletePublicationError
from backend.plotpilot_core.publication.application.service import (
    PublicationApplication,
)
from backend.plotpilot_core.publication.service import PublicationService
from backend.plotpilot_core.repositories import CoreAuthorityRepository


def _item(base, payload):
    target = {
        "workspace_id": "ws-1",
        "entity_kind": "document",
        "entity_id": "doc-1",
    }
    return {
        "schema": "candidate-item/v1",
        "item_id": "item-1",
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
def staged_candidate(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    repository.create_workspace(Workspace("ws-1", "Novel"))
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
        provenance="test:candidate-review",
    )
    service = CandidateService(repository, assets)
    staged = service.stage("stage-1", _item(base, payload))
    try:
        yield {
            "repository": repository,
            "assets": assets,
            "application": CandidateApplication(service),
            "candidate_id": staged.candidate_id,
        }
    finally:
        repository.close()


def _review(candidate_id, *, operation_key, decision):
    return {
        "schema": "candidate-review-command/v2",
        "operation_key": operation_key,
        "workspace_id": "ws-1",
        "candidate_id": candidate_id,
        "decision": decision,
        "decided_by": "editor-1",
        "expected_status": "complete",
    }


def test_candidate_review_approve_replays_and_binds_operation_key(staged_candidate):
    application = staged_candidate["application"]
    command = _review(
        staged_candidate["candidate_id"], operation_key="review-approve", decision="approve"
    )

    first = application.review(command)
    replay = application.review(command)

    assert first["status"] == "reviewed"
    assert first["publication_eligibility"] == "eligible"
    assert first["idempotent"] is False
    assert replay == {**first, "idempotent": True}
    with pytest.raises(CandidateConflictError, match="operation key"):
        application.review({**command, "decision": "reject"})


def test_rejected_candidate_stays_readable_but_cannot_be_published(staged_candidate):
    application = staged_candidate["application"]
    candidate_id = staged_candidate["candidate_id"]
    rejected = application.review(
        _review(candidate_id, operation_key="review-reject", decision="reject")
    )

    candidate = application.get_candidate(
        {
            "schema": "candidate-get-query/v2",
            "workspace_id": "ws-1",
            "candidate_id": candidate_id,
        }
    )["candidate"]
    publication = PublicationApplication(
        PublicationService(staged_candidate["repository"], staged_candidate["assets"])
    )

    assert rejected["status"] == "rejected"
    assert candidate["publication_eligibility"] == "none"
    with pytest.raises(IncompletePublicationError, match="rejected"):
        publication.accept_v2(
            {
                "schema": "publication-command/v2",
                "publication_operation_key": "publish-rejected",
                "workspace_id": "ws-1",
                "candidate_id": candidate_id,
                "accepted_by": "editor-1",
            }
        )


def test_candidate_cannot_receive_a_new_review_after_publication(staged_candidate):
    candidate_id = staged_candidate["candidate_id"]
    publication = PublicationApplication(
        PublicationService(staged_candidate["repository"], staged_candidate["assets"])
    )
    publication.accept_v2(
        {
            "schema": "publication-command/v2",
            "publication_operation_key": "publish-before-review",
            "workspace_id": "ws-1",
            "candidate_id": candidate_id,
            "accepted_by": "editor-1",
        }
    )

    with pytest.raises(CandidateConflictError, match="lifecycle"):
        staged_candidate["application"].review(
            _review(candidate_id, operation_key="late-review", decision="approve")
        )
