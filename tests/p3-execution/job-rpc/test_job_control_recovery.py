from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.backup.models import BackupBarrier
from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.jobs.backup import JobRuntimeBackupContributor
from backend.plotpilot_core.jobs.chapter_runtime import ChapterJobRuntime
from backend.plotpilot_core.jobs.checkpoint_adapter import (
    AuthorityAttemptStartAdapter,
    DurableCheckpointAdapter,
)
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_core.supervisor.job_control import JobControl, RunSnapshotWorker
from backend.plotpilot_core.supervisor.models import WorkerTicket
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

RELEASE = "e" * 64
PACKAGE = "a" * 64


class RecordingSupervisor:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str | None]] = []
        self.released: list[WorkerTicket] = []

    def acquire(self, worker_id: str, *, expected_release_id: str | None = None):
        self.requests.append((worker_id, expected_release_id))
        return WorkerTicket(
            worker_id=worker_id,
            lifecycle_id="worker-run-1",
            retain_id="retain-1",
            release_id=RELEASE,
            pin_epoch=1,
            retire_epoch=1,
        )

    def release(self, ticket: WorkerTicket) -> None:
        self.released.append(ticket)


def _authority(root: Path):
    database = root / "core.db"
    asset_root = root / "assets"
    repository = CoreAuthorityRepository(database)
    assets = AssetStore(asset_root)
    repository.create_workspace(Workspace("ws-1", "Novel"))
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
    return database, asset_root, repository, authority, snapshot


def test_attempt_start_reads_the_same_authority_row_and_runtime_uses_snapshot_worker(
    tmp_path,
):
    database, asset_root, repository, authority, snapshot = _authority(tmp_path)
    supervisor = RecordingSupervisor()
    runtime = ChapterJobRuntime(
        authority,
        job_control=JobControl(supervisor),
        checkpoints=DurableCheckpointAdapter(authority),
    )
    worker = RunSnapshotWorker(
        run_snapshot_id=snapshot["snapshot_id"],
        worker_id="worker-1",
        plugin_id="com.plotpilot.demo",
        generation_id="generation-1",
        release_id=RELEASE,
    )

    started = runtime.start_attempt(
        run_snapshot_worker=worker,
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        package_hash=PACKAGE,
        capability_id="writing.chapter.draft/v1",
        preallocated_receipt_id="receipt-1",
        expected_result_contract="candidate-batch/v1",
    )

    assert supervisor.requests == [("worker-1", RELEASE)]
    assert started.ticket.lifecycle_id == "worker-run-1"
    assert started.binding.worker_run_id == "worker-run-1"
    assert started.binding.lease_epoch == 1
    assert started.binding.expected_result_contract == "candidate-batch/v1"
    repository.close()

    reopened = CoreAuthorityRepository(database)
    try:
        replay = AuthorityAttemptStartAdapter(
            ExecutionAuthority(reopened, AssetStore(asset_root))
        ).start_attempt(
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
        assert replay == started.binding
    finally:
        reopened.close()

    source = Path("backend/plotpilot_core/jobs/checkpoint_adapter.py").read_text(
        encoding="utf-8"
    )
    assert "repository._connection" not in source


def test_backup_contributor_holds_quiesce_and_reports_only_durable_generation(tmp_path):
    database, _, repository, _, _ = _authority(tmp_path)
    contributor = JobRuntimeBackupContributor(repository)
    entered = Event()

    def writer() -> bool:
        entered.set()
        with repository.transaction() as connection:
            connection.execute(
                "UPDATE execution_job SET updated_at=updated_at WHERE job_id='job-1'"
            )
        return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        with contributor.quiesce(
            backup_epoch=7,
            created_at="2026-09-04T00:00:00Z",
            mode="full",
            workspace_ids=("ws-1",),
        ) as barrier:
            assert isinstance(barrier, BackupBarrier)
            generation = contributor.capture_durable_generation(barrier=barrier)
            future = pool.submit(writer)
            assert entered.wait(timeout=1)
            time.sleep(0.05)
            assert future.done() is False
            assert generation.barrier_token == barrier.token
            assert generation.backup_epoch == 7
            assert generation.job_count == 1
            assert generation.checkpoint_count == 0
            assert generation.durable_generation_hash in barrier.token
            assert not hasattr(generation, "files")
            assert not hasattr(generation, "rows")
        assert future.result(timeout=1) is True
    repository.close()

    reopened = CoreAuthorityRepository(database)
    try:
        restarted = JobRuntimeBackupContributor(reopened)
        with restarted.hold_for_backup(
            backup_epoch=8,
            created_at="2026-09-04T00:01:00Z",
            mode="data",
            workspace_ids=("ws-1",),
        ) as barrier:
            recovered = restarted.capture_durable_generation(barrier=barrier)
        assert recovered.durable_generation_hash == generation.durable_generation_hash
    finally:
        reopened.close()
