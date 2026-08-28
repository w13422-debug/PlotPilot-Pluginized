from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from backend.plotpilot_core.jobs.http_rpc import HostRpcApplicationDispatcher
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


@dataclass
class FakeSupervisor:
    events: tuple[RpcEvent, ...] = ()
    responses: list[tuple[WorkerTicket, str, dict[str, object]]] = field(default_factory=list)

    def drain_events(self, ticket):
        del ticket
        events, self.events = self.events, ()
        return events

    def respond_host_request(self, ticket, request_id, result):
        self.responses.append((ticket, request_id, result))


def test_dispatcher_maps_one_validated_request_to_one_supervisor_success():
    request = _request()
    supervisor = FakeSupervisor((RpcEvent("host_request", request),))
    calls = []

    def handler(message):
        calls.append(message)
        return {"accepted": True, "job_event_seq": 7}

    dispatcher = HostRpcApplicationDispatcher(
        supervisor,
        {"host.job.event/v1": handler},
    )
    batch = dispatcher.drain_and_dispatch(_ticket())

    assert calls == [request]
    assert batch.handled_request_ids == (request["id"],)
    assert batch.passthrough == ()
    assert supervisor.responses == [
        (_ticket(), request["id"], {"accepted": True, "job_event_seq": 7})
    ]


def test_pending_duplicate_is_not_executed_twice():
    request = _request()
    supervisor = FakeSupervisor((RpcEvent("host_request_duplicate", request),))
    calls = []
    dispatcher = HostRpcApplicationDispatcher(
        supervisor,
        {"host.job.event/v1": lambda value: calls.append(value) or {"accepted": True, "job_event_seq": 1}},
    )

    batch = dispatcher.drain_and_dispatch(_ticket())

    assert calls == []
    assert supervisor.responses == []
    assert batch.passthrough == (RpcEvent("host_request_duplicate", request),)


def test_uncomposed_method_and_wrong_result_fail_before_supervisor_write():
    request = _request()
    supervisor = FakeSupervisor()
    dispatcher = HostRpcApplicationDispatcher(supervisor, {})
    with pytest.raises(ContractError) as missing:
        dispatcher.dispatch_event(_ticket(), RpcEvent("host_request", request))
    assert missing.value.code == int(ErrorCode.INVALID_TRANSITION)

    dispatcher = HostRpcApplicationDispatcher(
        supervisor,
        {"host.job.event/v1": lambda _value: {"accepted": True, "dropped": False}},
    )
    with pytest.raises(ContractError) as wrong:
        dispatcher.dispatch_event(_ticket(), RpcEvent("host_request", request))
    assert wrong.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)
    assert supervisor.responses == []


def test_constructor_rejects_non_host_handler_registration():
    with pytest.raises(ValueError, match="unknown Host RPC handlers"):
        HostRpcApplicationDispatcher(FakeSupervisor(), {"job.start": lambda _value: {}})
