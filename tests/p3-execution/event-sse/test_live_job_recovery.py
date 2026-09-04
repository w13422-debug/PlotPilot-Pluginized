from __future__ import annotations

import pytest

from backend.plotpilot_core.api.v2.jobs.sse.adapter import JobSSEAdapter
from backend.plotpilot_core.events import (
    CoreEventStore,
    EventRecoveryService,
    JobEventStore,
    JobSnapshotStore,
)
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode
from backend.plotpilot_plugin_sdk.core_api_v2 import (
    parse_job_snapshot_v2,
    parse_job_sse_recovery_v2,
)

STREAM_HIGH_WATER = {
    "stream_id": "stream-1",
    "output_role": "chapter",
    "target": {
        "workspace_id": "ws-1",
        "entity_kind": "document",
        "entity_id": "document-1",
    },
    "acked_prefix_seq": 2,
    "acked_bytes": 5,
    "acked_prefix_hash": "a" * 64,
}


def _job_event(sequence: int) -> dict:
    return {
        "schema": "plugin-job-event/v1",
        "event_id": f"job-event-{sequence}",
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "event_type": "plugin.com.plotpilot.event.job.progress",
        "plugin_id": "com.plotpilot.event",
        "release_id": "a" * 64,
        "local_seq": sequence,
        "payload_asset_id": None,
        "payload_hash": None,
        "occurred_at": f"2026-08-28T00:01:{sequence:02d}Z",
    }


def _runtime_projection(connection, job_id: str) -> dict:
    assert connection.in_transaction
    assert job_id == "job-1"
    return {
        "current_checkpoint_id": None,
        "stream_high_waters": [{"step_id": "step-1", **STREAM_HIGH_WATER}],
    }


def _live_adapter(event_stack) -> tuple[JobEventStore, JobSnapshotStore, JobSSEAdapter]:
    events = JobEventStore(event_stack["repository"])
    snapshots = JobSnapshotStore(
        event_stack["repository"],
        event_stack["assets"],
        _runtime_projection,
    )
    recovery = EventRecoveryService(CoreEventStore(event_stack["repository"]), events)
    return events, snapshots, JobSSEAdapter(recovery, snapshots)


def test_job_snapshot_v2_projects_only_authoritative_job_time_and_runtime(
    event_stack,
) -> None:
    _events, snapshots, _adapter = _live_adapter(event_stack)
    with event_stack["repository"].transaction() as connection:
        connection.execute(
            "UPDATE execution_job SET created_at=?,updated_at=? WHERE job_id=?",
            ("2026-09-04T01:02:03Z", "2026-09-04T01:02:04Z", "job-1"),
        )

    persisted = snapshots.capture_v2("job-1", workspace_id="ws-1")
    snapshot = parse_job_snapshot_v2(persisted.value)

    assert snapshot["state"] == "running"
    assert snapshot["writer_epoch"] == 1
    assert snapshot["current_attempt_id"] == "attempt-1"
    assert snapshot["checkpoint_id"] is None
    assert snapshot["stream_high_waters"] == [STREAM_HIGH_WATER]
    assert snapshot["created_at"] == "2026-09-04T01:02:03Z"
    assert snapshot["updated_at"] == "2026-09-04T01:02:04Z"
    assert persisted.high_water_seq == snapshot["job_event_high_water"] == 0


def test_v2_replay_and_retention_gap_are_contiguous_and_snapshot_bound(
    event_stack,
) -> None:
    events, _snapshots, adapter = _live_adapter(event_stack)
    for sequence in range(1, 4):
        events.append(_job_event(sequence))

    replay = adapter.recover(
        workspace_id="ws-1",
        job_id="job-1",
        after_seq=1,
        last_event_id="job/job-1/1",
    )
    parse_job_sse_recovery_v2(replay)
    assert replay["gap"] is False
    assert replay["snapshot"] is None
    assert [event["job_event_seq"] for event in replay["tail"]] == [2, 3]
    assert replay["snapshot_cursor"] == "job/job-1/3"

    assert events.prune_through("job-1", 2) == 2
    recovered = adapter.recover(
        workspace_id="ws-1",
        job_id="job-1",
        after_seq=0,
        last_event_id="job/job-1/0",
    )
    parse_job_sse_recovery_v2(recovered)
    assert recovered["gap"] is True
    assert recovered["snapshot_required"] is True
    assert recovered["snapshot"] is not None
    assert recovered["snapshot_cursor"] == "job/job-1/3"
    assert recovered["snapshot"]["job_event_high_water"] == 3
    assert recovered["tail"] == []


@pytest.mark.parametrize(
    ("workspace_id", "after_seq", "last_event_id", "expected_code"),
    [
        ("ws-other", 0, "job/job-1/0", ErrorCode.INVALID_TRANSITION),
        ("ws-1", 0, "core/0", ErrorCode.RESULT_CONTRACT_MISMATCH),
        ("ws-1", 0, "job/job-other/0", ErrorCode.RESULT_CONTRACT_MISMATCH),
        ("ws-1", 1, "job/job-1/1", ErrorCode.INVALID_TRANSITION),
    ],
)
def test_v2_recovery_rejects_cross_workspace_wrong_domain_and_ahead_cursor(
    event_stack,
    workspace_id: str,
    after_seq: int,
    last_event_id: str,
    expected_code: ErrorCode,
) -> None:
    _events, _snapshots, adapter = _live_adapter(event_stack)
    with pytest.raises(ContractError) as caught:
        adapter.recover(
            workspace_id=workspace_id,
            job_id="job-1",
            after_seq=after_seq,
            last_event_id=last_event_id,
        )
    assert caught.value.code == int(expected_code)
