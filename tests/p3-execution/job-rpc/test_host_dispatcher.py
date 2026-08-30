from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from backend.plotpilot_core.jobs.http_rpc import (
    HostRpcApplicationDispatcher,
    PreparedHostRpcResult,
)
from backend.plotpilot_core.supervisor.models import WorkerTicket
from backend.plotpilot_core.supervisor.rpc import RpcEvent
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode
from backend.plotpilot_plugin_sdk.rpc import build_meta, build_request


def _ticket() -> WorkerTicket:
    return WorkerTicket("worker-1", "lifecycle-1", "retain-1", "rel-a", 1, 0)


def _request() -> dict:
    return build_request(
        "host.job.event/v1",
        {
            "operation_key": "event-op-1",
            "event_type": "plugin.com.plotpilot.alpha.progress",
            "payload_asset_id": None,
            "local_seq": 1,
        },
        build_meta(
            "attempt",
            generation_id="dg-a",
            plugin_release_id="e" * 64,
            deadline_at="2026-08-28T18:00:00Z",
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
        ),
        request_id="00000000-0000-4000-8000-000000000001",
    )


def _prepared(result, commit=lambda: None):
    return PreparedHostRpcResult(result, commit)


@dataclass
class FakeSupervisor:
    responses: list[tuple[WorkerTicket, str, dict[str, object]]] = field(default_factory=list)

    def respond_host_request(self, ticket, request_id, result):
        self.responses.append((ticket, request_id, result))


@dataclass
class FakeDisposition:
    events: list[RpcEvent] = field(default_factory=list)
    disposed: list[str] = field(default_factory=list)

    def peek_host_events(self, ticket):
        del ticket
        return tuple(self.events)

    def dispose_host_event(self, ticket, request_id):
        del ticket
        self.disposed.append(request_id)
        self.events = [
            event
            for event in self.events
            if not (event.kind == "host_request" and event.message.get("id") == request_id)
        ]


def test_dispatcher_maps_one_validated_request_to_one_supervisor_success():
    request = _request()
    supervisor = FakeSupervisor()
    disposition = FakeDisposition([RpcEvent("host_request", request)])
    calls = []
    commits = []

    def handler(message):
        calls.append(message)
        return _prepared(
            {"accepted": True, "job_event_seq": 7},
            lambda: commits.append(message["id"]),
        )

    dispatcher = HostRpcApplicationDispatcher(
        supervisor,
        {"host.job.event/v1": handler},
        event_disposition=disposition,
    )
    batch = dispatcher.dispatch_pending(_ticket())

    assert calls == [request]
    assert commits == [request["id"]]
    assert batch.handled_request_ids == (request["id"],)
    assert batch.passthrough == ()
    assert supervisor.responses == [
        (_ticket(), request["id"], {"accepted": True, "job_event_seq": 7})
    ]
    assert disposition.disposed == [request["id"]]
    assert disposition.events == []


def test_pending_duplicate_is_not_executed_twice():
    request = _request()
    supervisor = FakeSupervisor()
    disposition = FakeDisposition([RpcEvent("host_request_duplicate", request)])
    calls = []
    dispatcher = HostRpcApplicationDispatcher(
        supervisor,
        {
            "host.job.event/v1": lambda value: calls.append(value)
            or _prepared({"accepted": True, "job_event_seq": 1})
        },
        event_disposition=disposition,
    )

    batch = dispatcher.dispatch_pending(_ticket())

    assert calls == []
    assert supervisor.responses == []
    assert batch.passthrough == (RpcEvent("host_request_duplicate", request),)
    assert disposition.disposed == []
    assert disposition.events == [RpcEvent("host_request_duplicate", request)]


def test_uncomposed_method_and_wrong_result_fail_before_supervisor_write():
    request = _request()
    supervisor = FakeSupervisor()
    dispatcher = HostRpcApplicationDispatcher(supervisor, {})
    with pytest.raises(ContractError) as missing:
        dispatcher.dispatch_event(_ticket(), RpcEvent("host_request", request))
    assert missing.value.code == int(ErrorCode.INVALID_TRANSITION)

    dispatcher = HostRpcApplicationDispatcher(
        supervisor,
        {
            "host.job.event/v1": lambda _value: _prepared(
                {"accepted": True, "dropped": False}
            )
        },
    )
    with pytest.raises(ContractError) as wrong:
        dispatcher.dispatch_event(_ticket(), RpcEvent("host_request", request))
    assert wrong.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)
    assert supervisor.responses == []


def test_constructor_rejects_non_host_handler_registration():
    with pytest.raises(ValueError, match="unknown Host RPC handlers"):
        HostRpcApplicationDispatcher(FakeSupervisor(), {"job.start": lambda _value: {}})


@pytest.mark.parametrize("failure_index", [0, 1])
def test_handler_failure_retains_failed_and_trailing_events(failure_index):
    requests = []
    for index in range(1, 4):
        request = _request()
        request["id"] = f"00000000-0000-4000-8000-{index:012d}"
        request["params"] = {
            **request["params"],
            "operation_key": f"event-op-{index}",
            "local_seq": index,
        }
        requests.append(request)
    disposition = FakeDisposition(
        [RpcEvent("host_request", request) for request in requests]
    )
    supervisor = FakeSupervisor()
    calls = []

    def handler(request):
        calls.append(request["id"])
        if len(calls) - 1 == failure_index:
            raise RuntimeError("injected handler failure")
        return _prepared({"accepted": True, "job_event_seq": len(calls)})

    dispatcher = HostRpcApplicationDispatcher(
        supervisor,
        {"host.job.event/v1": handler},
        event_disposition=disposition,
    )
    with pytest.raises(RuntimeError, match="injected"):
        dispatcher.dispatch_pending(_ticket())

    assert disposition.disposed == [request["id"] for request in requests[:failure_index]]
    assert disposition.events == [
        RpcEvent("host_request", request) for request in requests[failure_index:]
    ]
    assert [response[1] for response in supervisor.responses] == [
        request["id"] for request in requests[:failure_index]
    ]


def test_empty_request_id_is_rejected_without_handler_ack_or_disposition():
    request = _request()
    request["id"] = ""
    disposition = FakeDisposition([RpcEvent("host_request", request)])
    supervisor = FakeSupervisor()
    calls = []
    dispatcher = HostRpcApplicationDispatcher(
        supervisor,
        {"host.job.event/v1": lambda value: calls.append(value) or _prepared({})},
        event_disposition=disposition,
    )
    with pytest.raises(ContractError):
        dispatcher.dispatch_pending(_ticket())
    assert calls == []
    assert supervisor.responses == []
    assert disposition.disposed == []


def test_request_bound_result_mismatch_has_no_ack_or_disposition():
    request = build_request(
        "host.capability.invoke/v1",
        {
            "operation_key": "invoke-1",
            "binding_id": "binding-1",
            "input_asset_id": "asset-input",
            "parameters_asset_id": None,
            "expected_result_contract": "candidate-batch/v1",
            "propagate_cancel": True,
        },
        build_meta(
            "attempt",
            generation_id="dg-a",
            plugin_release_id="e" * 64,
            deadline_at="2026-08-28T18:00:00Z",
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
        ),
        request_id="00000000-0000-4000-8000-000000000099",
    )
    wrong = {
        "accepted": True,
        "child_job_id": "child-job-1",
        "child_step_id": "child-step-1",
        "child_run_snapshot_asset_id": "asset-child-snapshot",
        "child_run_snapshot_hash": "a" * 64,
        "child_result_contract": "artifact-bundle/v1",
        "child_job_event_seq": 0,
    }
    disposition = FakeDisposition([RpcEvent("host_request", request)])
    supervisor = FakeSupervisor()
    mutations = []
    dispatcher = HostRpcApplicationDispatcher(
        supervisor,
        {
            "host.capability.invoke/v1": lambda _request: _prepared(
                wrong, lambda: mutations.append("committed")
            )
        },
        event_disposition=disposition,
    )
    with pytest.raises(ContractError) as caught:
        dispatcher.dispatch_pending(_ticket())
    assert caught.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)
    assert supervisor.responses == []
    assert disposition.disposed == []
    assert mutations == []


def test_batch_dispatch_stops_before_destructive_p2_drain_is_available():
    dispatcher = HostRpcApplicationDispatcher(FakeSupervisor(), {})
    with pytest.raises(ContractError, match="non-destructive"):
        dispatcher.dispatch_pending(_ticket())
