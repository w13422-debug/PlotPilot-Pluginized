from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.publication.application import StaleCasError
from backend.plotpilot_core.publication.application.service import (
    PublicationApplication,
)
from backend.plotpilot_core.publication.service import PublicationService
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

RELEASE = "e" * 64
PACKAGE = "a" * 64


def _stream_item(base, payload, item_id):
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
def writer_stack(tmp_path):
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


def _stage(authority, base, payload, *, attempt_id, epoch, operation_key, item_id):
    return authority.stage_incomplete_stream(
        job_id="job-1",
        step_id="step-1",
        attempt_id=attempt_id,
        lease_epoch=epoch,
        operation_key=operation_key,
        item=_stream_item(base, payload, item_id),
    )


def test_newer_attempt_fences_old_stream_candidate_and_its_publication(writer_stack):
    repository = writer_stack["repository"]
    assets = writer_stack["assets"]
    authority = writer_stack["authority"]
    first = _stage(
        authority,
        writer_stack["base"],
        assets.put(
            b"old partial",
            mime="text/plain",
            logical_role="candidate_payload",
            provenance="test:writer-fence",
        ),
        attempt_id="attempt-1",
        epoch=1,
        operation_key="stream-one",
        item_id="stream-one",
    )
    repository._connection.execute(
        "UPDATE execution_attempt SET state='failed' WHERE attempt_id='attempt-1'"
    )
    repository._connection.execute(
        "UPDATE execution_step SET state='failed' WHERE step_id='step-1'"
    )
    authority.start_attempt(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-2",
        worker_run_id="worker-2",
        plugin_id="com.plotpilot.demo",
        release_id=RELEASE,
        package_hash=PACKAGE,
        capability_id="writing.chapter.draft/v1",
        generation_id="generation-1",
        preallocated_receipt_id="receipt-2",
    )
    second = _stage(
        authority,
        writer_stack["base"],
        assets.put(
            b"new partial",
            mime="text/plain",
            logical_role="candidate_payload",
            provenance="test:writer-fence",
        ),
        attempt_id="attempt-2",
        epoch=2,
        operation_key="stream-two",
        item_id="stream-two",
    )

    with pytest.raises(ContractError) as stale_writer:
        _stage(
            authority,
            writer_stack["base"],
            assets.put(
                b"late partial",
                mime="text/plain",
                logical_role="candidate_payload",
                provenance="test:writer-fence",
            ),
            attempt_id="attempt-1",
            epoch=1,
            operation_key="stream-late",
            item_id="stream-late",
        )
    with pytest.raises(StaleCasError, match="writer fence"):
        PublicationService(repository, assets).accept(
            "publish-old", first.candidate_id, created_by="editor-1"
        )
    result = PublicationApplication(PublicationService(repository, assets)).accept_v2(
        {
            "schema": "publication-command/v2",
            "publication_operation_key": "publish-new",
            "workspace_id": "ws-1",
            "candidate_id": second.candidate_id,
            "accepted_by": "editor-1",
        }
    )

    fence = repository._connection.execute(
        "SELECT candidate_id,attempt_id,writer_epoch FROM chapter_writer_fence"
    ).fetchone()
    assert stale_writer.value.code == int(ErrorCode.STALE_LEASE)
    assert tuple(fence) == (second.candidate_id, "attempt-2", 2)
    assert repository.get_document("doc-1").content == "new partial"
    assert result["candidate_id"] == second.candidate_id


def test_writer_epoch_must_match_the_active_attempt_lease(writer_stack):
    payload = writer_stack["assets"].put(
        b"partial",
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:writer-fence",
    )

    with pytest.raises(ContractError) as caught:
        writer_stack["authority"].stage_incomplete_stream(
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
            writer_epoch=2,
            operation_key="wrong-writer-epoch",
            item=_stream_item(writer_stack["base"], payload, "wrong-epoch"),
        )

    assert caught.value.code == int(ErrorCode.STALE_LEASE)
    assert writer_stack["repository"]._connection.execute(
        "SELECT count(*) FROM candidate"
    ).fetchone()[0] == 0


def test_publication_rechecks_the_fence_operation_binding(writer_stack):
    repository = writer_stack["repository"]
    assets = writer_stack["assets"]
    staged = _stage(
        writer_stack["authority"],
        writer_stack["base"],
        assets.put(
            b"partial",
            mime="text/plain",
            logical_role="candidate_payload",
            provenance="test:writer-fence",
        ),
        attempt_id="attempt-1",
        epoch=1,
        operation_key="stream-bound",
        item_id="stream-bound",
    )
    repository._connection.execute(
        "UPDATE chapter_writer_fence SET operation_key='tampered' "
        "WHERE candidate_id=?",
        (staged.candidate_id,),
    )

    with pytest.raises(StaleCasError, match="writer fence"):
        PublicationService(repository, assets).accept(
            "publish-tampered", staged.candidate_id, created_by="editor-1"
        )

    assert repository.get_document("doc-1").current_revision_id == writer_stack["base"].revision_id
