from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from backend.plotpilot_core.api.v1.jobs.sse import JobSSEAdapter, StreamCursor
from backend.plotpilot_core.events import (
    CoreEventStore,
    CoreSnapshotStore,
    EventRecoveryService,
    JobEventPageStore,
    JobEventStore,
    JobPollProjectionStore,
    JobSnapshotStore,
    next_job_event_seq_to_after,
)
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode, verify_sse_recovery

sys.path.insert(0, str(Path(__file__).parent))
from conftest import append_core, core_event, job_event  # noqa: E402


def _decode_data(frame: bytes) -> dict:
    lines = frame.decode("utf-8").splitlines()
    payload = "\n".join(line[6:] for line in lines if line.startswith("data: "))
    return json.loads(payload)


def _adapter(event_stack):
    core = CoreEventStore(event_stack["repository"])
    jobs = JobEventStore(event_stack["repository"])
    return core, jobs, JobSSEAdapter(EventRecoveryService(core, jobs), retry_ms=1500)


def test_refresh_replays_once_and_advances_filtered_empty_cursor(event_stack) -> None:
    core, _jobs, adapter = _adapter(event_stack)
    append_core(core, core_event(1))
    append_core(core, core_event(2, event_type="backup.completed"))

    first = adapter.core_replay(last_event_id=None, workspace_id="ws-1")
    assert first.next_cursor.encode() == "core/2"
    assert [_decode_data(frame).get("event_id") for frame in first.frames[1:-1]] == [
        "core-event-1",
        "core-event-2",
    ]
    assert first.frames[-1] == b"id: core/2\n: durable high-water\n\n"

    refreshed = adapter.core_replay(
        last_event_id=first.next_cursor.encode(),
        workspace_id="ws-1",
        event_types=("job.terminal",),
    )
    assert len(refreshed.frames) == 2
    assert refreshed.next_cursor.sequence == 2
    assert _decode_data(refreshed.frames[0])["gap"] is False


def test_typed_last_event_id_rejects_core_job_domain_mix(event_stack) -> None:
    _core, _jobs, adapter = _adapter(event_stack)
    with pytest.raises(ContractError) as mixed:
        adapter.job_replay("job-1", last_event_id="core/0")
    assert mixed.value.code == int(ErrorCode.INVALID_TRANSITION)
    with pytest.raises(ContractError):
        adapter.core_replay(last_event_id="0")
    for noncanonical in ("core/+1", "core/01", "core/ 1"):
        with pytest.raises(ContractError):
            adapter.core_replay(last_event_id=noncanonical)
    assert StreamCursor.parse(
        "job/job/with/slash/7", stream_kind="job_event", aggregate_id="job/with/slash"
    ).sequence == 7


def test_job_retention_gap_emits_snapshot_then_converges_tail(event_stack) -> None:
    _core, jobs, adapter = _adapter(event_stack)
    snapshots = JobSnapshotStore(
        event_stack["repository"],
        event_stack["assets"],
        lambda _connection, _job_id: {
            "current_checkpoint_id": None,
            "stream_high_waters": [],
        },
    )
    for index in range(1, 4):
        jobs.append(job_event(index))
    jobs.prune_through("job-1", 2)

    def capture_then_advance(high_water: int):
        snapshot = snapshots.capture("job-1", high_water)
        jobs.append(job_event(4))
        return snapshot

    response = adapter.job_replay(
        "job-1", last_event_id="job/job-1/0", snapshot_provider=capture_then_advance
    )
    recovery = _decode_data(response.frames[0])
    verify_sse_recovery(recovery)
    assert recovery["gap"] is True
    assert recovery["replay_floor_seq"] == 2
    assert recovery["durable_high_water_seq"] == 4
    snapshot = _decode_data(response.frames[1])
    assert snapshot["schema"] == "job-snapshot/v1"
    assert snapshot["job_event_high_water"] == 3
    assert _decode_data(response.frames[2])["job_event_seq"] == 4
    assert response.status_code == 409
    assert response.next_cursor.encode() == "job/job-1/4"
    event_stack["assets"].require(
        recovery["snapshot_asset_id"], sha256=event_stack["assets"].describe(recovery["snapshot_asset_id"]).sha256
    )


def test_snapshot_capture_retries_when_high_water_advances_first(event_stack) -> None:
    _core, jobs, adapter = _adapter(event_stack)
    snapshots = JobSnapshotStore(
        event_stack["repository"],
        event_stack["assets"],
        lambda _connection, _job_id: {
            "current_checkpoint_id": None,
            "stream_high_waters": [],
        },
    )
    for index in range(1, 4):
        jobs.append(job_event(index))
    jobs.prune_through("job-1", 2)
    calls = {"count": 0}

    def advance_before_capture(high_water: int):
        calls["count"] += 1
        if calls["count"] == 1:
            jobs.append(job_event(4))
        return snapshots.capture("job-1", high_water)

    response = adapter.job_replay(
        "job-1",
        last_event_id="job/job-1/0",
        snapshot_provider=advance_before_capture,
    )
    assert calls["count"] == 2
    assert _decode_data(response.frames[1])["job_event_high_water"] == 4
    assert response.next_cursor.sequence == 4


def test_core_retention_gap_uses_core_projector_not_event_payloads(event_stack) -> None:
    core, _jobs, adapter = _adapter(event_stack)
    state = event_stack["assets"].put(
        b'{"workspace_id":"ws-1","title":"Event SSE"}',
        mime="application/json",
        logical_role="core-aggregate-state",
        provenance="test",
    )

    def aggregate_reader(_connection, workspace_id, event_types):
        assert workspace_id == "ws-1"
        assert event_types == ("backup.completed", "workspace.created")
        return [
            {
                "aggregate_type": "workspace",
                "aggregate_id": "ws-1",
                "aggregate_revision": 2,
                "state_asset_id": state.asset_id,
                "state_hash": state.sha256,
            }
        ]

    snapshots = CoreSnapshotStore(
        event_stack["repository"], event_stack["assets"], aggregate_reader
    )
    append_core(core, core_event(1))
    append_core(core, core_event(2, event_type="backup.completed"))
    core.prune_through(1)
    event_types = ("workspace.created", "backup.completed")
    response = adapter.core_replay(
        last_event_id="core/0",
        workspace_id="ws-1",
        event_types=event_types,
        snapshot_provider=lambda high_water: snapshots.capture(
            high_water, workspace_id="ws-1", event_types=event_types
        ),
    )
    recovery = _decode_data(response.frames[0])
    snapshot = _decode_data(response.frames[1])
    assert recovery["snapshot_schema"] == "core-snapshot/v1"
    assert snapshot["core_event_high_water"] == 2
    assert snapshot["covered_aggregates"][0]["state_asset_id"] == state.asset_id
    assert response.status_code == 409
    assert response.next_cursor.encode() == "core/2"


def test_gap_without_snapshot_and_cursor_ahead_fail_closed(event_stack) -> None:
    _core, jobs, adapter = _adapter(event_stack)
    jobs.append(job_event(1))
    jobs.prune_through("job-1", 1)
    with pytest.raises(ContractError) as missing:
        adapter.job_replay("job-1", last_event_id="job/job-1/0")
    assert missing.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)
    with pytest.raises(ContractError) as ahead:
        adapter.job_replay("job-1", last_event_id="job/job-1/2")
    assert ahead.value.code == int(ErrorCode.INVALID_TRANSITION)


def test_after_seq_conflict_and_broker_poll_assets(event_stack) -> None:
    _core, jobs, adapter = _adapter(event_stack)
    jobs.append(job_event(1))
    with pytest.raises(ContractError) as conflict:
        adapter.job_replay(
            "job-1", last_event_id="job/job-1/1", after_seq=0
        )
    assert conflict.value.code == int(ErrorCode.INVALID_TRANSITION)

    snapshots = JobSnapshotStore(
        event_stack["repository"],
        event_stack["assets"],
        lambda _connection, _job_id: {
            "current_checkpoint_id": None,
            "stream_high_waters": [],
        },
    )
    pages = JobEventPageStore(jobs, event_stack["assets"])
    projection = JobPollProjectionStore(event_stack["repository"], snapshots, pages)
    value = projection.project_poll(
        child_job_id="job-1",
        after_job_event_seq=0,
        execution_terminal=False,
        execution_events=[job_event(1)],
    )
    assert value["next_job_event_seq"] == 2
    assert value["child_state"] == "running"
    snapshot = json.loads(event_stack["assets"].read(value["job_snapshot_asset_id"]))
    page = json.loads(event_stack["assets"].read(value["job_event_page_asset_id"]))
    assert snapshot["job_event_high_water"] == page["high_water_seq"] == 1
    assert page["events"][0]["job_event_seq"] == 1

    with event_stack["repository"].transaction() as connection:
        connection.execute(
            "UPDATE execution_job SET job_state='succeeded' WHERE job_id='job-1'"
        )
    terminal = projection.project_poll(
        child_job_id="job-1",
        after_job_event_seq=1,
        execution_terminal=True,
        execution_events=[],
    )
    assert terminal["terminal"] is True
    assert terminal["child_state"] == "succeeded"


def test_poll_projection_is_pinned_against_second_connection_append_and_prune(event_stack) -> None:
    primary_jobs = JobEventStore(event_stack["repository"])
    assert primary_jobs.append(job_event(1))["job_event_seq"] == 1
    second_repository = CoreAuthorityRepository(event_stack["database"])
    second_jobs = JobEventStore(second_repository)
    raced = {"done": False}

    def race_during_aggregate_read(connection, job_id):
        assert connection.in_transaction
        assert job_id == "job-1"
        with second_repository.transaction() as writer:
            assert second_jobs.append(job_event(2), connection=writer)["job_event_seq"] == 2
            assert writer.execute(
                "DELETE FROM execution_job_event WHERE job_id='job-1' AND job_event_seq<=1"
            ).rowcount == 1
            writer.execute(
                "UPDATE execution_job SET job_state='succeeded' WHERE job_id='job-1'"
            )
        raced["done"] = True
        return {"current_checkpoint_id": None, "stream_high_waters": []}

    try:
        snapshots = JobSnapshotStore(
            event_stack["repository"],
            event_stack["assets"],
            race_during_aggregate_read,
        )
        pages = JobEventPageStore(primary_jobs, event_stack["assets"])
        projection = JobPollProjectionStore(event_stack["repository"], snapshots, pages)
        value = projection.project_poll(
            child_job_id="job-1",
            after_job_event_seq=0,
            execution_terminal=False,
            execution_events=[job_event(1)],
        )
        assert raced["done"]
        snapshot = json.loads(event_stack["assets"].read(value["job_snapshot_asset_id"]))
        page = json.loads(event_stack["assets"].read(value["job_event_page_asset_id"]))
        assert snapshot["job_event_high_water"] == page["high_water_seq"] == 1
        assert snapshot["job_state"] == value["child_state"] == "running"
        assert [item["job_event_seq"] for item in page["events"]] == [1]
        assert value["terminal"] is False
        assert value["next_job_event_seq"] == 2
        assert next_job_event_seq_to_after(value["next_job_event_seq"]) == 1

        current = second_jobs.window("job-1", 0)
        assert current.gap is True
        assert current.replay_floor_seq == 1
        assert current.durable_high_water_seq == 2
        assert [item["job_event_seq"] for item in current.events] == [2]
    finally:
        second_repository.close()


@pytest.mark.parametrize("value", [True, 0, -1, "2"])
def test_next_job_event_seq_conversion_rejects_non_positive_values(value) -> None:
    with pytest.raises(ValueError):
        next_job_event_seq_to_after(value)
