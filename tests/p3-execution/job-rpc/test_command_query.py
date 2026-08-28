from __future__ import annotations

import json

import pytest

from backend.plotpilot_core.api.v1.jobs.rpc import JobCommandQueryAdapter
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode
from backend.plotpilot_plugin_sdk.verifier import verify_job_snapshot


def test_create_is_request_key_suppressed_and_projects_the_existing_job(job_stack, snapshot_extensions):
    adapter = JobCommandQueryAdapter(
        job_stack["authority"], snapshot_extensions=snapshot_extensions
    )

    first = adapter.create_or_get(job_id="job-1", run_snapshot=job_stack["snapshot"])
    replay = adapter.create_or_get(job_id="job-2", run_snapshot=job_stack["snapshot"])

    assert first.to_dict() == {
        "job_id": "job-1",
        "workspace_id": "ws-1",
        "job_state": "queued",
        "job_revision": 1,
        "replayed": False,
    }
    assert replay.job_id == "job-1"
    assert replay.replayed is True
    with job_stack["repository"].read_connection() as connection:
        assert connection.execute("SELECT count(*) FROM execution_job").fetchone()[0] == 1


def test_snapshot_is_closed_hash_verified_and_reads_authority_state(job_stack, snapshot_extensions):
    adapter = JobCommandQueryAdapter(
        job_stack["authority"], snapshot_extensions=snapshot_extensions
    )
    adapter.create_or_get(job_id="job-1", run_snapshot=job_stack["snapshot"])
    adapter.freeze_plan(
        job_id="job-1",
        steps=[
            {
                "step_id": "step-1",
                "depends_on": [],
                "result_contract": "candidate-batch/v1",
            }
        ],
        output_step_id="step-1",
    )
    adapter.start_attempt(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        worker_run_id="worker-run-1",
        plugin_id="com.plotpilot.alpha",
        release_id="rel-a",
        package_hash="4" * 64,
        capability_id="writing.chapter.draft/v1",
        generation_id="dg-a",
        preallocated_receipt_id="receipt-1",
        expected_result_contract="candidate-batch/v1",
    )

    snapshot = adapter.get_snapshot(workspace_id="ws-1", job_id="job-1")

    verify_job_snapshot(snapshot)
    assert snapshot["job_state"] == "running"
    assert snapshot["job_revision"] == 2
    assert snapshot["steps"] == [{"step_id": "step-1", "state": "running", "revision": 2}]
    assert snapshot["attempts"] == [
        {"attempt_id": "attempt-1", "state": "running", "lease_epoch": 1}
    ]
    assert snapshot["candidate_ids"] == []
    assert snapshot["current_checkpoint_id"] is None
    assert snapshot["stream_high_waters"] == []
    assert snapshot["core_event_high_water"] == 0
    assert snapshot["job_event_high_water"] == 0


def test_event_page_is_workspace_scoped_and_uses_the_committed_high_water(
    job_stack, snapshot_extensions
):
    adapter = JobCommandQueryAdapter(
        job_stack["authority"], snapshot_extensions=snapshot_extensions
    )
    adapter.create_or_get(job_id="job-1", run_snapshot=job_stack["snapshot"])
    adapter.start_attempt(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        worker_run_id="worker-run-1",
        plugin_id="com.plotpilot.alpha",
        release_id="rel-a",
        package_hash="4" * 64,
        capability_id="writing.chapter.draft/v1",
        generation_id="dg-a",
    )
    event = {
        "schema": "plugin-job-event/v1",
        "event_id": "event-1",
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "job_event_seq": 1,
        "event_type": "plugin.com.plotpilot.alpha.progress",
        "plugin_id": "com.plotpilot.alpha",
        "release_id": "e" * 64,
        "local_seq": 1,
        "payload_asset_id": None,
        "payload_hash": None,
        "occurred_at": "2026-08-28T18:00:00Z",
    }
    with job_stack["repository"].transaction() as connection:
        connection.execute(
            "INSERT INTO execution_job_event VALUES(?,?,?,?,?,?)",
            ("job-1", 1, "event-1", "attempt-1", 1, json.dumps(event)),
        )
        connection.execute(
            "UPDATE execution_job SET job_event_high_water=1 WHERE job_id='job-1'"
        )

    page = adapter.get_event_page(
        workspace_id="ws-1", job_id="job-1", after_job_event_seq=0
    )
    assert page == {
        "schema": "job-event-page/v1",
        "job_id": "job-1",
        "after_job_event_seq": 0,
        "events": [event],
        "next_job_event_seq": 2,
        "high_water_seq": 1,
    }
    empty_page = adapter.get_event_page(
        workspace_id="ws-1", job_id="job-1", after_job_event_seq=1
    )
    assert empty_page["events"] == []
    assert empty_page["next_job_event_seq"] == 2


def test_unknown_and_cross_workspace_job_are_indistinguishable(job_stack, snapshot_extensions):
    adapter = JobCommandQueryAdapter(
        job_stack["authority"], snapshot_extensions=snapshot_extensions
    )
    adapter.create_or_get(job_id="job-1", run_snapshot=job_stack["snapshot"])

    observed = []
    for workspace_id, job_id in (("ws-1", "missing"), ("ws-2", "job-1")):
        with pytest.raises(ContractError) as caught:
            adapter.get_snapshot(workspace_id=workspace_id, job_id=job_id)
        observed.append((caught.value.code, str(caught.value)))

    assert observed[0] == observed[1]
    assert observed[0][0] == int(ErrorCode.INVALID_TRANSITION)

    for workspace_id, job_id in (("ws-1", "missing"), ("ws-2", "job-1")):
        with pytest.raises(ContractError) as caught:
            adapter.get_event_page(workspace_id=workspace_id, job_id=job_id)
        assert (caught.value.code, str(caught.value)) == observed[0]


def test_snapshot_extension_authority_must_be_composed_explicitly(job_stack):
    with pytest.raises(TypeError, match="snapshot_extensions"):
        JobCommandQueryAdapter(job_stack["authority"])  # type: ignore[call-arg]


@pytest.mark.parametrize("cursor", [-1, True, 1.5, "1"])
def test_event_page_rejects_non_contract_cursors(job_stack, snapshot_extensions, cursor):
    adapter = JobCommandQueryAdapter(
        job_stack["authority"], snapshot_extensions=snapshot_extensions
    )
    with pytest.raises(ContractError) as caught:
        adapter.get_event_page(
            workspace_id="ws-1", job_id="missing", after_job_event_seq=cursor
        )
    assert caught.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)
