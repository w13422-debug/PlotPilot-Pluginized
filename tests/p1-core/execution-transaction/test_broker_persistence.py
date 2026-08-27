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
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode


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
