from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from backend.plotpilot_core.api.v1.jobs.rpc import JobCommandQueryAdapter
from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.events.store import CoreEventStore, JobEventStore
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.repositories.checkpoints import SQLiteExecutionControlPort
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    ErrorCode,
    canonical_bytes,
)
from backend.plotpilot_plugin_sdk.verifier import hash_without_field


def _prepare_candidate_stage(stack, completion):
    stack["authority"].stage_candidate_batch(
        job_id=completion["job_id"],
        step_id=completion["step_id"],
        attempt_id=completion["attempt_id"],
        lease_epoch=completion["lease_epoch"],
        operation_key=completion["candidate_stage_operation_key"],
        worker_run_id=completion["worker_run_id"],
        result_bundle_asset_id=completion["result_bundle_asset_id"],
        input_snapshot_hash=stack["snapshot"]["snapshot_hash"],
        operation_meta=completion["operation_meta"],
    )


def _checkpoint_asset(
    stack,
    *,
    checkpoint_id: str,
    checkpoint_seq: int,
    completed_units: int,
    total_units: int = 2,
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
        "total_units": total_units,
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


def _prompt_asset(stack, content: bytes = b"confirm"):
    return stack["assets"].put(
        content,
        mime="text/plain",
        logical_role="await_user_prompt",
        provenance="test:p1-durable-authority",
    )


def _pause_kwargs(asset, *, operation_key: str = "pause-op-1"):
    return {
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "lease_epoch": 1,
        "operation_key": operation_key,
        "worker_run_id": "worker-run-1",
        "reason": "operator pause",
        "checkpoint_asset_id": asset.asset_id,
    }


def _authority_state(repository):
    connection = repository._connection
    tables = (
        "execution_job",
        "execution_step",
        "execution_attempt",
        "execution_checkpoint",
        "execution_checkpoint_operation",
        "execution_control_operation",
        "execution_job_event",
        "execution_core_event",
    )
    return {
        "tables": {
            table: [tuple(row) for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()]
            for table in tables
        },
        "sequences": [tuple(row) for row in connection.execute(
            "SELECT name,seq FROM sqlite_sequence WHERE name IN "
            "('execution_checkpoint','execution_checkpoint_operation',"
            "'execution_control_operation','execution_job_event','execution_core_event') "
            "ORDER BY name"
        ).fetchall()],
    }


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
            checkpoint_asset_id=asset.asset_id,
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


def test_control_cas_event_revision_high_water_rollback_sse_and_restart_replay(
    execution_stack, monkeypatch
):
    authority = execution_stack["authority"]
    control = authority.control_port
    _value, asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-control-1",
        checkpoint_seq=1,
        completed_units=1,
    )
    kwargs = _pause_kwargs(asset, operation_key="pause-atomic-1")
    original_append = control.core_events.append

    def append_then_fail(event_value, *, connection=None):
        original_append(event_value, connection=connection)
        raise RuntimeError("injected control Core Event failure")

    with monkeypatch.context() as patch:
        patch.setattr(control.core_events, "append", append_then_fail)
        with pytest.raises(RuntimeError, match="injected control Core Event failure"):
            control.pause(**kwargs)

    connection = execution_stack["repository"]._connection
    assert connection.execute("SELECT count(*) FROM execution_checkpoint").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM execution_control_operation").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM execution_job_event").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM execution_core_event").fetchone()[0] == 0
    assert tuple(connection.execute(
        "SELECT job_state,job_revision,job_event_high_water,core_event_high_water "
        "FROM execution_job WHERE job_id='job-1'"
    ).fetchone()) == ("running", 2, 0, 0)

    first = control.pause(**kwargs)
    assert first.accepted and not first.replayed
    job_event = connection.execute(
        "SELECT job_event_seq,event_json FROM execution_job_event "
        "WHERE event_id LIKE 'job-event-%' ORDER BY job_event_seq DESC LIMIT 1"
    ).fetchone()
    core_event = connection.execute(
        "SELECT core_event_seq,aggregate_revision,event_json FROM execution_core_event"
    ).fetchone()
    job = connection.execute(
        "SELECT job_state,job_revision,job_event_high_water,core_event_high_water "
        "FROM execution_job WHERE job_id='job-1'"
    ).fetchone()
    assert job[0] == "paused"
    assert int(job[2]) == int(job_event[0])
    assert int(job[3]) == int(core_event[0])
    assert int(job[1]) == int(core_event[1])
    assert '"event_type":"job.state.changed"' in core_event[2]

    job_window = JobEventStore(execution_stack["repository"]).window("job-1", 0)
    core_window = CoreEventStore(execution_stack["repository"]).window(
        0, workspace_id="ws-1", event_types=("job.state.changed",)
    )
    assert job_window.durable_high_water_seq == int(job[2])
    assert core_window.durable_high_water_seq == int(job[3])
    assert any(item["event_type"].endswith(".job.pause") for item in job_window.events)
    assert [item["event_type"] for item in core_window.events] == ["job.state.changed"]

    replay = control.pause(**kwargs)
    assert replay.replayed and replay.to_dict() == first.to_dict()
    assert connection.execute("SELECT count(*) FROM execution_control_operation").fetchone()[0] == 1

    reopened = CoreAuthorityRepository(execution_stack["database"])
    try:
        restarted = SQLiteExecutionControlPort(
            reopened,
            assets=AssetStore(execution_stack["asset_root"]),
        )
        restarted_replay = restarted.pause(**kwargs)
        assert restarted_replay.replayed and restarted_replay.to_dict() == first.to_dict()
    finally:
        reopened.close()


def test_durable_control_fences_attempt_owner_epoch_worker_and_method_before_replay(
    execution_stack,
):
    authority = execution_stack["authority"]
    control = authority.control_port
    _value, asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-fence-1",
        checkpoint_seq=1,
        completed_units=1,
    )
    kwargs = _pause_kwargs(asset, operation_key="pause-fence-1")
    control.pause(**kwargs)

    with pytest.raises(ContractError) as caught:
        control.pause(**{**kwargs, "worker_run_id": "worker-run-stale"})
    assert caught.value.code == int(ErrorCode.STALE_LEASE)

    with pytest.raises(ContractError) as caught:
        control.pause(**{**kwargs, "lease_epoch": 2})
    assert caught.value.code == int(ErrorCode.STALE_LEASE)

    with pytest.raises(ContractError) as caught:
        control.pause(**{**kwargs, "reason": "different request"})
    assert caught.value.code == int(ErrorCode.DUPLICATE_REQUEST)

    with pytest.raises(ContractError) as caught:
        control.cancel(
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
            operation_key="pause-fence-1",
            worker_run_id="worker-run-1",
            reason="cross-method",
        )
    assert caught.value.code == int(ErrorCode.DUPLICATE_REQUEST)

    connection = execution_stack["repository"]._connection
    connection.execute(
        "UPDATE execution_attempt SET release_id=? WHERE attempt_id='attempt-1'",
        ("f" * 64,),
    )
    with pytest.raises(ContractError) as caught:
        control.pause(**kwargs)
    assert caught.value.code == int(ErrorCode.INCOMPATIBLE_GENERATION)


def test_await_user_reason_prompt_asset_closure_and_restart_replay(execution_stack):
    authority = execution_stack["authority"]
    control = authority.control_port
    _value, checkpoint_asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-await-1",
        checkpoint_seq=1,
        completed_units=1,
    )
    prompt = _prompt_asset(execution_stack)
    kwargs = {
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "lease_epoch": 1,
        "operation_key": "await-1",
        "worker_run_id": "worker-run-1",
        "reason": "user_input",
        "checkpoint_asset_id": checkpoint_asset.asset_id,
        "prompt_asset_id": prompt.asset_id,
    }
    first = control.await_user(**kwargs)
    assert first.accepted
    assert first.result == {
        "accepted": True,
        "attempt_state": "suspended",
        "step_state": "waiting_user",
        "job_state": "waiting_user",
        "job_event_seq": 2,
    }
    stored_request = execution_stack["repository"]._connection.execute(
        "SELECT request_json FROM execution_control_operation WHERE operation_key='await-1'"
    ).fetchone()[0]
    request = json.loads(stored_request)
    assert request["reason"] == "user_input"
    assert request["prompt_asset_id"] == prompt.asset_id
    assert request["prompt_asset_hash"] == prompt.sha256

    replay = control.await_user(**kwargs)
    assert replay.replayed and replay.to_dict() == first.to_dict()

    with pytest.raises(ContractValidationError):
        control.await_user(**{**kwargs, "reason": "free-form operator text"})

    reopened = CoreAuthorityRepository(execution_stack["database"])
    try:
        restarted = SQLiteExecutionControlPort(
            reopened,
            assets=AssetStore(execution_stack["asset_root"]),
        )
        restarted_replay = restarted.await_user(**kwargs)
        assert restarted_replay.replayed and restarted_replay.to_dict() == first.to_dict()
    finally:
        reopened.close()

    digest = prompt.asset_id.removeprefix("asset-sha256-")
    Path(execution_stack["asset_root"], "objects", digest[:2], digest).unlink()
    with pytest.raises(ContractError) as caught:
        control.await_user(**kwargs)
    assert caught.value.code == int(ErrorCode.ASSET_ERROR)


def test_terminal_cancel_after_attempt_terminal_returns_terminal_known(execution_stack):
    from support import complete_kwargs, make_candidate_bundle

    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)
    completion = complete_kwargs(bundle_asset, receipt)
    _prepare_candidate_stage(execution_stack, completion)
    execution_stack["authority"].complete_attempt(**completion)
    terminal_kwargs = {
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "lease_epoch": 1,
        "operation_key": "cancel-after-attempt-terminal",
        "worker_run_id": "worker-run-1",
        "reason": "late cancellation",
    }
    attempt_terminal = execution_stack["authority"].control_port.cancel(**terminal_kwargs)
    assert attempt_terminal.result == {
        "accepted": True,
        "terminal_known": True,
        "attempt_state": "succeeded",
    }


def test_terminal_cancel_after_job_terminal_returns_terminal_known(execution_stack):
    connection = execution_stack["repository"]._connection
    connection.execute(
        "UPDATE execution_job SET job_state='failed' WHERE job_id='job-1'"
    )
    job_terminal = execution_stack["authority"].control_port.cancel(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        lease_epoch=1,
        operation_key="cancel-after-job-terminal",
        worker_run_id="worker-run-1",
        reason="late cancellation",
    )
    assert job_terminal.result == {
        "accepted": True,
        "terminal_known": True,
        "attempt_state": "running",
    }


def test_checkpoint_chain_rejects_total_units_drift_without_partial_state(execution_stack):
    repository = execution_stack["repository"]
    store = execution_stack["authority"].checkpoint_store
    first_value, first_asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-chain-1",
        checkpoint_seq=1,
        completed_units=1,
    )
    first = store.commit_checkpoint(
        first_value,
        checkpoint_asset_id=first_asset.asset_id,
        operation_key="checkpoint-chain-op-1",
        local_seq=1,
        worker_run_id="worker-run-1",
    )
    assert first.accepted
    before = _authority_state(repository)

    drift_value, drift_asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-chain-2",
        checkpoint_seq=2,
        completed_units=2,
        total_units=3,
    )
    with pytest.raises(ContractError) as caught:
        store.commit_checkpoint(
            drift_value,
            checkpoint_asset_id=drift_asset.asset_id,
            operation_key="checkpoint-chain-op-2",
            local_seq=2,
            worker_run_id="worker-run-1",
        )
    assert caught.value.code == int(ErrorCode.CHECKPOINT_INVALID)
    assert "total_units" in str(caught.value)
    assert _authority_state(repository) == before
    assert store.get_latest("job-1") == first_value


def test_direct_checkpoint_replay_fences_worker_epoch_and_cross_attempt_before_replay(
    execution_stack,
):
    repository = execution_stack["repository"]
    authority = execution_stack["authority"]
    store = authority.checkpoint_store
    control = authority.control_port
    value, asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-replay-fence-1",
        checkpoint_seq=1,
        completed_units=1,
    )
    operation_key = "checkpoint-replay-fence-op"
    first = store.commit_checkpoint(
        value,
        checkpoint_asset_id=asset.asset_id,
        operation_key=operation_key,
        local_seq=1,
        worker_run_id="worker-run-1",
    )
    assert first.accepted and not first.replayed
    before = _authority_state(repository)

    with pytest.raises(ContractError) as caught:
        store.commit_checkpoint(
            value,
            checkpoint_asset_id=asset.asset_id,
            operation_key=operation_key,
            worker_run_id="worker-run-stale",
        )
    assert caught.value.code == int(ErrorCode.STALE_LEASE)
    assert _authority_state(repository) == before

    stale_epoch_value, stale_epoch_asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-replay-fence-stale-epoch",
        checkpoint_seq=1,
        completed_units=1,
        lease_epoch=2,
    )
    with pytest.raises(ContractError) as caught:
        store.commit_checkpoint(
            stale_epoch_value,
            checkpoint_asset_id=stale_epoch_asset.asset_id,
            operation_key=operation_key,
            worker_run_id="worker-run-1",
        )
    assert caught.value.code == int(ErrorCode.STALE_LEASE)
    assert _authority_state(repository) == before

    next_value, next_asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-replay-fence-2",
        checkpoint_seq=2,
        completed_units=2,
    )
    paused = control.pause(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        lease_epoch=1,
        operation_key="pause-before-cross-attempt-replay",
        worker_run_id="worker-run-1",
        reason="prepare next attempt",
        checkpoint_asset_id=next_asset.asset_id,
    )
    assert paused.accepted
    resumed = control.resume(
        job_id="job-1",
        step_id="step-1",
        operation_key="resume-before-cross-attempt-replay",
        resume_of_attempt_id="attempt-1",
        worker_run_id="worker-run-2",
        new_attempt_id="attempt-2",
        checkpoint_asset_id=next_asset.asset_id,
    )
    assert resumed.accepted
    after_attempt_change = _authority_state(repository)

    with pytest.raises(ContractError) as caught:
        store.commit_checkpoint(
            value,
            checkpoint_asset_id=asset.asset_id,
            operation_key=operation_key,
            worker_run_id="worker-run-1",
        )
    assert caught.value.code == int(ErrorCode.STALE_LEASE)
    assert _authority_state(repository) == after_attempt_change
