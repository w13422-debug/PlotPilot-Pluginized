"""Stateless Host RPC success dispatcher over the accepted P2 supervisor.

Durable method handlers are injected.  This module never opens a database,
persists an operation key, constructs a provenance receipt, or reaches into a
supervisor's private session.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from backend.plotpilot_core.supervisor.rpc import RpcEvent
from backend.plotpilot_core.supervisor.models import WorkerTicket
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode
from backend.plotpilot_plugin_sdk.rpc import HOST_METHODS
from backend.plotpilot_plugin_sdk.verifier import validate_rpc_request, validate_rpc_result


class HostRpcHandler(Protocol):
    """Prepare a request-bound result without committing a durable mutation."""

    def __call__(self, request: Mapping[str, Any]) -> PreparedHostRpcResult: ...


@dataclass(frozen=True, slots=True)
class PreparedHostRpcResult:
    """Side-effect-free result plus its post-validation transaction callback."""

    result: Mapping[str, Any]
    commit: Callable[[], None]


class HostRpcSupervisorPort(Protocol):
    def respond_host_request(
        self,
        ticket: WorkerTicket,
        request_id: str,
        result: dict[str, object],
    ) -> None: ...


class HostRpcEventDispositionPort(Protocol):
    """P2-owned non-destructive event view and durable success disposition."""

    def peek_host_events(self, ticket: WorkerTicket) -> tuple[RpcEvent, ...]: ...

    def dispose_host_event(self, ticket: WorkerTicket, request_id: str) -> None: ...


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
        *,
        event_disposition: HostRpcEventDispositionPort | None = None,
    ) -> None:
        unknown = sorted(set(handlers) - set(HOST_METHODS))
        if unknown:
            raise ValueError(f"unknown Host RPC handlers: {', '.join(unknown)}")
        self._supervisor = supervisor
        self._handlers = dict(handlers)
        self._event_disposition = event_disposition

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
        prepared = handler(request)
        if not isinstance(prepared, PreparedHostRpcResult):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Host RPC handler must return PreparedHostRpcResult",
            )
        result = dict(prepared.result)
        validate_rpc_result(method, result, request=request)
        prepared.commit()
        self._supervisor.respond_host_request(ticket, str(request["id"]), result)
        return True

    def dispatch_pending(self, ticket: WorkerTicket) -> RpcDispatchBatch:
        """Handle a non-destructive P2 view and dispose only persisted success.

        The accepted P2 ``drain_events`` API clears the whole batch up front and
        is intentionally not used here.  Until P2 publishes this disposition
        port, batch integration stops before reading or losing an event.
        """

        if self._event_disposition is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "P2 non-destructive Host event disposition port is not composed",
            )
        handled: list[str] = []
        passthrough: list[RpcEvent] = []
        seen_request_ids: set[str] = set()
        for event in self._event_disposition.peek_host_events(ticket):
            request_id = str(event.message.get("id", ""))
            if event.kind == "host_request" and request_id in seen_request_ids:
                passthrough.append(event)
                continue
            if self.dispatch_event(ticket, event):
                seen_request_ids.add(request_id)
                self._event_disposition.dispose_host_event(ticket, request_id)
                handled.append(request_id)
            else:
                passthrough.append(event)
        return RpcDispatchBatch(tuple(handled), tuple(passthrough))
