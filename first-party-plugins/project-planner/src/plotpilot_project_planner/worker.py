"""Installable Host-RPC worker for the Project Planner package.

The reviewed Planner runtime still owns frozen-input validation and deterministic
proposal preparation.  This module is deliberately only a process/Host adapter:
it creates content-addressed Assets, asks Core to stage Candidates, and completes
the current Job through published Host RPC calls.  It has no database, Publisher,
or durable replay authority.
"""

from __future__ import annotations

import base64
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from plotpilot_plugin_sdk import (
    ContractError,
    ContractValidationError,
    ErrorCode,
    canonical_bytes,
    hash_jcs,
    sha256_hex,
)
from plotpilot_plugin_sdk.framing import FrameDecoder, encode_frame
from plotpilot_plugin_sdk.rpc import HOST_METHODS, build_request
from plotpilot_plugin_sdk.verifier import (
    validate_rpc_request,
    validate_rpc_response,
    validate_rpc_result,
    verify_result_bundle,
)

from .runtime import (
    PLANNER_CAPABILITY_ID,
    PLANNER_PLUGIN_ID,
    PreparedPlannerProposal,
    planner_candidate_items,
)


class HostPort(Protocol):
    """The sole outbound capability surface available to this worker."""

    def call(
        self, method: str, params: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class PlannerPreparationPort(Protocol):
    """P3/Core composition supplies an already-validated Planner proposal."""

    def __call__(
        self, params: Mapping[str, Any], meta: Mapping[str, Any]
    ) -> PreparedPlannerProposal: ...


class PlannerWorkerError(ContractError):
    """Closed worker/Host boundary failure."""


class StdioHostPort:
    """Synchronous Host bridge over the accepted framed stdio protocol."""

    def __init__(self, stdin: Any, stdout: Any) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._decoder = FrameDecoder()
        self._meta: dict[str, Any] | None = None
        self._lock = threading.RLock()

    def bind_meta(self, meta: Mapping[str, Any]) -> None:
        self._meta = dict(meta)

    def call(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        if method not in HOST_METHODS:
            raise PlannerWorkerError(
                ErrorCode.INVALID_TRANSITION,
                "Planner may call only published Host RPC methods",
            )
        if self._meta is None:
            raise PlannerWorkerError(
                ErrorCode.INVALID_TRANSITION, "Host call requires bound request meta"
            )
        request = build_request(method, params, self._meta)
        validate_rpc_request(request)
        with self._lock:
            self._stdout.write(encode_frame(request))
            self._stdout.flush()
            while True:
                chunk = self._stdin.read(65536)
                if not chunk:
                    raise PlannerWorkerError(
                        ErrorCode.ASSET_ERROR,
                        "Host RPC channel ended before its response",
                    )
                messages = self._decoder.feed(chunk)
                if not messages:
                    continue
                if len(messages) != 1:
                    raise PlannerWorkerError(
                        ErrorCode.INVALID_TRANSITION,
                        "Host returned multiple frames for one request",
                    )
                response = messages[0]
                if "method" in response or response.get("id") != request["id"]:
                    raise PlannerWorkerError(
                        ErrorCode.INVALID_TRANSITION,
                        "Host response is not bound to the Planner request",
                    )
                validate_rpc_response(response, request=request)
                if "error" in response:
                    error = response["error"]
                    raise PlannerWorkerError(int(error["code"]), str(error["message"]))
                result = response.get("result")
                if not isinstance(result, Mapping):
                    raise PlannerWorkerError(
                        ErrorCode.RESULT_CONTRACT_MISMATCH,
                        "Host result must be an object",
                    )
                return dict(result)


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise PlannerWorkerError(
            ErrorCode.RESULT_CONTRACT_MISMATCH, f"{label} must be a Host identifier"
        )
    return value


def _asset_operation_key(prefix: str, content: bytes) -> str:
    return f"{prefix}-{sha256_hex(content)}"


def _upload_asset(host: HostPort, *, content: bytes, mime: str, prefix: str) -> str:
    raw = bytes(content)
    digest = sha256_hex(raw)
    upload_id = f"{prefix}-upload-{digest[:40]}"
    result = host.call(
        "host.asset.create/v1",
        {
            "operation_key": _asset_operation_key(f"{prefix}-asset", raw),
            "upload_id": upload_id,
            "offset": 0,
            "mime": mime,
            "total_size": len(raw),
            "expected_hash": digest,
            "chunk_hash": digest,
            "base64_chunk": base64.b64encode(raw).decode("ascii"),
            "final": True,
        },
    )
    if result.get("completed") is not True or result.get("accepted_bytes") != len(raw):
        raise PlannerWorkerError(
            ErrorCode.ASSET_ERROR, "Host did not complete the Planner Asset upload"
        )
    asset_id = result.get("asset_id")
    if not isinstance(asset_id, str) or not asset_id:
        raise PlannerWorkerError(
            ErrorCode.ASSET_ERROR, "Host Asset upload returned no Asset ID"
        )
    return asset_id


def _producer(meta: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "plugin_id": PLANNER_PLUGIN_ID,
        "release_id": meta["plugin_release_id"],
        "capability_id": PLANNER_CAPABILITY_ID,
        "job_id": meta["job_id"],
        "step_id": meta["step_id"],
        "attempt_id": meta["attempt_id"],
        "lease_epoch": meta["lease_epoch"],
    }


def _candidate_bundle(
    proposal: PreparedPlannerProposal,
    *,
    asset_ids: Mapping[str, str],
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    items = list(planner_candidate_items(proposal, payload_asset_ids=asset_ids))
    identity = {
        "operation_key": proposal.operation_key,
        "operation_payload_hash": proposal.operation_payload_hash,
        "snapshot_hash": proposal.frozen_run.snapshot_hash,
        "item_ids": [item["item_id"] for item in items],
    }
    return {
        "schema": "result-bundle/v1",
        "contract_id": "candidate-batch/v1",
        "bundle_id": "planner-bundle-"
        + hash_jcs("project-planner-bundle/v1", identity)[:40],
        "bundle_type": "candidate_batch",
        "producer": _producer(meta),
        "input_snapshot_hash": proposal.frozen_run.snapshot_hash,
        "items": items,
        "warnings": [],
        "partial": False,
        "provenance_receipt_id": "planner-receipt-"
        + proposal.operation_payload_hash[:40],
        "skill_chain_result_refs": [],
    }


class PlannerHostPublisher:
    """Publish a prepared proposal through Host/Core ports, never a Publisher."""

    def __init__(self, host: HostPort) -> None:
        self._host = host

    def submit(
        self, proposal: PreparedPlannerProposal, *, meta: Mapping[str, Any]
    ) -> Mapping[str, object]:
        if not isinstance(proposal, PreparedPlannerProposal):
            raise TypeError("Planner Host publisher requires a PreparedPlannerProposal")
        asset_ids = {
            candidate.role: _upload_asset(
                self._host,
                content=candidate.payload,
                mime=candidate.mime,
                prefix="planner",
            )
            for candidate in proposal.candidates
        }
        bundle = _candidate_bundle(proposal, asset_ids=asset_ids, meta=meta)
        verify_result_bundle(
            bundle,
            snapshot_workspace_id=proposal.frozen_run.workspace_id,
            snapshot_hash_value=proposal.frozen_run.snapshot_hash,
        )
        bundle_bytes = canonical_bytes(bundle)
        bundle_asset_id = _upload_asset(
            self._host,
            content=bundle_bytes,
            mime="application/json",
            prefix="planner-bundle",
        )
        stage_key = "planner-stage-" + proposal.operation_payload_hash[:40]
        stage = self._host.call(
            "host.candidate.stage/v1",
            {
                "operation_key": stage_key,
                "result_bundle_asset_id": bundle_asset_id,
                "input_snapshot_hash": proposal.frozen_run.snapshot_hash,
            },
        )
        if stage.get("accepted") is not True:
            raise PlannerWorkerError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Core did not accept the Planner Candidate batch",
            )
        worker_run_id = "planner-run-" + proposal.operation_payload_hash[:40]
        complete = self._host.call(
            "host.job.complete/v1",
            {
                "operation_key": proposal.operation_key,
                "worker_run_id": worker_run_id,
                "outcome": "succeeded",
                "result_bundle_asset_id": bundle_asset_id,
                "candidate_stage_operation_key": stage_key,
                "terminal_detail_asset_id": None,
                "local_seq": 1,
            },
        )
        if complete.get("accepted") is not True:
            raise PlannerWorkerError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Core did not accept Planner terminal completion",
            )
        return {
            "accepted": True,
            "worker_run_id": worker_run_id,
            "provenance_receipt_id": _id(
                complete.get("provenance_receipt_id"), "provenance_receipt_id"
            ),
            "output_streams": [],
        }


def capability_descriptor(*, release_id: str) -> dict[str, object]:
    return {
        "schema": "capability-provider/v1",
        "capability_id": PLANNER_CAPABILITY_ID,
        "provider": {"plugin_id": PLANNER_PLUGIN_ID, "release_id": release_id},
        "input_schema": "run-snapshot/v1",
        "output_schema": "result-bundle/v1",
        "result_contract": "candidate-batch/v1",
        "supports": ["run", "resume", "cancel", "validate"],
        "deterministic": True,
        "accepted_data_formats": [],
    }


class PlannerWorker:
    """Minimal installable worker which delegates domain preparation to composition."""

    def __init__(
        self,
        host: HostPort | None = None,
        *,
        prepare: PlannerPreparationPort | None = None,
    ) -> None:
        self._host = host
        self._prepare = prepare
        self._lock = threading.RLock()
        self._worker_instance_id = str(uuid.uuid4())
        self._release_id: str | None = None
        self._shutdown = False
        self._replays: dict[tuple[str, str], tuple[str, Mapping[str, Any]]] = {}

    @staticmethod
    def _error(request: Mapping[str, Any], exc: Exception) -> dict[str, Any] | None:
        if request.get("method") == "runtime.heartbeat" and "id" not in request:
            return None
        code = (
            int(exc.code)
            if isinstance(exc, ContractError)
            else int(ErrorCode.INVALID_TRANSITION)
        )
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "error": {
                "code": code,
                "message": str(exc) or "Planner worker rejected request",
                "data": None,
            },
        }

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

    def _request_fingerprint(self, request: Mapping[str, Any]) -> str:
        stable = {key: value for key, value in request.items() if key != "id"}
        return hash_jcs("project-planner-worker-request/v1", stable)

    def _replay_or_remember(
        self, request: Mapping[str, Any], action: Callable[[], Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        meta = request["meta"]
        key = (str(request["method"]), str(meta["operation_id"]))
        fingerprint = self._request_fingerprint(request)
        previous = self._replays.get(key)
        if previous is not None:
            if previous[0] != fingerprint:
                raise PlannerWorkerError(
                    ErrorCode.DUPLICATE_REQUEST,
                    "worker operation ID was reused with a different payload",
                )
            return previous[1]
        result = dict(action())
        self._replays[key] = (fingerprint, result)
        return result

    def _require_host(self, request: Mapping[str, Any]) -> HostPort:
        if self._host is None:
            raise PlannerWorkerError(
                ErrorCode.INVALID_TRANSITION,
                "Planner requires the accepted Host/Core composition",
            )
        bind = getattr(self._host, "bind_meta", None)
        if callable(bind):
            bind(request["meta"])
        return self._host

    def _run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._prepare is None:
            raise PlannerWorkerError(
                ErrorCode.INVALID_TRANSITION,
                "Planner requires the composed RunSnapshot preparation port",
            )
        host = self._require_host(request)
        proposal = self._prepare(request["params"], request["meta"])
        return PlannerHostPublisher(host).submit(proposal, meta=request["meta"])

    def _dispatch(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        validate_rpc_request(request)
        if self._shutdown:
            raise PlannerWorkerError(
                ErrorCode.INVALID_TRANSITION, "Planner worker is shut down"
            )
        method = str(request["method"])
        meta = request["meta"]
        if method == "runtime.handshake":
            params = request["params"]
            if (
                params["host_protocol"] != "1"
                or params["generation_id"] != meta["generation_id"]
                or params["plugin_release_id"] != meta["plugin_release_id"]
            ):
                raise PlannerWorkerError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "Planner Host handshake is not identity-bound",
                )
            if (
                self._release_id is not None
                and self._release_id != meta["plugin_release_id"]
            ):
                raise PlannerWorkerError(
                    ErrorCode.INCOMPATIBLE_GENERATION,
                    "Planner worker cannot switch release in place",
                )
            self._release_id = str(meta["plugin_release_id"])
            return self._result(
                request,
                {
                    "plugin_protocol": "1",
                    "plugin_id": PLANNER_PLUGIN_ID,
                    "release_id": self._release_id,
                    "capabilities": [PLANNER_CAPABILITY_ID],
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
            if (
                request["params"]["capability_id"] != PLANNER_CAPABILITY_ID
                or self._release_id is None
            ):
                raise PlannerWorkerError(
                    ErrorCode.INVALID_TRANSITION,
                    "unknown Planner capability or missing handshake",
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
                    "plan_id": "planner-migration-plan",
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
            if request["params"]["capability_id"] != PLANNER_CAPABILITY_ID:
                raise PlannerWorkerError(
                    ErrorCode.INVALID_TRANSITION,
                    "Planner job request names another capability",
                )
            return self._result(
                request, self._replay_or_remember(request, lambda: self._run(request))
            )
        if method == "job.pause":
            return self._result(
                request,
                self._replay_or_remember(
                    request, lambda: {"accepted": False, "checkpoint_asset_id": None}
                ),
            )
        if method == "job.cancel":
            return self._result(
                request,
                self._replay_or_remember(
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
        raise PlannerWorkerError(
            ErrorCode.INVALID_TRANSITION, f"Planner does not implement {method}"
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
            raise PlannerWorkerError(
                ErrorCode.INVALID_TRANSITION, "Planner accepts exactly one RPC frame"
            )
        response = self.handle(messages[0])
        return None if response is None else encode_frame(response)


def serve(
    worker: PlannerWorker,
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
        for message in decoder.feed(chunk):
            response = worker.handle(message)
            if response is not None:
                target.write(encode_frame(response))
                target.flush()
    if decoder.buffer:
        raise PlannerWorkerError(
            ErrorCode.INVALID_TRANSITION,
            "Planner received an incomplete RPC frame at EOF",
        )


def main(*, worker: PlannerWorker | None = None) -> None:
    """Run the entrypoint; a no-argument process fails closed on job execution."""

    import sys

    serve(
        worker or PlannerWorker(StdioHostPort(sys.stdin.buffer, sys.stdout.buffer)),
        stdin=sys.stdin.buffer,
        stdout=sys.stdout.buffer,
    )


__all__ = [
    "HostPort",
    "PlannerHostPublisher",
    "PlannerPreparationPort",
    "PlannerWorker",
    "PlannerWorkerError",
    "StdioHostPort",
    "capability_descriptor",
    "main",
    "serve",
]
