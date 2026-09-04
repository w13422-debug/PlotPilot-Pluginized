"""Reusable bounded framed-stdio worker runtime for first-party plugins.

The dispatcher owns the only stdin reader and serialized stdout writer. Domain
handlers are synchronous; while they call Host RPC, the same reader classifies
frames and correlates responses. No state is persisted and stdout carries only
protocol frames.
"""

from __future__ import annotations

import base64
import hashlib
import re
import sys
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from .canonical import canonical_bytes, parse_json_bytes, sha256_hex
from .errors import ContractError, ErrorCode
from .framing import FrameDecoder, encode_frame
from .package import release_id as derive_release_id
from .rpc import (
    ERROR_CODES,
    HOST_METHODS,
    WORKER_METHODS,
    build_notification,
    build_request,
)
from .verifier import (
    validate_rpc_request,
    validate_rpc_response,
    validate_rpc_result,
    verify_capability_descriptor,
    verify_snapshot,
)

DEFAULT_IO_CHUNK_SIZE = 64 * 1024
DEFAULT_ASSET_PAGE_SIZE = 1024 * 1024
DEFAULT_MAX_ASSET_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_ASSET_PAGES = 256
MAX_ASSET_PAGE_SIZE = 4 * 1024 * 1024

_RPC_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RETRYABLE_CODES = {
    int(ErrorCode.STALE_LEASE),
    int(ErrorCode.CANCELLED),
    int(ErrorCode.DEADLINE_EXCEEDED),
    int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT),
}
_BUILTIN_METHODS = {
    "runtime.handshake",
    "runtime.health",
    "capability.describe",
    "job.start",
    "job.resume",
    "job.cancel",
    "runtime.shutdown",
}

DomainHandler = Callable[[Mapping[str, Any], "WorkerContext"], Mapping[str, Any]]
DescriptorFactory = Callable[[str], Mapping[str, Any]]


def _require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            f"{label} is not a canonical ID",
        )
    return value


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ContractError(
            ErrorCode.RESULT_CONTRACT_MISMATCH,
            f"{label} is not a lowercase SHA-256",
        )
    return value


def _require_positive(value: object, label: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")
    return value


def _now() -> str:
    current = datetime.now(timezone.utc)
    if current.microsecond // 1000 == 0:
        return current.isoformat(timespec="seconds").replace("+00:00", "Z")
    return current.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class HostAsset:
    """Authenticated immutable Asset bytes returned by Host RPC."""

    asset_id: str
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        _require_identifier(self.asset_id, "asset_id")
        if sha256_hex(self.content) != self.sha256:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "Host Asset digest does not match its bytes",
            )


@dataclass(frozen=True, slots=True)
class AttemptIdentity:
    """Complete in-memory authority bound before a Job handler runs."""

    generation_id: str
    plugin_release_id: str
    data_generation_id: str | None
    package_hash: str
    workspace_id: str
    job_id: str
    step_id: str
    attempt_id: str
    lease_epoch: int
    operation_id: str
    capability_id: str
    run_snapshot_asset_id: str
    run_snapshot_id: str
    run_snapshot_hash: str


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """Validated request context supplied to a registered domain handler."""

    request: Mapping[str, Any]
    meta: Mapping[str, Any]
    host: HostRpcClient
    assets: HostAssetClient
    identity: AttemptIdentity | None = None
    run_snapshot: Mapping[str, Any] | None = None
    run_snapshot_asset: HostAsset | None = None


@dataclass(frozen=True, slots=True)
class _Domain:
    descriptor: DescriptorFactory
    operations: frozenset[str]
    start: DomainHandler | None
    resume: DomainHandler | None
    cancel: DomainHandler | None


@dataclass(slots=True)
class _PendingCall:
    request: dict[str, Any]
    response: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _RunBinding:
    domain: _Domain
    identity: AttemptIdentity
    run_snapshot: Mapping[str, Any]
    run_snapshot_asset: HostAsset


class _CleanEOF(Exception):
    pass


class HostRpcClient:
    """Request-correlated Host port bound to one worker request meta."""

    def __init__(
        self,
        worker: FramedStdioWorker,
        meta: Mapping[str, Any],
        token: object,
    ) -> None:
        self._worker = worker
        self._meta = dict(meta)
        self._token = token

    def call(
        self,
        method: str,
        params: Mapping[str, object],
    ) -> Mapping[str, Any]:
        return self._worker._call_host(self._token, self._meta, method, params)

    def heartbeat(self, *, observed_at: str, local_seq: int) -> None:
        """Emit one explicit heartbeat; no background writer is created."""

        self._worker._write_heartbeat(
            self._token,
            self._meta,
            observed_at=observed_at,
            local_seq=local_seq,
        )


class HostAssetClient:
    """Bounded read/create helpers over the published Host Asset methods."""

    def __init__(
        self,
        host: HostRpcClient,
        *,
        page_size: int,
        max_bytes: int,
        max_pages: int,
    ) -> None:
        self._host = host
        self.page_size = page_size
        self.max_bytes = max_bytes
        self.max_pages = max_pages

    def read(self, asset_id: str) -> HostAsset:
        asset_id = _require_identifier(asset_id, "asset_id")
        chunks: list[bytes] = []
        offset = 0
        total = 0
        for _page in range(self.max_pages):
            result = self._host.call(
                "host.asset.read/v1",
                {
                    "asset_id": asset_id,
                    "offset": offset,
                    "length": self.page_size,
                },
            )
            try:
                chunk = base64.b64decode(result["base64_chunk"], validate=True)
            except Exception as exc:
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "Host Asset page is not valid base64",
                ) from exc
            if len(chunk) > self.page_size or total + len(chunk) > self.max_bytes:
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "Host Asset exceeds its byte bound",
                )
            if result["content_hash"] != sha256_hex(chunk):
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "Host Asset page hash is invalid",
                )
            next_offset = result["next_offset"]
            chunks.append(chunk)
            total += len(chunk)
            if next_offset is None:
                content = b"".join(chunks)
                if len(content) != total:
                    raise ContractError(
                        ErrorCode.ASSET_ERROR,
                        "Host Asset allocation is inconsistent",
                    )
                return HostAsset(asset_id, content, sha256_hex(content))
            if (
                type(next_offset) is not int
                or next_offset != offset + len(chunk)
                or next_offset <= offset
            ):
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "Host Asset pages are not contiguous",
                )
            offset = next_offset
        raise ContractError(
            ErrorCode.ASSET_ERROR,
            "Host Asset exceeds its page bound",
        )

    def create(
        self,
        content: bytes | str,
        *,
        operation_key: str,
        mime: str = "application/json",
    ) -> HostAsset:
        operation_key = _require_identifier(operation_key, "operation_key")
        if isinstance(content, str):
            data = content.encode("utf-8")
        elif isinstance(content, bytes):
            data = content
        else:
            raise TypeError("Host Asset content must be bytes or str")
        if len(data) > self.max_bytes:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "Host Asset upload exceeds its byte bound",
            )
        if not isinstance(mime, str) or not mime:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "Host Asset MIME must be non-empty",
            )

        digest = sha256_hex(data)
        upload_id = (
            "sdk-upload-"
            + hashlib.sha256(
                f"{operation_key}\n{digest}\n".encode("ascii")
            ).hexdigest()[:40]
        )
        chunks = (
            [b""]
            if not data
            else [
                data[index : index + self.page_size]
                for index in range(0, len(data), self.page_size)
            ]
        )
        if len(chunks) > self.max_pages:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "Host Asset upload exceeds its page bound",
            )

        accepted = 0
        asset_id: str | None = None
        for index, chunk in enumerate(chunks):
            chunk_hash = sha256_hex(chunk)
            final = index == len(chunks) - 1
            chunk_operation_key = (
                "sdk-asset-chunk-"
                + hashlib.sha256(
                    f"{operation_key}\n{upload_id}\n{accepted}\n{chunk_hash}\n".encode(
                        "ascii"
                    )
                ).hexdigest()[:48]
            )
            result = self._host.call(
                "host.asset.create/v1",
                {
                    "operation_key": chunk_operation_key,
                    "upload_id": upload_id,
                    "offset": accepted,
                    "mime": mime,
                    "total_size": len(data),
                    "expected_hash": digest,
                    "chunk_hash": chunk_hash,
                    "base64_chunk": base64.b64encode(chunk).decode("ascii"),
                    "final": final,
                },
            )
            if result["upload_id"] != upload_id:
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "Host upload changed upload_id",
                )
            if result["accepted_bytes"] != accepted + len(chunk):
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "Host upload acknowledgement is not contiguous",
                )
            returned_asset_id = result["asset_id"]
            if not final and (result["completed"] or returned_asset_id is not None):
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "Host upload completed before its final chunk",
                )
            if final:
                if not result["completed"] or returned_asset_id is None:
                    raise ContractError(
                        ErrorCode.ASSET_ERROR,
                        "Host upload did not complete",
                    )
                asset_id = _require_identifier(returned_asset_id, "asset_id")
            accepted = result["accepted_bytes"]
        if accepted != len(data) or asset_id is None:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "Host upload returned no immutable Asset",
            )
        return HostAsset(asset_id, data, digest)


class FramedStdioWorker:
    """Generic first-party worker dispatcher with bounded nested Host calls."""

    def __init__(
        self,
        *,
        plugin_id: str,
        plugin_version: str,
        release_id: str | None = None,
        worker_instance_id: str | None = None,
        clock: Callable[[], str] = _now,
        request_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        io_chunk_size: int = DEFAULT_IO_CHUNK_SIZE,
        asset_page_size: int = DEFAULT_ASSET_PAGE_SIZE,
        max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES,
        max_asset_pages: int = DEFAULT_MAX_ASSET_PAGES,
        max_pending_calls: int = 8,
        max_request_ids: int = 4096,
        max_identity_bindings: int = 256,
        max_dispatch_depth: int = 16,
    ) -> None:
        try:
            self.plugin_id = _require_identifier(plugin_id, "plugin_id")
        except ContractError as exc:
            raise ValueError(str(exc)) from exc
        if not isinstance(plugin_version, str) or not plugin_version:
            raise ValueError("plugin_version must be non-empty")
        try:
            plugin_version.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("plugin_version must be ASCII") from exc
        if release_id is not None and _SHA256.fullmatch(release_id) is None:
            raise ValueError("release_id must be a lowercase SHA-256")
        generated_instance_id = worker_instance_id or f"sdk-worker-{uuid.uuid4()}"
        try:
            self.worker_instance_id = _require_identifier(
                generated_instance_id,
                "worker_instance_id",
            )
        except ContractError as exc:
            raise ValueError(str(exc)) from exc

        self.plugin_version = plugin_version
        self._expected_release_id = release_id
        self._release_id: str | None = None
        self._generation_id: str | None = None
        self._data_generation_id: str | None = None
        self._clock = clock
        self._request_id_factory = request_id_factory
        self._io_chunk_size = _require_positive(
            io_chunk_size,
            "io_chunk_size",
            1024 * 1024,
        )
        self._asset_page_size = _require_positive(
            asset_page_size,
            "asset_page_size",
            MAX_ASSET_PAGE_SIZE,
        )
        self._max_asset_bytes = _require_positive(
            max_asset_bytes,
            "max_asset_bytes",
            1024 * 1024 * 1024,
        )
        self._max_asset_pages = _require_positive(
            max_asset_pages,
            "max_asset_pages",
            4096,
        )
        self._max_pending_calls = _require_positive(
            max_pending_calls,
            "max_pending_calls",
            64,
        )
        self._max_request_ids = _require_positive(
            max_request_ids,
            "max_request_ids",
            1_000_000,
        )
        self._max_identity_bindings = _require_positive(
            max_identity_bindings,
            "max_identity_bindings",
            65_536,
        )
        self._max_dispatch_depth = _require_positive(
            max_dispatch_depth,
            "max_dispatch_depth",
            64,
        )

        self._state = "new"
        self._domains: dict[str, _Domain] = {}
        self._handlers: dict[str, DomainHandler] = {}
        self._decoder = FrameDecoder()
        self._messages: deque[dict[str, Any]] = deque()
        self._pending: dict[str, _PendingCall] = {}
        self._seen_request_ids: set[str] = set()
        self._seen_operation_ids: dict[str, str] = {}
        self._host_request_ids: set[str] = set()
        self._lease_highwater: dict[tuple[str, ...], int] = {}
        self._attempts: dict[tuple[str, str, str], _RunBinding] = {}
        self._latest_attempt: dict[tuple[str, str], _RunBinding] = {}
        self._runs: dict[str, _RunBinding] = {}
        self._context_tokens: list[object] = []
        self._dispatch_depth = 0

        self._stdin: Any = None
        self._stdout: Any = None
        self._read_chunk: Callable[[int], bytes] | None = None
        self._writer_lock = threading.RLock()
        self._reader_thread_id: int | None = None
        self._serving = False
        self._input_eof = False
        self._transport_failed = False

    @property
    def state(self) -> str:
        return self._state

    @property
    def pending_call_count(self) -> int:
        return len(self._pending)

    def register_domain(
        self,
        capability_id: str,
        *,
        descriptor: DescriptorFactory,
        operations: Iterable[str] | None = None,
        start: DomainHandler | None = None,
        resume: DomainHandler | None = None,
        cancel: DomainHandler | None = None,
    ) -> None:
        """Register one capability and its Job lifecycle handlers before serve."""

        if self._state != "new" or self._serving:
            raise RuntimeError("domains must be registered before the worker starts")
        capability_id = _require_identifier(capability_id, "capability_id")
        if capability_id in self._domains:
            raise ValueError(f"duplicate capability registration: {capability_id}")
        if not callable(descriptor):
            raise TypeError("descriptor must be callable")
        selected_operations = frozenset(operations or (capability_id,))
        if not selected_operations:
            raise ValueError("a domain must accept at least one RunSnapshot operation")
        for operation in selected_operations:
            _require_identifier(operation, "operation")
        for name, handler in (
            ("start", start),
            ("resume", resume),
            ("cancel", cancel),
        ):
            if handler is not None and not callable(handler):
                raise TypeError(f"{name} handler must be callable")
        self._domains[capability_id] = _Domain(
            descriptor,
            selected_operations,
            start,
            resume,
            cancel,
        )

    def register_handler(self, method: str, handler: DomainHandler) -> None:
        """Register a non-built-in published worker method handler."""

        if self._state != "new" or self._serving:
            raise RuntimeError("handlers must be registered before the worker starts")
        if (
            method not in WORKER_METHODS
            or method in _BUILTIN_METHODS
            or method == "runtime.heartbeat"
        ):
            raise ValueError(f"{method!r} is not a registrable worker method")
        if method in self._handlers:
            raise ValueError(f"duplicate method registration: {method}")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handlers[method] = handler

    def serve(self, *, stdin: Any = None, stdout: Any = None) -> None:
        """Serve until clean EOF, fatal framing failure, or accepted shutdown."""

        if self._serving or self._state in {"shutdown", "eof", "failed"}:
            raise RuntimeError("worker cannot start another stdio session")
        source = sys.stdin.buffer if stdin is None else stdin
        target = sys.stdout.buffer if stdout is None else stdout
        read_chunk = getattr(source, "read1", None) or getattr(source, "read", None)
        if not callable(read_chunk) or not callable(getattr(target, "write", None)):
            raise TypeError("stdin/stdout must be binary streams")

        self._stdin = source
        self._stdout = target
        self._read_chunk = read_chunk
        self._reader_thread_id = threading.get_ident()
        self._serving = True
        try:
            while self._state != "shutdown" and not self._transport_failed:
                try:
                    message = self._receive_message()
                except _CleanEOF:
                    self._state = "eof"
                    break
                except Exception as exc:  # noqa: BLE001 - protocol boundary
                    self._transport_failed = True
                    self._write_message(self._error_response(None, exc))
                    self._state = "failed"
                    break

                response = self.handle(message)
                if response is not None:
                    self._write_message(response)
                if self._input_eof:
                    self._state = "failed" if self._transport_failed else "eof"
                    break
        finally:
            self._pending.clear()
            self._context_tokens.clear()
            self._stdin = None
            self._stdout = None
            self._read_chunk = None
            self._reader_thread_id = None
            self._serving = False

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        """Handle one decoded message; all failures become valid RPC errors."""

        request = dict(message) if isinstance(message, Mapping) else None
        if request is None:
            return self._error_response(
                None,
                TypeError("RPC message must be an object"),
            )
        if "method" not in request and ("result" in request or "error" in request):
            try:
                self._route_response(request)
                return None
            except Exception as exc:  # noqa: BLE001 - protocol boundary
                return self._error_response(request, exc)

        candidate_id = request.get("id")
        if isinstance(candidate_id, str) and _RPC_ID.fullmatch(candidate_id):
            if candidate_id in self._seen_request_ids:
                return self._error_response(
                    request,
                    ContractError(
                        ErrorCode.DUPLICATE_REQUEST,
                        "duplicate RPC request id",
                    ),
                )
            if len(self._seen_request_ids) >= self._max_request_ids:
                return self._error_response(
                    request,
                    ContractError(
                        ErrorCode.INVALID_TRANSITION,
                        "worker request-id budget is exhausted",
                    ),
                )
            self._seen_request_ids.add(candidate_id)

        try:
            method = request.get("method")
            if not isinstance(method, str) or method not in WORKER_METHODS:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    f"unknown worker method: {method!r}",
                )
            if method == "runtime.heartbeat":
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "runtime.heartbeat is emitted by the worker and cannot be "
                    "dispatched by Host",
                )
            validate_rpc_request(request)
            self._check_lifecycle_order(method)
            self._remember_operation(request)
            self._fence_meta(request)
            if self._dispatch_depth >= self._max_dispatch_depth:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "nested dispatch depth is exhausted",
                )

            token = object()
            meta = dict(request["meta"])
            host = HostRpcClient(self, meta, token)
            context = WorkerContext(
                request=dict(request),
                meta=meta,
                host=host,
                assets=HostAssetClient(
                    host,
                    page_size=self._asset_page_size,
                    max_bytes=self._max_asset_bytes,
                    max_pages=self._max_asset_pages,
                ),
            )
            self._dispatch_depth += 1
            self._context_tokens.append(token)
            try:
                result = self._dispatch(request, context)
            finally:
                popped = self._context_tokens.pop()
                self._dispatch_depth -= 1
                if popped is not token:
                    self._transport_failed = True
                    raise RuntimeError("worker context stack is corrupt")
            return self._success_response(request, result)
        except Exception as exc:  # noqa: BLE001 - isolate handler failures
            return self._error_response(request, exc)

    def _check_lifecycle_order(self, method: str) -> None:
        if self._state in {"shutdown", "eof", "failed"}:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                f"worker is already {self._state}",
            )
        if self._state == "new" and method != "runtime.handshake":
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "runtime.handshake must be the first worker request",
            )
        if self._state == "ready" and method == "runtime.handshake":
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "runtime.handshake may occur only once",
            )

    def _remember_operation(self, request: Mapping[str, Any]) -> None:
        operation_id = str(request["meta"]["operation_id"])
        request_id = str(request["id"])
        previous = self._seen_operation_ids.get(operation_id)
        if previous is not None and previous != request_id:
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST,
                "RPC operation_id was replayed under another request id",
            )
        if previous is None:
            if len(self._seen_operation_ids) >= self._max_request_ids:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "worker operation-id budget is exhausted",
                )
            self._seen_operation_ids[operation_id] = request_id

    def _fence_meta(self, request: Mapping[str, Any]) -> None:
        if request["method"] == "runtime.handshake":
            return
        meta = request["meta"]
        if (
            meta["generation_id"] != self._generation_id
            or meta["plugin_release_id"] != self._release_id
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "RPC generation/release differs from the handshaken worker",
            )
        context = meta["context"]
        if context == "attempt":
            key = (
                "attempt",
                meta["generation_id"],
                meta["job_id"],
                meta["step_id"],
                meta["attempt_id"],
            )
            epoch = meta["lease_epoch"]
        elif context == "install":
            key = (
                "install",
                meta["generation_id"],
                meta["install_operation_id"],
            )
            epoch = meta["install_lease_epoch"]
        else:
            return
        previous = self._lease_highwater.get(key)
        if previous is not None and epoch < previous:
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "RPC lease epoch is stale",
            )
        if (
            previous is None
            and len(self._lease_highwater) >= self._max_identity_bindings
        ):
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "worker identity budget is exhausted",
            )
        self._lease_highwater[key] = epoch if previous is None else max(previous, epoch)

    def _dispatch(
        self,
        request: Mapping[str, Any],
        context: WorkerContext,
    ) -> Mapping[str, Any]:
        method = request["method"]
        if method == "runtime.handshake":
            return self._handshake(request)
        if method == "runtime.health":
            return {
                "status": "ok",
                "details_asset_id": None,
                "checked_at": self._clock(),
            }
        if method == "capability.describe":
            return self._describe(request)
        if method in {"job.start", "job.resume"}:
            return self._dispatch_start_or_resume(request, context)
        if method == "job.cancel":
            return self._dispatch_cancel(request, context)
        if method == "runtime.shutdown":
            if self._pending:
                raise ContractError(
                    ErrorCode.INVALID_TRANSITION,
                    "runtime.shutdown cannot orphan pending Host calls",
                )
            result = {"accepted": True}
            validate_rpc_result(method, result, request=request)
            self._state = "shutdown"
            return result
        handler = self._handlers.get(method)
        if handler is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                f"no handler is registered for {method}",
            )
        return self._invoke_handler(handler, request["params"], context)

    def _handshake(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        meta = request["meta"]
        params = request["params"]
        release_id = params["plugin_release_id"]
        if (
            params["host_protocol"] != "1"
            or meta["protocol_version"] != "1"
            or params["generation_id"] != meta["generation_id"]
            or release_id != meta["plugin_release_id"]
            or (
                self._expected_release_id is not None
                and release_id != self._expected_release_id
            )
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "Host protocol/generation/release is incompatible with this worker",
            )
        result = {
            "plugin_protocol": "1",
            "plugin_id": self.plugin_id,
            "release_id": release_id,
            "capabilities": sorted(self._domains),
            "worker_instance_id": self.worker_instance_id,
        }
        validate_rpc_result(
            "runtime.handshake",
            result,
            request=request,
        )
        self._release_id = release_id
        self._generation_id = params["generation_id"]
        self._data_generation_id = params["data_generation_id"]
        self._state = "ready"
        return result

    def _describe(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        capability_id = request["params"]["capability_id"]
        domain = self._domains.get(capability_id)
        if domain is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "unknown capability",
            )
        assert self._release_id is not None
        descriptor = domain.descriptor(self._release_id)
        if not isinstance(descriptor, Mapping):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "capability descriptor factory returned a non-object",
            )
        value = dict(descriptor)
        verify_capability_descriptor(
            value,
            expected_capability_id=capability_id,
            expected_provider={
                "plugin_id": self.plugin_id,
                "release_id": self._release_id,
            },
        )
        return {"descriptor": value}

    def _dispatch_start_or_resume(
        self,
        request: Mapping[str, Any],
        context: WorkerContext,
    ) -> Mapping[str, Any]:
        method = request["method"]
        params = request["params"]
        capability_id = params["capability_id"]
        domain = self._domains.get(capability_id)
        if domain is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "unknown Job capability",
            )
        handler = domain.start if method == "job.start" else domain.resume
        if handler is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                f"no {method} handler is registered",
            )
        if len(self._runs) >= self._max_identity_bindings:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "worker run-binding budget is exhausted",
            )

        bound_context = self._bind_run_snapshot(request, context, domain)
        result = self._invoke_handler(handler, params, bound_context)
        validate_rpc_result(method, result, request=request)
        worker_run_id = _require_identifier(
            result["worker_run_id"],
            "worker_run_id",
        )
        assert bound_context.identity is not None
        assert bound_context.run_snapshot is not None
        assert bound_context.run_snapshot_asset is not None
        binding = _RunBinding(
            domain,
            bound_context.identity,
            bound_context.run_snapshot,
            bound_context.run_snapshot_asset,
        )
        old = self._runs.get(worker_run_id)
        if old is not None and old.identity != binding.identity:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "worker_run_id was rebound to another Attempt",
            )
        identity = binding.identity
        attempt_key = (
            identity.job_id,
            identity.step_id,
            identity.attempt_id,
        )
        self._runs[worker_run_id] = binding
        self._attempts[attempt_key] = binding
        self._latest_attempt[(identity.job_id, identity.step_id)] = binding
        return result

    def _bind_run_snapshot(
        self,
        request: Mapping[str, Any],
        context: WorkerContext,
        domain: _Domain,
    ) -> WorkerContext:
        params = request["params"]
        meta = request["meta"]
        snapshot_asset = context.assets.read(params["run_snapshot_asset_id"])
        try:
            parsed = parse_json_bytes(snapshot_asset.content)
            if not isinstance(parsed, dict):
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "RunSnapshot Asset is not an object",
                )
            if canonical_bytes(parsed) != snapshot_asset.content:
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "RunSnapshot Asset is not canonical JSON",
                )
            verify_snapshot(parsed)
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "RunSnapshot Asset is invalid",
            ) from exc

        releases = [
            item
            for item in parsed["plugin_releases"]
            if item["plugin_id"] == self.plugin_id
        ]
        if len(releases) != 1:
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "RunSnapshot does not bind exactly one release for this plugin",
            )
        release = releases[0]
        package_hash = _require_sha256(
            release["package_hash"],
            "package_hash",
        )
        expected_release = derive_release_id(
            self.plugin_id,
            self.plugin_version,
            package_hash,
        )
        if (
            release["release_id"] != self._release_id
            or expected_release != self._release_id
            or release["data_generation_id"] != self._data_generation_id
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "RunSnapshot package/release/data-generation identity is incompatible",
            )
        if parsed["scope"]["operation"] not in domain.operations:
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "RunSnapshot operation is not registered for this capability",
            )

        identity = AttemptIdentity(
            generation_id=meta["generation_id"],
            plugin_release_id=meta["plugin_release_id"],
            data_generation_id=self._data_generation_id,
            package_hash=package_hash,
            workspace_id=parsed["workspace_id"],
            job_id=meta["job_id"],
            step_id=meta["step_id"],
            attempt_id=meta["attempt_id"],
            lease_epoch=meta["lease_epoch"],
            operation_id=meta["operation_id"],
            capability_id=params["capability_id"],
            run_snapshot_asset_id=params["run_snapshot_asset_id"],
            run_snapshot_id=parsed["snapshot_id"],
            run_snapshot_hash=parsed["snapshot_hash"],
        )
        if request["method"] == "job.resume":
            resume_of = params["resume_of_attempt_id"]
            previous = self._attempts.get(
                (identity.job_id, identity.step_id, resume_of)
            )
            latest = self._latest_attempt.get((identity.job_id, identity.step_id))
            if previous is None:
                known_elsewhere = any(
                    binding.identity.attempt_id == resume_of
                    for binding in self._attempts.values()
                )
                if known_elsewhere or latest is not None:
                    raise ContractError(
                        ErrorCode.STALE_LEASE,
                        "job.resume does not name the current in-memory Job/Step "
                        "Attempt edge",
                    )
            else:
                if latest is not previous:
                    raise ContractError(
                        ErrorCode.STALE_LEASE,
                        "job.resume does not name the current in-memory Attempt edge",
                    )
                prior = previous.identity
                if identity.attempt_id == prior.attempt_id:
                    raise ContractError(
                        ErrorCode.INVALID_TRANSITION,
                        "job.resume must create a distinct Attempt",
                    )
                stable_before = (
                    prior.generation_id,
                    prior.plugin_release_id,
                    prior.data_generation_id,
                    prior.package_hash,
                    prior.workspace_id,
                    prior.capability_id,
                    prior.run_snapshot_asset_id,
                    prior.run_snapshot_id,
                    prior.run_snapshot_hash,
                )
                stable_after = (
                    identity.generation_id,
                    identity.plugin_release_id,
                    identity.data_generation_id,
                    identity.package_hash,
                    identity.workspace_id,
                    identity.capability_id,
                    identity.run_snapshot_asset_id,
                    identity.run_snapshot_id,
                    identity.run_snapshot_hash,
                )
                if stable_after != stable_before:
                    raise ContractError(
                        ErrorCode.STALE_LEASE,
                        "job.resume changed package, Workspace, capability, or "
                        "RunSnapshot identity",
                    )
        return replace(
            context,
            identity=identity,
            run_snapshot=parsed,
            run_snapshot_asset=snapshot_asset,
        )

    def _dispatch_cancel(
        self,
        request: Mapping[str, Any],
        context: WorkerContext,
    ) -> Mapping[str, Any]:
        params = request["params"]
        binding = self._runs.get(params["worker_run_id"])
        if binding is None:
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "job.cancel references an unknown worker run",
            )
        meta = request["meta"]
        prior = binding.identity
        if (
            meta["generation_id"] != prior.generation_id
            or meta["plugin_release_id"] != prior.plugin_release_id
            or meta["job_id"] != prior.job_id
            or meta["step_id"] != prior.step_id
            or meta["attempt_id"] != prior.attempt_id
            or meta["lease_epoch"] < prior.lease_epoch
        ):
            raise ContractError(
                ErrorCode.STALE_LEASE,
                "job.cancel is outside the bound Attempt",
            )
        if binding.domain.cancel is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "no job.cancel handler is registered",
            )
        identity = replace(
            prior,
            lease_epoch=meta["lease_epoch"],
            operation_id=meta["operation_id"],
        )
        bound_context = replace(
            context,
            identity=identity,
            run_snapshot=binding.run_snapshot,
            run_snapshot_asset=binding.run_snapshot_asset,
        )
        result = self._invoke_handler(
            binding.domain.cancel,
            params,
            bound_context,
        )
        validate_rpc_result(
            "job.cancel",
            result,
            request=request,
        )
        return result

    @staticmethod
    def _invoke_handler(
        handler: DomainHandler,
        params: Mapping[str, Any],
        context: WorkerContext,
    ) -> dict[str, Any]:
        result = handler(dict(params), context)
        if not isinstance(result, Mapping):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "domain handler returned a non-object",
            )
        return dict(result)

    def _check_bound_host_meta(
        self,
        token: object,
        meta: Mapping[str, Any],
    ) -> None:
        if (
            not self._serving
            or self._reader_thread_id != threading.get_ident()
            or not self._context_tokens
            or self._context_tokens[-1] is not token
        ):
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Host RPC is valid only in the active synchronous handler",
            )
        if self._state != "ready":
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Host RPC requires a ready worker",
            )
        if (
            meta["generation_id"] != self._generation_id
            or meta["plugin_release_id"] != self._release_id
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "Host RPC meta differs from the handshaken worker",
            )
        if meta["context"] == "attempt":
            key = (
                "attempt",
                meta["generation_id"],
                meta["job_id"],
                meta["step_id"],
                meta["attempt_id"],
            )
            if self._lease_highwater.get(key) != meta["lease_epoch"]:
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "Host RPC Attempt lease is no longer current",
                )
        elif meta["context"] == "install":
            key = (
                "install",
                meta["generation_id"],
                meta["install_operation_id"],
            )
            if self._lease_highwater.get(key) != meta["install_lease_epoch"]:
                raise ContractError(
                    ErrorCode.STALE_LEASE,
                    "Host RPC install lease is no longer current",
                )

    def _call_host(
        self,
        token: object,
        meta: Mapping[str, Any],
        method: str,
        params: Mapping[str, object],
    ) -> Mapping[str, Any]:
        self._check_bound_host_meta(token, meta)
        if method not in HOST_METHODS:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                f"unknown Host method: {method!r}",
            )
        if len(self._pending) >= self._max_pending_calls:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "pending Host-call bound is exhausted",
            )
        request_id = self._request_id_factory()
        if not isinstance(request_id, str) or _RPC_ID.fullmatch(request_id) is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Host request ID factory returned an invalid ID",
            )
        if request_id in self._host_request_ids:
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST,
                "Host request ID factory repeated an ID",
            )
        if len(self._host_request_ids) >= self._max_request_ids:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Host request-id budget is exhausted",
            )
        self._host_request_ids.add(request_id)
        request = build_request(
            method,
            params,
            meta,
            request_id=request_id,
        )
        validate_rpc_request(request)
        pending = _PendingCall(request)
        self._pending[request_id] = pending
        try:
            self._write_message(request)
            while pending.response is None:
                try:
                    message = self._receive_message()
                except _CleanEOF as exc:
                    raise ContractError(
                        ErrorCode.ASSET_ERROR,
                        "Host RPC channel ended before its response",
                    ) from exc
                if "method" not in message and (
                    "result" in message or "error" in message
                ):
                    self._route_response(message)
                    continue
                response = self.handle(message)
                if response is not None:
                    self._write_message(response)
            response = pending.response
            assert response is not None
            if "error" in response:
                error = response["error"]
                raise ContractError(
                    int(error["code"]),
                    str(error["message"]),
                    details=error.get("data"),
                )
            result = response.get("result")
            if not isinstance(result, Mapping):
                raise ContractError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "Host response result is not an object",
                )
            return dict(result)
        finally:
            self._pending.pop(request_id, None)

    def _write_heartbeat(
        self,
        token: object,
        meta: Mapping[str, Any],
        *,
        observed_at: str,
        local_seq: int,
    ) -> None:
        self._check_bound_host_meta(token, meta)
        if meta["context"] != "attempt":
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "heartbeat requires Attempt meta",
            )
        notification = build_notification(
            {
                "worker_instance_id": self.worker_instance_id,
                "observed_at": observed_at,
                "local_seq": local_seq,
            },
            meta,
        )
        validate_rpc_request(notification)
        self._write_message(notification)

    def _route_response(self, response: Mapping[str, Any]) -> None:
        response_id = response.get("id")
        if not isinstance(response_id, str) or _RPC_ID.fullmatch(response_id) is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Host response has no valid correlation ID",
            )
        pending = self._pending.get(response_id)
        if pending is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "Host response correlation ID is unknown",
            )
        if pending.response is not None:
            raise ContractError(
                ErrorCode.DUPLICATE_REQUEST,
                "Host response is duplicated",
            )
        validate_rpc_response(response, request=pending.request)
        pending.response = dict(response)

    def _receive_message(self) -> dict[str, Any]:
        if self._messages:
            return self._messages.popleft()
        if self._input_eof:
            raise _CleanEOF
        if self._read_chunk is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "stdio reader is not active",
            )
        while not self._messages:
            try:
                chunk = self._read_chunk(self._io_chunk_size)
            except Exception as exc:
                self._transport_failed = True
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "stdio read failed",
                ) from exc
            if not isinstance(chunk, (bytes, bytearray)):
                self._transport_failed = True
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "stdio reader returned non-bytes",
                )
            if not chunk:
                self._input_eof = True
                if self._decoder.buffer:
                    self._transport_failed = True
                    raise ContractError(
                        ErrorCode.INVALID_TRANSITION,
                        "worker received an incomplete RPC frame at EOF",
                    )
                raise _CleanEOF
            try:
                self._messages.extend(self._decoder.feed(bytes(chunk)))
            except Exception:
                self._transport_failed = True
                raise
        return self._messages.popleft()

    def _write_message(self, message: Mapping[str, Any]) -> None:
        if self._stdout is None:
            raise ContractError(
                ErrorCode.INVALID_TRANSITION,
                "stdio writer is not active",
            )
        frame = encode_frame(dict(message))
        with self._writer_lock:
            try:
                written = self._stdout.write(frame)
                if written is not None and written != len(frame):
                    raise OSError("partial stdout write")
                flush = getattr(self._stdout, "flush", None)
                if callable(flush):
                    flush()
            except Exception as exc:
                self._transport_failed = True
                raise ContractError(
                    ErrorCode.ASSET_ERROR,
                    "stdio frame write failed",
                ) from exc

    def _success_response(
        self,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(result, Mapping):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "worker result is not an object",
            )
        value = dict(result)
        validate_rpc_result(
            request["method"],
            value,
            request=request,
        )
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": value,
        }

    @staticmethod
    def _error_response(
        request: Mapping[str, Any] | None,
        exc: BaseException,
    ) -> dict[str, Any]:
        code = int(
            getattr(
                exc,
                "code",
                ErrorCode.RESULT_CONTRACT_MISMATCH,
            )
        )
        if code not in ERROR_CODES:
            code = int(ErrorCode.RESULT_CONTRACT_MISMATCH)
        message = str(exc) or type(exc).__name__
        candidate_id = request.get("id") if isinstance(request, Mapping) else None
        request_id = (
            candidate_id
            if isinstance(candidate_id, str) and _RPC_ID.fullmatch(candidate_id)
            else None
        )
        digest = hashlib.sha256(f"{code}\n{message}".encode()).hexdigest()[:24]
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message,
                "data": {
                    "error_id": f"stdio-worker-error-{digest}",
                    "retryable": code in _RETRYABLE_CODES,
                    "details_asset_id": None,
                },
            },
        }
        validate_rpc_response(response)
        return response


__all__ = [
    "AttemptIdentity",
    "FramedStdioWorker",
    "HostAsset",
    "HostAssetClient",
    "HostRpcClient",
    "WorkerContext",
]
