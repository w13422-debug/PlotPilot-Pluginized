from __future__ import annotations

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.candidates.application import CandidateApplication
from backend.plotpilot_core.candidates.service import CandidateError, CandidateService
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.publication.application.service import (
    PublicationApplication,
)
from backend.plotpilot_core.publication.service import PublicationService
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_plugin_sdk.core_api_v2 import (
    parse_story_state_projection_input_v2,
)


def _item(base, payload, *, document_id, item_id):
    target = {
        "workspace_id": "ws-1",
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
def batch_stack(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    repository.create_workspace(Workspace("ws-1", "Novel"))
    repository.create_document(Document("doc-1", "ws-1", "Chapter one"))
    repository.create_document(Document("doc-2", "ws-1", "Chapter two"))
    base_one = repository.publish_revision(
        document_id="doc-1",
        content="old one",
        expected_revision_id=None,
        created_by="user",
        revision_id="rev-one",
    )
    base_two = repository.publish_revision(
        document_id="doc-2",
        content="old two",
        expected_revision_id=None,
        created_by="user",
        revision_id="rev-two",
    )
    service = CandidateService(repository, assets)
    try:
        yield {
            "repository": repository,
            "assets": assets,
            "application": CandidateApplication(service),
            "base_one": base_one,
            "base_two": base_two,
        }
    finally:
        repository.close()


def test_story_state_batch_is_atomic_and_replay_bound(batch_stack):
    assets = batch_stack["assets"]
    application = batch_stack["application"]
    first_payload = assets.put(
        b"new one",
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:batch",
    )
    second_payload = assets.put(
        b"new two",
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:batch",
    )
    items = [
        _item(batch_stack["base_one"], first_payload, document_id="doc-1", item_id="one"),
        _item(batch_stack["base_two"], second_payload, document_id="doc-2", item_id="two"),
    ]

    first = application.stage_story_state_batch(
        operation_key="batch-ok", workspace_id="ws-1", items=items
    )
    replay = application.stage_story_state_batch(
        operation_key="batch-ok", workspace_id="ws-1", items=items
    )

    assert [item.stage_status for item in first] == ["created", "created"]
    assert [item.stage_status for item in replay] == ["idempotent", "idempotent"]
    assert [item.candidate_id for item in replay] == [item.candidate_id for item in first]
    assert batch_stack["repository"].get_document("doc-1").content == "old one"

    bad_items = [
        _item(batch_stack["base_one"], first_payload, document_id="doc-1", item_id="bad-one"),
        _item(batch_stack["base_one"], second_payload, document_id="doc-2", item_id="bad-two"),
    ]
    before = batch_stack["repository"]._connection.execute(
        "SELECT count(*) FROM candidate"
    ).fetchone()[0]
    with pytest.raises(CandidateError, match="stale candidate base"):
        application.stage_story_state_batch(
            operation_key="batch-roll-back", workspace_id="ws-1", items=bad_items
        )

    assert batch_stack["repository"]._connection.execute(
        "SELECT count(*) FROM candidate"
    ).fetchone()[0] == before
    assert batch_stack["repository"]._connection.execute(
        "SELECT count(*) FROM candidate_batch_operation "
        "WHERE operation_key LIKE '%batch-roll-back%'"
    ).fetchone()[0] == 0


def test_story_state_projection_is_a_valid_readback_of_published_batch_candidate(
    batch_stack,
):
    assets = batch_stack["assets"]
    application = batch_stack["application"]
    payload = assets.put(
        b"published chapter",
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:projection",
    )
    staged = application.stage_story_state_batch(
        operation_key="batch-projection",
        workspace_id="ws-1",
        items=[
            _item(
                batch_stack["base_one"],
                payload,
                document_id="doc-1",
                item_id="projection-item",
            )
        ],
    )[0]
    publication = PublicationApplication(
        PublicationService(batch_stack["repository"], assets)
    ).accept_v2(
        {
            "schema": "publication-command/v2",
            "publication_operation_key": "publish-projection",
            "workspace_id": "ws-1",
            "candidate_id": staged.candidate_id,
            "accepted_by": "editor-1",
        }
    )
    projection = application.story_state_projection_input(
        {
            "schema": "core-authority-query/v2",
            "workspace_id": "ws-1",
            "entity_kind": "document",
            "entity_id": "doc-1",
            "include_assets": True,
        }
    )

    assert parse_story_state_projection_input_v2(
        projection, expected_workspace_id="ws-1"
    ) == projection
    assert projection["publication"]["publication_id"] == publication["publication_id"]
    assert projection["current_revision"]["content_asset_id"] != projection["candidate"][
        "payload_asset_id"
    ]
    assert {asset["role"] for asset in projection["assets"]} == {
        "candidate.payload",
        "revision.content",
    }
