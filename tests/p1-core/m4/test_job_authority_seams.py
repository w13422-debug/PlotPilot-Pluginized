from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.bootstrap.m4_authority_adapters import (
    build_m4_authority_adapters,
)
from backend.plotpilot_core.candidates.application import (
    CandidateApplication,
    CandidateNotPublishableError,
)
from backend.plotpilot_core.candidates.service import CandidateService
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

RELEASE = "e" * 64
PACKAGE = "a" * 64


def _stream_item(base, payload):
    target = {
        "workspace_id": "ws-1",
        "entity_kind": "document",
        "entity_id": "doc-1",
    }
    return {
        "schema": "candidate-item/v1",
        "item_id": "stream-seam",
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


def _execution_stack(tmp_path):
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
    return repository, assets, base, authority


def test_composition_uses_one_core_writer_for_fenced_candidate_staging(tmp_path):
    repository, assets, base, authority = _execution_stack(tmp_path)
    try:
        adapters = build_m4_authority_adapters(
            repository, assets, execution_authority=authority
        )
        payload = assets.put(
            b"partial chapter",
            mime="text/plain",
            logical_role="candidate_payload",
            provenance="test:job-seam",
        )
        staged = adapters.candidates.stage_incomplete_stream(
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
            operation_key="stream-seam",
            item=_stream_item(base, payload),
        )
        fence = repository._connection.execute(
            "SELECT candidate_id,attempt_id,writer_epoch FROM chapter_writer_fence"
        ).fetchone()
        origin = repository._connection.execute(
            "SELECT source_job_id,source_attempt_id FROM chapter_candidate_authority"
        ).fetchone()

        assert adapters.candidates.repository is repository
        assert adapters.publication.repository is repository
        assert adapters.execution is authority
        assert adapters.http.candidates is adapters.candidates
        assert staged.candidate_id == fence["candidate_id"]
        assert (fence["attempt_id"], fence["writer_epoch"]) == ("attempt-1", 1)
        assert tuple(origin) == ("job-1", "attempt-1")
        assert repository.get_document("doc-1").current_revision_id == base.revision_id
    finally:
        repository.close()


def test_composition_builds_omitted_authority_from_exact_objects(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    try:
        adapters = build_m4_authority_adapters(repository, assets)

        assert adapters.execution.repository is repository
        assert adapters.execution.assets is assets
    finally:
        repository.close()


def test_composition_rejects_authority_with_mismatched_repository(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    other_repository = CoreAuthorityRepository(tmp_path / "other.db")
    assets = AssetStore(tmp_path / "assets")
    authority = ExecutionAuthority(other_repository, assets)
    try:
        with pytest.raises(ValueError) as error:
            build_m4_authority_adapters(
                repository, assets, execution_authority=authority
            )

        assert str(error.value) == (
            "execution_authority.repository must be the exact repository object"
        )
    finally:
        repository.close()
        other_repository.close()


def test_composition_rejects_authority_with_mismatched_asset_store(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    other_assets = AssetStore(tmp_path / "other-assets")
    authority = ExecutionAuthority(repository, other_assets)
    try:
        with pytest.raises(ValueError) as error:
            build_m4_authority_adapters(
                repository, assets, execution_authority=authority
            )

        assert str(error.value) == (
            "execution_authority.assets must be the exact AssetStore object"
        )
    finally:
        repository.close()


def test_incomplete_stream_requires_the_composed_core_writer(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    try:
        application = CandidateApplication(CandidateService(repository, AssetStore(tmp_path / "assets")))
        with pytest.raises(CandidateNotPublishableError, match="writer fence"):
            application.stage_incomplete_stream()
    finally:
        repository.close()
