from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.plotpilot_core.api.v1.jobs.rpc import JobCommandQueryAdapter
from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.jobs.checkpoint_adapter import (
    AuthorityAttemptStartAdapter,
    DurableCheckpointAdapter,
)
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode, canonical_bytes
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

RELEASE = "e" * 64
PACKAGE = "a" * 64
TARGET = {
    "workspace_id": "ws-1",
    "entity_kind": "document",
    "entity_id": "doc-1",
}


def _open_stack(root: Path):
    database = root / "core.db"
    asset_root = root / "assets"
    repository = CoreAuthorityRepository(database)
    assets = AssetStore(asset_root)
    authority = ExecutionAuthority(repository, assets)
    return database, asset_root, repository, assets, authority


def _create_running_stack(root: Path):
    database, asset_root, repository, assets, authority = _open_stack(root)
    repository.create_workspace(Workspace("ws-1", "Novel"))
    repository.create_workspace(Workspace("ws-2", "Other"))
    repository.create_document(Document("doc-1", "ws-1", "Chapter"))
    repository.publish_revision(
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
    binding = AuthorityAttemptStartAdapter(authority).start_attempt(
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
    assert binding.lease_epoch == 1
    return {
        "database": database,
        "asset_root": asset_root,
        "repository": repository,
        "assets": assets,
        "authority": authority,
        "snapshot": snapshot,
    }


def _persist(
    adapter: DurableCheckpointAdapter, snapshot: dict, prefix: bytes = b"chapter-prefix"
):
    return adapter.persist_stream_prefix(
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
        target=TARGET,
        prefix_seq=1,
        prefix=prefix,
        operation_key="stream-op-1",
        completed_units=1,
        total_units=2,
        replay_policy="checkpoint_resume",
        provider_outcome="confirmed",
        provider_replay_policy="idempotent_auto",
    )


def test_stream_checkpoint_is_asset_backed_idempotent_and_restart_exact(tmp_path):
    stack = _create_running_stack(tmp_path)
    adapter = DurableCheckpointAdapter(stack["authority"])
    prefix = "第一段\nsecond".encode()

    committed = _persist(adapter, stack["snapshot"], prefix)
    replay = _persist(adapter, stack["snapshot"], prefix)

    assert replay.replayed is True
    assert replay.checkpoint_id == committed.checkpoint_id
    assert replay.prefix == prefix
    assert stack["assets"].read(committed.prefix_asset_id) == prefix
    state_raw = stack["assets"].read(committed.runtime_state_asset_id)
    assert state_raw == canonical_bytes(committed.runtime_state)
    assert committed.checkpoint["state_asset_id"] == committed.runtime_state_asset_id
    assert committed.stream_prefix["prefix_asset_id"] == committed.prefix_asset_id
    assert committed.stream_prefix["prefix_hash"] == committed.prefix_hash

    snapshot_adapter = JobCommandQueryAdapter(
        stack["authority"], snapshot_extensions=adapter
    )
    projected = snapshot_adapter.get_snapshot(workspace_id="ws-1", job_id="job-1")
    assert projected["stream_high_waters"] == [
        {
            "stream_id": "stream-1",
            "step_id": "step-1",
            "output_role": "draft",
            "target": TARGET,
            "acked_prefix_seq": 1,
            "acked_bytes": len(prefix),
            "acked_prefix_hash": committed.prefix_hash,
        }
    ]

    stack["repository"].close()
    _, _, reopened, assets, authority = _open_stack(tmp_path)
    try:
        recovered = DurableCheckpointAdapter(authority).recover_stream(
            workspace_id="ws-1",
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
            worker_run_id="worker-run-1",
            stream_id="stream-1",
            run_snapshot_hash=stack["snapshot"]["snapshot_hash"],
            expected_result_contract="candidate-batch/v1",
        )
        assert recovered is not None
        assert recovered.prefix == prefix
        assert recovered.prefix_hash == committed.prefix_hash
        assert recovered.checkpoint_id == committed.checkpoint_id
        assert assets.read(recovered.runtime_state_asset_id) == canonical_bytes(
            recovered.runtime_state
        )
    finally:
        reopened.close()


def test_stream_checkpoint_rejects_wrong_hash_identity_epoch_and_old_attempt(tmp_path):
    stack = _create_running_stack(tmp_path)
    adapter = DurableCheckpointAdapter(stack["authority"])
    raw = b"durable"
    raw_asset = stack["assets"].put(
        raw,
        mime="text/plain; charset=utf-8",
        logical_role="chapter_stream_prefix",
        provenance="test:wrong-hash",
    )
    prefix_contract = {
        "schema": "stream-prefix/v1",
        "stream_id": "stream-1",
        "job_id": "job-1",
        "step_id": "step-1",
        "output_role": "draft",
        "target": TARGET,
        "attempt_id": "attempt-1",
        "lease_epoch": 1,
        "prefix_seq": 1,
        "prefix_asset_id": raw_asset.asset_id,
        "prefix_hash": "0" * 64,
        "byte_length": len(raw),
        "encoding": "utf-8",
    }
    contract_asset = stack["assets"].put(
        canonical_bytes(prefix_contract),
        mime="application/json",
        logical_role="stream_prefix_contract",
        provenance="test:wrong-hash",
    )

    with pytest.raises(ContractError) as wrong_hash:
        adapter.commit_stream_prefix(
            stream_prefix_asset_id=contract_asset.asset_id,
            operation_key="wrong-hash",
            worker_run_id="worker-run-1",
            workspace_id="ws-1",
            run_snapshot_hash=stack["snapshot"]["snapshot_hash"],
            expected_result_contract="candidate-batch/v1",
            completed_units=1,
            total_units=2,
        )
    assert wrong_hash.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)

    for changed, code in (
        ({"workspace_id": "ws-2"}, ErrorCode.CHECKPOINT_INVALID),
        ({"attempt_id": "attempt-other"}, ErrorCode.STALE_LEASE),
        ({"lease_epoch": 2}, ErrorCode.STALE_LEASE),
        ({"run_snapshot_hash": "f" * 64}, ErrorCode.CHECKPOINT_INVALID),
        (
            {"expected_result_contract": "artifact-bundle/v1"},
            ErrorCode.RESULT_CONTRACT_MISMATCH,
        ),
    ):
        kwargs = {
            "workspace_id": "ws-1",
            "job_id": "job-1",
            "step_id": "step-1",
            "attempt_id": "attempt-1",
            "lease_epoch": 1,
            "worker_run_id": "worker-run-1",
            "stream_id": "stream-1",
            "run_snapshot_hash": stack["snapshot"]["snapshot_hash"],
            "expected_result_contract": "candidate-batch/v1",
            **changed,
        }
        with pytest.raises(ContractError) as caught:
            adapter.recover_stream(**kwargs)
        assert caught.value.code == int(code)

    committed = _persist(adapter, stack["snapshot"])
    with stack["repository"].transaction() as connection:
        connection.execute(
            "UPDATE execution_attempt SET state='failed' WHERE attempt_id='attempt-1'"
        )
        connection.execute(
            "UPDATE execution_step SET state='failed' WHERE step_id='step-1'"
        )
    stack["authority"].start_attempt(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-2",
        worker_run_id="worker-run-2",
        plugin_id="com.plotpilot.demo",
        release_id=RELEASE,
        package_hash=PACKAGE,
        capability_id="writing.chapter.draft/v1",
        generation_id="generation-1",
        preallocated_receipt_id="receipt-2",
        expected_result_contract="candidate-batch/v1",
    )
    with pytest.raises(ContractError) as stale:
        adapter.recover_stream(
            workspace_id="ws-1",
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
            worker_run_id="worker-run-1",
            stream_id="stream-1",
            run_snapshot_hash=stack["snapshot"]["snapshot_hash"],
            expected_result_contract="candidate-batch/v1",
        )
    assert stale.value.code == int(ErrorCode.STALE_LEASE)
    assert committed.prefix == b"chapter-prefix"
    stack["repository"].close()


def test_fresh_attempt_continues_only_from_its_exact_resume_checkpoint(tmp_path):
    stack = _create_running_stack(tmp_path)
    adapter = DurableCheckpointAdapter(stack["authority"])
    first = _persist(adapter, stack["snapshot"], b"one")
    with stack["repository"].transaction() as connection:
        connection.execute(
            "UPDATE execution_attempt SET state='suspended' WHERE attempt_id='attempt-1'"
        )
        connection.execute(
            "UPDATE execution_step SET state='paused' WHERE step_id='step-1'"
        )
        connection.execute(
            "UPDATE execution_job SET job_state='paused' WHERE job_id='job-1'"
        )
    stack["authority"].control_port.resume(
        job_id="job-1",
        step_id="step-1",
        operation_key="resume-op-1",
        checkpoint_asset_id=first.checkpoint_asset_id,
        resume_of_attempt_id="attempt-1",
        lease_epoch=1,
        worker_run_id="worker-run-2",
        new_attempt_id="attempt-2",
    )

    recovered = adapter.recover_stream(
        workspace_id="ws-1",
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-2",
        lease_epoch=2,
        worker_run_id="worker-run-2",
        stream_id="stream-1",
        run_snapshot_hash=stack["snapshot"]["snapshot_hash"],
        expected_result_contract="candidate-batch/v1",
    )
    assert recovered is not None
    assert recovered.prefix == b"one"
    assert recovered.attempt_id == "attempt-2"
    assert recovered.checkpoint["source_attempt_id"] == "attempt-1"

    continued = adapter.persist_stream_prefix(
        workspace_id="ws-1",
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-2",
        lease_epoch=2,
        worker_run_id="worker-run-2",
        run_snapshot_hash=stack["snapshot"]["snapshot_hash"],
        expected_result_contract="candidate-batch/v1",
        stream_id="stream-1",
        output_role="draft",
        target=TARGET,
        prefix_seq=2,
        prefix=b"one-two",
        operation_key="stream-op-2",
        completed_units=2,
        total_units=2,
        replay_policy="checkpoint_resume",
        provider_outcome="confirmed",
        provider_replay_policy="idempotent_auto",
    )
    assert continued.prefix == b"one-two"
    assert continued.checkpoint["checkpoint_seq"] == 2
    stack["repository"].close()
