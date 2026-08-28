from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from threading import Barrier

import pytest

from backend.plotpilot_core.api.v1.jobs.rpc import (
    AttemptStartBinding,
    JobCheckpointBinding,
    JobCommandQueryAdapter,
    JobSnapshotExtensions,
    JobStreamHighWaterBinding,
)
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode
from backend.plotpilot_plugin_sdk.verifier import verify_job_snapshot


def test_create_is_request_key_suppressed_and_projects_the_existing_job(
    job_stack, snapshot_extensions
):
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
        "replayed": None,
    }
    assert replay.job_id == "job-1"
    assert replay.replayed is True
    with job_stack["repository"].read_connection() as connection:
        assert connection.execute("SELECT count(*) FROM execution_job").fetchone()[0] == 1


def test_snapshot_is_closed_hash_verified_and_reads_authority_state(
    job_stack, snapshot_extensions, attempt_starter
):
    adapter = JobCommandQueryAdapter(
        job_stack["authority"],
        snapshot_extensions=snapshot_extensions,
        attempt_starter=attempt_starter,
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
    job_stack, snapshot_extensions, attempt_starter
):
    adapter = JobCommandQueryAdapter(
        job_stack["authority"],
        snapshot_extensions=snapshot_extensions,
        attempt_starter=attempt_starter,
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


def test_invalid_command_identities_are_rejected_before_any_write(
    job_stack, snapshot_extensions
):
    calls = []

    class RecordingStarter:
        def start_attempt(self, **command):
            calls.append(command)
            raise AssertionError("invalid command reached the P1 port")

    adapter = JobCommandQueryAdapter(
        job_stack["authority"],
        snapshot_extensions=snapshot_extensions,
        attempt_starter=RecordingStarter(),
    )
    with pytest.raises(ContractError):
        adapter.create_or_get(job_id="", run_snapshot=job_stack["snapshot"])
    with job_stack["repository"].read_connection() as connection:
        assert connection.execute("SELECT count(*) FROM execution_job").fetchone()[0] == 0

    adapter.create_or_get(job_id="job-1", run_snapshot=job_stack["snapshot"])
    with pytest.raises(ContractError):
        adapter.freeze_plan(
            job_id="job-1",
            steps=[{"step_id": "", "depends_on": [], "result_contract": "candidate-batch/v1"}],
            output_step_id="step-1",
        )
    for invalid in ({"lease_epoch": 0}, {"lease_epoch": True}, {"secrets": {"token": "x"}}):
        command = {
            "job_id": "job-1",
            "step_id": "step-1",
            "attempt_id": "attempt-1",
            "worker_run_id": "worker-run-1",
            "plugin_id": "com.plotpilot.alpha",
            "release_id": "rel-a",
            "package_hash": "4" * 64,
            "capability_id": "writing.chapter.draft/v1",
            "generation_id": "dg-a",
            **invalid,
        }
        with pytest.raises(ContractError):
            adapter.start_attempt(**command)
    assert calls == []
    with job_stack["repository"].read_connection() as connection:
        assert connection.execute("SELECT count(*) FROM execution_step").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM execution_attempt").fetchone()[0] == 0


def test_attempt_start_uses_the_port_allocated_binding_and_stops_without_it(
    job_stack, snapshot_extensions
):
    command = {
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "worker_run_id": "worker-run-1",
        "plugin_id": "com.plotpilot.alpha",
        "release_id": "rel-a",
        "package_hash": "4" * 64,
        "capability_id": "writing.chapter.draft/v1",
        "generation_id": "dg-a",
        "lease_epoch": 1,
        "preallocated_receipt_id": "receipt-1",
        "expected_result_contract": "candidate-batch/v1",
    }
    allocated = AttemptStartBinding(
        **{key: value for key, value in command.items() if key != "lease_epoch"},
        lease_epoch=2,
    )

    class AllocatingPort:
        def __init__(self):
            self.calls = []

        def start_attempt(self, **value):
            self.calls.append(value)
            return allocated

    port = AllocatingPort()
    adapter = JobCommandQueryAdapter(
        job_stack["authority"],
        snapshot_extensions=snapshot_extensions,
        attempt_starter=port,
    )
    assert adapter.start_attempt(**command) == allocated
    assert port.calls == [command]

    stopped = JobCommandQueryAdapter(
        job_stack["authority"], snapshot_extensions=snapshot_extensions
    )
    with pytest.raises(ContractError, match="AttemptStartBinding"):
        stopped.start_attempt(**command)


def test_concurrent_same_key_never_claims_a_non_authoritative_first_creation(
    job_stack, snapshot_extensions
):
    barrier = Barrier(2)
    winner = {
        "job_id": "job-race",
        "workspace_id": "ws-1",
        "job_state": "queued",
        "job_revision": 1,
    }

    class RepositorySurface:
        @contextmanager
        def read_connection(self):
            yield None

    class ConcurrentAuthority:
        repository = RepositorySurface()

        def find_by_request_key(self, workspace_id, request_key):
            del workspace_id, request_key
            barrier.wait(timeout=5)
            return None

        def create_from_verified_snapshot(self, job_id, snapshot):
            del job_id, snapshot
            return winner

    adapter = JobCommandQueryAdapter(
        ConcurrentAuthority(),  # type: ignore[arg-type]
        snapshot_extensions=snapshot_extensions,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda job_id: adapter.create_or_get(
                    job_id=job_id, run_snapshot=job_stack["snapshot"]
                ),
                ("job-race", "job-other"),
            )
        )
    assert [result.replayed for result in results] == [None, True]


def _stream(step_id="step-1", workspace_id="ws-1", stream_id="stream-1"):
    return {
        "stream_id": stream_id,
        "step_id": step_id,
        "output_role": "draft",
        "target": {
            "workspace_id": workspace_id,
            "entity_kind": "document",
            "entity_id": "doc-a",
        },
        "acked_prefix_seq": 1,
        "acked_bytes": 10,
        "acked_prefix_hash": "a" * 64,
    }


@pytest.mark.parametrize(
    "extensions",
    [
        JobSnapshotExtensions(
            JobCheckpointBinding("ws-2", "job-1", "step-1", "checkpoint-1", {}), ()
        ),
        JobSnapshotExtensions(
            JobCheckpointBinding(
                "ws-1", "job-other", "step-1", "checkpoint-1", {}
            ),
            (),
        ),
        JobSnapshotExtensions(
            None,
            (
                JobStreamHighWaterBinding(
                    "ws-1", "job-1", "step-other", _stream(step_id="step-other")
                ),
            ),
        ),
        JobSnapshotExtensions(
            None,
            (
                JobStreamHighWaterBinding(
                    "ws-1", "job-1", "step-1", _stream(workspace_id="ws-2")
                ),
            ),
        ),
    ],
)
def test_snapshot_extensions_reject_cross_scope_bindings(job_stack, extensions):
    class StaticReader:
        def read(self, connection, *, job_id, workspace_id):
            del connection, job_id, workspace_id
            return extensions

    adapter = JobCommandQueryAdapter(
        job_stack["authority"], snapshot_extensions=StaticReader()
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
    with pytest.raises(ContractError, match="outside the Job scope"):
        adapter.get_snapshot(workspace_id="ws-1", job_id="job-1")


def test_snapshot_extensions_require_unique_canonical_stream_order(job_stack):
    duplicate = JobStreamHighWaterBinding("ws-1", "job-1", "step-1", _stream())

    class DuplicateReader:
        def read(self, connection, *, job_id, workspace_id):
            del connection, job_id, workspace_id
            return JobSnapshotExtensions(None, (duplicate, duplicate))

    adapter = JobCommandQueryAdapter(
        job_stack["authority"], snapshot_extensions=DuplicateReader()
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
    with pytest.raises(ContractError, match="duplicate stream_id"):
        adapter.get_snapshot(workspace_id="ws-1", job_id="job-1")


def test_typed_stream_extension_projects_only_its_bound_job_scope(job_stack):
    high_water = _stream()

    class BoundReader:
        def read(self, connection, *, job_id, workspace_id):
            del connection
            return JobSnapshotExtensions(
                None,
                (
                    JobStreamHighWaterBinding(
                        workspace_id, job_id, "step-1", high_water
                    ),
                ),
            )

    adapter = JobCommandQueryAdapter(
        job_stack["authority"], snapshot_extensions=BoundReader()
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
    snapshot = adapter.get_snapshot(workspace_id="ws-1", job_id="job-1")
    assert snapshot["stream_high_waters"] == [high_water]
    verify_job_snapshot(snapshot)


def test_same_scope_checkpoint_must_pass_contract_and_attempt_binding(
    job_stack, attempt_starter
):
    class InvalidCheckpointReader:
        def read(self, connection, *, job_id, workspace_id):
            del connection, job_id, workspace_id
            return JobSnapshotExtensions(
                JobCheckpointBinding(
                    "ws-1", "job-1", "step-1", "checkpoint-1", {}
                ),
                (),
            )

    adapter = JobCommandQueryAdapter(
        job_stack["authority"],
        snapshot_extensions=InvalidCheckpointReader(),
        attempt_starter=attempt_starter,
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
        expected_result_contract="candidate-batch/v1",
    )
    with pytest.raises(ContractError):
        adapter.get_snapshot(workspace_id="ws-1", job_id="job-1")
