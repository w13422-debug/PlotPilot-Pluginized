from __future__ import annotations

import json
from pathlib import Path

from backend.plotpilot_core.api.v2.jobs.sse.adapter import JobSSEAdapter
from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.events import (
    CoreEventStore,
    EventRecoveryService,
    JobEventStore,
    JobSnapshotStore,
)
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk.core_api_v2 import parse_job_sse_recovery_v2
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

RELEASE = "a" * 64
PACKAGE = "b" * 64


def _runtime_projection(connection, job_id: str) -> dict:
    assert connection.in_transaction
    assert job_id == "job-1"
    return {"current_checkpoint_id": None, "stream_high_waters": []}


def _job_event(sequence: int) -> dict:
    return {
        "schema": "plugin-job-event/v1",
        "event_id": f"restart-event-{sequence}",
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "event_type": "plugin.com.plotpilot.restart.job.progress",
        "plugin_id": "com.plotpilot.restart",
        "release_id": RELEASE,
        "local_seq": sequence,
        "payload_asset_id": None,
        "payload_hash": None,
        "occurred_at": f"2026-09-04T02:00:{sequence:02d}Z",
    }


def _adapter(repository: CoreAuthorityRepository, assets: AssetStore) -> JobSSEAdapter:
    events = JobEventStore(repository)
    snapshots = JobSnapshotStore(repository, assets, _runtime_projection)
    return JobSSEAdapter(
        EventRecoveryService(CoreEventStore(repository), events), snapshots
    )


def _seed(repository: CoreAuthorityRepository, assets: AssetStore) -> None:
    repository.create_workspace(Workspace("ws-1", "SSE restart"))
    run_snapshot = json.loads(
        Path("contracts/golden/run-snapshot/snapshot.json").read_text(encoding="utf-8")
    )
    run_snapshot["plugin_releases"] = [
        {
            "plugin_id": "com.plotpilot.restart",
            "release_id": RELEASE,
            "package_hash": PACKAGE,
            "data_generation_id": "generation-1",
        }
    ]
    run_snapshot["request_key"] = request_key(run_snapshot)
    run_snapshot["snapshot_hash"] = snapshot_hash(run_snapshot)
    authority = ExecutionAuthority(repository, assets)
    authority.create_from_verified_snapshot("job-1", run_snapshot)
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
        plugin_id="com.plotpilot.restart",
        release_id=RELEASE,
        package_hash=PACKAGE,
        capability_id="writing.chapter.draft/v1",
        generation_id="generation-1",
        preallocated_receipt_id="receipt-1",
    )


def test_restart_reconstructs_byte_equivalent_gap_snapshot_and_cursor(tmp_path) -> None:
    database = tmp_path / "core.db"
    assets = AssetStore(tmp_path / "assets")
    repository = CoreAuthorityRepository(database)
    try:
        _seed(repository, assets)
        events = JobEventStore(repository)
        for sequence in range(1, 4):
            events.append(_job_event(sequence))
        assert events.prune_through("job-1", 2) == 2
        before = _adapter(repository, assets).recover(
            workspace_id="ws-1",
            job_id="job-1",
            after_seq=0,
            last_event_id="job/job-1/0",
        )
    finally:
        repository.close()

    reopened = CoreAuthorityRepository(database)
    try:
        after = _adapter(reopened, assets).recover(
            workspace_id="ws-1",
            job_id="job-1",
            after_seq=0,
            last_event_id="job/job-1/0",
        )
        parse_job_sse_recovery_v2(after)
        assert after == before
        assert after["snapshot_cursor"] == "job/job-1/3"
        assert after["snapshot"]["current_attempt_id"] == "attempt-1"
        assert after["snapshot"]["writer_epoch"] == 1
    finally:
        reopened.close()
