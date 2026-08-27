"""Closed incremental RPC session for one fenced plugin process."""
from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from plotpilot_plugin_sdk.errors import ContractError, ErrorCode
from plotpilot_plugin_sdk.framing import FrameDecoder, encode_frame
from plotpilot_plugin_sdk.rpc import (
    HOST_METHODS,
    WORKER_METHODS,
    build_meta,
    build_request,
)
from plotpilot_plugin_sdk.verifier import validate_rpc_request, validate_rpc_response

from .models import AttemptFence, InstallFence


@dataclass(frozen=True)
class RpcEvent:
    kind: str
    message: Mapping[str, Any]


class FramedRpcSession:
    """Track request IDs and reject every unbound message shape."""

    def __init__(
        self,
        *,
        plugin_id: str,
        generation_id: str,
        release_id: str,
        expected_capabilities: tuple[str, ...] = (),
        id_factory: Callable[[], str] | None = None,
        attempt_authority: Callable[[AttemptFence], bool] | None = None,
        install_authority: Callable[[InstallFence], bool] | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.generation_id = generation_id
        self.release_id = release_id
        self.expected_capabilities = expected_capabilities
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._attempt_authority = attempt_authority
        self._install_authority = install_authority
        self._decoder = FrameDecoder()
        self._pending: dict[str, Mapping[str, Any]] = {}
        self._inbound: dict[str, Mapping[str, Any]] = {}
        self._handshake_request: Mapping[str, Any] | None = None
        self._handshake_complete = False
        self._worker_instance_id: str | None = None
        self._heartbeat_seq = 0
        self._attempt: AttemptFence | None = None
        self._install: InstallFence | None = None
        self._closed = False

    @property
    def handshake_complete(self) -> bool:
        return self._handshake_complete

    @property
    def worker_instance_id(self) -> str | None:
        return self._worker_instance_id

    @property
    def attempt_fence(self) -> AttemptFence | None:
        return self._attempt

    @property
    def install_fence(self) -> InstallFence | None:
        return self._install

    def bind_attempt(self, fence: AttemptFence) -> None:
        if self._closed or not self._handshake_complete:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Attempt binding requires a ready session")
        if self._attempt_authority is not None and not self._attempt_authority(fence):
            raise ContractError(ErrorCode.STALE_LEASE, "Core authority rejected the Attempt fence")
        self._attempt = fence
        self._heartbeat_seq = 0

    def unbind_attempt(self, fence: AttemptFence) -> None:
        if self._attempt != fence:
            raise ContractError(ErrorCode.STALE_LEASE, "Attempt unbind is stale")
        self._attempt = None
        self._heartbeat_seq = 0

    def bind_install(self, fence: InstallFence) -> None:
        if self._closed or not self._handshake_complete:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "install binding requires a ready session")
        if self._install_authority is not None and not self._install_authority(fence):
            raise ContractError(ErrorCode.STALE_LEASE, "Core authority rejected the install fence")
        self._install = fence

    def unbind_install(self, fence: InstallFence) -> None:
        if self._install != fence:
            raise ContractError(ErrorCode.STALE_LEASE, "install unbind is stale")
        self._install = None

    def _meta(self, *, deadline_at: str) -> dict[str, Any]:
        return build_meta(
            "control",
            generation_id=self.generation_id,
            plugin_release_id=self.release_id,
            deadline_at=deadline_at,
        )

    def begin_handshake(self, *, data_generation_id: str | None, deadline_at: str) -> bytes:
        if self._closed or self._handshake_request is not None:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "handshake may be started exactly once")
        request = build_request(
            "runtime.handshake",
            {
                "host_protocol": "1",
                "generation_id": self.generation_id,
                "plugin_release_id": self.release_id,
                "data_generation_id": data_generation_id,
            },
            self._meta(deadline_at=deadline_at),
            request_id=self._id_factory(),
        )
        validate_rpc_request(request)
        self._handshake_request = request
        self._pending[str(request["id"])] = request
        return encode_frame(request)

    def build_shutdown(self, *, reason: str, deadline_at: str) -> bytes:
        if self._closed or not self._handshake_complete:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "shutdown requires a completed handshake")
        request = build_request(
            "runtime.shutdown",
            {"reason": reason, "deadline_at": deadline_at},
            self._meta(deadline_at=deadline_at),
            request_id=self._id_factory(),
        )
        validate_rpc_request(request)
        self._pending[str(request["id"])] = request
        return encode_frame(request)

    def build_worker_request(
        self,
        method: str,
        params: Mapping[str, Any],
        meta: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> bytes:
        if self._closed or not self._handshake_complete:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "worker request requires a completed handshake")
        if method not in WORKER_METHODS or method in {"runtime.handshake", "runtime.heartbeat"}:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "method is not a callable worker operation")
        request = build_request(method, params, meta, request_id=request_id or self._id_factory())
        self._validate_request(request)
        self._pending[str(request["id"])] = request
        return encode_frame(request)

    def build_host_success(self, request_id: str, result: Mapping[str, Any]) -> bytes:
        if self._closed or not self._handshake_complete:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "Host response requires a completed handshake")
        request = self._inbound.get(request_id)
        if request is None:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Host response id is not pending")
        self._validate_request(request)
        response = {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}
        validate_rpc_response(response, request=request)
        self._inbound.pop(request_id, None)
        return encode_frame(response)

    def _assert_identity(self, message: Mapping[str, Any]) -> None:
        meta = message.get("meta")
        if not isinstance(meta, Mapping):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "RPC message has no closed meta object")
        if meta.get("generation_id") != self.generation_id or meta.get("plugin_release_id") != self.release_id:
            raise ContractError(ErrorCode.STALE_LEASE, "RPC message belongs to another generation or release")

    def _validate_request(self, message: Mapping[str, Any]) -> None:
        meta = message.get("meta")
        if not isinstance(meta, Mapping):
            validate_rpc_request(message)
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "RPC meta is absent")
        context = meta.get("context")
        if context == "attempt":
            fence = self._attempt
            if fence is None:
                raise ContractError(ErrorCode.STALE_LEASE, "no Attempt is bound to this worker session")
            if self._attempt_authority is not None and not self._attempt_authority(fence):
                raise ContractError(ErrorCode.STALE_LEASE, "bound Attempt is no longer authoritative")
            if (
                meta.get("job_id") != fence.job_id
                or meta.get("step_id") != fence.step_id
                or meta.get("attempt_id") != fence.attempt_id
                or meta.get("lease_epoch") != fence.lease_epoch
            ):
                raise ContractError(ErrorCode.STALE_LEASE, "RPC Attempt identity or epoch is stale")
            validate_rpc_request(message, expected_lease_epoch=fence.lease_epoch)
        elif context == "install":
            fence = self._install
            if fence is None:
                raise ContractError(ErrorCode.STALE_LEASE, "no install lease is bound to this worker session")
            if self._install_authority is not None and not self._install_authority(fence):
                raise ContractError(ErrorCode.STALE_LEASE, "bound install lease is no longer authoritative")
            if (
                meta.get("install_operation_id") != fence.install_operation_id
                or meta.get("install_lease_epoch") != fence.install_lease_epoch
            ):
                raise ContractError(ErrorCode.STALE_LEASE, "RPC install identity or epoch is stale")
            params = message.get("params")
            if isinstance(params, Mapping) and "owner_instance_id" in params and params["owner_instance_id"] != fence.owner_instance_id:
                raise ContractError(ErrorCode.STALE_LEASE, "RPC install owner is stale")
            validate_rpc_request(message, expected_lease_epoch=fence.install_lease_epoch)
        elif context == "control":
            validate_rpc_request(message)
        else:
            validate_rpc_request(message)
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "unknown RPC context")
        self._assert_identity(message)

    def _accept(self, message: Mapping[str, Any]) -> RpcEvent:
        if not self._handshake_complete:
            request = self._handshake_request
            if request is None or message.get("id") != request["id"] or "method" in message:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "first worker message must be the bound handshake response")
            validate_rpc_response(message, request=request)
            self._pending.pop(str(request["id"]), None)
            if "error" in message:
                raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "worker rejected runtime.handshake")
            result = message["result"]
            if result["plugin_id"] != self.plugin_id:
                raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "handshake plugin_id differs from immutable route")
            if tuple(result["capabilities"]) != self.expected_capabilities:
                raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "handshake capabilities differ from verified manifest")
            self._worker_instance_id = result["worker_instance_id"]
            self._handshake_complete = True
            return RpcEvent("handshake", message)

        if "method" in message:
            method = message.get("method")
            self._validate_request(message)
            if method == "runtime.heartbeat":
                params = message["params"]
                if params["worker_instance_id"] != self._worker_instance_id:
                    raise ContractError(ErrorCode.STALE_LEASE, "heartbeat worker_instance_id is stale")
                seq = params["local_seq"]
                if seq <= self._heartbeat_seq:
                    raise ContractError(ErrorCode.INVALID_TRANSITION, "heartbeat local_seq must increase")
                self._heartbeat_seq = seq
                return RpcEvent("heartbeat", message)
            if method not in HOST_METHODS:
                raise ContractError(ErrorCode.INVALID_TRANSITION, "worker may call only published Host RPC methods")
            request_id = str(message["id"])
            if request_id in self._inbound:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "worker reused a pending Host RPC id")
            self._inbound[request_id] = message
            return RpcEvent("host_request", message)

        request_id = message.get("id")
        request = self._pending.get(str(request_id))
        if request is None:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "unbound RPC response id")
        self._validate_request(request)
        validate_rpc_response(message, request=request)
        self._pending.pop(str(request_id), None)
        return RpcEvent("response", message)

    def feed(self, data: bytes) -> tuple[RpcEvent, ...]:
        if self._closed:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "RPC session is closed")
        try:
            return tuple(self._accept(message) for message in self._decoder.feed(data))
        except Exception:
            self._closed = True
            raise

    def close(self) -> None:
        self._closed = True

    def finish(self) -> None:
        """Close at EOF and reject every truncated header/body or tail byte."""

        self._closed = True
        if self._decoder.buffer:
            raise ContractError(ErrorCode.ASSET_ERROR, "RPC stream ended with a truncated or trailing frame")


__all__ = ["FramedRpcSession", "RpcEvent"]
