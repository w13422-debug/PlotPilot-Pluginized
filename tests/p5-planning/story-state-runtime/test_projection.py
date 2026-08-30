import copy
from dataclasses import replace

import plotpilot_story_state.projection as projection_module
import pytest
from plotpilot_plugin_sdk import hash_jcs
from plotpilot_plugin_sdk.core_api import parse_publication
from plotpilot_story_state import (
    CandidateAuthority,
    PublicationReceiptRef,
    PublishedStateRecord,
    StoryStateRuntime,
    rebuild_projection,
)

from .support import RecordingTerminal, request


def _record():
    terminal = RecordingTerminal()
    result = StoryStateRuntime(terminal).execute(request())
    command = terminal.calls[0]
    item = command.result_bundle["items"][0]
    candidate_id = result.candidates[0].candidate_id
    payload_asset = next(asset for asset in command.assets if asset.asset_id == item["payload_asset_id"])
    revision_id = "revision-published-1"
    publication_id = "publication-story-1"
    publication = {
        "schema": "publication-result/v1",
        "publication_id": publication_id,
        "candidate_id": candidate_id,
        "workspace_id": "workspace-1",
        "entity_kind": "document",
        "entity_id": "character-1",
        "resulting_revision": {
            "revision_id": revision_id,
            "workspace_id": "workspace-1",
            "entity_kind": "document",
            "entity_id": "character-1",
            "content_hash": payload_asset.sha256,
            "revision_number": 2,
        },
        "idempotent": False,
    }
    revision = {
        "schema": "core-revision/v1",
        "revision_id": revision_id,
        "workspace_id": "workspace-1",
        "document_id": "character-1",
        "node_id": None,
        "parent_revision_id": "revision-base-1",
        "content_hash": payload_asset.sha256,
        "created_by": "user-reviewer",
        "source_candidate_id": candidate_id,
        "created_at": "2026-08-30T00:00:02Z",
        "revision_number": 2,
        "payload_schema": "story-state/character/v1",
    }
    authority = CandidateAuthority(
        candidate_id,
        "published",
        item,
        "job-story-1",
        "step-story-1",
        "attempt-story-1",
        "bundle-story-1",
        "receipt-story-1",
    )
    publication_receipt = PublicationReceiptRef(
        publication_id,
        candidate_id,
        revision_id,
        "receipt-story-1",
        "job-story-1",
        "step-story-1",
        "attempt-story-1",
        "bundle-story-1",
        "item-story-1",
    )
    asset = {
        "schema": "asset-metadata/v1",
        "asset_id": payload_asset.asset_id,
        "sha256": payload_asset.sha256,
        "mime": "application/json",
        "size": len(payload_asset.content),
        "logical_role": "story_state_payload",
        "provenance": "core:execution",
        "rebuildable": False,
    }
    return PublishedStateRecord(
        publication,
        revision,
        revision_id,
        authority,
        publication_receipt,
        asset,
        payload_asset.content,
        result.to_dict()["committed_receipt"],
    )


def test_publication_revision_fact_asset_candidate_and_execution_lineage_project():
    record = _record()
    projection = rebuild_projection((record,))
    anchor = projection.anchors[0]
    assert anchor.publication_id == "publication-story-1"
    assert anchor.candidate_id == "candidate-item-story-1"
    assert anchor.receipt_id == "receipt-story-1"
    assert anchor.fact.key == ("workspace-1", "character", "character-1")
    assert projection.projection.source_revisions == ("revision-published-1",)


def test_story_state_publication_semantic_parser_parity(monkeypatch):
    calls = []

    def recording_parser(value, *, expected_workspace_id=None, command=None):
        calls.append((expected_workspace_id, command))
        return parse_publication(
            value,
            expected_workspace_id=expected_workspace_id,
            command=command,
        )

    monkeypatch.setattr(projection_module, "parse_publication", recording_parser)
    anchor, _payload = _record().validate()
    assert calls == [("workspace-1", None)]
    assert anchor.fact.workspace_id == "workspace-1"


@pytest.mark.parametrize("source", ["nested", "current"])
def test_story_state_cross_workspace_publication_negative(source):
    record = _record()
    if source == "nested":
        publication = copy.deepcopy(dict(record.publication))
        publication["resulting_revision"]["workspace_id"] = "workspace-2"
        forged = replace(record, publication=publication)
        message = "resulting Revision"
    else:
        revision = copy.deepcopy(dict(record.current_revision))
        revision["workspace_id"] = "workspace-2"
        forged = replace(record, current_revision=revision)
        message = "crosses workspace identity"
    with pytest.raises(Exception, match=message):
        forged.validate()


def test_story_state_mismatched_document_negative():
    record = _record()
    revision = copy.deepcopy(dict(record.current_revision))
    revision["document_id"] = "unrelated-document"
    with pytest.raises(ValueError, match="current Revision target"):
        replace(record, current_revision=revision).validate()


@pytest.mark.parametrize("drift", ["size", "role", "rebuildable", "mime", "bytes", "hash"])
def test_story_state_asset_semantic_closure(drift):
    record = _record()
    if drift == "bytes":
        forged = replace(record, payload_bytes=b"{}")
    else:
        asset = copy.deepcopy(dict(record.asset_metadata))
        if drift == "size":
            asset["size"] += 1
        elif drift == "role":
            asset["logical_role"] = "unrelated_payload"
        elif drift == "rebuildable":
            asset["rebuildable"] = True
        elif drift == "mime":
            asset["mime"] = "application/octet-stream"
        else:
            asset["sha256"] = "f" * 64
        forged = replace(record, asset_metadata=asset)
    with pytest.raises(ValueError, match="Asset semantic closure|content closure"):
        forged.validate()


def _assert_unrelated_receipt_is_rejected(field):
    record = _record()
    receipt = copy.deepcopy(dict(record.provenance_receipt))
    receipt[field] = f"unrelated-{field}"
    receipt["receipt_hash"] = hash_jcs(
        "provenance-receipt/v1",
        {key: value for key, value in receipt.items() if key != "receipt_hash"},
    )
    forged = PublishedStateRecord(
        record.publication,
        record.current_revision,
        record.current_pointer_revision_id,
        record.candidate,
        record.publication_receipt,
        record.asset_metadata,
        record.payload_bytes,
        receipt,
    )
    with pytest.raises(ValueError, match="receipt lineage"):
        rebuild_projection((forged,))


@pytest.mark.parametrize("field", ["receipt_id", "job_id", "attempt_id"])
def test_unrelated_validly_rehashed_receipt_is_rejected_before_anchor(field):
    _assert_unrelated_receipt_is_rejected(field)


def test_story_state_receipt_substitution_regression():
    for field in ("receipt_id", "job_id", "attempt_id"):
        _assert_unrelated_receipt_is_rejected(field)


def test_stale_revision_pointer_and_asset_bytes_are_rejected():
    record = _record()
    stale = PublishedStateRecord(
        record.publication,
        record.current_revision,
        "revision-other",
        record.candidate,
        record.publication_receipt,
        record.asset_metadata,
        record.payload_bytes,
        record.provenance_receipt,
    )
    with pytest.raises(ValueError, match="current authority pointer"):
        rebuild_projection((stale,))
    forged_asset = PublishedStateRecord(
        record.publication,
        record.current_revision,
        record.current_pointer_revision_id,
        record.candidate,
        record.publication_receipt,
        record.asset_metadata,
        b"{}",
        record.provenance_receipt,
    )
    with pytest.raises(ValueError, match="content closure"):
        rebuild_projection((forged_asset,))
