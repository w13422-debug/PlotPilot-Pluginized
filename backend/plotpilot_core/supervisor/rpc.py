"""Closed incremental RPC session for one fenced plugin process."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from plotpilot_plugin_sdk.errors import ContractError, ErrorCode
from plotpilot_plugin_sdk.framing import FrameDecoder, decode_frame, encode_frame
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


@dataclass
class _InboundCall:
    request: Mapping[str, Any]
    response_frame: bytes | None = None
    written: bool = False


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
        durable_host_replay: Callable[[Mapping[str, Any]], bytes | None] | None = None,
        pending_limit: int = 64,
        inbound_limit: int = 64,
    ) -> None:
        self.plugin_id = plugin_id
        self.generation_id = generation_id
        self.release_id = release_id
        self.expected_capabilities = expected_capabilities
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._attempt_authority = attempt_authority
        self._install_authority = install_authority
        self._durable_host_replay = durable_host_replay
        self._decoder = FrameDecoder()
        if (
            not isinstance(pending_limit, int)
            or isinstance(pending_limit, bool)
            or pending_limit < 1
            or not isinstance(inbound_limit, int)
            or isinstance(inbound_limit, bool)
            or inbound_limit < 1
        ):
            raise ValueError("RPC queue limits must be positive")
        self._pending_limit = pending_limit
        self._inbound_limit = inbound_limit
        self._pending: dict[str, Mapping[str, Any]] = {}
        self._worker_replay_frames: dict[str, bytes | None] = {}
        self._inbound: dict[str, _InboundCall] = {}
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

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def inbound_count(self) -> int:
        return len(self._inbound)

    def bind_attempt(
        self, fence: AttemptFence, *, authority_checked: bool = False
    ) -> None:
        if self._closed or not self._handshake_complete:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION, "Attempt binding requires a ready session"
            )
        if (
            not authority_checked
            and self._attempt_authority is not None
            and not self._attempt_authority(fence)
        ):
            raise ContractError(
                ErrorCode.STALE_LEASE, "Core authority rejected the Attempt fence"
            )
        self._attempt = fence
        self._heartbeat_seq = 0

    def unbind_attempt(self, fence: AttemptFence) -> None:
        if self._attempt != fence:
            raise ContractError(ErrorCode.STALE_LEASE, "Attempt unbind is stale")
        self._attempt = None
        self._heartbeat_seq = 0

    def bind_install(
        self, fence: InstallFence, *, authority_checked: bool = False
    ) -> None:
        if self._closed or not self._handshake_complete:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION, "install binding requires a ready session"
            )
        if (
            not authority_checked
            and self._install_authority is not None
            and not self._install_authority(fence)
        ):
            raise ContractError(
                ErrorCode.STALE_LEASE, "Core authority rejected the install fence"
            )
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

    def _reserve_pending(self, request: Mapping[str, Any]) -> None:
        request_id = str(request["id"])
        if request_id in self._pending:
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST, "outbound RPC id is already pending"
            )
        if len(self._pending) >= self._pending_limit:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION, "outbound RPC pending capacity exceeded"
            )
        self._pending[request_id] = request

    def begin_handshake(
        self, *, data_generation_id: str | None, deadline_at: str
    ) -> bytes:
        if self._closed or self._handshake_request is not None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION, "handshake may be started exactly once"
            )
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
        self._reserve_pending(request)
        self._handshake_request = request
        return encode_frame(request)

    def build_shutdown(self, *, reason: str, deadline_at: str) -> bytes:
        if self._closed or not self._handshake_complete:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION, "shutdown requires a completed handshake"
            )
        request = build_request(
            "runtime.shutdown",
            {"reason": reason, "deadline_at": deadline_at},
            self._meta(deadline_at=deadline_at),
            request_id=self._id_factory(),
        )
        validate_rpc_request(request)
        self._reserve_pending(request)
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
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "worker request requires a completed handshake",
            )
        if method not in WORKER_METHODS or method in {
            "runtime.handshake",
            "runtime.heartbeat",
        }:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "method is not a callable worker operation",
            )
        request = build_request(
            method, params, meta, request_id=request_id or self._id_factory()
        )
        self._validate_request(request)
        # ``secrets`` is a one-shot transport field for job.start/job.resume.
        # Keep the exact bytes only in this stack frame; the pending response
        # correlation retains the same validated request with the secret list
        # scrubbed.  Response validation is method/identity bound and never
        # needs the secret values.
        retained_request: Mapping[str, Any] = request
        one_shot_secrets = False
        request_params = request.get("params")
        if (
            method in {"job.start", "job.resume"}
            and isinstance(request_params, Mapping)
            and "secrets" in request_params
        ):
            one_shot_secrets = bool(request_params.get("secrets"))
            retained_params = dict(request_params)
            retained_params["secrets"] = []
            retained_request = {**request, "params": retained_params}
        request_key = str(request["id"])
        existing = self._pending.get(request_key)
        if existing is not None:
            if existing != retained_request:
                raise ContractError(
                    ErrorCode.DUPLICATE_REQUEST,
                    "outbound RPC id is already bound to another request",
                )
            replay = self._worker_replay_frames.get(request_key)
            if replay is None:
                raise ContractError(
                    ErrorCode.UNCERTAIN_EXTERNAL_EFFECT,
                    "one-shot secret Worker request cannot be replayed automatically",
                )
            return replay
        frame = encode_frame(request)
        self._reserve_pending(retained_request)
        # Requests carrying secret values are deliberately not retained as
        # replayable bytes.  A transport-uncertain retry must be reconciled by
        # policy/manual recovery rather than resending credentials.
        self._worker_replay_frames[request_key] = None if one_shot_secrets else frame
        return frame

    def build_host_success(self, request_id: str, result: Mapping[str, Any]) -> bytes:
        if self._closed or not self._handshake_complete:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Host response requires a completed handshake",
            )
        call = self._inbound.get(request_id)
        if call is None:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH, "Host response id is not pending"
            )
        response = {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}
        validate_rpc_response(response, request=call.request)
        frame = encode_frame(response)
        if call.response_frame is not None:
            if call.response_frame != frame:
                raise ContractError(
                    ErrorCode.DUPLICATE_REQUEST,
                    "Host response differs from canonical first response",
                )
            return call.response_frame
        call.response_frame = frame
        return frame

    def build_host_error(
        self,
        request_id: str,
        *,
        code: int,
        message: str,
        error_id: str,
        retryable: bool = False,
        details_asset_id: str | None = None,
    ) -> bytes:
        """Retain one canonical, request-bound Host RPC error frame."""

        if self._closed or not self._handshake_complete:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Host response requires a completed handshake",
            )
        call = self._inbound.get(request_id)
        if call is None:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH, "Host response id is not pending"
            )
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": int(code),
                "message": message,
                "data": {
                    "error_id": error_id,
                    "retryable": retryable,
                    "details_asset_id": details_asset_id,
                },
            },
        }
        validate_rpc_response(response, request=call.request)
        frame = encode_frame(response)
        if call.response_frame is not None:
            if call.response_frame != frame:
                raise ContractError(
                    ErrorCode.DUPLICATE_REQUEST,
                    "Host response differs from canonical first response",
                )
            return call.response_frame
        call.response_frame = frame
        return frame

    def mark_host_response_written(self, request_id: str, frame: bytes) -> None:
        call = self._inbound.get(request_id)
        if call is None or call.response_frame != frame:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Host response write does not match canonical frame",
            )
        call.written = True

    def mark_host_success_written(self, request_id: str, frame: bytes) -> None:
        """Backward-compatible name for the generic response write marker."""

        self.mark_host_response_written(request_id, frame)

    def host_replay_frame(self, request_id: str) -> bytes | None:
        call = self._inbound.get(request_id)
        return None if call is None else call.response_frame

    def has_staged_host_success(self, request_id: str) -> bool:
        """Return whether the retained replay bytes are one canonical success ACK."""

        call = self._inbound.get(request_id)
        frame = self.host_replay_frame(request_id)
        if call is None or frame is None:
            return False
        try:
            response = decode_frame(frame)
            validate_rpc_response(response, request=call.request)
        except Exception:  # noqa: BLE001 -- malformed retained bytes fail closed
            return False
        return (
            "result" in response
            and "error" not in response
            and encode_frame(response) == frame
        )

    def has_staged_host_response(self, request_id: str) -> bool:
        """Return whether a canonical success or error response is retained."""

        call = self._inbound.get(request_id)
        frame = self.host_replay_frame(request_id)
        if call is None or frame is None:
            return False
        try:
            response = decode_frame(frame)
            validate_rpc_response(response, request=call.request)
        except Exception:  # noqa: BLE001 -- malformed retained bytes fail closed
            return False
        return encode_frame(response) == frame

    def has_host_request(self, request_id: str) -> bool:
        """Return whether an admitted inbound id is a frozen Host RPC request."""

        call = self._inbound.get(request_id)
        return call is not None and call.request.get("method") in HOST_METHODS

    def _reserve_inbound(self, request_id: str, call: _InboundCall) -> None:
        if len(self._inbound) >= self._inbound_limit:
            written_id = next(
                (key for key, value in self._inbound.items() if value.written), None
            )
            if written_id is None:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION, "inbound Host RPC capacity exceeded"
                )
            self._inbound.pop(written_id)
        self._inbound[request_id] = call

    def _durable_replay(self, message: Mapping[str, Any]) -> bytes | None:
        if self._durable_host_replay is None:
            return None
        frame = self._durable_host_replay(message)
        if frame is None:
            return None
        try:
            response = decode_frame(frame)
            validate_rpc_response(response, request=message)
        except Exception as exc:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "durable Host replay frame is invalid",
            ) from exc
        if encode_frame(response) != frame:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "durable Host replay frame is not canonical",
            )
        return frame

    def has_terminal_attempt_commit(self, fence: AttemptFence) -> bool:
        terminal_methods = {"host.job.await_user/v1", "host.job.complete/v1"}
        for request_id, call in self._inbound.items():
            meta = call.request.get("meta")
            if (
                self.has_staged_host_success(request_id)
                and call.request.get("method") in terminal_methods
                and isinstance(meta, Mapping)
                and meta.get("job_id") == fence.job_id
                and meta.get("step_id") == fence.step_id
                and meta.get("attempt_id") == fence.attempt_id
                and meta.get("lease_epoch") == fence.lease_epoch
            ):
                return True
        return False

    def has_terminal_install_commit(self, fence: InstallFence) -> bool:
        for request_id, call in self._inbound.items():
            meta = call.request.get("meta")
            if (
                self.has_staged_host_success(request_id)
                and call.request.get("method") == "host.migration.lease.release/v1"
                and isinstance(meta, Mapping)
                and meta.get("install_operation_id") == fence.install_operation_id
                and meta.get("install_lease_epoch") == fence.install_lease_epoch
            ):
                return True
        return False

    def stage_terminal_replay(
        self,
        *,
        attempt: AttemptFence | None = None,
        install: InstallFence | None = None,
    ) -> tuple[str, bytes] | None:
        """Recover one exact durable terminal ACK after authority was consumed."""

        if (attempt is None) == (install is None):
            raise ValueError("exactly one terminal fence is required")
        for request_id, call in self._inbound.items():
            meta = call.request.get("meta")
            if not isinstance(meta, Mapping):
                continue
            attempt_match = (
                attempt is not None
                and call.request.get("method")
                in {"host.job.await_user/v1", "host.job.complete/v1"}
                and meta.get("job_id") == attempt.job_id
                and meta.get("step_id") == attempt.step_id
                and meta.get("attempt_id") == attempt.attempt_id
                and meta.get("lease_epoch") == attempt.lease_epoch
            )
            install_match = (
                install is not None
                and call.request.get("method") == "host.migration.lease.release/v1"
                and meta.get("install_operation_id") == install.install_operation_id
                and meta.get("install_lease_epoch") == install.install_lease_epoch
            )
            if not attempt_match and not install_match:
                continue
            frame = call.response_frame or self._durable_replay(call.request)
            if frame is None:
                return None
            call.response_frame = frame
            return request_id, frame
        return None

    def terminal_commit_write_pending(self) -> bool:
        """Return whether a staged terminal mutation ACK has not reached stdin."""

        terminal_methods = {
            "host.job.await_user/v1",
            "host.job.complete/v1",
            "host.migration.lease.release/v1",
        }
        return any(
            call.request.get("method") in terminal_methods
            and self.has_staged_host_success(request_id)
            and not call.written
            for request_id, call in self._inbound.items()
        )

    def _assert_identity(self, message: Mapping[str, Any]) -> None:
        meta = message.get("meta")
        if not isinstance(meta, Mapping):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "RPC message has no closed meta object",
            )
        if (
            meta.get("generation_id") != self.generation_id
            or meta.get("plugin_release_id") != self.release_id
        ):
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "RPC message belongs to another generation or release",
            )

    def _validate_request(self, message: Mapping[str, Any]) -> None:
        meta = message.get("meta")
        if not isinstance(meta, Mapping):
            validate_rpc_request(message)
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH, "RPC meta is absent"
            )
        context = meta.get("context")
        if context == "attempt":
            fence = self._attempt
            if fence is None:
                raise ContractError(
                    ErrorCode.STALE_LEASE, "no Attempt is bound to this worker session"
                )
            if self._attempt_authority is not None and not self._attempt_authority(
                fence
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "bound Attempt is no longer authoritative"
                )
            if (
                meta.get("job_id") != fence.job_id
                or meta.get("step_id") != fence.step_id
                or meta.get("attempt_id") != fence.attempt_id
                or meta.get("lease_epoch") != fence.lease_epoch
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "RPC Attempt identity or epoch is stale"
                )
            validate_rpc_request(message, expected_lease_epoch=fence.lease_epoch)
        elif context == "install":
            fence = self._install
            if fence is None:
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "no install lease is bound to this worker session",
                )
            if self._install_authority is not None and not self._install_authority(
                fence
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "bound install lease is no longer authoritative",
                )
            if (
                meta.get("install_operation_id") != fence.install_operation_id
                or meta.get("install_lease_epoch") != fence.install_lease_epoch
            ):
                raise ContractError(
                    ErrorCode.STALE_LEASE, "RPC install identity or epoch is stale"
                )
            params = message.get("params")
            if (
                isinstance(params, Mapping)
                and "owner_instance_id" in params
                and params["owner_instance_id"] != fence.owner_instance_id
            ):
                raise ContractError(ErrorCode.STALE_LEASE, "RPC install owner is stale")
            validate_rpc_request(
                message, expected_lease_epoch=fence.install_lease_epoch
            )
        elif context == "control":
            validate_rpc_request(message)
        else:
            validate_rpc_request(message)
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH, "unknown RPC context"
            )
        self._assert_identity(message)

    def _accept(self, message: Mapping[str, Any]) -> RpcEvent:
        if not self._handshake_complete:
            request = self._handshake_request
            if (
                request is None
                or message.get("id") != request["id"]
                or "method" in message
            ):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "first worker message must be the bound handshake response",
                )
            validate_rpc_response(message, request=request)
            self._pending.pop(str(request["id"]), None)
            if "error" in message:
                raise ContractError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "worker rejected runtime.handshake",
                )
            result = message["result"]
            if result["plugin_id"] != self.plugin_id:
                raise ContractError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "handshake plugin_id differs from immutable route",
                )
            if tuple(result["capabilities"]) != self.expected_capabilities:
                raise ContractError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "handshake capabilities differ from verified manifest",
                )
            self._worker_instance_id = result["worker_instance_id"]
            self._handshake_complete = True
            return RpcEvent("handshake", message)

        if "method" in message:
            method = message.get("method")
            request_id = str(message.get("id"))
            existing = self._inbound.get(request_id) if "id" in message else None
            if existing is not None:
                if existing.request != message:
                    raise ContractError(
                        ErrorCode.DUPLICATE_REQUEST,
                        "worker reused a Host RPC id with different bytes",
                    )
                if existing.response_frame is None:
                    try:
                        self._validate_request(message)
                    except ContractError as exc:
                        if (
                            exc.code != ErrorCode.STALE_LEASE
                            or method not in HOST_METHODS
                        ):
                            raise
                        replay = self._durable_replay(message)
                        if replay is None:
                            raise
                        existing.response_frame = replay
                return RpcEvent(
                    "host_replay"
                    if existing.response_frame is not None
                    else "host_request_duplicate",
                    message,
                )
            try:
                self._validate_request(message)
            except ContractError as exc:
                if (
                    exc.code != ErrorCode.STALE_LEASE
                    or method not in HOST_METHODS
                    or "id" not in message
                ):
                    raise
                replay = self._durable_replay(message)
                if replay is None:
                    raise
                self._reserve_inbound(
                    request_id, _InboundCall(message, response_frame=replay)
                )
                return RpcEvent("host_replay", message)
            if method == "runtime.heartbeat":
                params = message["params"]
                if params["worker_instance_id"] != self._worker_instance_id:
                    raise ContractError(
                        ErrorCode.STALE_LEASE, "heartbeat worker_instance_id is stale"
                    )
                seq = params["local_seq"]
                if seq <= self._heartbeat_seq:
                    raise ContractError(
                        ErrorCode.INVALID_TRANSITION,
                        "heartbeat local_seq must increase",
                    )
                self._heartbeat_seq = seq
                return RpcEvent("heartbeat", message)
            if method not in HOST_METHODS:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "worker may call only published Host RPC methods",
                )
            request_id = str(message["id"])
            self._reserve_inbound(request_id, _InboundCall(message))
            return RpcEvent("host_request", message)

        request_id = message.get("id")
        request = self._pending.get(str(request_id))
        if request is None:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH, "unbound RPC response id"
            )
        self._validate_request(request)
        validate_rpc_response(message, request=request)
        self._pending.pop(str(request_id), None)
        self._worker_replay_frames.pop(str(request_id), None)
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
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "RPC stream ended with a truncated or trailing frame",
            )


__all__ = ["FramedRpcSession", "RpcEvent"]
