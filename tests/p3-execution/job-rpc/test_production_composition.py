from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.jobs.http_rpc.composition import (
    JobRuntimeComposition,
    compose_job_runtime,
)
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_core.supervisor.models import WorkerTicket
from backend.plotpilot_core.supervisor.rpc import RpcEvent
from backend.plotpilot_plugin_sdk.rpc import build_meta, build_request
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

RELEASE = "e" * 64
PACKAGE = "a" * 64


@dataclass
class SupervisorStub:
    responses: list[tuple[str, object]] = field(default_factory=list)

    def acquire(self, worker_id, *, expected_release_id=None):
        return WorkerTicket(
            worker_id, "lifecycle-1", "retain-1", expected_release_id, 1, 1
        )

    def release(self, ticket):
        del ticket

    def _peek_host_events(self, ticket):
        del ticket
        return ()

    def _dispose_host_event(self, ticket, request_id):
        del ticket, request_id

    def bind_attempt(self, ticket, fence):
        del ticket, fence

    def unbind_attempt(self, ticket, fence):
        del ticket, fence

    def send_worker_request(self, ticket, method, params, meta, *, request_id):
        del ticket, method, params, meta
        return request_id

    def take_worker_response(self, ticket, request_id):
        del ticket, request_id

    def respond_host_request(self, ticket, request_id, result):
        del ticket
        self.responses.append((request_id, result))

    def respond_host_error(self, ticket, request_id, **error):
        del ticket
        self.responses.append((request_id, error))


def _never_called(*_args, **_kwargs):
    raise AssertionError("resolver should not run during composition")


def test_one_production_composition_reuses_every_authority(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    authority = ExecutionAuthority(repository, assets)
    supervisor = SupervisorStub()
    try:
        composition = compose_job_runtime(
            authority,
            supervisor,
            start_resolver=_never_called,
            control_resolver=_never_called,
            provenance_receipt_resolver=_never_called,
        )

        assert isinstance(composition, JobRuntimeComposition)
        assert composition.authority is authority
        assert composition.repository is repository
        assert composition.assets is assets
        assert composition.supervisor is supervisor
        assert composition.checkpoints.authority is authority
        assert composition.snapshots.repository is repository
        assert (
            composition.snapshots.runtime_projection_reader is composition.checkpoints
        )
        assert composition.chapter.authority is authority
        assert composition.chapter.job_control is composition.job_control
        assert composition.chapter.attempt_lifecycle is supervisor
        assert composition.backup.repository is repository
        assert composition.dispatcher._supervisor is supervisor
        assert composition.dispatcher._event_disposition is composition.job_control
        assert set(composition.handlers) == {
            "host.checkpoint.commit/v1",
            "host.stream.commit/v1",
            "host.job.event/v1",
            "host.job.await_user/v1",
            "host.job.complete/v1",
        }
    finally:
        repository.close()


def test_composition_has_no_implicit_resolver_or_fake_authority(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    authority = ExecutionAuthority(repository, assets)
    try:
        with pytest.raises(TypeError):
            compose_job_runtime(authority, SupervisorStub())
    finally:
        repository.close()


def test_composed_dispatcher_resolves_attempt_from_durable_authority(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    authority = ExecutionAuthority(repository, assets)
    supervisor = SupervisorStub()
    try:
        repository.create_workspace(Workspace("ws-1", "Novel"))
        snapshot = json.loads(
            Path("contracts/golden/run-snapshot/snapshot.json").read_text(
                encoding="utf-8"
            )
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
        authority.start_attempt(
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            worker_run_id="lifecycle-1",
            plugin_id="com.plotpilot.demo",
            release_id=RELEASE,
            package_hash=PACKAGE,
            capability_id="writing.chapter.draft/v1",
            generation_id="generation-1",
            lease_epoch=1,
            preallocated_receipt_id="receipt-1",
            expected_result_contract="candidate-batch/v1",
        )
        composition = compose_job_runtime(
            authority,
            supervisor,
            start_resolver=_never_called,
            control_resolver=_never_called,
            provenance_receipt_resolver=_never_called,
        )
        request = build_request(
            "host.job.event/v1",
            {
                "operation_key": "event-op-1",
                "event_type": "plugin.com.plotpilot.demo.progress",
                "payload_asset_id": None,
                "local_seq": 1,
            },
            build_meta(
                "attempt",
                generation_id="generation-1",
                plugin_release_id=RELEASE,
                deadline_at="2026-09-04T13:00:00Z",
                job_id="job-1",
                step_id="step-1",
                attempt_id="attempt-1",
                lease_epoch=1,
            ),
            request_id="00000000-0000-4000-8000-000000000001",
        )
        ticket = WorkerTicket("worker-1", "lifecycle-1", "retain-1", RELEASE, 1, 1)

        assert composition.dispatcher.dispatch_event(
            ticket, RpcEvent("host_request", request)
        )
        assert supervisor.responses == [
            (
                request["id"],
                {"accepted": True, "job_event_seq": 1},
            )
        ]
        window = composition.events.window("job-1", 0)
        assert [event["event_id"] for event in window.events]
        assert window.events[0]["attempt_id"] == "attempt-1"
    finally:
        repository.close()
