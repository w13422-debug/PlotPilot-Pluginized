from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from plotpilot_plugin_sdk.canonical import canonical_bytes, sha256_hex
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode
from plotpilot_plugin_sdk.fake_provider import FakeProvider
from plotpilot_plugin_sdk.framing import (
    FrameDecoder,
    decode_frame,
    encode_frame,
)
from plotpilot_plugin_sdk.package import release_id
from plotpilot_plugin_sdk.rpc import (
    ChunkUploadLedger,
    OperationLedger,
    build_meta,
    build_request,
)
from plotpilot_plugin_sdk.stdio_worker import (
    FramedStdioWorker,
    WorkerContext,
)
from plotpilot_plugin_sdk.verifier import (
    request_key,
    snapshot_hash,
    validate_rpc_response,
)


def test_frame_is_incremental_and_rejects_trailing_bytes() -> None:
    frame = encode_frame({"jsonrpc": "2.0", "method": "runtime.heartbeat"})
    decoder = FrameDecoder()
    assert decoder.feed(frame[:10]) == []
    assert decoder.feed(frame[10:]) == [
        {"jsonrpc": "2.0", "method": "runtime.heartbeat"}
    ]
    with pytest.raises(ContractError):
        decode_frame(frame + b"x")


def test_operation_ledger_replays_exact_frame_and_rejects_payload_drift() -> None:
    ledger = OperationLedger()
    calls = 0

    def action() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"accepted": True, "value": "stable"}

    first = ledger.apply_frame(
        "attempt-1",
        "host.job.event/v1",
        "op-1",
        {"x": 1},
        action,
        response_id="123e4567-e89b-12d3-a456-426614174000",
    )
    second = ledger.apply_frame(
        "attempt-1",
        "host.job.event/v1",
        "op-1",
        {"x": 1},
        action,
        response_id="different",
    )
    assert first == second
    assert calls == 1
    with pytest.raises(ContractError) as caught:
        ledger.apply("attempt-1", "host.job.event/v1", "op-1", {"x": 2}, action)
    assert caught.value.code == ErrorCode.DUPLICATE_REQUEST


def test_chunk_upload_covers_final_hash_and_status_recovery() -> None:
    ledger = ChunkUploadLedger()
    content = b"hello"
    digest = hashlib.sha256(content).hexdigest()
    result = ledger.create(
        operation_key="op-1",
        upload_id="upload-1",
        offset=0,
        total_size=5,
        expected_hash=digest,
        chunk_hash=digest,
        base64_chunk="aGVsbG8=",
        final=True,
    )
    assert result["completed"] is True
    assert ledger.status("upload-1", digest)["asset_id"] == "asset-upload-upload-1"
    with pytest.raises(ContractError) as caught:
        ledger.create(
            operation_key="op-2",
            upload_id="upload-1",
            offset=0,
            total_size=5,
            expected_hash=digest,
            chunk_hash=digest,
            base64_chunk="aGVsbG8=",
            final=False,
        )
    assert caught.value.code == ErrorCode.INVALID_TRANSITION


def test_fake_provider_stream_pause_resume_cancel_and_receipt_are_deterministic() -> (
    None
):
    provider = FakeProvider()
    run = provider.start("invocation-1", "request", chunks=("a", "b"))
    assert [chunk.text for chunk in provider.stream(run.run_id)] == ["a", "ab"]
    receipt = provider.receipt(run.run_id)
    assert receipt.state == "succeeded"
    assert receipt.response_hash == hashlib.sha256(b"ab").hexdigest()


PLUGIN_ID = "com.plotpilot.shared-worker-test"
PLUGIN_VERSION = "1.0.0"
CAPABILITY_ID = "shared.worker.run/v1"
PACKAGE_HASH = "a" * 64
DATA_GENERATION_ID = "data-generation-1"
GENERATION_ID = "generation-1"
DEADLINE = "2026-09-04T12:00:00Z"
RELEASE_ID = release_id(PLUGIN_ID, PLUGIN_VERSION, PACKAGE_HASH)


def _rpc_id(value: int) -> str:
    return f"00000000-0000-4000-8000-{value:012x}"


def _control_meta(
    operation_id: str,
    *,
    generation_id: str = GENERATION_ID,
    plugin_release_id: str = RELEASE_ID,
) -> dict[str, object]:
    return build_meta(
        "control",
        generation_id=generation_id,
        plugin_release_id=plugin_release_id,
        deadline_at=DEADLINE,
        operation_id=operation_id,
    )


def _attempt_meta(
    operation_id: str,
    *,
    job_id: str = "job-1",
    step_id: str = "step-1",
    attempt_id: str = "attempt-1",
    lease_epoch: int = 1,
    generation_id: str = GENERATION_ID,
    plugin_release_id: str = RELEASE_ID,
) -> dict[str, object]:
    return build_meta(
        "attempt",
        generation_id=generation_id,
        plugin_release_id=plugin_release_id,
        deadline_at=DEADLINE,
        operation_id=operation_id,
        job_id=job_id,
        step_id=step_id,
        attempt_id=attempt_id,
        lease_epoch=lease_epoch,
    )


def _request(
    method: str,
    params: dict[str, object],
    meta: dict[str, object],
    request_id: int,
) -> dict[str, object]:
    return build_request(method, params, meta, request_id=_rpc_id(request_id))


def _handshake(
    request_id: int = 1,
    *,
    generation_id: str = GENERATION_ID,
    plugin_release_id: str = RELEASE_ID,
    data_generation_id: str | None = DATA_GENERATION_ID,
) -> dict[str, object]:
    return _request(
        "runtime.handshake",
        {
            "host_protocol": "1",
            "generation_id": generation_id,
            "plugin_release_id": plugin_release_id,
            "data_generation_id": data_generation_id,
        },
        _control_meta(
            f"handshake-{request_id}",
            generation_id=generation_id,
            plugin_release_id=plugin_release_id,
        ),
        request_id,
    )


def _health(
    request_id: int,
    *,
    operation_id: str | None = None,
    generation_id: str = GENERATION_ID,
    plugin_release_id: str = RELEASE_ID,
) -> dict[str, object]:
    return _request(
        "runtime.health",
        {
            "probe_id": f"probe-{request_id}",
            "db_lease_id": None,
            "db_lease_epoch": None,
            "owner_instance_id": None,
        },
        _control_meta(
            operation_id or f"health-{request_id}",
            generation_id=generation_id,
            plugin_release_id=plugin_release_id,
        ),
        request_id,
    )


def _describe(request_id: int) -> dict[str, object]:
    return _request(
        "capability.describe",
        {"capability_id": CAPABILITY_ID},
        _control_meta(f"describe-{request_id}"),
        request_id,
    )


def _start(
    request_id: int,
    *,
    asset_id: str = "run-snapshot-asset",
    attempt_id: str = "attempt-1",
    lease_epoch: int = 1,
    job_id: str = "job-1",
    step_id: str = "step-1",
) -> dict[str, object]:
    return _request(
        "job.start",
        {
            "capability_id": CAPABILITY_ID,
            "run_snapshot_asset_id": asset_id,
            "checkpoint_asset_id": None,
            "secrets": [],
        },
        _attempt_meta(
            f"start-{request_id}",
            job_id=job_id,
            step_id=step_id,
            attempt_id=attempt_id,
            lease_epoch=lease_epoch,
        ),
        request_id,
    )


def _resume(
    request_id: int,
    *,
    asset_id: str = "run-snapshot-asset",
    attempt_id: str = "attempt-2",
    resume_of_attempt_id: str = "attempt-1",
    lease_epoch: int = 1,
    job_id: str = "job-1",
    step_id: str = "step-1",
) -> dict[str, object]:
    return _request(
        "job.resume",
        {
            "capability_id": CAPABILITY_ID,
            "run_snapshot_asset_id": asset_id,
            "resume_of_attempt_id": resume_of_attempt_id,
            "checkpoint_asset_id": None,
            "resume_intent_id": f"resume-intent-{request_id}",
            "resume_reason": "retry",
            "secrets": [],
        },
        _attempt_meta(
            f"resume-{request_id}",
            job_id=job_id,
            step_id=step_id,
            attempt_id=attempt_id,
            lease_epoch=lease_epoch,
        ),
        request_id,
    )


def _cancel(
    request_id: int,
    *,
    worker_run_id: str = "worker-run-1",
    attempt_id: str = "attempt-1",
    lease_epoch: int = 1,
    job_id: str = "job-1",
    step_id: str = "step-1",
) -> dict[str, object]:
    return _request(
        "job.cancel",
        {"worker_run_id": worker_run_id, "reason": "requested"},
        _attempt_meta(
            f"cancel-{request_id}",
            job_id=job_id,
            step_id=step_id,
            attempt_id=attempt_id,
            lease_epoch=lease_epoch,
        ),
        request_id,
    )


def _shutdown(request_id: int, *, operation_id: str | None = None) -> dict[str, object]:
    return _request(
        "runtime.shutdown",
        {"reason": "test", "deadline_at": DEADLINE},
        _control_meta(operation_id or f"shutdown-{request_id}"),
        request_id,
    )


def _settings_validate(request_id: int) -> dict[str, object]:
    return _request(
        "settings.validate",
        {
            "settings_revision_id": f"settings-revision-{request_id}",
            "plugin_release_id": RELEASE_ID,
            "schema_hash": "c" * 64,
            "payload_asset_id": "settings-payload-asset",
        },
        _control_meta(f"settings-validate-{request_id}"),
        request_id,
    )


def _snapshot(
    *,
    package_hash: str = PACKAGE_HASH,
    release_value: str | None = None,
    data_generation_id: str | None = DATA_GENERATION_ID,
    workspace_id: str = "ws-1",
    operation: str = CAPABILITY_ID,
    mutate_without_rehash: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    value = json.loads(
        (ROOT / "contracts" / "examples" / "run-snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    value["workspace_id"] = workspace_id
    value["scope"]["operation"] = operation
    value["plugin_releases"] = [
        {
            "plugin_id": PLUGIN_ID,
            "release_id": release_value
            or release_id(PLUGIN_ID, PLUGIN_VERSION, package_hash),
            "package_hash": package_hash,
            "data_generation_id": data_generation_id,
        }
    ]
    value["request_key"] = request_key(value)
    value["snapshot_hash"] = snapshot_hash(value)
    if mutate_without_rehash is not None:
        mutate_without_rehash(value)
    return value


def _descriptor(release_value: str) -> dict[str, object]:
    return {
        "schema": "capability-provider/v1",
        "capability_id": CAPABILITY_ID,
        "provider": {"plugin_id": PLUGIN_ID, "release_id": release_value},
        "input_schema": "run-snapshot/v1",
        "output_schema": "result-bundle/v1",
        "result_contract": "candidate-batch/v1",
        "supports": ["run", "resume", "cancel"],
        "deterministic": True,
        "accepted_data_formats": [],
    }


def _job_result(worker_run_id: str) -> dict[str, object]:
    return {
        "accepted": True,
        "worker_run_id": worker_run_id,
        "provenance_receipt_id": f"receipt-{worker_run_id}",
        "output_streams": [],
    }


def _host_response(request_id: str, result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _asset_read_response(
    request_id: str,
    raw: bytes,
    *,
    next_offset: int | None = None,
) -> dict[str, object]:
    return _host_response(
        request_id,
        {
            "base64_chunk": base64.b64encode(raw).decode("ascii"),
            "next_offset": next_offset,
            "content_hash": sha256_hex(raw),
        },
    )


def _upload_identity(operation_key: str, data: bytes) -> str:
    digest = sha256_hex(data)
    return (
        "sdk-upload-"
        + hashlib.sha256(f"{operation_key}\n{digest}\n".encode("ascii")).hexdigest()[
            :40
        ]
    )


def _decode_messages(raw: bytes) -> list[dict[str, object]]:
    decoder = FrameDecoder()
    messages = decoder.feed(raw)
    assert decoder.buffer == bytearray()
    return messages


def _serve(
    worker: FramedStdioWorker,
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    stdout = io.BytesIO()
    worker.serve(
        stdin=io.BytesIO(b"".join(encode_frame(message) for message in messages)),
        stdout=stdout,
    )
    return _decode_messages(stdout.getvalue())


def _serve_raw(worker: FramedStdioWorker, raw: bytes) -> list[dict[str, object]]:
    stdout = io.BytesIO()
    worker.serve(stdin=io.BytesIO(raw), stdout=stdout)
    return _decode_messages(stdout.getvalue())


def _response_for(
    output: list[dict[str, object]],
    request: Mapping[str, object],
) -> dict[str, object]:
    matches = [message for message in output if message.get("id") == request["id"]]
    assert len(matches) == 1
    return matches[0]


def _error_code(response: Mapping[str, object]) -> int:
    error = response["error"]
    assert isinstance(error, Mapping)
    code = error["code"]
    assert isinstance(code, int)
    return code


def _new_worker(
    host_request_ids: list[str] | None = None,
    *,
    start: Callable[[Mapping[str, object], WorkerContext], Mapping[str, object]]
    | None = None,
    resume: Callable[[Mapping[str, object], WorkerContext], Mapping[str, object]]
    | None = None,
    cancel: Callable[[Mapping[str, object], WorkerContext], Mapping[str, object]]
    | None = None,
    max_pending_calls: int = 8,
) -> FramedStdioWorker:
    ids = deque(host_request_ids or [])

    def next_id() -> str:
        return ids.popleft()

    worker = FramedStdioWorker(
        plugin_id=PLUGIN_ID,
        plugin_version=PLUGIN_VERSION,
        release_id=RELEASE_ID,
        worker_instance_id="shared-worker-instance-1",
        clock=lambda: "2026-09-04T12:00:01Z",
        request_id_factory=next_id,
        max_pending_calls=max_pending_calls,
    )
    worker.register_domain(
        CAPABILITY_ID,
        descriptor=_descriptor,
        start=start,
        resume=resume,
        cancel=cancel,
    )
    return worker


def test_shared_stdio_worker_full_bidirectional_lifecycle_and_asset_helpers() -> None:
    snapshot_raw = canonical_bytes(_snapshot())
    created = b"candidate bundle"
    host_ids = [_rpc_id(900), _rpc_id(901), _rpc_id(902)]
    observed: list[tuple[str, str, str, str, int]] = []

    def start(
        _params: Mapping[str, object],
        context: WorkerContext,
    ) -> Mapping[str, object]:
        assert context.identity is not None
        assert context.run_snapshot is not None
        observed.append(
            (
                "start",
                context.identity.workspace_id,
                context.identity.job_id,
                context.identity.attempt_id,
                context.identity.lease_epoch,
            )
        )
        asset = context.assets.create(
            created,
            operation_key="candidate-bundle-create",
        )
        assert asset.asset_id == "created-asset-1"
        context.host.heartbeat(observed_at="2026-09-04T12:00:02Z", local_seq=1)
        return _job_result("worker-run-1")

    def resume(
        _params: Mapping[str, object],
        context: WorkerContext,
    ) -> Mapping[str, object]:
        assert context.identity is not None
        observed.append(
            (
                "resume",
                context.identity.workspace_id,
                context.identity.job_id,
                context.identity.attempt_id,
                context.identity.lease_epoch,
            )
        )
        return _job_result("worker-run-2")

    def cancel(
        _params: Mapping[str, object],
        context: WorkerContext,
    ) -> Mapping[str, object]:
        assert context.identity is not None
        observed.append(
            (
                "cancel",
                context.identity.workspace_id,
                context.identity.job_id,
                context.identity.attempt_id,
                context.identity.lease_epoch,
            )
        )
        return {
            "accepted": True,
            "terminal_known": False,
            "attempt_state": "cancelling",
        }

    worker = _new_worker(
        host_ids,
        start=start,
        resume=resume,
        cancel=cancel,
    )
    upload_id = _upload_identity("candidate-bundle-create", created)
    messages = [
        _handshake(),
        _health(2),
        _describe(3),
        _start(4),
        _asset_read_response(host_ids[0], snapshot_raw),
        _host_response(
            host_ids[1],
            {
                "upload_id": upload_id,
                "accepted_bytes": len(created),
                "completed": True,
                "asset_id": "created-asset-1",
            },
        ),
        _resume(5),
        _asset_read_response(host_ids[2], snapshot_raw),
        _cancel(6, worker_run_id="worker-run-2", attempt_id="attempt-2"),
        _shutdown(7),
        _health(8),
    ]

    output = _serve(worker, messages)

    assert worker.state == "shutdown"
    assert worker.pending_call_count == 0
    assert observed == [
        ("start", "ws-1", "job-1", "attempt-1", 1),
        ("resume", "ws-1", "job-1", "attempt-2", 1),
        ("cancel", "ws-1", "job-1", "attempt-2", 1),
    ]
    assert output[0]["result"]["plugin_id"] == PLUGIN_ID
    assert output[1]["result"]["status"] == "ok"
    assert output[2]["result"]["descriptor"]["capability_id"] == CAPABILITY_ID
    assert output[3]["method"] == "host.asset.read/v1"
    assert output[4]["method"] == "host.asset.create/v1"
    assert output[5]["method"] == "runtime.heartbeat"
    assert output[6]["result"]["worker_run_id"] == "worker-run-1"
    assert output[7]["method"] == "host.asset.read/v1"
    assert output[8]["result"]["worker_run_id"] == "worker-run-2"
    assert output[9]["result"]["attempt_state"] == "cancelling"
    assert output[10]["result"] == {"accepted": True}
    assert len(output) == 11
    for request, response in (
        (messages[0], output[0]),
        (messages[1], output[1]),
        (messages[2], output[2]),
        (messages[8], output[9]),
        (messages[9], output[10]),
    ):
        validate_rpc_response(response, request=request)


def test_shared_stdio_worker_recovers_after_malformed_unknown_and_missing_handler() -> (
    None
):
    malformed = _health(2)
    del malformed["params"]["probe_id"]
    unknown = _health(3)
    unknown["method"] = "runtime.unknown"
    missing_handler = _settings_validate(4)
    healthy = _health(5)
    shutdown = _shutdown(6)
    worker = _new_worker()

    output = _serve(
        worker,
        [_handshake(), malformed, unknown, missing_handler, healthy, shutdown],
    )

    for request in (malformed, unknown, missing_handler):
        response = _response_for(output, request)
        assert "error" in response
        error = response["error"]
        assert isinstance(error, Mapping)
        data = error["data"]
        assert isinstance(data, Mapping)
        assert str(data["error_id"]).startswith("stdio-worker-error-")
    assert _error_code(_response_for(output, unknown)) == ErrorCode.INVALID_TRANSITION
    assert (
        _error_code(_response_for(output, missing_handler))
        == ErrorCode.INVALID_TRANSITION
    )
    assert _response_for(output, healthy)["result"]["status"] == "ok"
    assert _response_for(output, shutdown)["result"] == {"accepted": True}
    assert worker.state == "shutdown"


def test_shared_stdio_worker_generic_handler_registry() -> None:
    calls = 0

    def validate_settings(
        params: Mapping[str, object],
        context: WorkerContext,
    ) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        assert params["plugin_release_id"] == RELEASE_ID
        assert context.identity is None
        return {
            "valid": True,
            "evaluated_payload_hash": "d" * 64,
            "details_asset_id": None,
            "errors": [],
        }

    request = _settings_validate(2)
    shutdown = _shutdown(3)
    worker = _new_worker()
    worker.register_handler("settings.validate", validate_settings)

    output = _serve(worker, [_handshake(), request, shutdown])

    assert calls == 1
    assert _response_for(output, request)["result"]["valid"] is True
    assert _response_for(output, shutdown)["result"] == {"accepted": True}


def test_shared_stdio_worker_rejects_duplicate_request_and_operation_ids() -> None:
    first = _health(2)
    duplicate_request = json.loads(json.dumps(first))
    first_operation = _health(3, operation_id="shared-operation-id")
    duplicate_operation = _health(4, operation_id="shared-operation-id")
    healthy = _health(5)
    shutdown = _shutdown(6)
    worker = _new_worker()

    output = _serve(
        worker,
        [
            _handshake(),
            first,
            duplicate_request,
            first_operation,
            duplicate_operation,
            healthy,
            shutdown,
        ],
    )

    repeated = [message for message in output if message.get("id") == first["id"]]
    assert len(repeated) == 2
    assert repeated[0]["result"]["status"] == "ok"
    assert _error_code(repeated[1]) == ErrorCode.DUPLICATE_REQUEST
    assert _response_for(output, first_operation)["result"]["status"] == "ok"
    assert (
        _error_code(_response_for(output, duplicate_operation))
        == ErrorCode.DUPLICATE_REQUEST
    )
    assert _response_for(output, healthy)["result"]["status"] == "ok"
    assert worker.state == "shutdown"


@pytest.mark.parametrize(
    ("identity_case", "expected_code"),
    [
        ("package", ErrorCode.INCOMPATIBLE_GENERATION),
        ("release", ErrorCode.INCOMPATIBLE_GENERATION),
        ("data-generation", ErrorCode.INCOMPATIBLE_GENERATION),
        ("operation", ErrorCode.RESULT_CONTRACT_MISMATCH),
        ("snapshot-hash", ErrorCode.RESULT_CONTRACT_MISMATCH),
    ],
)
def test_shared_stdio_worker_fences_run_snapshot_identity_before_handler(
    identity_case: str,
    expected_code: ErrorCode,
) -> None:
    calls = 0

    def start(
        _params: Mapping[str, object],
        _context: WorkerContext,
    ) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        return _job_result("worker-run-forbidden")

    if identity_case == "package":
        snapshot = _snapshot(package_hash="b" * 64, release_value=RELEASE_ID)
    elif identity_case == "release":
        snapshot = _snapshot(release_value="b" * 64)
    elif identity_case == "data-generation":
        snapshot = _snapshot(data_generation_id="data-generation-2")
    elif identity_case == "operation":
        snapshot = _snapshot(operation="different.operation/v1")
    else:
        snapshot = _snapshot(
            mutate_without_rehash=lambda value: value.__setitem__(
                "snapshot_hash", "0" * 64
            )
        )
    host_id = _rpc_id(900)
    start_request = _start(2)
    healthy = _health(3)
    shutdown = _shutdown(4)
    worker = _new_worker([host_id], start=start)

    output = _serve(
        worker,
        [
            _handshake(),
            start_request,
            _asset_read_response(host_id, canonical_bytes(snapshot)),
            healthy,
            shutdown,
        ],
    )

    assert calls == 0
    assert _error_code(_response_for(output, start_request)) == expected_code
    assert _response_for(output, healthy)["result"]["status"] == "ok"
    assert worker.pending_call_count == 0
    assert worker.state == "shutdown"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation_id", "generation-foreign"),
        ("plugin_release_id", "b" * 64),
    ],
)
def test_shared_stdio_worker_fences_handshake_identity_before_host_or_handler(
    field: str,
    value: str,
) -> None:
    calls = 0

    def start(
        _params: Mapping[str, object],
        _context: WorkerContext,
    ) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        return _job_result("worker-run-forbidden")

    start_request = _start(2)
    start_request["meta"][field] = value
    healthy = _health(3)
    shutdown = _shutdown(4)
    worker = _new_worker(start=start)

    output = _serve(worker, [_handshake(), start_request, healthy, shutdown])

    assert calls == 0
    assert (
        _error_code(_response_for(output, start_request))
        == ErrorCode.INCOMPATIBLE_GENERATION
    )
    assert not any(message.get("method") == "host.asset.read/v1" for message in output)
    assert _response_for(output, healthy)["result"]["status"] == "ok"
    assert worker.state == "shutdown"


@pytest.mark.parametrize(
    ("drift", "expected_code"),
    [
        ("workspace", ErrorCode.STALE_LEASE),
        ("job", ErrorCode.STALE_LEASE),
        ("step", ErrorCode.STALE_LEASE),
        ("resume-edge", ErrorCode.STALE_LEASE),
        ("attempt", ErrorCode.INVALID_TRANSITION),
        ("run-snapshot", ErrorCode.STALE_LEASE),
    ],
)
def test_shared_stdio_worker_rejects_resume_identity_drift(
    drift: str,
    expected_code: ErrorCode,
) -> None:
    start_calls = 0
    resume_calls = 0

    def start(
        _params: Mapping[str, object],
        _context: WorkerContext,
    ) -> Mapping[str, object]:
        nonlocal start_calls
        start_calls += 1
        return _job_result("worker-run-1")

    def resume(
        _params: Mapping[str, object],
        _context: WorkerContext,
    ) -> Mapping[str, object]:
        nonlocal resume_calls
        resume_calls += 1
        return _job_result("worker-run-forbidden")

    resume_options: dict[str, object] = {}
    resume_snapshot = _snapshot()
    if drift == "workspace":
        resume_snapshot = _snapshot(workspace_id="workspace-foreign")
    elif drift == "job":
        resume_options["job_id"] = "job-foreign"
    elif drift == "step":
        resume_options["step_id"] = "step-foreign"
    elif drift == "resume-edge":
        resume_options["resume_of_attempt_id"] = "attempt-foreign"
    elif drift == "attempt":
        resume_options["attempt_id"] = "attempt-1"
    else:
        resume_options["asset_id"] = "run-snapshot-asset-foreign"

    host_ids = [_rpc_id(900), _rpc_id(901)]
    start_request = _start(2)
    resume_request = _resume(3, **resume_options)
    healthy = _health(4)
    shutdown = _shutdown(5)
    worker = _new_worker(host_ids, start=start, resume=resume)
    initial_raw = canonical_bytes(_snapshot())

    output = _serve(
        worker,
        [
            _handshake(),
            start_request,
            _asset_read_response(host_ids[0], initial_raw),
            resume_request,
            _asset_read_response(host_ids[1], canonical_bytes(resume_snapshot)),
            healthy,
            shutdown,
        ],
    )

    assert start_calls == 1
    assert resume_calls == 0
    assert _error_code(_response_for(output, resume_request)) == expected_code
    assert _response_for(output, healthy)["result"]["status"] == "ok"
    assert worker.pending_call_count == 0
    assert worker.state == "shutdown"


def test_shared_stdio_worker_rejects_stale_lease_and_wrong_attempt_cancel() -> None:
    cancel_calls = 0

    def start(
        _params: Mapping[str, object],
        _context: WorkerContext,
    ) -> Mapping[str, object]:
        return _job_result("worker-run-1")

    def cancel(
        _params: Mapping[str, object],
        context: WorkerContext,
    ) -> Mapping[str, object]:
        nonlocal cancel_calls
        cancel_calls += 1
        assert context.identity is not None
        assert context.identity.lease_epoch == 3
        return {
            "accepted": True,
            "terminal_known": False,
            "attempt_state": "cancelling",
        }

    host_id = _rpc_id(900)
    start_request = _start(2, lease_epoch=2)
    stale = _cancel(3, lease_epoch=1)
    wrong_attempt = _cancel(4, attempt_id="attempt-foreign")
    current = _cancel(5, lease_epoch=3)
    shutdown = _shutdown(6)
    worker = _new_worker([host_id], start=start, cancel=cancel)

    output = _serve(
        worker,
        [
            _handshake(),
            start_request,
            _asset_read_response(host_id, canonical_bytes(_snapshot())),
            stale,
            wrong_attempt,
            current,
            shutdown,
        ],
    )

    assert _error_code(_response_for(output, stale)) == ErrorCode.STALE_LEASE
    assert _error_code(_response_for(output, wrong_attempt)) == ErrorCode.STALE_LEASE
    assert _response_for(output, current)["result"]["attempt_state"] == "cancelling"
    assert cancel_calls == 1
    assert worker.state == "shutdown"


def test_shared_stdio_worker_does_not_correlate_a_stale_host_response() -> None:
    host_ids = [_rpc_id(900), _rpc_id(901)]

    def start(
        _params: Mapping[str, object],
        context: WorkerContext,
    ) -> Mapping[str, object]:
        context.host.call(
            "host.asset.read/v1",
            {"asset_id": "nested-asset", "offset": 0, "length": 1},
        )
        return _job_result("worker-run-forbidden")

    start_request = _start(2)
    healthy = _health(3)
    shutdown = _shutdown(4)
    worker = _new_worker(host_ids, start=start)
    snapshot_raw = canonical_bytes(_snapshot())

    output = _serve(
        worker,
        [
            _handshake(),
            start_request,
            _asset_read_response(host_ids[0], snapshot_raw),
            _asset_read_response(host_ids[0], b"x"),
            healthy,
            shutdown,
        ],
    )

    assert (
        _error_code(_response_for(output, start_request))
        == ErrorCode.INVALID_TRANSITION
    )
    assert _response_for(output, healthy)["result"]["status"] == "ok"
    assert worker.pending_call_count == 0
    assert worker.state == "shutdown"


def test_shared_stdio_worker_rejects_shutdown_while_host_call_is_pending() -> None:
    host_ids = [_rpc_id(900), _rpc_id(901)]

    def start(
        _params: Mapping[str, object],
        context: WorkerContext,
    ) -> Mapping[str, object]:
        result = context.host.call(
            "host.asset.read/v1",
            {"asset_id": "nested-asset", "offset": 0, "length": 1},
        )
        assert result["base64_chunk"] == "eA=="
        return _job_result("worker-run-1")

    start_request = _start(2)
    blocked_shutdown = _shutdown(3)
    healthy = _health(4)
    final_shutdown = _shutdown(5)
    worker = _new_worker(host_ids, start=start)

    output = _serve(
        worker,
        [
            _handshake(),
            start_request,
            _asset_read_response(host_ids[0], canonical_bytes(_snapshot())),
            blocked_shutdown,
            _asset_read_response(host_ids[1], b"x"),
            healthy,
            final_shutdown,
        ],
    )

    assert (
        _error_code(_response_for(output, blocked_shutdown))
        == ErrorCode.INVALID_TRANSITION
    )
    assert (
        _response_for(output, start_request)["result"]["worker_run_id"]
        == "worker-run-1"
    )
    assert _response_for(output, healthy)["result"]["status"] == "ok"
    assert _response_for(output, final_shutdown)["result"] == {"accepted": True}
    assert worker.pending_call_count == 0
    assert worker.state == "shutdown"


def test_shared_stdio_worker_enforces_pending_host_call_bound() -> None:
    host_id = _rpc_id(900)
    calls = 0

    def start(
        _params: Mapping[str, object],
        _context: WorkerContext,
    ) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        return _job_result("worker-run-1")

    outer = _start(2)
    nested = _start(
        3,
        asset_id="nested-run-snapshot",
        attempt_id="attempt-nested",
        job_id="job-nested",
        step_id="step-nested",
    )
    healthy = _health(4)
    shutdown = _shutdown(5)
    worker = _new_worker([host_id], start=start, max_pending_calls=1)

    output = _serve(
        worker,
        [
            _handshake(),
            outer,
            nested,
            _asset_read_response(host_id, canonical_bytes(_snapshot())),
            healthy,
            shutdown,
        ],
    )

    assert _error_code(_response_for(output, nested)) == ErrorCode.INVALID_TRANSITION
    assert _response_for(output, outer)["result"]["worker_run_id"] == "worker-run-1"
    assert calls == 1
    assert worker.pending_call_count == 0
    assert worker.state == "shutdown"


def test_shared_stdio_worker_isolates_handler_exception_and_expires_host_client() -> (
    None
):
    captured = []

    def start(
        _params: Mapping[str, object],
        context: WorkerContext,
    ) -> Mapping[str, object]:
        captured.append(context.host)
        raise RuntimeError("domain handler failed")

    host_id = _rpc_id(900)
    start_request = _start(2)
    healthy = _health(3)
    shutdown = _shutdown(4)
    worker = _new_worker([host_id], start=start)

    output = _serve(
        worker,
        [
            _handshake(),
            start_request,
            _asset_read_response(host_id, canonical_bytes(_snapshot())),
            healthy,
            shutdown,
        ],
    )

    assert (
        _error_code(_response_for(output, start_request))
        == ErrorCode.RESULT_CONTRACT_MISMATCH
    )
    assert _response_for(output, healthy)["result"]["status"] == "ok"
    assert _response_for(output, shutdown)["result"] == {"accepted": True}
    assert len(captured) == 1
    with pytest.raises(ContractError) as caught:
        captured[0].call(
            "host.asset.read/v1",
            {"asset_id": "late", "offset": 0, "length": 1},
        )
    assert caught.value.code == ErrorCode.INVALID_TRANSITION
    assert worker.pending_call_count == 0
    assert worker.state == "shutdown"


def test_shared_stdio_worker_rejects_shutdown_before_handshake_and_ignores_trailing() -> (
    None
):
    early_shutdown = _shutdown(1)
    handshake = _handshake(2)
    shutdown = _shutdown(3)
    trailing = _health(4)
    worker = _new_worker()

    output = _serve(worker, [early_shutdown, handshake, shutdown, trailing])

    assert (
        _error_code(_response_for(output, early_shutdown))
        == ErrorCode.INCOMPATIBLE_GENERATION
    )
    assert _response_for(output, handshake)["result"]["plugin_id"] == PLUGIN_ID
    assert _response_for(output, shutdown)["result"] == {"accepted": True}
    assert not any(message.get("id") == trailing["id"] for message in output)
    assert len(output) == 3
    assert worker.state == "shutdown"


def test_shared_stdio_worker_eof_states_are_deterministic_and_clear_pending() -> None:
    clean = _new_worker()
    assert _serve_raw(clean, b"") == []
    assert clean.state == "eof"
    assert clean.pending_call_count == 0

    partial = _new_worker()
    output = _serve_raw(partial, encode_frame(_handshake())[:-1])
    assert len(output) == 1
    assert output[0]["id"] is None
    assert _error_code(output[0]) == ErrorCode.INVALID_TRANSITION
    assert partial.state == "failed"
    assert partial.pending_call_count == 0


def test_shared_stdio_worker_host_call_eof_returns_framed_error_and_clears_pending() -> (
    None
):
    host_ids = [_rpc_id(900), _rpc_id(901)]

    def start(
        _params: Mapping[str, object],
        context: WorkerContext,
    ) -> Mapping[str, object]:
        context.host.call(
            "host.asset.read/v1",
            {"asset_id": "never-answered", "offset": 0, "length": 1},
        )
        return _job_result("worker-run-forbidden")

    start_request = _start(2)
    worker = _new_worker(host_ids, start=start)
    raw = b"".join(
        encode_frame(message)
        for message in (
            _handshake(),
            start_request,
            _asset_read_response(host_ids[0], canonical_bytes(_snapshot())),
        )
    )

    output = _serve_raw(worker, raw)

    assert _error_code(_response_for(output, start_request)) == ErrorCode.ASSET_ERROR
    assert [
        message["id"]
        for message in output
        if message.get("method") == "host.asset.read/v1"
    ] == host_ids
    assert worker.state == "eof"
    assert worker.pending_call_count == 0


def test_shared_stdio_worker_sdk_version_is_exactly_0_1_2() -> None:
    value = tomllib.loads(
        (ROOT / "backend" / "plotpilot_plugin_sdk" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    assert value["project"]["version"] == "0.1.2"
