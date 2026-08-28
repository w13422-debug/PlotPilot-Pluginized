"""Stateless Host RPC success dispatcher over the accepted P2 supervisor.

Durable method handlers are injected.  This module never opens a database,
persists an operation key, constructs a provenance receipt, or reaches into a
supervisor's private session.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from backend.plotpilot_core.supervisor.rpc import RpcEvent
from backend.plotpilot_core.supervisor.models import WorkerTicket
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode
from backend.plotpilot_plugin_sdk.rpc import HOST_METHODS
from backend.plotpilot_plugin_sdk.verifier import validate_rpc_request, validate_rpc_result


class HostRpcHandler(Protocol):
    """A durable application handler; retries must replay its first result."""

    def __call__(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class HostRpcSupervisorPort(Protocol):
    def drain_events(self, ticket: WorkerTicket) -> tuple[RpcEvent, ...]: ...

    def respond_host_request(
        self,
        ticket: WorkerTicket,
        request_id: str,
        result: dict[str, object],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RpcDispatchBatch:
    handled_request_ids: tuple[str, ...]
    passthrough: tuple[RpcEvent, ...]


class HostRpcApplicationDispatcher:
    """Bind validated Host requests to durable handlers and P2 ACK writes."""

    def __init__(
        self,
        supervisor: HostRpcSupervisorPort,
        handlers: Mapping[str, HostRpcHandler],
    ) -> None:
        unknown = sorted(set(handlers) - set(HOST_METHODS))
        if unknown:
            raise ValueError(f"unknown Host RPC handlers: {', '.join(unknown)}")
        self._supervisor = supervisor
        self._handlers = dict(handlers)

    def dispatch_event(self, ticket: WorkerTicket, event: RpcEvent) -> bool:
        """Dispatch one request; return ``False`` for unrelated P2 events.

        P2 automatically writes ``host_replay`` frames.  A
        ``host_request_duplicate`` means the first request is still pending,
        so it is not executed twice.  The dispatcher owns no retry ledger.
        """

        if event.kind != "host_request":
            return False
        request = event.message
        validate_rpc_request(request)
        method = str(request.get("method", ""))
        handler = self._handlers.get(method)
        if handler is None:
            raise ContractError(ErrorCode.INVALID_TRANSITION, f"Host RPC method is not composed: {method}")
        result = dict(handler(request))
        validate_rpc_result(method, result)
        self._supervisor.respond_host_request(ticket, str(request["id"]), result)
        return True

    def drain_and_dispatch(self, ticket: WorkerTicket) -> RpcDispatchBatch:
        handled: list[str] = []
        passthrough: list[RpcEvent] = []
        for event in self._supervisor.drain_events(ticket):
            if self.dispatch_event(ticket, event):
                handled.append(str(event.message["id"]))
            else:
                passthrough.append(event)
        return RpcDispatchBatch(tuple(handled), tuple(passthrough))
