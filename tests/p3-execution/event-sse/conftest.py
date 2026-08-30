from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash


RELEASE = "a" * 64
PACKAGE = "b" * 64


@pytest.fixture
def event_stack(tmp_path):
    database = tmp_path / "core.db"
    repository = CoreAuthorityRepository(database)
    assets = AssetStore(tmp_path / "assets")
    repository.create_workspace(Workspace("ws-1", "Event SSE"))
    snapshot = json.loads(
        Path("contracts/golden/run-snapshot/snapshot.json").read_text(encoding="utf-8")
    )
    snapshot["plugin_releases"] = [
        {
            "plugin_id": "com.plotpilot.event",
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
        [{"step_id": "step-1", "depends_on": [], "result_contract": "candidate-batch/v1"}],
        output_step_id="step-1",
    )
    authority.start_attempt(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        worker_run_id="worker-1",
        plugin_id="com.plotpilot.event",
        release_id=RELEASE,
        package_hash=PACKAGE,
        capability_id="writing.chapter.draft/v1",
        generation_id="generation-1",
        preallocated_receipt_id="receipt-1",
    )
    yield {
        "database": database,
        "repository": repository,
        "assets": assets,
        "authority": authority,
    }
    repository.close()


def core_event(index: int, *, event_type: str = "workspace.created") -> dict:
    return {
        "schema": "core-event/v1",
        "event_id": f"core-event-{index}",
        "event_type": event_type,
        "aggregate_id": "ws-1",
        "aggregate_revision": index,
        "workspace_id": "ws-1",
        "occurred_at": f"2026-08-28T00:00:{index:02d}Z",
        "producer": {
            "producer_type": "core",
            "producer_id": "event-sse-test",
            "release_id": None,
        },
        "causation_id": None,
        "correlation_id": f"correlation-{index}",
        "payload_asset_id": None,
        "payload_hash": None,
    }


def append_core(store, event: dict) -> dict:
    with store.repository.transaction() as connection:
        return store.append(event, connection=connection)


def job_event(index: int) -> dict:
    return {
        "schema": "plugin-job-event/v1",
        "event_id": f"job-event-{index}",
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "event_type": "plugin.com.plotpilot.event.job.progress",
        "plugin_id": "com.plotpilot.event",
        "release_id": RELEASE,
        "local_seq": index,
        "payload_asset_id": None,
        "payload_hash": None,
        "occurred_at": f"2026-08-28T00:01:{index:02d}Z",
    }
