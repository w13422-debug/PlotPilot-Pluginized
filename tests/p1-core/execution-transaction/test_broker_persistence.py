from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from backend.plotpilot_core.broker.service import (
    BrokerInvocationEnvelope,
    CapabilityBroker,
    CallerAttemptContext,
    CapabilityBinding,
    ChildCreationRequest,
)
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode, canonical_bytes
from backend.plotpilot_plugin_sdk.verifier import hash_without_field


def _request(stack, operation_key="invoke-1"):
    context = CallerAttemptContext(
        "job-1", "step-1", "attempt-1", 1,
        generation_id="generation-1", plugin_release_id="e" * 64,
    )
    binding = CapabilityBinding(
        "binding-1", "writing.chapter.draft/v1", "com.plotpilot.demo", "1.0.0",
        "candidate-batch/v1", True, True,
    )
    input_meta = stack["assets"].put(b"input", mime="text/plain", logical_role="input", provenance="test")
    envelope = BrokerInvocationEnvelope.from_assets(
        invocation_id="invocation-1", parent=context, invoke_operation_key=operation_key,
        binding=binding, input_asset_id=input_meta.asset_id, input_asset_bytes=b"input",
    )
    envelope_id = stack["assets"].create_asset(envelope.canonical_bytes(), mime="application/json")
    return context, binding, ChildCreationRequest(
        envelope_id, envelope, input_meta.asset_id, None, binding, None,
        "e" * 64, "generation-1", context,
    )


def _checkpoint_asset(
    stack,
    *,
    checkpoint_id="checkpoint-resume-1",
    checkpoint_seq=1,
    source="attempt-1",
    epoch=1,
):
    value = {
        "schema": "checkpoint/v1",
        "checkpoint_id": checkpoint_id,
        "checkpoint_seq": checkpoint_seq,
        "job_id": "job-1",
        "step_id": "step-1",
        "source_attempt_id": source,
        "lease_epoch": epoch,
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
        provenance="test:direct-resume",
    )
    return value, asset


def test_sqlite_reservation_round_trips_and_rejects_drift(execution_stack):
    context, _binding, request = _request(execution_stack)
    ledger = execution_stack["authority"].operation_ledger
    identity = context.identity()
    reservation = ledger.reserve(
        context_identity=identity, method="host.capability.invoke/v1",
        operation_key="invoke-1", payload_hash=request.envelope.asset_hash,
    )
    assert reservation.child_creation is None
    ledger.attach_envelope(
        context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1",
        payload_hash=request.envelope.asset_hash, envelope_asset_id=request.envelope_asset_id,
    )
    with pytest.raises(ContractError):
        ledger.reserve(
            context_identity=identity, method="host.capability.invoke/v1",
            operation_key="invoke-1", payload_hash="0" * 64,
        )


def test_p3_job_migration_is_registered_from_its_verified_manifest(execution_stack):
    from pathlib import Path
    manifest = json.loads(Path("backend/plotpilot_core/jobs/migrations/manifest.json").read_text(encoding="utf-8"))
    row = execution_stack["repository"]._connection.execute(
        "SELECT sha256 FROM schema_migration WHERE migration_id='p3-jobs-001'"
    ).fetchone()
    assert row is not None and row[0] == manifest["steps"][0]["sha256"]


def test_atomic_create_or_recover_child_commits_response_and_single_lineage(execution_stack):
    context, _binding, request = _request(execution_stack)
    ledger = execution_stack["authority"].operation_ledger
    identity = context.identity()
    ledger.reserve(context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1", payload_hash=request.envelope.asset_hash)
    ledger.attach_envelope(
        context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1",
        payload_hash=request.envelope.asset_hash, envelope_asset_id=request.envelope_asset_id,
    )
    first = execution_stack["authority"].create_or_recover_child(request)
    second = execution_stack["authority"].create_or_recover_child(request)
    assert second.to_dict() == first.to_dict()
    assert ledger.lookup(context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1") is not None
    repository = execution_stack["repository"]
    assert repository._connection.execute("SELECT count(*) FROM execution_child_creation").fetchone()[0] == 1
    assert repository._connection.execute("SELECT count(*) FROM p3_broker_child_record").fetchone()[0] == 1
    assert repository._connection.execute("SELECT count(*) FROM execution_job WHERE job_id=?", (first.child_job_id,)).fetchone()[0] == 1
    attempt = repository._connection.execute(
        "SELECT * FROM execution_attempt WHERE attempt_id=?", (first.child_attempt_id,)
    ).fetchone()
    assert attempt["state"] == "created" and attempt["package_hash"] is None
    execution_stack["authority"].start_attempt(
        job_id=first.child_job_id, step_id=first.child_step_id,
        attempt_id=first.child_attempt_id, worker_run_id="child-worker-1",
        plugin_id=request.binding.plugin_id, release_id=request.plugin_release_id,
        package_hash="b" * 64, capability_id=request.binding.capability_id,
        generation_id=request.generation_id, lease_epoch=1,
        preallocated_receipt_id=attempt["preallocated_receipt_id"],
    )
    started = repository._connection.execute(
        "SELECT state,worker_run_id,package_hash FROM execution_attempt WHERE attempt_id=?",
        (first.child_attempt_id,),
    ).fetchone()
    assert tuple(started) == ("running", "child-worker-1", "b" * 64)


def test_envelope_only_reservation_recovers_after_repository_restart(execution_stack):
    context, _binding, request = _request(execution_stack)
    identity = context.identity()
    ledger = execution_stack["authority"].operation_ledger
    ledger.reserve(context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1", payload_hash=request.envelope.asset_hash)
    ledger.attach_envelope(
        context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1",
        payload_hash=request.envelope.asset_hash, envelope_asset_id=request.envelope_asset_id,
    )
    execution_stack["repository"].close()
    from backend.plotpilot_core.assets import AssetStore
    from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
    reopened = CoreAuthorityRepository(execution_stack["database"])
    execution_stack["repository"] = reopened
    authority = ExecutionAuthority(reopened, AssetStore(execution_stack["asset_root"]))
    execution_stack["authority"] = authority
    created = authority.create_or_recover_child(request)
    assert authority.operation_ledger.lookup(
        context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1"
    ) is not None
    assert reopened._connection.execute(
        "SELECT count(*) FROM execution_job WHERE job_id=?", (created.child_job_id,)
    ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("table", "operation"),
    [
        ("execution_job", "INSERT"),
        ("execution_step", "INSERT"),
        ("execution_attempt", "INSERT"),
        ("execution_child_creation", "INSERT"),
        ("p3_broker_operation", "UPDATE"),
        ("p3_broker_child_record", "INSERT"),
    ],
)
def test_child_factory_failure_windows_roll_back_and_retry_once(execution_stack, table, operation):
    context, _binding, request = _request(execution_stack)
    ledger = execution_stack["authority"].operation_ledger
    identity = context.identity()
    ledger.reserve(context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1", payload_hash=request.envelope.asset_hash)
    ledger.attach_envelope(
        context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1",
        payload_hash=request.envelope.asset_hash, envelope_asset_id=request.envelope_asset_id,
    )
    repository = execution_stack["repository"]
    before = {
        name: repository._connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        for name in ("execution_job", "execution_step", "execution_attempt", "execution_child_creation", "p3_broker_child_record")
    }
    trigger = "fail_child_" + table
    repository._connection.execute(
        f"CREATE TEMP TRIGGER {trigger} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'injected child factory failure'); END"
    )
    with pytest.raises(Exception, match="injected"):
        execution_stack["authority"].create_or_recover_child(request)
    after = {
        name: repository._connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        for name in before
    }
    assert after == before
    reservation = ledger.get_reservation(
        context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1"
    )
    assert reservation.child_creation is None and ledger.lookup(
        context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1"
    ) is None
    repository._connection.execute(f"DROP TRIGGER {trigger}")
    created = execution_stack["authority"].create_or_recover_child(request)
    assert repository._connection.execute(
        "SELECT count(*) FROM execution_job WHERE job_id=?", (created.child_job_id,)
    ).fetchone()[0] == 1


def test_child_reservation_and_response_are_set_once(execution_stack):
    context, _binding, request = _request(execution_stack)
    ledger = execution_stack["authority"].operation_ledger
    identity = context.identity()
    ledger.reserve(context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1", payload_hash=request.envelope.asset_hash)
    ledger.attach_envelope(
        context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1",
        payload_hash=request.envelope.asset_hash, envelope_asset_id=request.envelope_asset_id,
    )
    child = execution_stack["authority"].create_or_recover_child(request)
    drift = child.to_dict(); drift["state"] = "running"
    with pytest.raises(ContractError):
        ledger.attach_child(
            context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1",
            payload_hash=request.envelope.asset_hash, child_creation=drift,
        )
    with pytest.raises(ContractError):
        ledger.record(
            context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1",
            payload_hash=request.envelope.asset_hash, response=b'{"drift":true}',
        )


def test_committed_child_authority_corruption_fails_closed(execution_stack):
    context, _binding, request = _request(execution_stack)
    ledger = execution_stack["authority"].operation_ledger
    identity = context.identity()
    ledger.reserve(context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1", payload_hash=request.envelope.asset_hash)
    ledger.attach_envelope(
        context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1",
        payload_hash=request.envelope.asset_hash, envelope_asset_id=request.envelope_asset_id,
    )
    execution_stack["authority"].create_or_recover_child(request)
    execution_stack["repository"]._connection.execute("DELETE FROM p3_broker_child_record")
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].create_or_recover_child(request)
    assert caught.value.code == int(ErrorCode.ASSET_ERROR)


def test_concurrent_factories_create_one_durable_child(execution_stack):
    context, _binding, request = _request(execution_stack)
    ledger = execution_stack["authority"].operation_ledger
    identity = context.identity()
    ledger.reserve(context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1", payload_hash=request.envelope.asset_hash)
    ledger.attach_envelope(
        context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1",
        payload_hash=request.envelope.asset_hash, envelope_asset_id=request.envelope_asset_id,
    )
    from backend.plotpilot_core.assets import AssetStore
    from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
    second_repository = CoreAuthorityRepository(execution_stack["database"])
    second_authority = ExecutionAuthority(second_repository, AssetStore(execution_stack["asset_root"]))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(authority.create_or_recover_child, request)
                for authority in (execution_stack["authority"], second_authority)
            ]
            results = [future.result() for future in futures]
        assert results[0].to_dict() == results[1].to_dict()
        assert execution_stack["repository"]._connection.execute(
            "SELECT count(*) FROM execution_child_creation"
        ).fetchone()[0] == 1
        assert execution_stack["repository"]._connection.execute(
            "SELECT count(*) FROM p3_broker_child_record"
        ).fetchone()[0] == 1
    finally:
        second_repository.close()


def test_production_broker_uses_p1_ports_and_replays_after_restart(execution_stack):
    context, binding, request = _request(execution_stack)
    class Runtime:
        @staticmethod
        def resolve_release(_plugin_id, _requirement): return "e" * 64
        @staticmethod
        def current_generation(): return "generation-1"
    class Execution:
        pass
    authority = execution_stack["authority"]
    broker = CapabilityBroker(
        core=execution_stack["assets"], execution=Execution(), runtime=Runtime(),
        bindings={binding.binding_id: binding}, child_factory=authority,
        operation_ledger=authority.operation_ledger, child_records=authority.child_records,
        attempt_context=authority,
    )
    first = broker.invoke(context, operation_key="invoke-1", binding_id="binding-1", input_asset_id=request.input_asset_id)
    execution_stack["repository"].close()
    from backend.plotpilot_core.assets import AssetStore
    from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
    reopened = CoreAuthorityRepository(execution_stack["database"])
    assets = AssetStore(execution_stack["asset_root"])
    execution_stack["repository"] = reopened
    authority = ExecutionAuthority(reopened, assets)
    broker = CapabilityBroker(
        core=assets, execution=Execution(), runtime=Runtime(), bindings={binding.binding_id: binding},
        child_factory=authority, operation_ledger=authority.operation_ledger,
        child_records=authority.child_records, attempt_context=authority,
    )
    second = broker.invoke(context, operation_key="invoke-1", binding_id="binding-1", input_asset_id=request.input_asset_id)
    assert second.to_dict() == first.to_dict()
    assert reopened._connection.execute("SELECT count(*) FROM execution_child_creation").fetchone()[0] == 1


def test_direct_resume_alias_reuses_exact_child_and_survives_restart(execution_stack):
    class Execution:
        def __init__(self, stack):
            self.cancel_calls = 0
            self.snapshot_id = stack["assets"].create_asset(
                b"snapshot", mime="application/json"
            )
            self.events_id = stack["assets"].create_asset(
                b"events", mime="application/json"
            )

        def poll(self, _child_job_id, after_event_seq):
            return {
                "job_snapshot_asset_id": self.snapshot_id,
                "job_event_page_asset_id": self.events_id,
                "next_job_event_seq": after_event_seq,
                "terminal": False,
                "result_bundle_asset_id": None,
                "provenance_receipt_id": None,
                "child_state": "queued",
            }

        def cancel(self, _operation_key, _child_job_id):
            self.cancel_calls += 1
            return {
                "accepted": True,
                "terminal_known": False,
                "child_state": "cancelling",
                "child_job_event_seq": 0,
            }

    execution = Execution(execution_stack)
    broker, source, request = _production_broker(execution_stack, execution=execution)
    first = broker.invoke(
        source,
        operation_key="invoke-1",
        binding_id="binding-1",
        input_asset_id=request.input_asset_id,
    )
    _checkpoint, checkpoint_asset = _checkpoint_asset(execution_stack)
    paused = execution_stack["authority"].control_port.pause(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        lease_epoch=1,
        operation_key="pause-for-direct-resume",
        worker_run_id="worker-run-1",
        reason="resume child wait",
        checkpoint_asset_id=checkpoint_asset.asset_id,
    )
    assert paused.accepted
    resumed = execution_stack["authority"].control_port.resume(
        job_id="job-1",
        step_id="step-1",
        operation_key="resume-direct-child",
        resume_of_attempt_id="attempt-1",
        worker_run_id="worker-run-2",
        new_attempt_id="attempt-2",
        checkpoint_asset_id=checkpoint_asset.asset_id,
    )
    assert resumed.accepted
    current = CallerAttemptContext(
        "job-1",
        "step-1",
        "attempt-2",
        2,
        generation_id="generation-1",
        plugin_release_id="e" * 64,
    )

    aliased = broker.invoke(
        current,
        operation_key="invoke-1",
        binding_id="binding-1",
        input_asset_id=request.input_asset_id,
    )
    assert aliased.to_dict() == first.to_dict()
    with pytest.raises(ContractError, match="before every child"):
        execution_stack["authority"].complete_attempt(
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-2",
            lease_epoch=2,
            operation_key="complete-resumed-parent",
            worker_run_id="worker-run-2",
            outcome="failed",
            result_bundle_asset_id=None,
            candidate_stage_operation_key=None,
            terminal_detail_asset_id=None,
            local_seq=1,
            provenance_receipt=None,
            operation_meta={
                "protocol_version": "1",
                "generation_id": "generation-1",
                "plugin_release_id": "e" * 64,
                "deadline_at": "2026-09-04T01:00:00Z",
                "context": "attempt",
                "operation_id": "complete-resumed-parent",
                "job_id": "job-1",
                "step_id": "step-1",
                "attempt_id": "attempt-2",
                "lease_epoch": 2,
            },
        )
    assert broker.poll(current, child_job_id=first.child_job_id).terminal is False
    for _ in range(2):
        cancelled = broker.cancel(
            current,
            operation_key="cancel-resumed-child",
            child_job_id=first.child_job_id,
            reason="stop resumed parent",
        )
        assert cancelled.accepted
    assert execution.cancel_calls == 1
    connection = execution_stack["repository"]._connection
    assert (
        connection.execute("SELECT count(*) FROM execution_child_creation").fetchone()[
            0
        ]
        == 1
    )
    assert (
        connection.execute("SELECT count(*) FROM p3_broker_child_record").fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM p3_broker_operation "
            "WHERE method='host.capability.invoke/v1'"
        ).fetchone()[0]
        == 2
    )

    execution_stack["repository"].close()
    from backend.plotpilot_core.assets import AssetStore
    from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository

    reopened = CoreAuthorityRepository(execution_stack["database"])
    assets = AssetStore(execution_stack["asset_root"])
    execution_stack["repository"] = reopened
    execution_stack["assets"] = assets
    execution_stack["authority"] = ExecutionAuthority(reopened, assets)
    restarted, _source, restarted_request = _production_broker(
        execution_stack, execution=Execution(execution_stack)
    )
    replay = restarted.invoke(
        current,
        operation_key="invoke-1",
        binding_id="binding-1",
        input_asset_id=restarted_request.input_asset_id,
    )
    assert replay.to_dict() == first.to_dict()
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM execution_child_creation"
        ).fetchone()[0]
        == 1
    )
    assert (
        reopened._connection.execute(
            "SELECT count(*) FROM p3_broker_child_record"
        ).fetchone()[0]
        == 1
    )


def test_invoke_response_requires_child_and_child_drift_is_rejected(execution_stack):
    context, _binding, request = _request(execution_stack)
    identity = context.identity(); ledger = execution_stack["authority"].operation_ledger
    ledger.reserve(context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1", payload_hash=request.envelope.asset_hash)
    ledger.attach_envelope(context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1", payload_hash=request.envelope.asset_hash, envelope_asset_id=request.envelope_asset_id)
    with pytest.raises(ContractError) as caught:
        ledger.record(context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1", payload_hash=request.envelope.asset_hash, response=b"{}")
    assert caught.value.code == int(ErrorCode.INVALID_TRANSITION)


def test_committed_child_replay_is_fenced_before_lookup(execution_stack):
    context, _binding, request = _request(execution_stack)
    identity = context.identity(); ledger = execution_stack["authority"].operation_ledger
    ledger.reserve(context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1", payload_hash=request.envelope.asset_hash)
    ledger.attach_envelope(context_identity=identity, method="host.capability.invoke/v1", operation_key="invoke-1", payload_hash=request.envelope.asset_hash, envelope_asset_id=request.envelope_asset_id)
    execution_stack["authority"].create_or_recover_child(request)
    execution_stack["repository"]._connection.execute("UPDATE execution_attempt SET lease_epoch=2 WHERE attempt_id='attempt-1'")
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].validate_attempt(context)
    assert caught.value.code == int(ErrorCode.STALE_LEASE)


def _production_broker(stack, *, execution, ledger=None):
    context, binding, request = _request(stack)

    class Runtime:
        @staticmethod
        def resolve_release(_plugin_id, _requirement): return "e" * 64

        @staticmethod
        def current_generation(): return "generation-1"

    authority = stack["authority"]
    broker = CapabilityBroker(
        core=stack["assets"], execution=execution, runtime=Runtime(),
        bindings={binding.binding_id: binding}, child_factory=authority,
        operation_ledger=ledger or authority.operation_ledger,
        child_records=authority.child_records, attempt_context=authority,
    )
    return broker, context, request


def _prepared_direct_resume_case(stack):
    class Execution:
        pass

    broker, source, request = _production_broker(stack, execution=Execution())
    child = broker.invoke(
        source,
        operation_key="invoke-1",
        binding_id="binding-1",
        input_asset_id=request.input_asset_id,
    )
    _checkpoint, checkpoint_asset = _checkpoint_asset(stack)
    stack["authority"].control_port.pause(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        lease_epoch=1,
        operation_key="pause-for-alias-negative",
        worker_run_id="worker-run-1",
        reason="negative alias setup",
        checkpoint_asset_id=checkpoint_asset.asset_id,
    )
    stack["authority"].control_port.resume(
        job_id="job-1",
        step_id="step-1",
        operation_key="resume-for-alias-negative",
        resume_of_attempt_id="attempt-1",
        worker_run_id="worker-run-2",
        new_attempt_id="attempt-2",
        checkpoint_asset_id=checkpoint_asset.asset_id,
    )
    current = CallerAttemptContext(
        "job-1",
        "step-1",
        "attempt-2",
        2,
        generation_id="generation-1",
        plugin_release_id="e" * 64,
    )
    return broker, request, child, current


def _receipt_for_attempt(attempt, *, parent_receipt_ids):
    receipt = {
        "schema": "provenance-receipt/v1",
        "receipt_id": attempt["preallocated_receipt_id"],
        "plugin_id": attempt["plugin_id"],
        "release_id": attempt["release_id"],
        "package_hash": attempt["package_hash"],
        "capability_id": attempt["capability_id"],
        "job_id": attempt["job_id"],
        "step_id": attempt["step_id"],
        "attempt_id": attempt["attempt_id"],
        "lease_epoch": attempt["lease_epoch"],
        "run_snapshot_hash": attempt["run_snapshot_hash"],
        "bundle_id": None,
        "bundle_hash": None,
        "parent_receipt_ids": list(parent_receipt_ids),
        "model_receipt_ids": [],
        "skill_chain_result_refs": [],
        "staged_items": [],
        "created_at": "2026-09-04T00:00:00Z",
    }
    receipt["receipt_hash"] = hash_without_field(
        receipt, "receipt_hash", "provenance-receipt/v1"
    )
    return receipt


def _terminal_direct_resume_case(stack):
    broker, request, child, current = _prepared_direct_resume_case(stack)
    aliased = broker.invoke(
        current,
        operation_key="invoke-1",
        binding_id="binding-1",
        input_asset_id=request.input_asset_id,
    )
    assert aliased.to_dict() == child.to_dict()

    authority = stack["authority"]
    connection = stack["repository"]._connection
    child_attempt = connection.execute(
        "SELECT a.*,j.run_snapshot_hash FROM execution_attempt a "
        "JOIN execution_job j ON j.job_id=a.job_id WHERE a.job_id=?",
        (child.child_job_id,),
    ).fetchone()
    authority.start_attempt(
        job_id=child.child_job_id,
        step_id=child.child_step_id,
        attempt_id=child_attempt["attempt_id"],
        worker_run_id="child-worker-terminal",
        plugin_id=request.binding.plugin_id,
        release_id=request.plugin_release_id,
        package_hash="b" * 64,
        capability_id=request.binding.capability_id,
        generation_id=request.generation_id,
        lease_epoch=child_attempt["lease_epoch"],
        preallocated_receipt_id=child_attempt["preallocated_receipt_id"],
    )
    child_attempt = connection.execute(
        "SELECT a.*,j.run_snapshot_hash FROM execution_attempt a "
        "JOIN execution_job j ON j.job_id=a.job_id WHERE a.job_id=?",
        (child.child_job_id,),
    ).fetchone()
    child_receipt = _receipt_for_attempt(child_attempt, parent_receipt_ids=())
    child_commit = authority.complete_attempt(
        job_id=child.child_job_id,
        step_id=child.child_step_id,
        attempt_id=child_attempt["attempt_id"],
        lease_epoch=child_attempt["lease_epoch"],
        operation_key="complete-terminal-child",
        worker_run_id="child-worker-terminal",
        outcome="failed",
        result_bundle_asset_id=None,
        candidate_stage_operation_key=None,
        terminal_detail_asset_id=None,
        local_seq=1,
        provenance_receipt=child_receipt,
        operation_meta={
            "protocol_version": "1",
            "generation_id": request.generation_id,
            "plugin_release_id": request.plugin_release_id,
            "deadline_at": "2026-09-04T01:00:00Z",
            "context": "attempt",
            "operation_id": "complete-terminal-child",
            "job_id": child.child_job_id,
            "step_id": child.child_step_id,
            "attempt_id": child_attempt["attempt_id"],
            "lease_epoch": child_attempt["lease_epoch"],
        },
    )
    assert child_commit.result["attempt_state"] == "failed"

    parent_attempt = connection.execute(
        "SELECT a.*,j.run_snapshot_hash FROM execution_attempt a "
        "JOIN execution_job j ON j.job_id=a.job_id "
        "WHERE a.attempt_id='attempt-2'"
    ).fetchone()
    parent_receipt = _receipt_for_attempt(
        parent_attempt,
        parent_receipt_ids=(child_receipt["receipt_id"],),
    )
    parent_completion = {
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-2",
        "lease_epoch": 2,
        "operation_key": "complete-resumed-parent",
        "worker_run_id": "worker-run-2",
        "outcome": "failed",
        "result_bundle_asset_id": None,
        "candidate_stage_operation_key": None,
        "terminal_detail_asset_id": None,
        "local_seq": 2,
        "provenance_receipt": parent_receipt,
        "operation_meta": {
            "protocol_version": "1",
            "generation_id": "generation-1",
            "plugin_release_id": "e" * 64,
            "deadline_at": "2026-09-04T01:00:00Z",
            "context": "attempt",
            "operation_id": "complete-resumed-parent",
            "job_id": "job-1",
            "step_id": "step-1",
            "attempt_id": "attempt-2",
            "lease_epoch": 2,
        },
    }
    return current, child_receipt["receipt_id"], parent_completion


def test_direct_resume_terminal_alias_closure_allows_parent_completion(
    execution_stack,
):
    _current, child_receipt_id, parent_completion = _terminal_direct_resume_case(
        execution_stack
    )
    commit = execution_stack["authority"].complete_attempt(**parent_completion)
    assert commit.result["accepted"] is True
    assert commit.result["attempt_state"] == "failed"

    connection = execution_stack["repository"]._connection
    parent_attempt = connection.execute(
        "SELECT state FROM execution_attempt WHERE attempt_id='attempt-2'"
    ).fetchone()
    assert parent_attempt["state"] == "failed"
    parent_receipt = connection.execute(
        "SELECT receipt_json FROM execution_receipt WHERE receipt_id=?",
        (commit.result["provenance_receipt_id"],),
    ).fetchone()
    assert json.loads(parent_receipt["receipt_json"])["parent_receipt_ids"] == [
        child_receipt_id
    ]


@pytest.mark.parametrize("drift", ["payload", "envelope", "response"])
def test_direct_resume_terminal_alias_closure_drift_is_zero_write(
    execution_stack, drift
):
    current, _child_receipt_id, parent_completion = _terminal_direct_resume_case(
        execution_stack
    )
    connection = execution_stack["repository"]._connection
    current_identity = current.identity()
    operation = connection.execute(
        "SELECT * FROM p3_broker_operation "
        "WHERE context_identity=? AND method='host.capability.invoke/v1' "
        "AND operation_key='invoke-1'",
        (current_identity,),
    ).fetchone()
    if drift == "payload":
        connection.execute(
            "UPDATE p3_broker_operation SET payload_hash=? "
            "WHERE context_identity=? AND method='host.capability.invoke/v1' "
            "AND operation_key='invoke-1'",
            ("f" * 64, current_identity),
        )
    elif drift == "envelope":
        source_envelope_asset_id = connection.execute(
            "SELECT envelope_asset_id FROM p3_broker_operation "
            "WHERE context_identity<>? AND method='host.capability.invoke/v1' "
            "AND operation_key='invoke-1'",
            (current_identity,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE p3_broker_operation SET envelope_asset_id=? "
            "WHERE context_identity=? AND method='host.capability.invoke/v1' "
            "AND operation_key='invoke-1'",
            (source_envelope_asset_id, current_identity),
        )
    else:
        response = json.loads(bytes(operation["response"]))
        response["child_job_event_seq"] += 1
        connection.execute(
            "UPDATE p3_broker_operation SET response=? "
            "WHERE context_identity=? AND method='host.capability.invoke/v1' "
            "AND operation_key='invoke-1'",
            (canonical_bytes(response), current_identity),
        )
    database_before = connection.serialize()
    assets_before = sorted(
        str(path.relative_to(execution_stack["assets"].root))
        for path in execution_stack["assets"].root.rglob("*")
        if path.is_file()
    )

    with pytest.raises(
        ContractError, match="direct resume child alias closure drifted"
    ) as caught:
        execution_stack["authority"].complete_attempt(**parent_completion)

    assert caught.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)
    assert connection.serialize() == database_before
    assert (
        sorted(
            str(path.relative_to(execution_stack["assets"].root))
            for path in execution_stack["assets"].root.rglob("*")
            if path.is_file()
        )
        == assets_before
    )
    assert (
        connection.execute(
            "SELECT state FROM execution_attempt WHERE attempt_id='attempt-2'"
        ).fetchone()[0]
        == "running"
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM execution_outcome WHERE attempt_id='attempt-2'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "drift",
    [
        "sibling",
        "wrong-checkpoint",
        "release",
        "generation",
        "package",
        "capability",
        "snapshot",
        "payload",
        "worker",
    ],
)
def test_direct_resume_alias_identity_drift_rejects_before_effect(
    execution_stack, drift
):
    broker, request, child, current = _prepared_direct_resume_case(execution_stack)
    connection = execution_stack["repository"]._connection
    input_asset_id = request.input_asset_id
    if drift == "sibling":
        sibling_attempt_id = connection.execute(
            "SELECT attempt_id FROM execution_attempt WHERE job_id=?",
            (child.child_job_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE execution_attempt SET resume_of_attempt_id=? "
            "WHERE attempt_id='attempt-2'",
            (sibling_attempt_id,),
        )
    elif drift == "wrong-checkpoint":
        connection.execute(
            "UPDATE execution_attempt SET resume_checkpoint_id='checkpoint-missing' "
            "WHERE attempt_id='attempt-2'"
        )
    elif drift == "release":
        connection.execute(
            "UPDATE execution_attempt SET release_id=? WHERE attempt_id='attempt-2'",
            ("f" * 64,),
        )
        current = CallerAttemptContext(
            "job-1",
            "step-1",
            "attempt-2",
            2,
            generation_id="generation-1",
            plugin_release_id="f" * 64,
        )
    elif drift == "generation":
        connection.execute(
            "UPDATE execution_attempt SET generation_id='generation-2' "
            "WHERE attempt_id='attempt-2'"
        )
        current = CallerAttemptContext(
            "job-1",
            "step-1",
            "attempt-2",
            2,
            generation_id="generation-2",
            plugin_release_id="e" * 64,
        )
    elif drift == "package":
        connection.execute(
            "UPDATE execution_attempt SET package_hash=? WHERE attempt_id='attempt-2'",
            ("f" * 64,),
        )
    elif drift == "capability":
        connection.execute(
            "UPDATE execution_attempt SET capability_id='writing.other/v1' "
            "WHERE attempt_id='attempt-2'"
        )
    elif drift == "snapshot":
        connection.execute(
            "UPDATE execution_checkpoint SET run_snapshot_hash=? "
            "WHERE checkpoint_id='checkpoint-resume-1'",
            ("f" * 64,),
        )
    elif drift == "payload":
        input_asset_id = execution_stack["assets"].create_asset(
            b"different input", mime="text/plain"
        )
    elif drift == "worker":
        connection.execute(
            "UPDATE execution_attempt SET owner_instance_id='worker-derived' "
            "WHERE attempt_id='attempt-2'"
        )
    before_db = connection.serialize()
    before_assets = sorted(
        str(path.relative_to(execution_stack["assets"].root))
        for path in execution_stack["assets"].root.rglob("*")
        if path.is_file()
    )

    with pytest.raises(ContractError):
        broker.invoke(
            current,
            operation_key="invoke-1",
            binding_id="binding-1",
            input_asset_id=input_asset_id,
        )

    assert connection.serialize() == before_db
    assert (
        sorted(
            str(path.relative_to(execution_stack["assets"].root))
            for path in execution_stack["assets"].root.rglob("*")
            if path.is_file()
        )
        == before_assets
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM p3_broker_operation "
            "WHERE method='host.capability.invoke/v1'"
        ).fetchone()[0]
        == 1
    )


def test_non_direct_resume_alias_chain_is_rejected(execution_stack):
    broker, request, first, attempt_2 = _prepared_direct_resume_case(execution_stack)
    aliased = broker.invoke(
        attempt_2,
        operation_key="invoke-1",
        binding_id="binding-1",
        input_asset_id=request.input_asset_id,
    )
    assert aliased.child_job_id == first.child_job_id
    _checkpoint, checkpoint_asset = _checkpoint_asset(
        execution_stack,
        checkpoint_id="checkpoint-resume-2",
        checkpoint_seq=2,
        source="attempt-2",
        epoch=2,
    )
    execution_stack["authority"].control_port.pause(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-2",
        lease_epoch=2,
        operation_key="pause-for-nondirect-alias",
        worker_run_id="worker-run-2",
        reason="second resume",
        checkpoint_asset_id=checkpoint_asset.asset_id,
    )
    execution_stack["authority"].control_port.resume(
        job_id="job-1",
        step_id="step-1",
        operation_key="resume-nondirect-alias",
        resume_of_attempt_id="attempt-2",
        worker_run_id="worker-run-3",
        new_attempt_id="attempt-3",
        checkpoint_asset_id=checkpoint_asset.asset_id,
    )
    attempt_3 = CallerAttemptContext(
        "job-1",
        "step-1",
        "attempt-3",
        3,
        generation_id="generation-1",
        plugin_release_id="e" * 64,
    )
    connection = execution_stack["repository"]._connection
    before = connection.serialize()
    with pytest.raises(ContractError):
        broker.invoke(
            attempt_3,
            operation_key="invoke-1",
            binding_id="binding-1",
            input_asset_id=request.input_asset_id,
        )
    assert connection.serialize() == before
    assert (
        connection.execute(
            "SELECT count(*) FROM p3_broker_operation "
            "WHERE method='host.capability.invoke/v1'"
        ).fetchone()[0]
        == 2
    )


def test_f001_cancel_reservation_precedes_side_effect_and_recovers_record_failure(execution_stack):
    authority = execution_stack["authority"]

    class FaultOnceLedger:
        def __init__(self, delegate):
            self.delegate = delegate
            self.failed = False

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def record(self, **kwargs):
            if kwargs["method"] == "host.capability.cancel/v1" and not self.failed:
                self.failed = True
                raise RuntimeError("injected ACK persistence failure")
            return self.delegate.record(**kwargs)

    class Execution:
        def __init__(self): self.cancel_calls = 0

        def cancel(self, _operation_key, _child_job_id):
            self.cancel_calls += 1
            return True

    execution = Execution()
    ledger = FaultOnceLedger(authority.operation_ledger)
    broker, context, request = _production_broker(execution_stack, execution=execution, ledger=ledger)
    child = broker.invoke(context, operation_key="invoke-1", binding_id="binding-1", input_asset_id=request.input_asset_id)
    with pytest.raises(RuntimeError, match="injected"):
        broker.cancel(context, operation_key="cancel-1", child_job_id=child.child_job_id)
    replay = broker.cancel(context, operation_key="cancel-1", child_job_id=child.child_job_id)
    assert replay.accepted and replay.child_state == "cancelling"
    assert execution.cancel_calls == 1
    assert execution_stack["repository"]._connection.execute(
        "SELECT count(*) FROM p3_broker_operation WHERE method='host.capability.cancel/v1'"
    ).fetchone()[0] == 1


def test_f003_required_queued_child_blocks_parent_success_without_mutation(execution_stack):
    class Execution:
        pass

    broker, context, request = _production_broker(execution_stack, execution=Execution())
    broker.invoke(context, operation_key="invoke-1", binding_id="binding-1", input_asset_id=request.input_asset_id)
    from support import complete_kwargs, make_candidate_bundle
    bundle, receipt, _ = make_candidate_bundle(execution_stack)
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].complete_attempt(**complete_kwargs(bundle, receipt))
    assert caught.value.code == int(ErrorCode.INVALID_TRANSITION)
    connection = execution_stack["repository"]._connection
    assert connection.execute("SELECT job_state FROM execution_job WHERE job_id='job-1'").fetchone()[0] == "running"
    assert connection.execute("SELECT count(*) FROM execution_outcome WHERE job_id='job-1'").fetchone()[0] == 0


def test_f015_invoke_replay_requires_snapshot_bytes_and_child_identity(execution_stack):
    class Execution:
        pass

    broker, context, request = _production_broker(execution_stack, execution=Execution())
    child = broker.invoke(context, operation_key="invoke-1", binding_id="binding-1", input_asset_id=request.input_asset_id)
    digest = child.child_run_snapshot_asset_id.removeprefix("asset-sha256-")
    (execution_stack["assets"].objects / digest[:2] / digest).unlink()
    with pytest.raises(ContractError) as caught:
        broker.invoke(context, operation_key="invoke-1", binding_id="binding-1", input_asset_id=request.input_asset_id)
    assert caught.value.code == int(ErrorCode.ASSET_ERROR)


def test_f015_broker_child_exact_completion_replay_is_byte_equivalent_and_read_only(execution_stack):
    context, _binding, request = _request(execution_stack)
    identity = context.identity()
    authority = execution_stack["authority"]
    ledger = authority.operation_ledger
    ledger.reserve(
        context_identity=identity,
        method="host.capability.invoke/v1",
        operation_key="invoke-1",
        payload_hash=request.envelope.asset_hash,
    )
    ledger.attach_envelope(
        context_identity=identity,
        method="host.capability.invoke/v1",
        operation_key="invoke-1",
        payload_hash=request.envelope.asset_hash,
        envelope_asset_id=request.envelope_asset_id,
    )
    child = authority.create_or_recover_child(request)
    attempt = execution_stack["repository"]._connection.execute(
        "SELECT * FROM execution_attempt WHERE attempt_id=?",
        (child.child_attempt_id,),
    ).fetchone()
    authority.start_attempt(
        job_id=child.child_job_id,
        step_id=child.child_step_id,
        attempt_id=child.child_attempt_id,
        worker_run_id="child-worker-1",
        plugin_id=request.binding.plugin_id,
        release_id=request.plugin_release_id,
        package_hash="b" * 64,
        capability_id=request.binding.capability_id,
        generation_id=request.generation_id,
        lease_epoch=child.child_lease_epoch,
        preallocated_receipt_id=attempt["preallocated_receipt_id"],
    )
    receipt = {
        "schema": "provenance-receipt/v1",
        "receipt_id": attempt["preallocated_receipt_id"],
        "plugin_id": request.binding.plugin_id,
        "release_id": request.plugin_release_id,
        "package_hash": "b" * 64,
        "capability_id": request.binding.capability_id,
        "job_id": child.child_job_id,
        "step_id": child.child_step_id,
        "attempt_id": child.child_attempt_id,
        "lease_epoch": child.child_lease_epoch,
        "run_snapshot_hash": child.child_run_snapshot_hash,
        "bundle_id": None,
        "bundle_hash": None,
        "parent_receipt_ids": [],
        "model_receipt_ids": [],
        "skill_chain_result_refs": [],
        "staged_items": [],
        "created_at": "2026-08-28T00:00:00Z",
    }
    receipt["receipt_hash"] = hash_without_field(
        receipt, "receipt_hash", "provenance-receipt/v1"
    )
    kwargs = {
        "job_id": child.child_job_id,
        "step_id": child.child_step_id,
        "attempt_id": child.child_attempt_id,
        "lease_epoch": child.child_lease_epoch,
        "operation_key": "complete-child-1",
        "worker_run_id": "child-worker-1",
        "outcome": "failed",
        "result_bundle_asset_id": None,
        "candidate_stage_operation_key": None,
        "terminal_detail_asset_id": None,
        "local_seq": 1,
        "provenance_receipt": receipt,
        "operation_meta": {
            "protocol_version": "1",
            "generation_id": request.generation_id,
            "plugin_release_id": request.plugin_release_id,
            "deadline_at": "2026-08-28T01:00:00Z",
            "context": "attempt",
            "operation_id": "rpc-complete-child-1",
            "job_id": child.child_job_id,
            "step_id": child.child_step_id,
            "attempt_id": child.child_attempt_id,
            "lease_epoch": child.child_lease_epoch,
        },
    }
    first = authority.complete_attempt(**kwargs)
    database_before_replay = execution_stack["repository"]._connection.serialize()
    assets_before_replay = sorted(
        str(path.relative_to(execution_stack["assets"].objects))
        for path in execution_stack["assets"].objects.rglob("*")
        if path.is_file()
    )
    second = authority.complete_attempt(**kwargs)
    assert not first.replayed and second.replayed
    assert first.to_dict() == second.to_dict()
    assert first.response_frame == second.response_frame
    assert execution_stack["repository"]._connection.serialize() == database_before_replay
    assert sorted(
        str(path.relative_to(execution_stack["assets"].objects))
        for path in execution_stack["assets"].objects.rglob("*")
        if path.is_file()
    ) == assets_before_replay
