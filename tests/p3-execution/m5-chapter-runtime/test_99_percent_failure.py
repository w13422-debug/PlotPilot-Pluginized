from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.jobs.chapter_runtime import ChapterJobRuntime
from backend.plotpilot_core.jobs.checkpoint_adapter import DurableCheckpointAdapter
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

RELEASE = "e" * 64
PACKAGE = "a" * 64


def _stack(root: Path):
    database = root / "core.db"
    asset_root = root / "assets"
    repository = CoreAuthorityRepository(database)
    assets = AssetStore(asset_root)
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
        worker_run_id="worker-run-1",
        plugin_id="com.plotpilot.demo",
        release_id=RELEASE,
        package_hash=PACKAGE,
        capability_id="writing.chapter.draft/v1",
        generation_id="generation-1",
        preallocated_receipt_id="receipt-1",
        expected_result_contract="candidate-batch/v1",
    )
    return database, asset_root, repository, assets, authority, snapshot, base


@pytest.mark.parametrize("provider_policy", ["manual_if_unknown", "never_replay"])
def test_99_percent_restart_keeps_exact_prefix_and_one_candidate_no_revision(
    tmp_path, provider_policy
):
    database, asset_root, repository, _, authority, snapshot, base = _stack(tmp_path)
    adapter = DurableCheckpointAdapter(authority)
    prefix = ("章" * 33).encode("utf-8")
    assert len(prefix) == 99
    committed = adapter.persist_stream_prefix(
        workspace_id="ws-1",
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        lease_epoch=1,
        worker_run_id="worker-run-1",
        run_snapshot_hash=snapshot["snapshot_hash"],
        expected_result_contract="candidate-batch/v1",
        stream_id="stream-1",
        output_role="draft",
        target={
            "workspace_id": "ws-1",
            "entity_kind": "document",
            "entity_id": "doc-1",
        },
        prefix_seq=99,
        prefix=prefix,
        operation_key="ack-at-99-percent",
        completed_units=99,
        total_units=100,
        replay_policy="checkpoint_resume",
        provider_outcome="unknown",
        provider_replay_policy=provider_policy,
    )
    assert committed.prefix == prefix
    repository.close()

    reopened = CoreAuthorityRepository(database)
    assets = AssetStore(asset_root)
    restarted_authority = ExecutionAuthority(reopened, assets)
    runtime = ChapterJobRuntime(
        restarted_authority,
        checkpoints=DurableCheckpointAdapter(restarted_authority),
    )
    try:
        recovered = runtime.recover_stream(
            workspace_id="ws-1",
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
            worker_run_id="worker-run-1",
            stream_id="stream-1",
            run_snapshot_hash=snapshot["snapshot_hash"],
            expected_result_contract="candidate-batch/v1",
        )
        assert recovered is not None
        assert recovered.prefix == prefix
        assert recovered.recovery_action == provider_policy
        assert recovered.automatic_replay_allowed is False
        with pytest.raises(ContractError) as blocked:
            runtime.require_automatic_replay(recovered)
        assert blocked.value.code == int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT)

        first = runtime.materialize_incomplete_candidate(
            recovered,
            base_revision_id=base.revision_id,
            base_content_hash=base.content_hash,
        )
        replay = runtime.materialize_incomplete_candidate(
            recovered,
            base_revision_id=base.revision_id,
            base_content_hash=base.content_hash,
        )
        assert replay.candidate_id == first.candidate_id
        with reopened.read_connection() as connection:
            assert (
                connection.execute("SELECT count(*) FROM candidate").fetchone()[0] == 1
            )
            assert (
                connection.execute("SELECT count(*) FROM revision").fetchone()[0] == 1
            )
        assert reopened.get_document("doc-1").content == "old"
    finally:
        reopened.close()


def test_unknown_provider_outcome_cannot_be_marked_automatically_replayable(tmp_path):
    _, _, repository, _, authority, snapshot, _ = _stack(tmp_path)
    try:
        with pytest.raises(ContractError) as caught:
            DurableCheckpointAdapter(authority).persist_stream_prefix(
                workspace_id="ws-1",
                job_id="job-1",
                step_id="step-1",
                attempt_id="attempt-1",
                lease_epoch=1,
                worker_run_id="worker-run-1",
                run_snapshot_hash=snapshot["snapshot_hash"],
                expected_result_contract="candidate-batch/v1",
                stream_id="stream-1",
                output_role="draft",
                target={
                    "workspace_id": "ws-1",
                    "entity_kind": "document",
                    "entity_id": "doc-1",
                },
                prefix_seq=1,
                prefix=b"partial",
                operation_key="unsafe-auto-replay",
                completed_units=1,
                total_units=2,
                replay_policy="checkpoint_resume",
                provider_outcome="unknown",
                provider_replay_policy="idempotent_auto",
            )
        assert caught.value.code == int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT)
        with repository.read_connection() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM execution_checkpoint"
                ).fetchone()[0]
                == 0
            )
    finally:
        repository.close()
