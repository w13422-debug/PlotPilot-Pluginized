from __future__ import annotations

from hashlib import sha256

import pytest

from backend.plotpilot_core.broker import (
    BrokerChildRecord,
    BrokerInvocationEnvelope,
    CapabilityBinding,
    CapabilityBroker,
    CallerAttemptContext,
    ChildCreationResult,
    InMemoryAttemptContextPort,
    InMemoryBrokerChildRecordStore,
    InMemoryBrokerOperationLedger,
    aggregate_child_outcomes,
    build_receipt_propagation,
    derive_child_cancel_operation_key,
)
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ErrorCode,
    canonical_bytes,
    hash_jcs,
    sha256_hex,
    validate_contract,
)
from backend.plotpilot_plugin_sdk.ports import (
    FakeCoreAuthorityPort,
    FakeExecutionPort,
    FakePluginRuntimePort,
)


HASH_A = "a" * 64
HASH_B = "b" * 64


class CountingExecution(FakeExecutionPort):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls: list[tuple[str, str]] = []

    def cancel(self, operation_key: str, job_id: str) -> bool:
        self.cancel_calls.append((operation_key, job_id))
        return super().cancel(operation_key, job_id)


class AttestedChildFactory:
    def __init__(self, execution: FakeExecutionPort, core: FakeCoreAuthorityPort, *, state: str = "running") -> None:
        self.execution = execution
        self.core = core
        self.state = state
        self.calls = []
        self.created = {}

    def create_or_recover_child(self, request):
        self.calls.append(request)
        key = request.envelope.invoke_operation_key
        if key in self.created:
            return self.created[key]
        index = len(self.created) + 1
        child_job_id = f"child-job-{index}"
        child_step_id = f"child-step-{index}"
        child_attempt_id = f"child-attempt-{index}"
        self.execution.jobs[child_job_id] = []
        attestation = {
            "child_job_id": child_job_id,
            "child_step_id": child_step_id,
            "child_attempt_id": child_attempt_id,
            "broker_invocation_asset_id": request.envelope_asset_id,
            "broker_invocation_hash": request.envelope.asset_hash,
            "input_asset_id": request.input_asset_id,
            "input_hash": request.envelope.input_hash,
            "parameters_asset_id": request.envelope_asset_id,
        }
        if request.parameters_asset_id is not None:
            attestation["source_parameters_asset_id"] = request.parameters_asset_id
            attestation["source_parameters_hash"] = request.envelope.parameters_hash
        snapshot_bytes = canonical_bytes(attestation)
        snapshot_id = self.core.create_asset(snapshot_bytes, mime="application/json")
        result = ChildCreationResult(
            child_job_id=child_job_id,
            child_step_id=child_step_id,
            child_attempt_id=child_attempt_id,
            child_lease_epoch=1,
            child_plugin_release_id=request.plugin_release_id,
            child_run_snapshot_asset_id=snapshot_id,
            child_run_snapshot_hash=sha256_hex(snapshot_bytes),
            child_job_event_seq=1,
            state=self.state,
            binding_attestation=attestation,
        )
        self.created[key] = result
        return result


class ProjectionPort:
    def __init__(self, *, state: str = "succeeded", receipt: str | None = None) -> None:
        self.state = state
        self.receipt = receipt
        self.calls = []

    def project_poll(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "job_snapshot_asset_id": "job-snapshot-1",
            "job_event_page_asset_id": "job-events-1",
            "next_job_event_seq": kwargs["after_job_event_seq"] + 1,
            "terminal": self.state in {"succeeded", "failed", "cancelled"},
            "result_bundle_asset_id": None,
            "provenance_receipt_id": self.receipt,
            "child_state": self.state,
        }


def _binding(*, propagate_cancel: bool = True, required: bool = True) -> CapabilityBinding:
    return CapabilityBinding(
        binding_id="binding-1",
        capability_id="capability.writer/v1",
        plugin_id="plugin.writer",
        release_requirement="1.2.3",
        result_contract="artifact-bundle/v1",
        required=required,
        propagate_cancel=propagate_cancel,
    )


def _context() -> CallerAttemptContext:
    return CallerAttemptContext(
        parent_job_id="parent-job-1",
        parent_step_id="parent-step-1",
        parent_attempt_id="parent-attempt-1",
        lease_epoch=1,
    )


def _broker(
    *,
    binding: CapabilityBinding | None = None,
    factory=None,
    execution=None,
    projection=None,
    records=None,
    receipt_port=None,
):
    core = FakeCoreAuthorityPort()
    input_asset_id = core.create_asset(b"immutable input", mime="text/plain")
    parameters_asset_id = core.create_asset(b"immutable parameters", mime="application/json")
    execution = execution or CountingExecution()
    runtime = FakePluginRuntimePort(
        releases={("plugin.writer", "1.2.3"): HASH_A},
        generation_id="generation-1",
    )
    factory = factory or AttestedChildFactory(execution, core)
    broker = CapabilityBroker.for_test(
        core=core,
        execution=execution,
        runtime=runtime,
        bindings={"binding-1": binding or _binding()},
        child_factory=factory,
        operation_ledger=InMemoryBrokerOperationLedger(),
        child_records=records if records is not None else InMemoryBrokerChildRecordStore(),
        child_snapshot_port=projection,
        receipt_port=receipt_port,
    )
    return broker, core, input_asset_id, parameters_asset_id, execution, factory


def test_envelope_and_child_record_are_exact_closed_hash_bound_values():
    envelope = BrokerInvocationEnvelope(
        invocation_id="invocation-1",
        parent_job_id="parent-job-1",
        parent_step_id="parent-step-1",
        parent_attempt_id="parent-attempt-1",
        invoke_operation_key="invoke-1",
        binding_id="binding-1",
        input_asset_id="input-1",
        input_hash=HASH_A,
        parameters_asset_id=None,
        parameters_hash=None,
        expected_result_contract="artifact-bundle/v1",
        required=True,
        propagate_cancel=True,
    )
    raw = envelope.to_dict()
    assert set(raw) == {
        "schema",
        "invocation_id",
        "parent_job_id",
        "parent_step_id",
        "parent_attempt_id",
        "invoke_operation_key",
        "binding_id",
        "input_asset_id",
        "input_hash",
        "parameters_asset_id",
        "parameters_hash",
        "expected_result_contract",
        "required",
        "propagate_cancel",
    }
    assert validate_contract("broker-invocation-v1", raw) == []
    assert envelope.asset_hash == sha256(canonical_bytes(raw)).hexdigest()
    assert BrokerInvocationEnvelope.from_mapping(raw) == envelope
    with pytest.raises(ContractError):
        BrokerInvocationEnvelope.from_mapping({**raw, "unexpected": True})
    with pytest.raises(ContractError):
        BrokerInvocationEnvelope.from_mapping({**raw, "parameters_hash": HASH_B})

    record = BrokerChildRecord(
        child_job_id="child-job-1",
        parent_job_id="parent-job-1",
        parent_step_id="parent-step-1",
        parent_attempt_id="parent-attempt-1",
        invoke_operation_key="invoke-1",
        binding_id="binding-1",
        broker_invocation_asset_id="broker-envelope-1",
        broker_invocation_hash=envelope.asset_hash,
        child_run_snapshot_asset_id="child-snapshot-1",
        child_run_snapshot_hash=HASH_B,
        result_contract="artifact-bundle/v1",
        required=True,
        propagate_cancel=True,
        state="running",
        result_bundle_asset_id=None,
        provenance_receipt_id=None,
    )
    assert set(record.to_dict()) == {
        "child_job_id",
        "parent_job_id",
        "parent_step_id",
        "parent_attempt_id",
        "invoke_operation_key",
        "binding_id",
        "broker_invocation_asset_id",
        "broker_invocation_hash",
        "child_run_snapshot_asset_id",
        "child_run_snapshot_hash",
        "result_contract",
        "required",
        "propagate_cancel",
        "state",
        "result_bundle_asset_id",
        "provenance_receipt_id",
    }
    assert BrokerChildRecord.from_mapping(record.to_dict()) == record


def test_invoke_ack_loss_replay_is_value_equivalent_and_drift_is_duplicate_request():
    broker, core, input_id, parameters_id, _execution, factory = _broker()
    caller = _context()
    first = broker.invoke(
        caller,
        operation_key="invoke-1",
        binding_id="binding-1",
        input_asset_id=input_id,
        parameters_asset_id=parameters_id,
    )
    asset_count_after_first = len(core.assets)
    replay = broker.invoke(
        caller,
        operation_key="invoke-1",
        binding_id="binding-1",
        input_asset_id=input_id,
        parameters_asset_id=parameters_id,
    )
    assert replay.to_dict() == first.to_dict()
    assert replay.canonical_bytes() == first.canonical_bytes()
    assert len(factory.calls) == 1
    assert len(core.assets) == asset_count_after_first

    other_input = core.create_asset(b"different input", mime="text/plain")
    with pytest.raises(ContractError) as caught:
        broker.invoke(
            caller,
            operation_key="invoke-1",
            binding_id="binding-1",
            input_asset_id=other_input,
            parameters_asset_id=parameters_id,
        )
    assert caught.value.code == int(ErrorCode.DUPLICATE_REQUEST)
    assert len(factory.calls) == 1
    assert len(core.assets) == asset_count_after_first + 1


def test_binding_contract_is_frozen_and_cannot_be_overridden():
    broker, _core, input_id, _parameters_id, _execution, _factory = _broker()
    with pytest.raises(ContractError) as caught:
        broker.invoke(
            _context(),
            operation_key="invoke-2",
            binding_id="binding-1",
            input_asset_id=input_id,
            expected_result_contract="candidate-batch/v1",
        )
    assert caught.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)


def test_child_operation_and_snapshot_attestation_are_bound_without_authority_mutation():
    broker, core, input_id, parameters_id, _execution, factory = _broker()
    result = broker.invoke(
        _context(),
        operation_key="invoke-3",
        binding_id="binding-1",
        input_asset_id=input_id,
        parameters_asset_id=parameters_id,
    )
    record = broker.child_records.get_by_child_job(result.child_job_id)
    assert record is not None
    envelope = BrokerInvocationEnvelope.from_mapping(
        __import__("json").loads(core.assets[record.broker_invocation_asset_id])
    )
    assert envelope.input_asset_id == input_id
    assert envelope.parameters_asset_id == parameters_id
    assert record.broker_invocation_hash == envelope.asset_hash
    assert record.child_run_snapshot_hash == result.child_run_snapshot_hash
    assert factory.calls[0].envelope_asset_id == record.broker_invocation_asset_id
    assert core.staged == {}
    assert core.checkpoints == {}


def test_missing_child_factory_fails_closed_before_fabricating_child_or_envelope():
    core = FakeCoreAuthorityPort()
    input_id = core.create_asset(b"input", mime="text/plain")
    runtime = FakePluginRuntimePort(
        releases={("plugin.writer", "1.2.3"): "release-1"},
        generation_id="generation-1",
    )
    broker = CapabilityBroker.for_test(
        core=core,
        execution=CountingExecution(),
        runtime=runtime,
        bindings={"binding-1": _binding()},
    )
    before = set(core.assets)
    with pytest.raises(ContractError) as caught:
        broker.invoke(
            _context(),
            operation_key="invoke-missing-child-port",
            binding_id="binding-1",
            input_asset_id=input_id,
        )
    assert caught.value.code == int(ErrorCode.INVALID_TRANSITION)
    assert set(core.assets) == before


def test_poll_projects_durable_assets_and_rejects_nonterminal_receipt():
    projection = ProjectionPort(state="running", receipt=None)
    broker, _core, input_id, _parameters_id, _execution, _factory = _broker(projection=projection)
    caller = _context()
    result = broker.invoke(
        caller,
        operation_key="invoke-poll",
        binding_id="binding-1",
        input_asset_id=input_id,
    )
    polled = broker.poll(caller, child_job_id=result.child_job_id, after_job_event_seq=0)
    assert polled["job_snapshot_asset_id"] == "job-snapshot-1"
    assert polled["provenance_receipt_id"] is None
    record = broker.child_records.get_by_child_job(result.child_job_id)
    assert record is not None and record.provenance_receipt_id is None

    projection.receipt = "receipt-invalid-before-terminal"
    with pytest.raises(ContractError) as caught:
        broker.poll(caller, child_job_id=result.child_job_id, after_job_event_seq=1)
    assert caught.value.code == int(ErrorCode.INVALID_TRANSITION)


def test_required_optional_aggregation_and_receipt_propagation():
    def record(job: str, *, required: bool, state: str, receipt: str | None):
        return BrokerChildRecord(
            child_job_id=job,
            parent_job_id="parent-job-1",
            parent_step_id="parent-step-1",
            parent_attempt_id="parent-attempt-1",
            invoke_operation_key=f"invoke-{job}",
            binding_id="binding-1",
            broker_invocation_asset_id=f"envelope-{job}",
            broker_invocation_hash=HASH_A,
            child_run_snapshot_asset_id=f"snapshot-{job}",
            child_run_snapshot_hash=HASH_B,
            result_contract="artifact-bundle/v1",
            required=required,
            propagate_cancel=True,
            state=state,
            result_bundle_asset_id=None,
            provenance_receipt_id=receipt,
        )

    children = (
        record("required-ok", required=True, state="succeeded", receipt="receipt-required"),
        record("optional-failed", required=False, state="failed", receipt="receipt-optional"),
        record("optional-interrupted", required=False, state="interrupted", receipt="receipt-interrupted"),
        record("optional-fenced", required=False, state="fenced", receipt="receipt-fenced"),
        record("optional-terminal", required=False, state="terminal", receipt="receipt-terminal"),
        record("optional-partial", required=False, state="partial", receipt="receipt-partial"),
    )
    aggregate = aggregate_child_outcomes(children)
    assert aggregate.all_terminal is True
    assert aggregate.required_satisfied is True
    assert aggregate.parent_success_allowed is True
    assert aggregate.optional_failures == (
        "optional-failed",
        "optional-interrupted",
        "optional-fenced",
        "optional-terminal",
    )
    propagation = build_receipt_propagation(children)
    assert propagation.child_receipt_ids == (
        "receipt-required",
        "receipt-optional",
        "receipt-interrupted",
        "receipt-fenced",
        "receipt-terminal",
        "receipt-partial",
    )
    assert propagation.required_child_receipt_ids == ("receipt-required",)
    assert propagation.optional_failure_child_job_ids == aggregate.optional_failures

    blocked = aggregate_child_outcomes(
        (*children, record("required-failed", required=True, state="needs_attention", receipt="receipt-failed"))
    )
    assert blocked.required_satisfied is False
    assert blocked.parent_success_allowed is False
    assert blocked.required_blockers == ("required-failed",)
    with pytest.raises(ContractError, match="missing its provenance receipt"):
        aggregate_child_outcomes(
            (record("terminal-without-receipt", required=True, state="failed", receipt=None),)
        )


def test_cancel_propagates_once_and_terminal_child_is_a_noop():
    execution = CountingExecution()
    broker, _core, input_id, _parameters_id, _execution, _factory = _broker(execution=execution)
    caller = _context()
    result = broker.invoke(
        caller,
        operation_key="invoke-cancel",
        binding_id="binding-1",
        input_asset_id=input_id,
    )
    first = broker.propagate_cancel(caller, result.child_job_id)
    replay = broker.propagate_cancel(caller, result.child_job_id)
    assert first is not None and replay is not None
    assert first.to_dict() == replay.to_dict()
    assert len(execution.cancel_calls) == 1
    assert execution.cancel_calls[0][0] == derive_child_cancel_operation_key(
        "invoke-cancel", result.child_job_id
    )

    record = broker.child_records.get_by_child_job(result.child_job_id)
    assert record is not None
    broker.child_records.replace(record.with_projection(state="succeeded"))
    terminal = broker.propagate_cancel(caller, result.child_job_id)
    assert terminal is not None and terminal.terminal_known is True
    assert len(execution.cancel_calls) == 1


def test_cancel_propagation_false_is_not_derived_or_sent():
    execution = CountingExecution()
    broker, _core, input_id, _parameters_id, _execution, _factory = _broker(
        binding=_binding(propagate_cancel=False), execution=execution
    )
    result = broker.invoke(
        _context(),
        operation_key="invoke-no-cancel",
        binding_id="binding-1",
        input_asset_id=input_id,
    )
    assert broker.propagate_cancel(_context(), result.child_job_id) is None
    assert execution.cancel_calls == []


def test_unattested_factory_result_is_rejected_instead_of_faking_snapshot():
    class UnattestedFactory(AttestedChildFactory):
        def create_or_recover_child(self, request):
            self.calls.append(request)
            self.execution.jobs["child-job-unattested"] = []
            return ChildCreationResult(
                child_job_id="child-job-unattested",
                child_step_id="child-step-unattested",
                child_attempt_id="child-attempt-unattested",
                child_lease_epoch=1,
                child_plugin_release_id=request.plugin_release_id,
                child_run_snapshot_asset_id="child-snapshot-unattested",
                child_run_snapshot_hash=HASH_B,
            )

    execution = CountingExecution()
    core = FakeCoreAuthorityPort()
    factory = UnattestedFactory(execution, core)
    broker, _core, input_id, _parameters_id, _execution, _factory = _broker(
        execution=execution, factory=factory
    )
    with pytest.raises(ContractError) as caught:
        broker.invoke(
            _context(),
            operation_key="invoke-unattested",
            binding_id="binding-1",
            input_asset_id=input_id,
        )
    assert caught.value.code == int(ErrorCode.ASSET_ERROR)


def test_invoke_fails_closed_when_the_child_record_or_operation_ledger_is_missing():
    broker, core, input_id, _parameters_id, _execution, factory = _broker()
    caller = _context()
    broker.invoke(caller, operation_key="invoke-orphan-record", binding_id="binding-1", input_asset_id=input_id)
    before_assets = set(core.assets)
    original_ledger = broker.operation_ledger
    broker.operation_ledger = InMemoryBrokerOperationLedger()
    with pytest.raises(ContractError) as caught_record:
        broker.invoke(caller, operation_key="invoke-orphan-record", binding_id="binding-1", input_asset_id=input_id)
    assert caught_record.value.code == int(ErrorCode.INVALID_TRANSITION)
    assert len(factory.calls) == 1
    assert set(core.assets) == before_assets

    broker.child_records = InMemoryBrokerChildRecordStore()
    broker.operation_ledger = original_ledger
    with pytest.raises(ContractError) as caught_ledger:
        broker.invoke(caller, operation_key="invoke-orphan-record", binding_id="binding-1", input_asset_id=input_id)
    assert caught_ledger.value.code == int(ErrorCode.ASSET_ERROR)
    assert len(factory.calls) == 1


def test_factory_business_type_error_is_not_retried_as_a_compatibility_call():
    class TypeErrorFactory(AttestedChildFactory):
        def create_or_recover_child(self, request):
            self.calls.append(request)
            raise TypeError("factory body failure")

    execution = CountingExecution()
    core = FakeCoreAuthorityPort()
    factory = TypeErrorFactory(execution, core)
    broker, _core, input_id, _parameters_id, _execution, _factory = _broker(
        execution=execution,
        factory=factory,
    )
    with pytest.raises(TypeError, match="factory body failure"):
        broker.invoke(_context(), operation_key="invoke-factory-type-error", binding_id="binding-1", input_asset_id=input_id)
    assert len(factory.calls) == 1


def test_production_constructor_requires_authorities_and_preserves_falsey_ports():
    core = FakeCoreAuthorityPort()
    execution = CountingExecution()
    runtime = FakePluginRuntimePort(
        releases={("plugin.writer", "1.2.3"): HASH_A},
        generation_id="generation-1",
    )
    factory = AttestedChildFactory(execution, core)
    required_ports = {
        "attempt_context": InMemoryAttemptContextPort(),
        "operation_ledger": InMemoryBrokerOperationLedger(),
        "child_records": InMemoryBrokerChildRecordStore(),
    }
    for missing, message in (
        ("attempt_context", "authoritative Attempt"),
        ("operation_ledger", "durable operation ledger"),
        ("child_records", "durable child-record"),
    ):
        kwargs = {key: value for key, value in required_ports.items() if key != missing}
        with pytest.raises(ContractError, match=message):
            CapabilityBroker(
                core=core,
                execution=execution,
                runtime=runtime,
                bindings={"binding-1": _binding()},
                child_factory=factory,
                **kwargs,
            )

    with pytest.raises(ContractError, match="test-only InMemory"):
        CapabilityBroker(
            core=core,
            execution=execution,
            runtime=runtime,
            bindings={"binding-1": _binding()},
            child_factory=factory,
            **required_ports,
        )

    class FalseyLedger:
        def __bool__(self):
            return False

        lookup = get_reservation = reserve = attach_envelope = attach_child = record = (
            lambda *args, **kwargs: None
        )

    class FalseyRecords:
        def __bool__(self):
            return False

        get_by_operation = get_by_child_job = save = replace = lambda *args, **kwargs: None

    class FalseyAttempt:
        def __bool__(self):
            return False

        @staticmethod
        def validate_attempt(_context):
            return True

    ledger = FalseyLedger()
    records = FalseyRecords()
    attempt = FalseyAttempt()
    authoritative_ports = {
        "operation_ledger": ledger,
        "child_records": records,
        "attempt_context": attempt,
    }
    for field_name, in_memory in required_ports.items():
        ports = {**authoritative_ports, field_name: in_memory}
        with pytest.raises(ContractError, match="test-only InMemory"):
            CapabilityBroker(
                core=core,
                execution=execution,
                runtime=runtime,
                bindings={"binding-1": _binding()},
                child_factory=factory,
                **ports,
            )
    broker = CapabilityBroker(
        core=core,
        execution=execution,
        runtime=runtime,
        bindings={"binding-1": _binding()},
        child_factory=factory,
        operation_ledger=ledger,
        child_records=records,
        attempt_context=attempt,
    )
    assert broker.operation_ledger is ledger
    assert broker.child_records is records
    assert broker.attempt_context is attempt


def test_post_factory_failure_recovers_reserved_child_without_second_creation():
    class FailOnceRecords(InMemoryBrokerChildRecordStore):
        def __init__(self):
            super().__init__()
            self.failures = 1

        def save(self, record):
            if self.failures:
                self.failures -= 1
                raise RuntimeError("simulated durable store interruption")
            return super().save(record)

    records = FailOnceRecords()
    broker, _core, input_id, _parameters_id, _execution, factory = _broker(records=records)
    with pytest.raises(RuntimeError, match="store interruption"):
        broker.invoke(
            _context(),
            operation_key="invoke-post-factory-failure",
            binding_id="binding-1",
            input_asset_id=input_id,
        )
    result = broker.invoke(
        _context(),
        operation_key="invoke-post-factory-failure",
        binding_id="binding-1",
        input_asset_id=input_id,
    )
    assert result.child_job_id == "child-job-1"
    assert len(factory.created) == 1
    assert len(factory.calls) == 1


def test_terminal_receipt_hash_lineage_and_monotonicity_are_fail_closed():
    class ReceiptPort:
        def __init__(self):
            self.receipts = {}

        def read_receipt(self, receipt_id):
            return self.receipts[receipt_id]

    receipts = ReceiptPort()
    projection = ProjectionPort(state="succeeded", receipt="receipt-child-1")
    broker, _core, input_id, _parameters_id, _execution, factory = _broker(
        projection=projection,
        receipt_port=receipts,
    )
    result = broker.invoke(
        _context(),
        operation_key="invoke-receipt-binding",
        binding_id="binding-1",
        input_asset_id=input_id,
    )
    creation = factory.created["invoke-receipt-binding"]
    receipt = {
        "schema": "provenance-receipt/v1",
        "receipt_id": "receipt-child-1",
        "job_id": creation.child_job_id,
        "step_id": creation.child_step_id,
        "attempt_id": creation.child_attempt_id,
        "lease_epoch": creation.child_lease_epoch,
        "run_snapshot_hash": creation.child_run_snapshot_hash,
        "plugin_id": "plugin.writer",
        "release_id": creation.child_plugin_release_id,
        "package_hash": HASH_A,
        "capability_id": "capability.writer/v1",
        "bundle_id": None,
        "bundle_hash": None,
        "staged_items": [],
        "model_receipt_ids": [],
        "skill_chain_result_refs": [],
        "parent_receipt_ids": [],
        "created_at": "2026-08-27T12:00:00Z",
    }
    receipt["receipt_hash"] = hash_jcs("provenance-receipt/v1", receipt)
    receipts.receipts["receipt-child-1"] = receipt
    broker.poll(_context(), child_job_id=result.child_job_id)

    receipts.receipts["receipt-child-1"] = {**receipt, "receipt_hash": "0" * 64}
    with pytest.raises(ContractError):
        broker.poll(_context(), child_job_id=result.child_job_id)

    receipts.receipts["receipt-child-1"] = receipt
    projection.receipt = None
    with pytest.raises(ContractError, match="requires a provenance receipt"):
        broker.poll(_context(), child_job_id=result.child_job_id)


def test_factory_cannot_publish_terminal_child_without_verified_receipt():
    core = FakeCoreAuthorityPort()
    execution = CountingExecution()
    factory = AttestedChildFactory(execution, core, state="succeeded")
    broker, _owned_core, input_id, _parameters_id, _execution, _factory = _broker(
        execution=execution,
        factory=factory,
    )
    # The supplied factory writes its Snapshot to a different fake authority,
    # so bind it to the broker authority before invoking.
    factory.core = _owned_core
    with pytest.raises(ContractError, match="terminal child"):
        broker.invoke(
            _context(),
            operation_key="invoke-terminal-factory",
            binding_id="binding-1",
            input_asset_id=input_id,
        )
