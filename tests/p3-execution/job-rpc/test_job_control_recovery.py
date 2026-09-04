from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

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
from backend.plotpilot_core.supervisor.models import AttemptFence, WorkerTicket
from backend.plotpilot_core.supervisor.rpc import RpcEvent
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

RELEASE = "e" * 64
PACKAGE = "a" * 64


class RecordingSupervisor:
    def __init__(
        self,
        *,
        lifecycle_id: str = "worker-run-1",
        retain_id: str = "retain-1",
    ) -> None:
        self.lifecycle_id = lifecycle_id
        self.retain_id = retain_id
        self.requests: list[tuple[str, str | None]] = []
        self.released: list[WorkerTicket] = []
        self.bound: list[tuple[WorkerTicket, AttemptFence]] = []
        self.unbound: list[tuple[WorkerTicket, AttemptFence]] = []

    def acquire(self, worker_id: str, *, expected_release_id: str | None = None):
        self.requests.append((worker_id, expected_release_id))
        return WorkerTicket(
            worker_id=worker_id,
            lifecycle_id=self.lifecycle_id,
            retain_id=self.retain_id,
            release_id=RELEASE,
            pin_epoch=1,
            retire_epoch=1,
        )

    def release(self, ticket: WorkerTicket) -> None:
        self.released.append(ticket)

    def bind_attempt(self, ticket: WorkerTicket, fence: AttemptFence) -> None:
        self.bound.append((ticket, fence))

    def interrupt_attempt(
        self, ticket: WorkerTicket, fence: AttemptFence, reason: str
    ) -> None:
        raise AssertionError((ticket, fence, reason))

    def unbind_attempt(self, ticket: WorkerTicket, fence: AttemptFence) -> None:
        self.unbound.append((ticket, fence))

    def send_worker_request(
        self,
        ticket: WorkerTicket,
        method: str,
        params,
        meta,
        *,
        request_id: str,
    ) -> str:
        raise AssertionError((ticket, method, params, meta, request_id))

    def take_worker_response(
        self, ticket: WorkerTicket, request_id: str
    ) -> RpcEvent | None:
        del ticket, request_id
        return None


class WorkerResponseSupervisor(RecordingSupervisor):
    def __init__(self, worker_run_id: str | None) -> None:
        super().__init__()
        self.response_worker_run_id = worker_run_id

    def take_worker_response(
        self, ticket: WorkerTicket, request_id: str
    ) -> RpcEvent | None:
        del ticket
        result = {
            "accepted": True,
            "provenance_receipt_id": "receipt-1",
            "output_streams": [],
        }
        if self.response_worker_run_id is not None:
            result["worker_run_id"] = self.response_worker_run_id
        return RpcEvent(
            "response",
            {"jsonrpc": "2.0", "id": request_id, "result": result},
        )


class InjectedBindFailure(RuntimeError):
    pass


class InjectedCompensationFailure(RuntimeError):
    pass


class FailingBindSupervisor(RecordingSupervisor):
    def __init__(
        self,
        repository: CoreAuthorityRepository,
        *,
        interruption_error: BaseException | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.interruption_error = interruption_error
        self.events: list[tuple[str, str, int, str | None]] = []

    def bind_attempt(self, ticket: WorkerTicket, fence: AttemptFence) -> None:
        self.events.append(("bind", fence.attempt_id, fence.lease_epoch, None))
        raise InjectedBindFailure("injected P2 bind failure")

    def interrupt_attempt(
        self, ticket: WorkerTicket, fence: AttemptFence, reason: str
    ) -> None:
        if self.interruption_error is not None:
            raise self.interruption_error
        with self.repository.transaction() as connection:
            interrupted = connection.execute(
                "UPDATE execution_attempt SET state='interrupted',revision=revision+1 "
                "WHERE job_id=? AND step_id=? AND attempt_id=? AND lease_epoch=? "
                "AND worker_run_id=? AND state='running'",
                (
                    fence.job_id,
                    fence.step_id,
                    fence.attempt_id,
                    fence.lease_epoch,
                    ticket.lifecycle_id,
                ),
            )
            if interrupted.rowcount != 1:
                raise AssertionError(
                    "test authority did not interrupt the exact Attempt"
                )
            step = connection.execute(
                "UPDATE execution_step SET state='interrupted',revision=revision+1 "
                "WHERE job_id=? AND step_id=? AND active_attempt_id=?",
                (fence.job_id, fence.step_id, fence.attempt_id),
            )
            if step.rowcount != 1:
                raise AssertionError("test authority did not fence the exact Step")
        self.events.append(("interrupt", fence.attempt_id, fence.lease_epoch, reason))

    def release(self, ticket: WorkerTicket) -> None:
        with self.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT attempt_id,lease_epoch,state FROM execution_attempt "
                "WHERE worker_run_id=? ORDER BY lease_epoch DESC LIMIT 1",
                (ticket.lifecycle_id,),
            ).fetchone()
        assert row is not None
        self.events.append(
            ("release", str(row["attempt_id"]), int(row["lease_epoch"]), row["state"])
        )
        super().release(ticket)


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


@pytest.mark.parametrize(
    ("response_worker_run_id", "accepted"),
    [(None, False), ("worker-derived", False), ("worker-run-1", True)],
)
def test_worker_start_response_requires_authoritative_p2_lifecycle_identity(
    tmp_path, response_worker_run_id, accepted
):
    _, _, repository, authority, snapshot = _authority(tmp_path)
    supervisor = WorkerResponseSupervisor(response_worker_run_id)
    runtime = ChapterJobRuntime(
        authority,
        job_control=JobControl(supervisor),
        checkpoints=DurableCheckpointAdapter(authority),
        attempt_lifecycle=supervisor,
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
    with repository.read_connection() as connection:
        before = tuple(connection.iterdump())

    if accepted:
        event = runtime.take_worker_response(started, "request-1")
        assert event is not None
        assert event.message["result"]["worker_run_id"] == "worker-run-1"
    else:
        with pytest.raises(ContractError) as exc_info:
            runtime.take_worker_response(started, "request-1")
        assert exc_info.value.code == int(ErrorCode.STALE_LEASE)

    with repository.read_connection() as connection:
        after = tuple(connection.iterdump())
    assert after == before
    runtime.release_worker(started)
    repository.close()


def test_bind_failure_interrupts_before_release_and_retry_advances_epoch(tmp_path):
    database, asset_root, repository, authority, snapshot = _authority(tmp_path)
    supervisor = FailingBindSupervisor(repository)
    runtime = ChapterJobRuntime(
        authority,
        job_control=JobControl(supervisor),
        checkpoints=DurableCheckpointAdapter(authority),
        attempt_lifecycle=supervisor,
    )
    worker = RunSnapshotWorker(
        run_snapshot_id=snapshot["snapshot_id"],
        worker_id="worker-1",
        plugin_id="com.plotpilot.demo",
        generation_id="generation-1",
        release_id=RELEASE,
    )

    with pytest.raises(InjectedBindFailure, match="injected P2 bind failure"):
        runtime.start_attempt(
            run_snapshot_worker=worker,
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            package_hash=PACKAGE,
            capability_id="writing.chapter.draft/v1",
            preallocated_receipt_id="receipt-1",
            expected_result_contract="candidate-batch/v1",
        )

    assert supervisor.events == [
        ("bind", "attempt-1", 1, None),
        (
            "interrupt",
            "attempt-1",
            1,
            "P2 bind_attempt failed after P1 start_attempt committed",
        ),
        ("release", "attempt-1", 1, "interrupted"),
    ]
    assert len(supervisor.released) == 1
    with repository.read_connection() as connection:
        failed = connection.execute(
            "SELECT state,lease_epoch,worker_run_id FROM execution_attempt "
            "WHERE attempt_id='attempt-1'"
        ).fetchone()
    assert failed is not None
    assert tuple(failed) == ("interrupted", 1, "worker-run-1")
    repository.close()

    reopened = CoreAuthorityRepository(database)
    retry_supervisor = RecordingSupervisor(
        lifecycle_id="worker-run-2", retain_id="retain-2"
    )
    retry_authority = ExecutionAuthority(reopened, AssetStore(asset_root))
    retry_runtime = ChapterJobRuntime(
        retry_authority,
        job_control=JobControl(retry_supervisor),
        checkpoints=DurableCheckpointAdapter(retry_authority),
        attempt_lifecycle=retry_supervisor,
    )
    try:
        retried = retry_runtime.start_attempt(
            run_snapshot_worker=worker,
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-2",
            package_hash=PACKAGE,
            capability_id="writing.chapter.draft/v1",
            preallocated_receipt_id="receipt-2",
            expected_result_contract="candidate-batch/v1",
        )

        assert retried.binding.lease_epoch == 2
        assert retried.binding.worker_run_id == "worker-run-2"
        with reopened.read_connection() as connection:
            attempts = connection.execute(
                "SELECT attempt_id,state,lease_epoch FROM execution_attempt "
                "WHERE step_id='step-1' ORDER BY lease_epoch"
            ).fetchall()
        assert [tuple(row) for row in attempts] == [
            ("attempt-1", "interrupted", 1),
            ("attempt-2", "running", 2),
        ]
        retry_runtime.release_worker(retried)
        assert retry_supervisor.unbound == [(retried.ticket, retried.fence)]
        assert retry_supervisor.released == [retried.ticket]
    finally:
        reopened.close()


def test_bind_failure_does_not_release_retain_when_compensation_raises(tmp_path):
    _, _, repository, authority, snapshot = _authority(tmp_path)
    compensation_error = InjectedCompensationFailure(
        "injected supervisor compensation failure"
    )
    supervisor = FailingBindSupervisor(
        repository, interruption_error=compensation_error
    )
    runtime = ChapterJobRuntime(
        authority,
        job_control=JobControl(supervisor),
        checkpoints=DurableCheckpointAdapter(authority),
        attempt_lifecycle=supervisor,
    )
    worker = RunSnapshotWorker(
        run_snapshot_id=snapshot["snapshot_id"],
        worker_id="worker-1",
        plugin_id="com.plotpilot.demo",
        generation_id="generation-1",
        release_id=RELEASE,
    )

    try:
        with pytest.raises(InjectedCompensationFailure) as caught:
            runtime.start_attempt(
                run_snapshot_worker=worker,
                job_id="job-1",
                step_id="step-1",
                attempt_id="attempt-1",
                package_hash=PACKAGE,
                capability_id="writing.chapter.draft/v1",
                preallocated_receipt_id="receipt-1",
                expected_result_contract="candidate-batch/v1",
            )

        assert caught.value is compensation_error
        assert isinstance(caught.value.__cause__, InjectedBindFailure)
        assert supervisor.released == []
        with repository.read_connection() as connection:
            state = connection.execute(
                "SELECT state FROM execution_attempt WHERE attempt_id='attempt-1'"
            ).fetchone()[0]
        assert state == "running"
    finally:
        repository.close()


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
