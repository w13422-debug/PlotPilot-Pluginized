from __future__ import annotations

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.candidates.application import CandidateApplication
from backend.plotpilot_core.candidates.service import CandidateService
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.publication.application import OperationKeyReuseError
from backend.plotpilot_core.publication.application.service import (
    PublicationApplication,
)
from backend.plotpilot_core.publication.service import PublicationService
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_plugin_sdk.core_api_v2 import validate_publication_v2


def _item(base, payload, item_id):
    target = {
        "workspace_id": "ws-1",
        "entity_kind": "document",
        "entity_id": "doc-1",
    }
    return {
        "schema": "candidate-item/v1",
        "item_id": item_id,
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
def publication_stack(tmp_path):
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
    service = CandidateService(repository, assets)
    first_payload = assets.put(
        b"published text",
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:publication-v2",
    )
    second_payload = assets.put(
        b"other text",
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:publication-v2",
    )
    first = service.stage("stage-first", _item(base, first_payload, "item-first"))
    second = service.stage("stage-second", _item(base, second_payload, "item-second"))
    try:
        yield {
            "repository": repository,
            "assets": assets,
            "base": base,
            "first": first,
            "second": second,
            "application": PublicationApplication(PublicationService(repository, assets)),
        }
    finally:
        repository.close()


def _command(candidate_id, operation_key):
    return {
        "schema": "publication-command/v2",
        "publication_operation_key": operation_key,
        "workspace_id": "ws-1",
        "candidate_id": candidate_id,
        "accepted_by": "editor-1",
    }


def test_v2_publication_readback_binds_cas_write_set_and_replay(publication_stack):
    application = publication_stack["application"]
    command = _command(publication_stack["first"].candidate_id, "publish-v2")

    first = application.accept_v2(command)
    replay = application.accept_v2(command)
    candidate = CandidateApplication(
        CandidateService(publication_stack["repository"], publication_stack["assets"])
    ).get_candidate(
        {
            "schema": "candidate-get-query/v2",
            "workspace_id": "ws-1",
            "candidate_id": publication_stack["first"].candidate_id,
        }
    )["candidate"]
    revision = publication_stack["repository"].get_revision(first["revision_id"])

    validate_publication_v2(command, first, candidate=candidate)
    assert first["idempotent"] is False
    assert replay == {**first, "idempotent": True}
    assert revision.content == "published text"
    assert first["content_hash"] == revision.content_hash
    assert first["cas"]["base_revision_id"] == publication_stack["base"].revision_id
    assert first["cas"]["base_content_hash"] == publication_stack["base"].content_hash
    assert first["cas"]["write_set"] == candidate["write_set"]
    stored = publication_stack["repository"]._connection.execute(
        "SELECT response_json FROM core_authority_operation "
        "WHERE route_id='publication.accept/v2' AND operation_key='publish-v2'"
    ).fetchone()[0]
    assert '"idempotent":false' in stored


def test_v2_publication_operation_key_cannot_be_rebound_to_another_candidate(
    publication_stack,
):
    application = publication_stack["application"]
    operation_key = "publish-reused"
    application.accept_v2(_command(publication_stack["first"].candidate_id, operation_key))

    with pytest.raises(OperationKeyReuseError, match="different command"):
        application.accept_v2(_command(publication_stack["second"].candidate_id, operation_key))
