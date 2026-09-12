from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from test_model_broker import (
    CALLER_RELEASE_ID,
    FakeProviderPort,
    _broker,
    _build_authority,
    _host_request,
    _row,
)

from backend.plotpilot_core.jobs.http_rpc.dispatcher import HostRpcApplicationDispatcher
from backend.plotpilot_core.jobs.http_rpc.model_handlers import (
    build_model_host_handlers,
)
from backend.plotpilot_core.supervisor.models import WorkerTicket
from backend.plotpilot_core.supervisor.rpc import RpcEvent
from backend.plotpilot_plugin_sdk import ErrorCode


class RecordingSupervisor:
    def __init__(self, stack, *, fail_ack: bool = False) -> None:
        self.stack = stack
        self.fail_ack = fail_ack
        self.successes: list[tuple[str, dict[str, Any], str]] = []
        self.errors: list[tuple[str, int, str]] = []

    def respond_host_request(self, ticket, request_id, result) -> None:
        del ticket
        state = str(_row(self.stack)["state"])
        self.successes.append((request_id, dict(result), state))
        if self.fail_ack:
            self.fail_ack = False
            raise BrokenPipeError("injected ACK loss")

    def respond_host_error(
        self,
        ticket,
        request_id,
        *,
        code,
        message,
        error_id,
        retryable=False,
        details_asset_id=None,
    ) -> None:
        del ticket, message, error_id, retryable, details_asset_id
        self.errors.append((request_id, int(code), str(_row(self.stack)["state"])))


def _ticket() -> WorkerTicket:
    return WorkerTicket("worker-1", "lifecycle-1", "retain-1", CALLER_RELEASE_ID, 1, 1)


def test_host_model_handler_commits_t3_before_success_ack(tmp_path: Path) -> None:
    stack = _build_authority(tmp_path, "handler-success")
    port = FakeProviderPort(stack)
    broker = _broker(stack, port)
    supervisor = RecordingSupervisor(stack)
    dispatcher = HostRpcApplicationDispatcher(
        supervisor, build_model_host_handlers(broker)
    )
    request = _host_request(stack)
    try:
        assert dispatcher.dispatch_event(_ticket(), RpcEvent("host_request", request))
        assert supervisor.successes == [
            (request["id"], {"state": "received", "response_asset_id": _row(stack)["response_asset_id"], "receipt_id": _row(stack)["receipt_asset_id"], "uncertainty": None}, "received")
        ]
        assert not supervisor.errors
        assert port.invoke_calls == 1
    finally:
        stack.close()


def test_receiptless_failure_is_durable_before_host_error_ack(tmp_path: Path) -> None:
    stack = _build_authority(tmp_path, "handler-error")
    port = FakeProviderPort(stack, mode="raise")
    broker = _broker(stack, port)
    supervisor = RecordingSupervisor(stack)
    dispatcher = HostRpcApplicationDispatcher(
        supervisor, build_model_host_handlers(broker)
    )
    request = _host_request(stack)
    try:
        assert dispatcher.dispatch_event(_ticket(), RpcEvent("host_request", request))
        assert supervisor.errors == [
            (request["id"], int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT), "uncertain")
        ]
        assert not supervisor.successes
        assert port.invoke_calls == 1
    finally:
        stack.close()


def test_ack_loss_replays_terminal_without_provider_call(tmp_path: Path) -> None:
    stack = _build_authority(tmp_path, "handler-ack-loss")
    port = FakeProviderPort(stack)
    broker = _broker(stack, port)
    supervisor = RecordingSupervisor(stack, fail_ack=True)
    dispatcher = HostRpcApplicationDispatcher(
        supervisor, build_model_host_handlers(broker)
    )
    request = _host_request(stack)
    try:
        with pytest.raises(BrokenPipeError, match="ACK loss"):
            dispatcher.dispatch_event(_ticket(), RpcEvent("host_request", request))
        assert _row(stack)["state"] == "received"
        assert port.invoke_calls == 1
        assert dispatcher.dispatch_event(
            _ticket(), RpcEvent("host_request", _host_request(stack))
        )
        assert port.invoke_calls == 1
        assert [state for _request_id, _result, state in supervisor.successes] == [
            "received",
            "received",
        ]
    finally:
        stack.close()
