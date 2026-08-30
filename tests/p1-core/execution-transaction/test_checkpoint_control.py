from __future__ import annotations

import copy

import pytest

from backend.plotpilot_core.api.v1.jobs.rpc import JobCommandQueryAdapter
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode, canonical_bytes
from backend.plotpilot_plugin_sdk.verifier import hash_without_field


def _checkpoint_asset(
    stack,
    *,
    checkpoint_id: str,
    checkpoint_seq: int,
    completed_units: int,
    source_attempt_id: str = "attempt-1",
    lease_epoch: int = 1,
):
    value = {
        "schema": "checkpoint/v1",
        "checkpoint_id": checkpoint_id,
        "checkpoint_seq": checkpoint_seq,
        "job_id": "job-1",
        "step_id": "step-1",
        "source_attempt_id": source_attempt_id,
        "lease_epoch": lease_epoch,
        "run_snapshot_hash": stack["snapshot"]["snapshot_hash"],
        "replay_policy": "checkpoint_resume",
        "completed_units": completed_units,
        "total_units": 2,
        "unit_set_hash": "b" * 64,
        "state_asset_id": None,
        "created_at": "2026-08-30T00:00:00Z",
    }
    value["checkpoint_hash"] = hash_without_field(value, "checkpoint_hash", "checkpoint/v1")
    asset = stack["assets"].put(
        canonical_bytes(value),
        mime="application/json",
        logical_role="checkpoint",
        provenance="test:p1-durable-authority",
    )
    return value, asset


def test_checkpoint_and_matching_job_event_commit_atomically(execution_stack, monkeypatch):
    authority = execution_stack["authority"]
    store = authority.checkpoint_store
    value, asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-1",
        checkpoint_seq=1,
        completed_units=1,
    )

    committed = store.commit_checkpoint(
        value,
        checkpoint_asset_id=asset.asset_id,
        operation_key="checkpoint-op-1",
        local_seq=1,
        worker_run_id="worker-run-1",
    )
    assert committed.to_dict() == {
        "accepted": True,
        "checkpoint_id": "checkpoint-1",
        "completed_units": 1,
        "total_units": 2,
        "job_event_seq": 1,
    }
    repository = execution_stack["repository"]
    event = repository._connection.execute(
        "SELECT job_event_seq,event_json FROM execution_job_event WHERE job_id='job-1'"
    ).fetchone()
    assert event[0] == 1
    assert '"event_type":"plugin.com.plotpilot.demo.job.checkpoint"' in event[1]
    assert tuple(repository._connection.execute(
        "SELECT current_checkpoint_id,job_event_high_water FROM execution_job WHERE job_id='job-1'"
    ).fetchone()) == ("checkpoint-1", 1)
    assert tuple(repository._connection.execute(
        "SELECT checkpoint_id,job_event_seq FROM execution_checkpoint WHERE job_id='job-1'"
    ).fetchone()) == ("checkpoint-1", 1)

    value2, asset2 = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-2",
        checkpoint_seq=2,
        completed_units=2,
    )
    original_append = store.events.append

    def append_then_fail(event_value, *, connection=None):
        original_append(event_value, connection=connection)
        raise RuntimeError("injected checkpoint commit failure")

    monkeypatch.setattr(store.events, "append", append_then_fail)
    with pytest.raises(RuntimeError, match="injected checkpoint commit failure"):
        store.commit_checkpoint(
            value2,
            checkpoint_asset_id=asset2.asset_id,
            operation_key="checkpoint-op-2",
            local_seq=2,
            worker_run_id="worker-run-1",
        )

    assert repository._connection.execute(
        "SELECT count(*) FROM execution_checkpoint WHERE job_id='job-1'"
    ).fetchone()[0] == 1
    assert repository._connection.execute(
        "SELECT count(*) FROM execution_job_event WHERE job_id='job-1'"
    ).fetchone()[0] == 1
    assert tuple(repository._connection.execute(
        "SELECT current_checkpoint_id,job_event_high_water FROM execution_job WHERE job_id='job-1'"
    ).fetchone()) == ("checkpoint-1", 1)


def test_checkpoint_duplicate_operation_is_replayed_and_payload_drift_is_rejected(execution_stack):
    authority = execution_stack["authority"]
    value, asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-1",
        checkpoint_seq=1,
        completed_units=1,
    )
    first = authority.checkpoint_store.commit_checkpoint(
        value,
        checkpoint_asset_id=asset.asset_id,
        operation_key="checkpoint-op-1",
        worker_run_id="worker-run-1",
    )
    before = {
        table: execution_stack["repository"]._connection.execute(
            f"SELECT count(*) FROM {table}"
        ).fetchone()[0]
        for table in ("execution_checkpoint", "execution_checkpoint_operation", "execution_job_event")
    }

    replay = authority.checkpoint_store.commit_checkpoint(
        value,
        checkpoint_asset_id=asset.asset_id,
        operation_key="checkpoint-op-1",
        worker_run_id="worker-run-1",
    )
    assert replay.replayed
    assert replay.to_dict() == first.to_dict()
    assert {
        table: execution_stack["repository"]._connection.execute(
            f"SELECT count(*) FROM {table}"
        ).fetchone()[0]
        for table in before
    } == before

    drift = copy.deepcopy(value)
    drift["completed_units"] = 0
    drift["checkpoint_hash"] = hash_without_field(drift, "checkpoint_hash", "checkpoint/v1")
    with pytest.raises(ContractError) as caught:
        authority.checkpoint_store.commit_checkpoint(
            drift,
            operation_key="checkpoint-op-1",
            worker_run_id="worker-run-1",
        )
    assert caught.value.code == int(ErrorCode.DUPLICATE_REQUEST)


def test_latest_checkpoint_survives_fresh_repository_restart(execution_stack):
    value, asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-1",
        checkpoint_seq=1,
        completed_units=1,
    )
    first = execution_stack["authority"].checkpoint_store.commit_checkpoint(
        value,
        checkpoint_asset_id=asset.asset_id,
        operation_key="checkpoint-op-1",
        worker_run_id="worker-run-1",
    )
    database = execution_stack["database"]
    asset_root = execution_stack["asset_root"]
    execution_stack["repository"].close()

    from backend.plotpilot_core.assets import AssetStore
    from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
    from backend.plotpilot_core.repositories.checkpoints import SQLiteCheckpointStore

    reopened = CoreAuthorityRepository(database)
    try:
        recovered = SQLiteCheckpointStore(reopened, AssetStore(asset_root)).get_latest("job-1")
        assert recovered == value
        assert first.job_event_seq == 1
        assert reopened._connection.execute(
            "SELECT current_checkpoint_id FROM execution_job WHERE job_id='job-1'"
        ).fetchone()[0] == "checkpoint-1"
    finally:
        reopened.close()


def test_checkpoint_store_is_compatible_with_existing_job_snapshot_read_gate(execution_stack):
    value, asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-1",
        checkpoint_seq=1,
        completed_units=1,
    )
    execution_stack["authority"].checkpoint_store.commit_checkpoint(
        value,
        checkpoint_asset_id=asset.asset_id,
        operation_key="checkpoint-op-1",
        worker_run_id="worker-run-1",
    )

    adapter = JobCommandQueryAdapter(
        execution_stack["authority"],
        snapshot_extensions=execution_stack["authority"].snapshot_extensions,
    )
    snapshot = adapter.get_snapshot(workspace_id="ws-1", job_id="job-1")
    assert snapshot["current_checkpoint_id"] == "checkpoint-1"


def test_pause_resume_cancel_are_idempotent_cas_and_fence_late_worker(execution_stack):
    authority = execution_stack["authority"]
    control = authority.control_port
    value, asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-1",
        checkpoint_seq=1,
        completed_units=1,
    )
    pause_kwargs = {
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "lease_epoch": 1,
        "operation_key": "pause-op-1",
        "worker_run_id": "worker-run-1",
        "reason": "operator pause",
        "checkpoint_asset_id": asset.asset_id,
    }
    paused = control.pause(**pause_kwargs)
    assert paused.accepted
    assert paused.result["checkpoint_asset_id"] == asset.asset_id
    paused_replay = control.pause(**pause_kwargs)
    assert paused_replay.replayed and paused_replay.to_dict() == paused.to_dict()

    resume_kwargs = {
        "job_id": "job-1",
        "step_id": "step-1",
        "operation_key": "resume-op-1",
        "resume_of_attempt_id": "attempt-1",
        "worker_run_id": "worker-run-2",
        "new_attempt_id": "attempt-2",
        "checkpoint_asset_id": asset.asset_id,
    }
    resumed = control.resume(**resume_kwargs)
    assert resumed.accepted
    assert resumed.result["worker_run_id"] == "worker-run-2"
    resumed_replay = control.resume(**resume_kwargs)
    assert resumed_replay.replayed and resumed_replay.to_dict() == resumed.to_dict()

    cancel_kwargs = {
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-2",
        "lease_epoch": 2,
        "operation_key": "cancel-op-1",
        "worker_run_id": "worker-run-2",
        "reason": "operator cancel",
    }
    cancelled = control.cancel(**cancel_kwargs)
    assert cancelled.accepted and cancelled.result["attempt_state"] == "cancelling"
    cancelled_replay = control.cancel(**cancel_kwargs)
    assert cancelled_replay.replayed and cancelled_replay.to_dict() == cancelled.to_dict()

    with pytest.raises(ContractError) as caught:
        control.cancel(
            **{
                **cancel_kwargs,
                "operation_key": "late-cancel",
                "reason": "late worker",
            }
        )
    assert caught.value.code == int(ErrorCode.INVALID_TRANSITION)
    repository = execution_stack["repository"]
    assert repository._connection.execute(
        "SELECT job_state FROM execution_job WHERE job_id='job-1'"
    ).fetchone()[0] == "cancelling"


def test_resume_rejects_package_hash_drift_and_invalid_success_ids(execution_stack):
    authority = execution_stack["authority"]
    control = authority.control_port
    value, asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-1",
        checkpoint_seq=1,
        completed_units=1,
    )
    control.pause(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        lease_epoch=1,
        operation_key="pause-op-1",
        worker_run_id="worker-run-1",
        reason="operator pause",
        checkpoint_asset_id=asset.asset_id,
    )

    with pytest.raises(ContractError) as caught:
        control.resume(
            job_id="job-1",
            step_id="step-1",
            operation_key="resume-drift-1",
            resume_of_attempt_id="attempt-1",
            worker_run_id="worker-run-2",
            new_attempt_id="attempt-2",
            package_hash="c" * 64,
            checkpoint_asset_id=asset.asset_id,
        )
    assert caught.value.code == int(ErrorCode.INCOMPATIBLE_GENERATION)
    assert execution_stack["repository"]._connection.execute(
        "SELECT count(*) FROM execution_attempt WHERE attempt_id='attempt-2'"
    ).fetchone()[0] == 0

    with pytest.raises(ContractError) as caught:
        control.resume(
            job_id="job-1",
            step_id="step-1",
            operation_key="resume-empty-worker-1",
            resume_of_attempt_id="attempt-1",
            worker_run_id="",
            new_attempt_id="attempt-2",
            checkpoint_asset_id=asset.asset_id,
        )
    assert caught.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)
