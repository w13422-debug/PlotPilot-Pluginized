from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.candidates import CandidateError, CandidateService
from backend.plotpilot_core.candidates.application import CandidateApplication
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

RELEASE = "e" * 64
PACKAGE = "a" * 64


def _stream_item(base, payload, item_id="stream-1"):
    target = {
        "workspace_id": "ws-1",
        "entity_kind": "document",
        "entity_id": "doc-1",
    }
    return {
        "schema": "candidate-item/v1",
        "item_id": item_id,
        "item_kind": "incomplete_stream",
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
        "status": "partial",
    }


@pytest.fixture
def execution_stack(tmp_path):
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
    snapshot = json.loads(
        Path("contracts/golden/run-snapshot/snapshot.json").read_text(encoding="utf-8")
    )
    snapshot["plugin_releases"] = [
        {
            "plugin_id": "com.plotpilot.demo",
            "release_id": RELEASE,
            "package_hash": PACKAGE,
            "data_generation_id": "generation-1",
        }
    ]
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    authority = ExecutionAuthority(repository, assets)
    authority.create_from_verified_snapshot("job-1", snapshot)
    authority.freeze_plan(
        "job-1",
        [
            {
                "step_id": "step-1",
                "depends_on": [],
                "result_contract": "candidate-batch/v1",
            }
        ],
        output_step_id="step-1",
    )
    authority.start_attempt(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        worker_run_id="worker-1",
        plugin_id="com.plotpilot.demo",
        release_id=RELEASE,
        package_hash=PACKAGE,
        capability_id="writing.chapter.draft/v1",
        generation_id="generation-1",
        preallocated_receipt_id="receipt-1",
    )
    try:
        yield {
            "repository": repository,
            "assets": assets,
            "base": base,
            "authority": authority,
        }
    finally:
        repository.close()


def test_incomplete_stream_is_candidate_only_until_explicit_publication(execution_stack):
    repository = execution_stack["repository"]
    assets = execution_stack["assets"]
    payload = assets.put(
        b"partial chapter",
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:incomplete-stream",
    )
    item = _stream_item(execution_stack["base"], payload)

    with pytest.raises(CandidateError, match="durable-stream authority"):
        CandidateService(repository, assets).stage("direct-stream", item)
    staged = execution_stack["authority"].stage_incomplete_stream(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        lease_epoch=1,
        operation_key="core-stream",
        item=item,
    )
    replay = execution_stack["authority"].stage_incomplete_stream(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        lease_epoch=1,
        operation_key="core-stream",
        item=item,
    )
    candidate = CandidateApplication(
        CandidateService(repository, assets),
        execution_authority=execution_stack["authority"],
    ).get_candidate(
        {
            "schema": "candidate-get-query/v2",
            "workspace_id": "ws-1",
            "candidate_id": staged.candidate_id,
        }
    )["candidate"]

    assert replay.candidate_id == staged.candidate_id
    assert repository.get_document("doc-1").current_revision_id == execution_stack["base"].revision_id
    assert repository._connection.execute("SELECT count(*) FROM revision").fetchone()[0] == 1
    assert candidate["item_kind"] == "incomplete_stream"
    assert candidate["status"] == "partial"
    assert candidate["publication_eligibility"] == "eligible"
    assert candidate["source_job_id"] == "job-1"
