"""Installable Host-RPC worker for Story-State.

The worker only drives the reviewed ``StoryStateRuntime`` through typed ports.
It never receives a Core repository or a publication capability.  Candidate
staging and terminal completion use the published Host RPC protocol; a
Core-composed receipt reader supplies the committed receipt after completion.
"""

from __future__ import annotations

import base64
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

try:
    from plotpilot_plugin_sdk import (
        ContractError,
        ContractValidationError,
        ErrorCode,
        hash_jcs,
        sha256_hex,
    )
    from plotpilot_plugin_sdk.framing import FrameDecoder, encode_frame
    from plotpilot_plugin_sdk.rpc import HOST_METHODS, build_request
    from plotpilot_plugin_sdk.verifier import (
        validate_rpc_request,
        validate_rpc_response,
        validate_rpc_result,
    )
except ModuleNotFoundError:  # pragma: no cover - repository-source fallback
    from backend.plotpilot_plugin_sdk import (
        ContractError,
        ContractValidationError,
        ErrorCode,
        hash_jcs,
        sha256_hex,
    )
    from backend.plotpilot_plugin_sdk.framing import FrameDecoder, encode_frame
    from backend.plotpilot_plugin_sdk.rpc import HOST_METHODS, build_request
    from backend.plotpilot_plugin_sdk.verifier import (
        validate_rpc_request,
        validate_rpc_response,
        validate_rpc_result,
    )

from .ports import (
    CandidateCommit,
    PreparedAsset,
    TerminalCommand,
    TerminalCompletion,
    TerminalPort,
)
from .runtime import RuntimeResult

PLUGIN_ID = "com.plotpilot.story-state"
CAPABILITY_ID = "planning.story-state.settle/v1"


class StoryStateWorkerError(ContractError):
    """Fail-closed worker/Host boundary error."""


class HostPort(Protocol):
    def call(
        self, method: str, params: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class CommittedReceiptReader(Protocol):
    """P1/P3 composition reads the already-committed provenance receipt."""

    def __call__(
        self, receipt_id: str, expected: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class StoryStateExecutionPort(Protocol):
    """Composed execution keeps request extraction and fact reads outside worker state."""

    def __call__(
        self, params: Mapping[str, Any], meta: Mapping[str, Any], terminal: TerminalPort
    ) -> RuntimeResult: ...


class StdioHostPort:
    """The accepted framed stdio Host bridge; no private transport is introduced."""

    def __init__(self, stdin: Any, stdout: Any) -> None:
        self._stdin, self._stdout = stdin, stdout
        self._decoder = FrameDecoder()
        self._meta: dict[str, Any] | None = None
        self._lock = threading.RLock()

    def bind_meta(self, meta: Mapping[str, Any]) -> None:
        self._meta = dict(meta)

    def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        if method not in HOST_METHODS or self._meta is None:
            raise StoryStateWorkerError(
                ErrorCode.INVALID_TRANSITION, "Story-State Host call is not composed"
            )
        request = build_request(method, params, self._meta)
        validate_rpc_request(request)
        with self._lock:
            self._stdout.write(encode_frame(request))
            self._stdout.flush()
            while True:
                chunk = self._stdin.read(65536)
                if not chunk:
                    raise StoryStateWorkerError(
                        ErrorCode.ASSET_ERROR, "Host channel ended before response"
                    )
                values = self._decoder.feed(chunk)
                if not values:
                    continue
                if len(values) != 1:
                    raise StoryStateWorkerError(
                        ErrorCode.INVALID_TRANSITION, "Host returned multiple frames"
                    )
                response = values[0]
                if "method" in response or response.get("id") != request["id"]:
                    raise StoryStateWorkerError(
                        ErrorCode.INVALID_TRANSITION,
                        "Host response is not request-bound",
                    )
                validate_rpc_response(response, request=request)
                if "error" in response:
                    error = response["error"]
                    raise StoryStateWorkerError(
                        int(error["code"]), str(error["message"])
                    )
                result = response.get("result")
                if not isinstance(result, Mapping):
                    raise StoryStateWorkerError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "Host result is not an object",
                    )
                return dict(result)


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _upload(host: HostPort, asset: PreparedAsset) -> None:
    digest = sha256_hex(asset.content)
    result = host.call(
        "host.asset.create/v1",
        {
            "operation_key": "story-state-asset-" + digest,
            "upload_id": "story-state-upload-" + digest[:40],
            "offset": 0,
            "mime": asset.mime,
            "total_size": len(asset.content),
            "expected_hash": digest,
            "chunk_hash": digest,
            "base64_chunk": base64.b64encode(asset.content).decode("ascii"),
            "final": True,
        },
    )
    if (
        result.get("completed") is not True
        or result.get("accepted_bytes") != len(asset.content)
        or result.get("asset_id") != asset.asset_id
    ):
        raise StoryStateWorkerError(
            ErrorCode.ASSET_ERROR,
            "Host Asset identity does not preserve Story-State content addressing",
        )


class HostTerminalPort:
    """One terminal call performs only the accepted Asset/Candidate/Job RPC chain."""

    def __init__(self, host: HostPort, receipt_reader: CommittedReceiptReader) -> None:
        self._host = host
        self._receipt_reader = receipt_reader

    def complete(self, command: TerminalCommand) -> TerminalCompletion:
        # Receipt composition is required before the first effect, so a missing
        # seam cannot leave a partially submitted Story-State operation.
        if not callable(self._receipt_reader):
            raise StoryStateWorkerError(
                ErrorCode.INVALID_TRANSITION,
                "Story-State requires a committed receipt reader",
            )
        for asset in command.assets:
            _upload(self._host, asset)
        stage = self._host.call(
            "host.candidate.stage/v1",
            {
                "operation_key": command.candidate_stage_operation_key,
                "result_bundle_asset_id": command.result_bundle_asset.asset_id,
                "input_snapshot_hash": command.result_bundle["input_snapshot_hash"],
            },
        )
        if stage.get("accepted") is not True:
            raise StoryStateWorkerError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Core rejected Story-State Candidate staging",
            )
        expected_items = [
            item["item_id"]
            for item in command.result_bundle["items"]
            if item["status"] == "complete"
        ]
        staged = stage.get("staged_items")
        if (
            not isinstance(staged, list)
            or [
                item.get("item_id") if isinstance(item, Mapping) else None
                for item in staged
            ]
            != expected_items
        ):
            raise StoryStateWorkerError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Core staged Candidate mapping is not exact",
            )
        candidates: list[CandidateCommit] = []
        for item in staged:
            if not isinstance(item, Mapping) or item.get("candidate_id") is None:
                raise StoryStateWorkerError(
                    ErrorCode.RESULT_CONTRACT_MISMATCH,
                    "Core did not return a staged Candidate identity",
                )
            status = item.get("stage_status")
            eligibility = item.get("publication_eligibility")
            candidates.append(
                CandidateCommit(
                    str(item["item_id"]),
                    str(item["candidate_id"]),
                    "created"
                    if status == "created"
                    else "idempotent"
                    if status == "existing"
                    else str(status),
                    str(eligibility),
                )
            )
        complete = self._host.call(
            "host.job.complete/v1",
            {
                "operation_key": command.operation_key,
                "worker_run_id": "story-state-run-"
                + sha256_hex(command.operation_key.encode("utf-8"))[:40],
                "outcome": command.outcome,
                "result_bundle_asset_id": command.result_bundle_asset.asset_id,
                "candidate_stage_operation_key": command.candidate_stage_operation_key,
                "terminal_detail_asset_id": command.terminal_detail_asset_id,
                "local_seq": command.local_seq,
            },
        )
        if complete.get("accepted") is not True:
            raise StoryStateWorkerError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Core rejected Story-State terminal completion",
            )
        receipt_id = complete.get("provenance_receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            raise StoryStateWorkerError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Core completion has no provenance receipt identity",
            )
        committed = self._receipt_reader(receipt_id, command.provenance_receipt)
        if (
            not isinstance(committed, Mapping)
            or committed.get("receipt_id") != receipt_id
        ):
            raise StoryStateWorkerError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "committed Story-State receipt is not Core-bound",
            )
        return TerminalCompletion(
            True,
            command.outcome,
            str(command.result_bundle["bundle_id"]),
            tuple(candidates),
            dict(committed),
            all(item.get("stage_status") == "existing" for item in staged),
        )


def capability_descriptor(*, release_id: str) -> dict[str, object]:
    return {
        "schema": "capability-provider/v1",
        "capability_id": CAPABILITY_ID,
        "provider": {"plugin_id": PLUGIN_ID, "release_id": release_id},
        "input_schema": "run-snapshot/v1",
        "output_schema": "result-bundle/v1",
        "result_contract": "candidate-batch/v1",
        "supports": ["run", "resume", "cancel", "validate"],
        "deterministic": True,
        "accepted_data_formats": [],
    }


class StoryStateWorker:
    def __init__(
        self,
        host: HostPort | None = None,
        *,
        execute: StoryStateExecutionPort | None = None,
        receipt_reader: CommittedReceiptReader | None = None,
    ) -> None:
        self._host, self._execute, self._receipt_reader = host, execute, receipt_reader
        self._lock = threading.RLock()
        self._worker_instance_id = str(uuid.uuid4())
        self._release_id: str | None = None
        self._shutdown = False
        self._replays: dict[tuple[str, str], tuple[str, Mapping[str, Any]]] = {}

    @staticmethod
    def _result(
        request: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, Any]:
        method = str(request["method"])
        try:
            validate_rpc_result(method, result, request=request)
        except ContractValidationError as exc:
            # The frozen v1 schema has byte-identical job.start/job.resume
            # success branches.  Method-field binding has already completed.
            if method not in {
                "job.start",
                "job.resume",
            } or "is valid under each of" not in str(exc):
                raise
        return {"jsonrpc": "2.0", "id": request["id"], "result": dict(result)}

    @staticmethod
    def _error(request: Mapping[str, Any], exc: Exception) -> dict[str, Any] | None:
        if request.get("method") == "runtime.heartbeat" and "id" not in request:
            return None
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "error": {
                "code": int(exc.code)
                if isinstance(exc, ContractError)
                else int(ErrorCode.INVALID_TRANSITION),
                "message": str(exc) or "Story-State worker rejected request",
                "data": None,
            },
        }

    def _replay(
        self, request: Mapping[str, Any], action: Callable[[], Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        key = (str(request["method"]), str(request["meta"]["operation_id"]))
        fingerprint = hash_jcs(
            "story-state-worker-request/v1",
            {name: value for name, value in request.items() if name != "id"},
        )
        existing = self._replays.get(key)
        if existing is not None:
            if existing[0] != fingerprint:
                raise StoryStateWorkerError(
                    ErrorCode.DUPLICATE_REQUEST,
                    "worker operation ID was reused with a different payload",
                )
            return existing[1]
        result = dict(action())
        self._replays[key] = (fingerprint, result)
        return result

    def _run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._host is None or self._execute is None or self._receipt_reader is None:
            raise StoryStateWorkerError(
                ErrorCode.INVALID_TRANSITION,
                "Story-State requires the composed Host/Core execution ports",
            )
        bind = getattr(self._host, "bind_meta", None)
        if callable(bind):
            bind(request["meta"])
        runtime_result = self._execute(
            request["params"],
            request["meta"],
            HostTerminalPort(self._host, self._receipt_reader),
        )
        if not isinstance(runtime_result, RuntimeResult):
            raise StoryStateWorkerError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Story-State execution port returned an invalid result",
            )
        receipt_id = runtime_result.committed_receipt.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            raise StoryStateWorkerError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Story-State runtime has no committed receipt",
            )
        return {
            "accepted": True,
            "worker_run_id": "story-state-run-"
            + sha256_hex(runtime_result.bundle["bundle_id"].encode("utf-8"))[:40],
            "provenance_receipt_id": receipt_id,
            "output_streams": [],
        }

    def _dispatch(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        validate_rpc_request(request)
        if self._shutdown:
            raise StoryStateWorkerError(
                ErrorCode.INVALID_TRANSITION, "Story-State worker is shut down"
            )
        method, meta, params = (
            str(request["method"]),
            request["meta"],
            request["params"],
        )
        if method == "runtime.handshake":
            if (
                params["host_protocol"] != "1"
                or params["generation_id"] != meta["generation_id"]
                or params["plugin_release_id"] != meta["plugin_release_id"]
            ):
                raise StoryStateWorkerError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "Story-State handshake is not identity-bound",
                )
            if (
                self._release_id is not None
                and self._release_id != meta["plugin_release_id"]
            ):
                raise StoryStateWorkerError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "Story-State worker cannot switch release in place",
                )
            self._release_id = str(meta["plugin_release_id"])
            return self._result(
                request,
                {
                    "plugin_protocol": "1",
                    "plugin_id": PLUGIN_ID,
                    "release_id": self._release_id,
                    "capabilities": [CAPABILITY_ID],
                    "worker_instance_id": self._worker_instance_id,
                },
            )
        if method == "runtime.health":
            return self._result(
                request,
                {"status": "ok", "details_asset_id": None, "checked_at": _now()},
            )
        if method == "runtime.heartbeat":
            return None
        if method == "capability.describe":
            if params["capability_id"] != CAPABILITY_ID or self._release_id is None:
                raise StoryStateWorkerError(
                    ErrorCode.INVALID_TRANSITION,
                    "unknown Story-State capability or missing handshake",
                )
            return self._result(
                request,
                {"descriptor": capability_descriptor(release_id=self._release_id)},
            )
        if method == "settings.validate":
            return self._result(
                request,
                {
                    "valid": False,
                    "evaluated_payload_hash": None,
                    "details_asset_id": None,
                    "errors": [],
                },
            )
        if method == "migration.plan":
            return self._result(
                request,
                {
                    "plan_id": "story-state-migration-plan",
                    "steps": [],
                    "backward_compatible": True,
                    "requires_verified_backup": False,
                },
            )
        if method == "migration.apply":
            return self._result(
                request, {"applied_schema": "0" * 64, "receipt_hash": "0" * 64}
            )
        if method == "migration.verify":
            return self._result(
                request, {"valid": True, "schema_hash": "0" * 64, "errors": []}
            )
        if method in {"job.start", "job.resume"}:
            if params["capability_id"] != CAPABILITY_ID:
                raise StoryStateWorkerError(
                    ErrorCode.INVALID_TRANSITION,
                    "Story-State job names another capability",
                )
            return self._result(
                request, self._replay(request, lambda: self._run(request))
            )
        if method == "job.pause":
            return self._result(
                request,
                self._replay(
                    request, lambda: {"accepted": False, "checkpoint_asset_id": None}
                ),
            )
        if method == "job.cancel":
            return self._result(
                request,
                self._replay(
                    request,
                    lambda: {
                        "accepted": True,
                        "terminal_known": False,
                        "attempt_state": "cancelling",
                    },
                ),
            )
        if method == "runtime.shutdown":
            result = self._result(request, {"accepted": True})
            self._shutdown = True
            return result
        raise StoryStateWorkerError(
            ErrorCode.INVALID_TRANSITION, f"Story-State does not implement {method}"
        )

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            try:
                return self._dispatch(request)
            except Exception as exc:  # noqa: BLE001 - never leak unframed worker errors
                return self._error(request, exc)

    def handle_frame(self, frame: bytes) -> bytes | None:
        decoder = FrameDecoder()
        messages = decoder.feed(frame)
        if len(messages) != 1 or decoder.buffer:
            raise StoryStateWorkerError(
                ErrorCode.INVALID_TRANSITION,
                "Story-State accepts exactly one RPC frame",
            )
        response = self.handle(messages[0])
        return None if response is None else encode_frame(response)


def serve(
    worker: StoryStateWorker,
    *,
    stdin: Any = None,
    stdout: Any = None,
    chunk_size: int = 65536,
) -> None:
    import sys

    source = sys.stdin.buffer if stdin is None else stdin
    target = sys.stdout.buffer if stdout is None else stdout
    decoder = FrameDecoder()
    read_chunk = getattr(source, "read1", source.read)
    while chunk := read_chunk(chunk_size):
        for request in decoder.feed(chunk):
            response = worker.handle(request)
            if response is not None:
                target.write(encode_frame(response))
                target.flush()
    if decoder.buffer:
        raise StoryStateWorkerError(
            ErrorCode.INVALID_TRANSITION, "Story-State received incomplete RPC input"
        )


def main(*, worker: StoryStateWorker | None = None) -> None:
    import sys

    serve(
        worker or StoryStateWorker(StdioHostPort(sys.stdin.buffer, sys.stdout.buffer)),
        stdin=sys.stdin.buffer,
        stdout=sys.stdout.buffer,
    )


__all__ = [
    "CAPABILITY_ID",
    "PLUGIN_ID",
    "CommittedReceiptReader",
    "HostPort",
    "HostTerminalPort",
    "StdioHostPort",
    "StoryStateExecutionPort",
    "StoryStateWorker",
    "StoryStateWorkerError",
    "capability_descriptor",
    "main",
    "serve",
]
