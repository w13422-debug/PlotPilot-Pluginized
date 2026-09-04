from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.broker.service import (
    CapabilityBinding,
    CapabilityBroker,
)
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.jobs.http_rpc.composition import (
    JobRuntimeComposition,
    compose_job_runtime,
)
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_core.supervisor.models import WorkerTicket
from backend.plotpilot_core.supervisor.rpc import RpcEvent
from backend.plotpilot_plugin_sdk import canonical_bytes, sha256_hex
from backend.plotpilot_plugin_sdk.rpc import build_meta, build_request, encode_frame
from backend.plotpilot_plugin_sdk.verifier import (
    hash_without_field,
    request_key,
    snapshot_hash,
)

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

    def interrupt_attempt(self, ticket, fence, reason):
        del ticket, fence, reason

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


class SupervisorWithoutInterrupt(SupervisorStub):
    interrupt_attempt = None


def _never_called(*_args, **_kwargs):
    raise AssertionError("resolver should not run during composition")


class _StreamCommitPolicyResolver:
    def resolve_stream_commit_policy(self, _request, _stream_prefix, _attempt):
        raise AssertionError("stream policy resolver should not run during composition")


class _StartResolverWithStreamCommitPolicy(_StreamCommitPolicyResolver):
    def resolve_start(self, _command):
        raise AssertionError("start resolver should not run during composition")


class _ControlResolverWithStreamCommitPolicy(_StreamCommitPolicyResolver):
    def resolve_control(self, _command):
        raise AssertionError("control resolver should not run during composition")


class _ReceiptResolver:
    def __init__(self, receipt):
        self.receipt = receipt

    def resolve_provenance_receipt(self, _request):
        return self.receipt


class _BrokerRuntime:
    @staticmethod
    def resolve_release(_plugin_id, _requirement):
        return RELEASE

    @staticmethod
    def current_generation():
        return "generation-1"


class _BrokerExecution:
    def __init__(self, assets: AssetStore) -> None:
        self.poll_calls: list[tuple[str, int]] = []
        self.cancel_calls: list[tuple[str, str]] = []
        self.snapshot_asset_id = assets.create_asset(
            b"snapshot", mime="application/json"
        )
        self.events_asset_id = assets.create_asset(b"events", mime="application/json")

    def poll(self, child_job_id: str, after_job_event_seq: int):
        self.poll_calls.append((child_job_id, after_job_event_seq))
        return {
            "job_snapshot_asset_id": self.snapshot_asset_id,
            "job_event_page_asset_id": self.events_asset_id,
            "next_job_event_seq": max(1, after_job_event_seq),
            "terminal": False,
            "result_bundle_asset_id": None,
            "provenance_receipt_id": None,
            "child_state": "queued",
        }

    def cancel(self, operation_key: str, child_job_id: str):
        self.cancel_calls.append((operation_key, child_job_id))
        return {
            "accepted": True,
            "terminal_known": False,
            "child_state": "cancelling",
            "child_job_event_seq": 1,
        }


def _capability_broker(authority: ExecutionAuthority) -> CapabilityBroker:
    return CapabilityBroker(
        core=authority.assets,
        execution=_BrokerExecution(authority.assets),
        runtime=_BrokerRuntime(),
        bindings={
            "binding-1": CapabilityBinding(
                "binding-1",
                "writing.chapter.draft/v1",
                "com.plotpilot.demo",
                "1.0.0",
                "candidate-batch/v1",
                True,
                True,
            )
        },
        child_factory=authority,
        operation_ledger=authority.operation_ledger,
        child_records=authority.child_records,
        attempt_context=authority,
    )


def test_one_production_composition_reuses_every_authority(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    authority = ExecutionAuthority(repository, assets)
    supervisor = SupervisorStub()
    try:
        composition = compose_job_runtime(
            authority,
            supervisor,
            capability_broker=_capability_broker(authority),
            start_resolver=_never_called,
            control_resolver=_never_called,
            provenance_receipt_resolver=_never_called,
            stream_commit_policy_resolver=_StreamCommitPolicyResolver(),
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
        assert composition.capability_broker is composition.handlers._capability_broker
        assert composition.capability_broker.attempt_context is authority
        assert composition.backup.repository is repository
        assert composition.dispatcher._supervisor is supervisor
        assert composition.dispatcher._event_disposition is composition.job_control
        assert set(composition.handlers) == {
            "host.asset.read/v1",
            "host.asset.create/v1",
            "host.candidate.stage/v1",
            "host.capability.invoke/v1",
            "host.capability.poll/v1",
            "host.capability.cancel/v1",
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


def test_composition_rejects_supervisor_without_attempt_interrupt_port(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    authority = ExecutionAuthority(repository, assets)
    try:
        with pytest.raises(TypeError, match="accepted P2 Job ports"):
            compose_job_runtime(
                authority,
                SupervisorWithoutInterrupt(),
                capability_broker=_capability_broker(authority),
                start_resolver=_never_called,
                control_resolver=_never_called,
                provenance_receipt_resolver=_never_called,
                stream_commit_policy_resolver=_StreamCommitPolicyResolver(),
            )
    finally:
        repository.close()


def test_composition_rejects_absent_stream_commit_policy(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    authority = ExecutionAuthority(repository, assets)
    try:
        with pytest.raises(TypeError, match="stream_commit_policy_resolver"):
            compose_job_runtime(
                authority,
                SupervisorStub(),
                capability_broker=_capability_broker(authority),
                start_resolver=_never_called,
                control_resolver=_never_called,
                provenance_receipt_resolver=_never_called,
            )
    finally:
        repository.close()


def test_composition_selects_explicit_or_resolver_stream_policy(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    authority = ExecutionAuthority(repository, assets)
    explicit = _StreamCommitPolicyResolver()
    start = _StartResolverWithStreamCommitPolicy()
    control = _ControlResolverWithStreamCommitPolicy()
    try:
        explicit_composition = compose_job_runtime(
            authority,
            SupervisorStub(),
            capability_broker=_capability_broker(authority),
            start_resolver=start,
            control_resolver=control,
            provenance_receipt_resolver=_never_called,
            stream_commit_policy_resolver=explicit,
        )
        assert explicit_composition.handlers._stream_policy_resolver is explicit

        fallback_composition = compose_job_runtime(
            authority,
            SupervisorStub(),
            capability_broker=_capability_broker(authority),
            start_resolver=start,
            control_resolver=control,
            provenance_receipt_resolver=_never_called,
            stream_commit_policy_resolver=object(),
        )
        assert fallback_composition.handlers._stream_policy_resolver is start

        control_composition = compose_job_runtime(
            authority,
            SupervisorStub(),
            capability_broker=_capability_broker(authority),
            start_resolver=_never_called,
            control_resolver=control,
            provenance_receipt_resolver=_never_called,
        )
        assert control_composition.handlers._stream_policy_resolver is control
    finally:
        repository.close()


def test_composed_dispatcher_resolves_attempt_from_durable_authority(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    authority = ExecutionAuthority(repository, assets)
    supervisor = SupervisorStub()
    try:
        repository.create_workspace(Workspace("ws-1", "Novel"))
        repository.create_document(Document("doc-1", "ws-1", "Chapter"))
        base = repository.publish_revision(
            document_id="doc-1",
            content="old",
            expected_revision_id=None,
            created_by="user",
            revision_id="rev-base",
        )
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
            capability_broker=_capability_broker(authority),
            start_resolver=_never_called,
            control_resolver=_never_called,
            provenance_receipt_resolver=_never_called,
            stream_commit_policy_resolver=_StreamCommitPolicyResolver(),
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

        payload = assets.put(
            b"new text",
            mime="text/plain",
            logical_role="candidate_payload",
            provenance="plugin:test",
        )
        item = {
            "schema": "candidate-item/v1",
            "item_id": "item-1",
            "item_kind": "document",
            "target": {
                "workspace_id": "ws-1",
                "entity_kind": "document",
                "entity_id": "doc-1",
            },
            "mutation": {
                "mode": "replace",
                "payload_schema": "core/document-text/v1",
                "payload_hash": payload.sha256,
            },
            "payload_asset_id": payload.asset_id,
            "base": {
                "revision_id": base.revision_id,
                "content_hash": base.content_hash,
            },
            "write_set": [
                {
                    "workspace_id": "ws-1",
                    "entity_kind": "document",
                    "entity_id": "doc-1",
                    "revision_id": base.revision_id,
                    "content_hash": base.content_hash,
                }
            ],
            "parent_candidate_ids": [],
            "source_refs": [],
            "status": "complete",
        }
        bundle = {
            "schema": "result-bundle/v1",
            "contract_id": "candidate-batch/v1",
            "bundle_id": "bundle-1",
            "bundle_type": "candidate_batch",
            "producer": {
                "plugin_id": "com.plotpilot.demo",
                "release_id": RELEASE,
                "capability_id": "writing.chapter.draft/v1",
                "job_id": "job-1",
                "step_id": "step-1",
                "attempt_id": "attempt-1",
                "lease_epoch": 1,
            },
            "input_snapshot_hash": snapshot["snapshot_hash"],
            "items": [item],
            "warnings": [],
            "partial": False,
            "provenance_receipt_id": "receipt-1",
            "skill_chain_result_refs": [],
        }
        bundle_bytes = canonical_bytes(bundle)
        bundle_hash = sha256_hex(bundle_bytes)
        shared_meta = build_meta(
            "attempt",
            generation_id="generation-1",
            plugin_release_id=RELEASE,
            deadline_at="2026-09-04T13:00:00Z",
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
        )
        upload = build_request(
            "host.asset.create/v1",
            {
                "operation_key": "upload-bundle-1",
                "upload_id": "upload-bundle-1",
                "offset": 0,
                "mime": "application/json",
                "total_size": len(bundle_bytes),
                "expected_hash": bundle_hash,
                "chunk_hash": bundle_hash,
                "base64_chunk": base64.b64encode(bundle_bytes).decode("ascii"),
                "final": True,
            },
            shared_meta,
            request_id="00000000-0000-4000-8000-000000000002",
        )
        assert composition.dispatcher.dispatch_event(
            ticket, RpcEvent("host_request", upload)
        )
        uploaded = supervisor.responses[-1][1]
        assert uploaded == {
            "upload_id": "upload-bundle-1",
            "accepted_bytes": len(bundle_bytes),
            "completed": True,
            "asset_id": f"asset-sha256-{bundle_hash}",
        }

        read = build_request(
            "host.asset.read/v1",
            {
                "asset_id": uploaded["asset_id"],
                "offset": 0,
                "length": len(bundle_bytes) + 1,
            },
            shared_meta,
            request_id="00000000-0000-4000-8000-000000000003",
        )
        assert composition.dispatcher.dispatch_event(
            ticket, RpcEvent("host_request", read)
        )
        assert (
            base64.b64decode(supervisor.responses[-1][1]["base64_chunk"])
            == bundle_bytes
        )
        assert supervisor.responses[-1][1]["next_offset"] is None

        stage = build_request(
            "host.candidate.stage/v1",
            {
                "operation_key": "stage-1",
                "result_bundle_asset_id": uploaded["asset_id"],
                "input_snapshot_hash": snapshot["snapshot_hash"],
            },
            shared_meta,
            request_id="00000000-0000-4000-8000-000000000004",
        )
        assert composition.dispatcher.dispatch_event(
            ticket, RpcEvent("host_request", stage)
        )
        assert supervisor.responses[-1][1]["staged_items"] == [
            {
                "item_id": "item-1",
                "candidate_id": supervisor.responses[-1][1]["staged_items"][0][
                    "candidate_id"
                ],
                "stage_status": "created",
                "publication_eligibility": "eligible",
            }
        ]
        with repository.read_connection() as connection:
            assert (
                connection.execute(
                    "SELECT state FROM execution_attempt WHERE attempt_id='attempt-1'"
                ).fetchone()[0]
                == "running"
            )
            assert (
                connection.execute(
                    "SELECT status FROM candidate WHERE item_id='item-1'"
                ).fetchone()[0]
                == "prepared"
            )

        invoke = build_request(
            "host.capability.invoke/v1",
            {
                "operation_key": "invoke-1",
                "binding_id": "binding-1",
                "input_asset_id": payload.asset_id,
                "parameters_asset_id": None,
                "expected_result_contract": "candidate-batch/v1",
                "propagate_cancel": True,
            },
            shared_meta,
            request_id="00000000-0000-4000-8000-000000000005",
        )
        assert composition.dispatcher.dispatch_event(
            ticket, RpcEvent("host_request", invoke)
        )
        child_job_id = supervisor.responses[-1][1]["child_job_id"]
        for cursor, request_id in (
            (0, "00000000-0000-4000-8000-000000000006"),
            (1, "00000000-0000-4000-8000-000000000007"),
        ):
            poll = build_request(
                "host.capability.poll/v1",
                {
                    "child_job_id": child_job_id,
                    "after_job_event_seq": cursor,
                },
                shared_meta,
                request_id=request_id,
            )
            assert composition.dispatcher.dispatch_event(
                ticket, RpcEvent("host_request", poll)
            )
            assert supervisor.responses[-1][1]["terminal"] is False
            assert supervisor.responses[-1][1]["next_job_event_seq"] == 1

        regressed_poll = build_request(
            "host.capability.poll/v1",
            {"child_job_id": child_job_id, "after_job_event_seq": 0},
            shared_meta,
            request_id="00000000-0000-4000-8000-000000000008",
        )
        response_count = len(supervisor.responses)
        assert composition.dispatcher.dispatch_event(
            ticket, RpcEvent("host_request", regressed_poll)
        )
        assert len(supervisor.responses) == response_count + 1
        assert supervisor.responses[-1][1]["code"] == 1010
        assert "cursor regressed" in supervisor.responses[-1][1]["message"]

        cancel = build_request(
            "host.capability.cancel/v1",
            {
                "operation_key": "cancel-1",
                "child_job_id": child_job_id,
                "reason": "test cancel",
            },
            shared_meta,
            request_id="00000000-0000-4000-8000-000000000009",
        )
        for _ in range(2):
            assert composition.dispatcher.dispatch_event(
                ticket, RpcEvent("host_request", cancel)
            )
        broker_execution = composition.capability_broker.execution
        assert broker_execution.cancel_calls == [("cancel-1", child_job_id)]
    finally:
        repository.close()


def test_candidate_and_complete_lost_ack_replay_survives_repository_reopen(tmp_path):
    database = tmp_path / "core.db"
    asset_root = tmp_path / "assets"
    repository = CoreAuthorityRepository(database)
    assets = AssetStore(asset_root)
    authority = ExecutionAuthority(repository, assets)
    try:
        repository.create_workspace(Workspace("ws-1", "Novel"))
        repository.create_document(Document("doc-1", "ws-1", "Chapter"))
        base = repository.publish_revision(
            document_id="doc-1",
            content="old",
            expected_revision_id=None,
            created_by="user",
            revision_id="rev-base",
        )
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
        payload = assets.put(
            b"new text",
            mime="text/plain",
            logical_role="candidate_payload",
            provenance="plugin:test",
        )
        item = {
            "schema": "candidate-item/v1",
            "item_id": "item-1",
            "item_kind": "document",
            "target": {
                "workspace_id": "ws-1",
                "entity_kind": "document",
                "entity_id": "doc-1",
            },
            "mutation": {
                "mode": "replace",
                "payload_schema": "core/document-text/v1",
                "payload_hash": payload.sha256,
            },
            "payload_asset_id": payload.asset_id,
            "base": {
                "revision_id": base.revision_id,
                "content_hash": base.content_hash,
            },
            "write_set": [
                {
                    "workspace_id": "ws-1",
                    "entity_kind": "document",
                    "entity_id": "doc-1",
                    "revision_id": base.revision_id,
                    "content_hash": base.content_hash,
                }
            ],
            "parent_candidate_ids": [],
            "source_refs": [],
            "status": "complete",
        }
        bundle = {
            "schema": "result-bundle/v1",
            "contract_id": "candidate-batch/v1",
            "bundle_id": "bundle-1",
            "bundle_type": "candidate_batch",
            "producer": {
                "plugin_id": "com.plotpilot.demo",
                "release_id": RELEASE,
                "capability_id": "writing.chapter.draft/v1",
                "job_id": "job-1",
                "step_id": "step-1",
                "attempt_id": "attempt-1",
                "lease_epoch": 1,
            },
            "input_snapshot_hash": snapshot["snapshot_hash"],
            "items": [item],
            "warnings": [],
            "partial": False,
            "provenance_receipt_id": "receipt-1",
            "skill_chain_result_refs": [],
        }
        bundle_bytes = canonical_bytes(bundle)
        bundle_asset = assets.put(
            bundle_bytes,
            mime="application/json",
            logical_role="result_bundle",
            provenance="plugin:test",
        )
        receipt = {
            "schema": "provenance-receipt/v1",
            "receipt_id": "receipt-1",
            "plugin_id": "com.plotpilot.demo",
            "release_id": RELEASE,
            "package_hash": PACKAGE,
            "capability_id": "writing.chapter.draft/v1",
            "job_id": "job-1",
            "step_id": "step-1",
            "attempt_id": "attempt-1",
            "lease_epoch": 1,
            "run_snapshot_hash": snapshot["snapshot_hash"],
            "bundle_id": "bundle-1",
            "bundle_hash": sha256_hex(bundle_bytes),
            "parent_receipt_ids": [],
            "model_receipt_ids": [],
            "skill_chain_result_refs": [],
            "staged_items": ["item-1"],
            "created_at": "2026-09-04T00:00:00Z",
        }
        receipt["receipt_hash"] = hash_without_field(
            receipt, "receipt_hash", "provenance-receipt/v1"
        )
        meta = build_meta(
            "attempt",
            generation_id="generation-1",
            plugin_release_id=RELEASE,
            deadline_at="2026-09-04T13:00:00Z",
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
        )
        stage_request = build_request(
            "host.candidate.stage/v1",
            {
                "operation_key": "stage-1",
                "result_bundle_asset_id": bundle_asset.asset_id,
                "input_snapshot_hash": snapshot["snapshot_hash"],
            },
            meta,
            request_id="00000000-0000-4000-8000-000000000201",
        )
        resolver = _ReceiptResolver(receipt)
        supervisor = SupervisorStub()
        composition = compose_job_runtime(
            authority,
            supervisor,
            capability_broker=_capability_broker(authority),
            start_resolver=_never_called,
            control_resolver=_never_called,
            provenance_receipt_resolver=resolver,
            stream_commit_policy_resolver=_StreamCommitPolicyResolver(),
        )
        ticket = WorkerTicket("worker-1", "lifecycle-1", "retain-1", RELEASE, 1, 1)
        assert composition.dispatcher.dispatch_event(
            ticket, RpcEvent("host_request", stage_request)
        )
        first_stage = supervisor.responses[-1][1]
        assert first_stage["accepted"] is True
        assert (
            repository._connection.execute(
                "SELECT count(*) FROM candidate WHERE status='prepared'"
            ).fetchone()[0]
            == 1
        )

        repository.close()
        repository = CoreAuthorityRepository(database)
        assets = AssetStore(asset_root)
        authority = ExecutionAuthority(repository, assets)
        replay_supervisor = SupervisorStub()
        composition = compose_job_runtime(
            authority,
            replay_supervisor,
            capability_broker=_capability_broker(authority),
            start_resolver=_never_called,
            control_resolver=_never_called,
            provenance_receipt_resolver=resolver,
            stream_commit_policy_resolver=_StreamCommitPolicyResolver(),
        )
        assert composition.dispatcher.dispatch_event(
            ticket, RpcEvent("host_request", stage_request)
        )
        assert replay_supervisor.responses[-1][1] == first_stage
        assert (
            repository._connection.execute(
                "SELECT count(*) FROM candidate WHERE status='prepared'"
            ).fetchone()[0]
            == 1
        )

        complete_params = {
            "operation_key": "complete-1",
            "worker_run_id": "lifecycle-1",
            "outcome": "succeeded",
            "result_bundle_asset_id": bundle_asset.asset_id,
            "candidate_stage_operation_key": "stage-1",
            "terminal_detail_asset_id": None,
            "local_seq": 1,
        }
        complete_request = build_request(
            "host.job.complete/v1",
            complete_params,
            meta,
            request_id="00000000-0000-4000-8000-000000000202",
        )
        for worker_value in (None, "worker-derived"):
            rejected = {
                **complete_request,
                "id": (
                    "00000000-0000-4000-8000-000000000203"
                    if worker_value is None
                    else "00000000-0000-4000-8000-000000000204"
                ),
                "params": dict(complete_params),
            }
            if worker_value is None:
                rejected["params"].pop("worker_run_id")
            else:
                rejected["params"]["worker_run_id"] = worker_value
            before = repository._connection.serialize()
            assert composition.dispatcher.dispatch_event(
                ticket, RpcEvent("host_request", rejected)
            )
            assert "code" in replay_supervisor.responses[-1][1]
            assert repository._connection.serialize() == before

        assert composition.dispatcher.dispatch_event(
            ticket, RpcEvent("host_request", complete_request)
        )
        first_complete = replay_supervisor.responses[-1][1]
        persisted_frame = bytes(
            repository._connection.execute(
                "SELECT response_frame FROM p3_host_operation_ledger "
                "WHERE method='host.job.complete/v1' AND operation_key='complete-1'"
            ).fetchone()[0]
        )
        assert persisted_frame == encode_frame(
            {
                "jsonrpc": "2.0",
                "id": complete_request["id"],
                "result": first_complete,
            }
        )

        repository.close()
        repository = CoreAuthorityRepository(database)
        authority = ExecutionAuthority(repository, AssetStore(asset_root))
        terminal_supervisor = SupervisorStub()
        terminal_composition = compose_job_runtime(
            authority,
            terminal_supervisor,
            capability_broker=_capability_broker(authority),
            start_resolver=_never_called,
            control_resolver=_never_called,
            provenance_receipt_resolver=resolver,
            stream_commit_policy_resolver=_StreamCommitPolicyResolver(),
        )
        before_terminal_replay = repository._connection.serialize()
        assert terminal_composition.dispatcher.dispatch_event(
            ticket, RpcEvent("host_request", complete_request)
        )
        assert terminal_supervisor.responses[-1][1] == first_complete
        assert repository._connection.serialize() == before_terminal_replay
        assert (
            bytes(
                repository._connection.execute(
                    "SELECT response_frame FROM p3_host_operation_ledger "
                    "WHERE method='host.job.complete/v1' AND operation_key='complete-1'"
                ).fetchone()[0]
            )
            == persisted_frame
        )
    finally:
        repository.close()
