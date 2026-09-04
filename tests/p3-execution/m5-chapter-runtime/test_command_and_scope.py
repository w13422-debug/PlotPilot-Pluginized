from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path

import pytest

from backend.plotpilot_core.api.v2.jobs.rpc.command_query import (
    JobCommandQueryAdapter,
    JobCommandResolution,
    JobHttpApplicationError,
)
from backend.plotpilot_core.api.v2.jobs.sse.adapter import JobSSEAdapter
from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.broker.service import CallerAttemptContext
from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.events import (
    CoreEventStore,
    EventRecoveryService,
    JobEventStore,
    JobSnapshotStore,
)
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ErrorCode,
    canonical_bytes,
    verify_snapshot,
)
from backend.plotpilot_plugin_sdk.verifier import (
    hash_without_field,
    request_key,
    snapshot_hash,
)

RELEASE = "e" * 64
PACKAGE = "a" * 64
CAPABILITY = "writing.chapter.draft/v1"
GENERATION = "generation-1"


class CatalogAuthority:
    """Test P1 catalog port; it contains identities, never Job state."""

    def __init__(self, execution_authority: ExecutionAuthority) -> None:
        self.execution_authority = execution_authority
        self.repository = execution_authority.repository
        self._ids: dict[str, list[str]] = {}

    def add(self, workspace_id: str, job_id: str) -> None:
        values = self._ids.setdefault(workspace_id, [])
        if job_id not in values:
            values.append(job_id)

    def list_job_ids(self, *, workspace_id: str):
        return tuple(self._ids.get(workspace_id, ()))


class StartResolver:
    def __init__(
        self,
        authority: ExecutionAuthority,
        catalog: CatalogAuthority,
        assets: AssetStore,
        snapshots: JobSnapshotStore,
    ) -> None:
        self.authority = authority
        self.repository = authority.repository
        self.catalog = catalog
        self.assets = assets
        self.snapshots = snapshots
        self.calls: list[dict[str, object]] = []

    def resolve_start(self, command):
        command = dict(command)
        self.calls.append(command)
        value = json.loads(
            self.assets.read(str(command["run_snapshot_asset_id"])).decode("utf-8")
        )
        verify_snapshot(value)
        if value["workspace_id"] != command["workspace_id"]:
            raise JobHttpApplicationError(
                409, "invalid_transition", "RunSnapshot crosses Workspace"
            )
        if value["scope"]["operation"] != command["capability_id"]:
            raise JobHttpApplicationError(
                409, "invalid_transition", "capability is not resolved by RunSnapshot"
            )
        created = self.authority.create_from_verified_snapshot(
            str(command["job_id"]), value
        )
        if created["job_id"] != command["job_id"]:
            raise JobHttpApplicationError(
                409, "duplicate_operation", "RunSnapshot is bound to another Job"
            )
        self.authority.freeze_plan(
            str(command["job_id"]),
            [
                {
                    "step_id": "step-1",
                    "depends_on": [],
                    "result_contract": "candidate-batch/v1",
                }
            ],
            output_step_id="step-1",
        )
        self.authority.start_attempt(
            job_id=str(command["job_id"]),
            step_id="step-1",
            attempt_id="attempt-1",
            worker_run_id="worker-run-1",
            plugin_id="com.plotpilot.demo",
            release_id=RELEASE,
            package_hash=PACKAGE,
            capability_id=str(command["capability_id"]),
            generation_id=GENERATION,
            lease_epoch=int(command["writer_epoch"]),
            preallocated_receipt_id="receipt-1",
            expected_result_contract="candidate-batch/v1",
        )
        self.catalog.add(str(command["workspace_id"]), str(command["job_id"]))
        current = self.snapshots.capture_v2(
            str(command["job_id"]), workspace_id=str(command["workspace_id"])
        ).value
        return JobCommandResolution(
            operation_key=str(command["operation_key"]),
            workspace_id=str(command["workspace_id"]),
            job_id=str(command["job_id"]),
            command="start",
            accepted=True,
            idempotent=False,
            terminal_known=False,
            state=str(current["state"]),
            job_revision=int(current["job_revision"]),
            snapshot_cursor=(
                f"job/{command['job_id']}/{current['job_event_high_water']}"
            ),
        )


class ControlResolver:
    def __init__(
        self,
        authority: ExecutionAuthority,
        snapshots: JobSnapshotStore,
    ) -> None:
        self.authority = authority
        self.repository = authority.repository
        self.snapshots = snapshots
        self.calls: list[dict[str, object]] = []
        self.checkpoint_asset_id: str | None = None
        self.active_attempt_id = "attempt-1"
        self.active_worker_run_id = "worker-run-1"
        self.active_epoch = 1
        self.resume_overrides: dict[str, str] = {}
        self.last_resume_binding: dict[str, object] | None = None

    def resolve_control(self, command):
        command = dict(command)
        self.calls.append(command)
        before = self.snapshots.capture_v2(
            str(command["job_id"]), workspace_id=str(command["workspace_id"])
        ).value
        if int(command["expected_job_revision"]) != int(before["job_revision"]):
            raise JobHttpApplicationError(
                409, "stale_revision", "expected Job revision is stale"
            )

        operation = str(command["command"])
        if operation == "pause":
            decision = self.authority.control_port.pause(
                job_id=str(command["job_id"]),
                step_id="step-1",
                attempt_id=self.active_attempt_id,
                lease_epoch=self.active_epoch,
                operation_key=str(command["operation_key"]),
                worker_run_id=self.active_worker_run_id,
                reason=str(command["reason"]),
                checkpoint_asset_id=self.checkpoint_asset_id,
            )
        elif operation == "resume":
            resume_intent = command["resume_intent_id"] or "anonymous"
            binding = {
                "job_id": str(command["job_id"]),
                "step_id": "step-1",
                "operation_key": str(command["operation_key"]),
                "resume_of_attempt_id": self.active_attempt_id,
                "worker_run_id": f"worker-{resume_intent}",
                "new_attempt_id": f"attempt-{resume_intent}",
                "checkpoint_asset_id": self.checkpoint_asset_id,
                "plugin_id": self.resume_overrides.get(
                    "plugin_id", "com.plotpilot.demo"
                ),
                "release_id": self.resume_overrides.get("release_id", RELEASE),
                "package_hash": self.resume_overrides.get("package_hash", PACKAGE),
                "capability_id": self.resume_overrides.get("capability_id", CAPABILITY),
                "generation_id": self.resume_overrides.get("generation_id", GENERATION),
                "preallocated_receipt_id": f"receipt-{resume_intent}",
                "resume_reason": str(command["reason"]),
            }
            self.last_resume_binding = binding
            decision = self.authority.control_port.resume(**binding)
            if not decision.replayed:
                self.active_attempt_id = str(binding["new_attempt_id"])
                self.active_worker_run_id = str(binding["worker_run_id"])
                self.active_epoch += 1
        elif operation == "cancel":
            decision = self.authority.control_port.cancel(
                job_id=str(command["job_id"]),
                step_id="step-1",
                attempt_id=self.active_attempt_id,
                lease_epoch=self.active_epoch,
                operation_key=str(command["operation_key"]),
                worker_run_id=self.active_worker_run_id,
                reason=str(command["reason"]),
            )
        else:  # pragma: no cover - schema validation owns this branch
            raise AssertionError(operation)

        current = self.snapshots.capture_v2(
            str(command["job_id"]), workspace_id=str(command["workspace_id"])
        ).value
        terminal_known = bool(decision.result.get("terminal_known", False))
        return JobCommandResolution(
            operation_key=str(command["operation_key"]),
            workspace_id=str(command["workspace_id"]),
            job_id=str(command["job_id"]),
            command=operation,
            accepted=decision.accepted,
            idempotent=decision.replayed,
            terminal_known=terminal_known,
            state=str(current["state"]),
            job_revision=int(current["job_revision"]),
            snapshot_cursor=(
                f"job/{command['job_id']}/{current['job_event_high_water']}"
            ),
        )


@pytest.fixture
def stack(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    repository.create_workspace(Workspace("ws-1", "Novel"))
    snapshot = json.loads(
        Path("contracts/golden/run-snapshot/snapshot.json").read_text(encoding="utf-8")
    )
    snapshot["plugin_releases"] = [
        {
            "plugin_id": "com.plotpilot.demo",
            "release_id": RELEASE,
            "package_hash": PACKAGE,
            "data_generation_id": GENERATION,
        }
    ]
    snapshot["scope"]["operation"] = CAPABILITY
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    run_snapshot_asset = assets.put(
        canonical_bytes(snapshot),
        mime="application/json",
        logical_role="run-snapshot",
        provenance="test:p3-v2-command-query",
    )
    execution = ExecutionAuthority(repository, assets)
    catalog = CatalogAuthority(execution)
    snapshots = JobSnapshotStore(repository, assets, execution.snapshot_extensions)
    events = JobEventStore(repository)
    sse = JobSSEAdapter(
        EventRecoveryService(CoreEventStore(repository), events), snapshots
    )
    start_resolver = StartResolver(execution, catalog, assets, snapshots)
    control_resolver = ControlResolver(execution, snapshots)
    adapter = JobCommandQueryAdapter(
        catalog,
        snapshots=snapshots,
        events=events,
        sse=sse,
        start_resolver=start_resolver,
        control_resolver=control_resolver,
    )
    yield {
        "repository": repository,
        "assets": assets,
        "snapshot": snapshot,
        "run_snapshot_asset": run_snapshot_asset,
        "execution": execution,
        "catalog": catalog,
        "snapshots": snapshots,
        "events": events,
        "sse": sse,
        "start_resolver": start_resolver,
        "control_resolver": control_resolver,
        "adapter": adapter,
    }
    repository.close()


def _start_command(stack, **overrides):
    return {
        "schema": "job-start-command/v2",
        "operation_key": "start-op-1",
        "workspace_id": "ws-1",
        "job_id": "job-1",
        "capability_id": CAPABILITY,
        "run_snapshot_asset_id": stack["run_snapshot_asset"].asset_id,
        "writer_epoch": 1,
        **overrides,
    }


def _control(command: str, revision: int, **overrides):
    return {
        "schema": "job-control-command/v2",
        "operation_key": f"{command}-op-1",
        "workspace_id": "ws-1",
        "job_id": "job-1",
        "expected_job_revision": revision,
        "reason": f"operator requested {command}",
        "command": command,
        "resume_intent_id": "resume-1" if command == "resume" else None,
        **overrides,
    }


def _start(stack):
    status, result = stack["adapter"].handle(
        "job.start",
        _start_command(stack),
        path_identity={"workspace_id": "ws-1"},
    )
    assert status == 201
    assert result["state"] == "running"
    assert result["job_revision"] == 2
    return result


def _checkpoint_asset(stack):
    value = {
        "schema": "checkpoint/v1",
        "checkpoint_id": "checkpoint-1",
        "checkpoint_seq": 1,
        "job_id": "job-1",
        "step_id": "step-1",
        "source_attempt_id": "attempt-1",
        "lease_epoch": 1,
        "run_snapshot_hash": stack["snapshot"]["snapshot_hash"],
        "replay_policy": "checkpoint_resume",
        "completed_units": 1,
        "total_units": 2,
        "unit_set_hash": "b" * 64,
        "state_asset_id": None,
        "created_at": "2026-09-04T00:00:00Z",
    }
    value["checkpoint_hash"] = hash_without_field(
        value, "checkpoint_hash", "checkpoint/v1"
    )
    asset = stack["assets"].put(
        canonical_bytes(value),
        mime="application/json",
        logical_role="checkpoint",
        provenance="test:p3-v2-command-query",
    )
    return value, asset


def test_all_eight_routes_use_frozen_v2_contracts_and_one_authority(stack):
    started = _start(stack)
    assert started == {
        "schema": "job-command-result/v2",
        "operation_key": "start-op-1",
        "workspace_id": "ws-1",
        "job_id": "job-1",
        "command": "start",
        "accepted": True,
        "idempotent": False,
        "terminal_known": False,
        "state": "running",
        "job_revision": 2,
        "snapshot_cursor": "job/job-1/0",
    }

    status, listing = stack["adapter"].handle(
        "job.list",
        {
            "schema": "job-list-query/v2",
            "workspace_id": "ws-1",
            "cursor": None,
            "limit": 50,
            "state": None,
        },
        path_identity={"workspace_id": "ws-1"},
    )
    assert status == 200
    assert listing["total"] == 1
    assert [item["job_id"] for item in listing["items"]] == ["job-1"]
    assert listing["next_cursor"] == "job/job-1/0"

    status, snapshot = stack["adapter"].handle(
        "job.get",
        {
            "schema": "job-snapshot-query/v2",
            "workspace_id": "ws-1",
            "job_id": "job-1",
        },
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert status == 200
    assert snapshot["snapshot"]["current_attempt_id"] == "attempt-1"
    assert snapshot["cursor"] == "job/job-1/0"

    event = stack["events"].append(
        {
            "schema": "plugin-job-event/v1",
            "event_id": "event-1",
            "job_id": "job-1",
            "step_id": "step-1",
            "attempt_id": "attempt-1",
            "event_type": "plugin.com.plotpilot.demo.progress",
            "plugin_id": "com.plotpilot.demo",
            "release_id": RELEASE,
            "local_seq": 1,
            "payload_asset_id": None,
            "payload_hash": None,
            "occurred_at": "2026-09-04T00:00:01Z",
        }
    )
    assert event["job_event_seq"] == 1

    status, page = stack["adapter"].handle(
        "job.events",
        {
            "schema": "job-event-page-query/v2",
            "workspace_id": "ws-1",
            "job_id": "job-1",
            "after_cursor": "job/job-1/0",
            "after_job_event_seq": 0,
            "limit": 50,
        },
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert status == 200
    assert page["next_cursor"] == "job/job-1/1"
    assert page["high_water_seq"] == 1
    assert [item["event_id"] for item in page["events"]] == ["event-1"]

    status, recovery = stack["adapter"].handle(
        "job.sse-recovery",
        {
            "schema": "job-sse-recovery-query/v2",
            "workspace_id": "ws-1",
            "job_id": "job-1",
            "after_seq": 0,
            "last_event_id": "job/job-1/0",
            "requested_cursor_domain": "job",
        },
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert status == 200
    assert recovery["gap"] is False
    assert recovery["snapshot"] is None
    assert [item["event_id"] for item in recovery["tail"]] == ["event-1"]

    _value, checkpoint = _checkpoint_asset(stack)
    stack["control_resolver"].checkpoint_asset_id = checkpoint.asset_id
    status, paused = stack["adapter"].handle(
        "job.pause",
        _control("pause", 2),
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert status == 200 and paused["state"] == "paused"
    status, resumed = stack["adapter"].handle(
        "job.resume",
        _control("resume", 3),
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert status == 200 and resumed["state"] == "running"
    status, cancelled = stack["adapter"].handle(
        "job.cancel",
        _control("cancel", 4),
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert status == 200 and cancelled["state"] == "cancelling"


def test_path_body_and_cursor_mismatches_fail_before_resolvers(stack):
    start = _start_command(stack)
    status, error = stack["adapter"].handle(
        "job.start", start, path_identity={"workspace_id": "ws-other"}
    )
    assert (status, error["error_code"]) == (400, "malformed_request")
    assert stack["start_resolver"].calls == []

    status, error = stack["adapter"].handle(
        "job.list",
        {
            "schema": "job-list-query/v2",
            "workspace_id": "ws-1",
            "cursor": "core/0",
            "limit": 50,
            "state": None,
        },
        path_identity={"workspace_id": "ws-1"},
    )
    assert (status, error["error_code"], error["cursor_domain"]) == (
        400,
        "cursor_domain_mismatch",
        "job",
    )

    for route_id, request in (
        (
            "job.events",
            {
                "schema": "job-event-page-query/v2",
                "workspace_id": "ws-1",
                "job_id": "job-1",
                "after_cursor": "job/job-1/1",
                "after_job_event_seq": 0,
                "limit": 50,
            },
        ),
        (
            "job.sse-recovery",
            {
                "schema": "job-sse-recovery-query/v2",
                "workspace_id": "ws-1",
                "job_id": "job-1",
                "after_seq": 0,
                "last_event_id": "job/job-1/1",
                "requested_cursor_domain": "job",
            },
        ),
        (
            "job.pause",
            _control("resume", 1),
        ),
    ):
        status, error = stack["adapter"].handle(
            route_id,
            request,
            path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
        )
        assert (status, error["error_code"]) == (400, "malformed_request")
    assert stack["control_resolver"].calls == []


def test_unknown_and_cross_workspace_jobs_have_no_cross_scope_projection(stack):
    _start(stack)
    observed = []
    for workspace_id, job_id in (("ws-1", "missing"), ("ws-2", "job-1")):
        status, error = stack["adapter"].handle(
            "job.get",
            {
                "schema": "job-snapshot-query/v2",
                "workspace_id": workspace_id,
                "job_id": job_id,
            },
            path_identity={"workspace_id": workspace_id, "job_id": job_id},
        )
        observed.append((status, error["error_code"]))
    assert observed == [(404, "unknown_job"), (404, "unknown_job")]


def test_pause_without_durable_checkpoint_fails_and_keeps_running(stack):
    _start(stack)
    status, error = stack["adapter"].handle(
        "job.pause",
        _control("pause", 2),
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert (status, error["error_code"]) == (409, "invalid_transition")
    current = stack["snapshots"].capture_v2("job-1", workspace_id="ws-1").value
    assert (current["state"], current["job_revision"]) == ("running", 2)
    assert stack["execution"].checkpoint_store.get_latest("job-1") is None


def test_resume_allocates_fresh_attempt_and_reuses_exact_lineage(stack):
    _start(stack)
    checkpoint_value, checkpoint = _checkpoint_asset(stack)
    resolver = stack["control_resolver"]
    resolver.checkpoint_asset_id = checkpoint.asset_id
    pause_status, _ = stack["adapter"].handle(
        "job.pause",
        _control("pause", 2),
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert pause_status == 200

    status, result = stack["adapter"].handle(
        "job.resume",
        _control("resume", 3),
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert status == 200
    assert (result["state"], result["job_revision"]) == ("running", 4)
    snapshot = stack["snapshots"].capture_v2("job-1", workspace_id="ws-1").value
    assert snapshot["current_attempt_id"] == "attempt-resume-1"
    assert snapshot["writer_epoch"] == 2
    assert snapshot["checkpoint_id"] == "checkpoint-1"
    assert stack["execution"].checkpoint_store.get_latest("job-1") == checkpoint_value

    binding = resolver.last_resume_binding
    assert binding is not None
    assert binding["resume_of_attempt_id"] == "attempt-1"
    assert binding["release_id"] == RELEASE
    assert binding["generation_id"] == GENERATION
    assert binding["package_hash"] == PACKAGE
    # Existing Attempt replay validates the persisted release/generation/hash
    # closure without reading repository-private state from this adapter test.
    stack["execution"].start_attempt(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-resume-1",
        worker_run_id="worker-resume-1",
        plugin_id="com.plotpilot.demo",
        release_id=RELEASE,
        package_hash=PACKAGE,
        capability_id=CAPABILITY,
        generation_id=GENERATION,
        lease_epoch=2,
        preallocated_receipt_id="receipt-resume-1",
        expected_result_contract="candidate-batch/v1",
    )
    stack["execution"].validate_attempt(
        CallerAttemptContext(
            parent_job_id="job-1",
            parent_step_id="step-1",
            parent_attempt_id="attempt-resume-1",
            lease_epoch=2,
            plugin_release_id=RELEASE,
            generation_id=GENERATION,
        )
    )


@pytest.mark.parametrize(
    ("field", "drift"),
    [
        ("release_id", "d" * 64),
        ("generation_id", "generation-other"),
        ("package_hash", "c" * 64),
    ],
)
def test_resume_rejects_release_generation_or_package_drift(stack, field, drift):
    _start(stack)
    _value, checkpoint = _checkpoint_asset(stack)
    resolver = stack["control_resolver"]
    resolver.checkpoint_asset_id = checkpoint.asset_id
    status, _ = stack["adapter"].handle(
        "job.pause",
        _control("pause", 2),
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert status == 200
    resolver.resume_overrides[field] = drift

    status, error = stack["adapter"].handle(
        "job.resume",
        _control("resume", 3),
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert (status, error["error_code"]) == (409, "invalid_transition")
    current = stack["snapshots"].capture_v2("job-1", workspace_id="ws-1").value
    assert current["state"] == "paused"
    assert current["current_attempt_id"] == "attempt-1"


def test_stale_revision_and_duplicate_operation_map_to_frozen_errors(stack):
    _start(stack)
    status, error = stack["adapter"].handle(
        "job.cancel",
        _control("cancel", 1),
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert (status, error["error_code"]) == (409, "stale_revision")

    class DuplicateControlResolver:
        repository = stack["repository"]

        @staticmethod
        def resolve_control(_command):
            raise ContractError(ErrorCode.DUPLICATE_REQUEST, "operation key reused")

    adapter = JobCommandQueryAdapter(
        stack["catalog"],
        snapshots=stack["snapshots"],
        events=stack["events"],
        sse=stack["sse"],
        start_resolver=stack["start_resolver"],
        control_resolver=DuplicateControlResolver(),
    )
    status, error = adapter.handle(
        "job.cancel",
        _control("cancel", 2, operation_key="duplicate-op"),
        path_identity={"workspace_id": "ws-1", "job_id": "job-1"},
    )
    assert (status, error["error_code"], error["operation_key"]) == (
        409,
        "duplicate_operation",
        "duplicate-op",
    )


def test_list_filter_cursor_and_catalog_integrity_are_contract_bound(stack):
    _start(stack)
    second = copy.deepcopy(stack["snapshot"])
    second["snapshot_id"] = "snapshot-2"
    second["run_intent_id"] = "intent-2"
    second["request_key"] = request_key(second)
    second["snapshot_hash"] = snapshot_hash(second)
    stack["execution"].create_from_verified_snapshot("job-2", second)
    stack["catalog"].add("ws-1", "job-2")

    status, queued = stack["adapter"].handle(
        "job.list",
        {
            "schema": "job-list-query/v2",
            "workspace_id": "ws-1",
            "cursor": None,
            "limit": 50,
            "state": "queued",
        },
        path_identity={"workspace_id": "ws-1"},
    )
    assert status == 200
    assert queued["total"] == 1
    assert [item["job_id"] for item in queued["items"]] == ["job-2"]

    status, after_first = stack["adapter"].handle(
        "job.list",
        {
            "schema": "job-list-query/v2",
            "workspace_id": "ws-1",
            "cursor": "job/job-1/0",
            "limit": 1,
            "state": None,
        },
        path_identity={"workspace_id": "ws-1"},
    )
    assert status == 200
    assert after_first["total"] == 2
    assert [item["job_id"] for item in after_first["items"]] == ["job-2"]

    stack["catalog"]._ids["ws-1"].append("job-2")
    status, error = stack["adapter"].handle(
        "job.list",
        {
            "schema": "job-list-query/v2",
            "workspace_id": "ws-1",
            "cursor": None,
            "limit": 50,
            "state": None,
        },
        path_identity={"workspace_id": "ws-1"},
    )
    assert (status, error["error_code"]) == (400, "malformed_request")


def test_constructor_is_explicit_same_repository_and_has_no_fallback(stack, tmp_path):
    parameters = tuple(inspect.signature(JobCommandQueryAdapter.__init__).parameters)
    assert parameters == (
        "self",
        "authority",
        "snapshots",
        "events",
        "sse",
        "start_resolver",
        "control_resolver",
    )

    other_repository = CoreAuthorityRepository(tmp_path / "other.db")
    try:
        with pytest.raises(TypeError, match="one P1 repository"):
            JobCommandQueryAdapter(
                stack["catalog"],
                snapshots=stack["snapshots"],
                events=JobEventStore(other_repository),
                sse=stack["sse"],
                start_resolver=stack["start_resolver"],
                control_resolver=stack["control_resolver"],
            )
        with pytest.raises(TypeError, match="start_resolver"):
            JobCommandQueryAdapter(
                stack["catalog"],
                snapshots=stack["snapshots"],
                events=stack["events"],
                sse=stack["sse"],
                start_resolver=object(),
                control_resolver=stack["control_resolver"],
            )
        with pytest.raises(TypeError, match="list_job_ids"):
            JobCommandQueryAdapter(
                stack["execution"],  # type: ignore[arg-type]
                snapshots=stack["snapshots"],
                events=stack["events"],
                sse=stack["sse"],
                start_resolver=stack["start_resolver"],
                control_resolver=stack["control_resolver"],
            )
    finally:
        other_repository.close()

    source = Path("backend/plotpilot_core/api/v2/jobs/rpc/command_query.py").read_text(
        encoding="utf-8"
    )
    assert "connection.execute" not in source
    assert "OperationKeyLedgerV2" not in source
    assert "sqlite3" not in source
    assert "secrets" not in source
