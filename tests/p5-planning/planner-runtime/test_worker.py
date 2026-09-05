from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from plotpilot_plugin_sdk import canonical_bytes, release_id, sha256_hex
from plotpilot_plugin_sdk.framing import FrameDecoder, encode_frame
from plotpilot_plugin_sdk.rpc import build_meta, build_request
from plotpilot_plugin_sdk.verifier import request_key, snapshot_hash
from plotpilot_project_planner import (
    PLANNER_CAPABILITY_ID,
    PLANNER_OPERATION,
    PLANNER_PROMPT_SKILL_ID,
    PlannerBindingSelection,
    PlannerDocumentTarget,
    PlannerWorker,
    freeze_planner_run,
    planner_prepare_operation_key,
    prepare_planner_run,
)
from plotpilot_project_planner.worker import StdioHostPort
from plotpilot_project_planner.worker import main as planner_main

ROOT = Path(__file__).resolve().parents[3]
HASHES = [char * 64 for char in "abcdef123456789"]


def _snapshot() -> dict[str, object]:
    value = json.loads(
        (ROOT / "contracts/examples/run-snapshot.json").read_text("utf-8")
    )
    value.update(
        {
            "snapshot_id": "snapshot-planner-1",
            "workspace_id": "workspace-planner",
            "scope": {
                "document_id": "document-setting",
                "node_id": None,
                "operation": PLANNER_OPERATION,
            },
            "input_revisions": [
                {
                    "document_id": "document-setting",
                    "revision_id": "revision-setting-1",
                    "content_hash": HASHES[0],
                },
                {
                    "document_id": "document-bible",
                    "revision_id": "revision-bible-1",
                    "content_hash": HASHES[1],
                },
                {
                    "document_id": "document-outline",
                    "revision_id": "revision-outline-1",
                    "content_hash": HASHES[2],
                },
            ],
            "plan_revision_id": "plan-revision-7",
            "plugin_releases": [
                {
                    "plugin_id": "com.plotpilot.project-planner",
                    "release_id": HASHES[3],
                    "package_hash": HASHES[4],
                    "data_generation_id": None,
                }
            ],
            "plugin_settings_revisions": [
                {
                    "plugin_id": "com.plotpilot.project-planner",
                    "settings_revision_id": "planner-settings-3",
                    "scope": "workspace",
                    "scope_id": "workspace-planner",
                    "schema_hash": HASHES[5],
                    "validated_by_release_id": HASHES[3],
                }
            ],
            "data_bindings": [],
            "skill_releases": [
                {
                    "skill_id": PLANNER_PROMPT_SKILL_ID,
                    "release_id": HASHES[6],
                    "package_hash": HASHES[7],
                    "parameters_asset_id": "asset-prompt-parameters",
                    "order": 10,
                }
            ],
            "model_profile_revision_id": "model-profile-revision-9",
            "parameters_asset_id": "asset-run-parameters",
            "asset_hashes": [
                {"asset_id": "asset-prompt-parameters", "sha256": HASHES[10]},
                {"asset_id": "asset-run-parameters", "sha256": HASHES[11]},
            ],
            "run_intent_id": "planner-intent-1",
            "created_at": "2026-08-28T00:00:00Z",
        }
    )
    value["request_key"] = request_key(value)
    value["snapshot_hash"] = snapshot_hash(value)
    return value


def _proposal():
    frozen = freeze_planner_run(
        _snapshot(),
        PlannerBindingSelection(
            PLANNER_PROMPT_SKILL_ID,
            HASHES[6],
            HASHES[3],
            "planner-settings-3",
            "plan-revision-7",
            "model-profile-revision-9",
        ),
    )
    targets = {
        "setting": PlannerDocumentTarget(
            "setting",
            "workspace-planner",
            "document-setting",
            "revision-setting-1",
            HASHES[0],
        ),
        "bible": PlannerDocumentTarget(
            "bible",
            "workspace-planner",
            "document-bible",
            "revision-bible-1",
            HASHES[1],
        ),
        "outline": PlannerDocumentTarget(
            "outline",
            "workspace-planner",
            "document-outline",
            "revision-outline-1",
            HASHES[2],
        ),
    }
    generated = {
        "setting": {"tone": "quiet"},
        "bible": {"premise": "x"},
        "outline": "Act one",
    }
    operation_key = planner_prepare_operation_key(
        frozen, generated, targets, attempt_id="attempt-1"
    )
    return prepare_planner_run(
        frozen, generated, targets, attempt_id="attempt-1", operation_key=operation_key
    )


class Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._assets = 0

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "host.asset.create/v1":
            self._assets += 1
            return {
                "upload_id": params["upload_id"],
                "accepted_bytes": params["total_size"],
                "completed": True,
                "asset_id": f"asset-{self._assets}",
            }
        if method == "host.candidate.stage/v1":
            return {"accepted": True, "staged_items": [], "job_event_seq": 1}
        if method == "host.job.complete/v1":
            return {
                "accepted": True,
                "attempt_state": "succeeded",
                "step_state": "succeeded",
                "job_state": "succeeded",
                "provenance_receipt_id": "receipt-planner-1",
                "job_event_seq": 2,
                "core_event_high_water": 2,
            }
        raise AssertionError(method)


def _handshake(worker: PlannerWorker) -> None:
    response = worker.handle(
        build_request(
            "runtime.handshake",
            {
                "host_protocol": "1",
                "generation_id": "generation-1",
                "plugin_release_id": "a" * 64,
                "data_generation_id": None,
            },
            build_meta(
                "control",
                generation_id="generation-1",
                plugin_release_id="a" * 64,
                deadline_at="2026-09-04T00:00:00Z",
            ),
            request_id="123e4567-e89b-12d3-a456-426614174001",
        )
    )
    assert (
        response and response["result"]["plugin_id"] == "com.plotpilot.project-planner"
    )


def test_worker_submits_source_bound_candidates_only_once_through_host_ports():
    host = Host()
    proposal = _proposal()
    worker = PlannerWorker(host, prepare=lambda _params, _meta: proposal)
    _handshake(worker)
    request = build_request(
        "job.start",
        {
            "capability_id": PLANNER_CAPABILITY_ID,
            "run_snapshot_asset_id": "snapshot-asset-1",
            "checkpoint_asset_id": None,
            "secrets": None,
        },
        build_meta(
            "attempt",
            generation_id="generation-1",
            plugin_release_id="a" * 64,
            deadline_at="2026-09-04T00:00:00Z",
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
            operation_id="p2-lifecycle-planner-legacy-1",
        ),
        request_id="123e4567-e89b-12d3-a456-426614174002",
    )
    first = worker.handle(request)
    replay = worker.handle({**request, "id": "123e4567-e89b-12d3-a456-426614174003"})
    assert first and replay and first["result"] == replay["result"]
    assert [name for name, _params in host.calls] == ["host.asset.create/v1"] * 4 + [
        "host.candidate.stage/v1",
        "host.job.complete/v1",
    ]
    assert all(not name.startswith("publication.") for name, _params in host.calls)
    bundle_upload = host.calls[3][1]
    assert bundle_upload["mime"] == "application/json"
    assert first["result"]["worker_run_id"] == "p2-lifecycle-planner-legacy-1"


class _Duplex:
    """Script the accepted framed Host session without a second transport."""

    def __init__(
        self,
        requests: list[dict[str, Any]],
        assets: dict[str, bytes],
        *,
        receipt_id: str,
    ) -> None:
        self._incoming = deque(encode_frame(request) for request in requests)
        self._decoder = FrameDecoder()
        self.assets = dict(assets)
        self.receipt_id = receipt_id
        self.messages: list[dict[str, Any]] = []

    def read(self, _size: int) -> bytes:
        return self._incoming.popleft() if self._incoming else b""

    def write(self, data: bytes) -> int:
        for message in self._decoder.feed(bytes(data)):
            self.messages.append(message)
            if "method" in message:
                self._incoming.append(
                    encode_frame(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "result": self._host_result(message),
                        }
                    )
                )
        return len(data)

    def flush(self) -> None:
        return None

    def _host_result(self, request: dict[str, Any]) -> dict[str, Any]:
        method = request["method"]
        params = request["params"]
        if method == "host.asset.read/v1":
            content = self.assets[params["asset_id"]]
            offset = params["offset"]
            chunk = content[offset : offset + params["length"]]
            next_offset = offset + len(chunk)
            return {
                "base64_chunk": base64.b64encode(chunk).decode("ascii"),
                "next_offset": next_offset if next_offset < len(content) else None,
                "content_hash": sha256_hex(chunk),
            }
        if method == "host.asset.create/v1":
            content = base64.b64decode(params["base64_chunk"])
            digest = hashlib.sha256(content).hexdigest()
            assert digest == params["expected_hash"] == params["chunk_hash"]
            asset_id = f"asset-sha256-{digest}"
            self.assets[asset_id] = content
            return {
                "upload_id": params["upload_id"],
                "accepted_bytes": len(content),
                "completed": True,
                "asset_id": asset_id,
            }
        if method == "host.candidate.stage/v1":
            return {"accepted": True, "staged_items": [], "job_event_seq": 1}
        if method == "host.job.complete/v1":
            return {
                "accepted": True,
                "attempt_state": "succeeded",
                "step_state": "succeeded",
                "job_state": "succeeded",
                "provenance_receipt_id": self.receipt_id,
                "job_event_seq": 2,
                "core_event_high_water": 2,
            }
        raise AssertionError(method)


class _TextStream:
    def __init__(self, buffer: _Duplex) -> None:
        self.buffer = buffer


def _production_case(
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, bytes], str, str]:
    operation_id = "p2-lifecycle-planner-1"
    package_hash = HASHES[4]
    plugin_release_id = release_id(
        "com.plotpilot.project-planner", "1.0.0", package_hash
    )
    snapshot = _snapshot()
    snapshot["plugin_releases"] = [
        {
            "plugin_id": "com.plotpilot.project-planner",
            "release_id": plugin_release_id,
            "package_hash": package_hash,
            "data_generation_id": "data-generation-1",
        }
    ]
    snapshot["plugin_settings_revisions"][0]["validated_by_release_id"] = (
        plugin_release_id
    )
    runtime_input: dict[str, Any] = {
        "schema": "project-planner-runtime-input/v1",
        "run_snapshot": {
            "asset_id": "snapshot-asset-planner-1",
            "snapshot_id": "snapshot-planner-1",
            "workspace_id": "workspace-planner",
            "parameters_asset_id": "asset-run-parameters",
        },
        "execution": {
            "plugin_id": "com.plotpilot.project-planner",
            "release_id": plugin_release_id,
            "package_hash": package_hash,
            "data_generation_id": "data-generation-1",
            "capability_id": PLANNER_CAPABILITY_ID,
            "job_id": "job-planner-1",
            "step_id": "step-planner-1",
            "attempt_id": "attempt-planner-1",
            "lease_epoch": 1,
            "worker_run_id": operation_id,
        },
        "selection": {
            "prompt_skill_id": PLANNER_PROMPT_SKILL_ID,
            "prompt_release_id": HASHES[6],
            "planner_release_id": plugin_release_id,
            "planner_settings_revision_id": "planner-settings-3",
            "plan_revision_id": "plan-revision-7",
            "model_profile_revision_id": "model-profile-revision-9",
        },
        "generated": {
            "setting": {"tone": "quiet"},
            "bible": {"premise": "x"},
            "outline": "Act one",
        },
        "targets": {
            "setting": {
                "workspace_id": "workspace-planner",
                "document_id": "document-setting",
                "revision_id": "revision-setting-1",
                "content_hash": HASHES[0],
            },
            "bible": {
                "workspace_id": "workspace-planner",
                "document_id": "document-bible",
                "revision_id": "revision-bible-1",
                "content_hash": HASHES[1],
            },
            "outline": {
                "workspace_id": "workspace-planner",
                "document_id": "document-outline",
                "revision_id": "revision-outline-1",
                "content_hash": HASHES[2],
            },
        },
        "source_refs": {role: [] for role in ("setting", "bible", "outline")},
    }
    if mutate is not None:
        mutate(runtime_input)
    parameters = canonical_bytes(runtime_input)
    parameter_hash = sha256_hex(parameters)
    snapshot["asset_hashes"] = [
        {"asset_id": "asset-prompt-parameters", "sha256": HASHES[10]},
        {"asset_id": "asset-run-parameters", "sha256": parameter_hash},
    ]
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    handshake = build_request(
        "runtime.handshake",
        {
            "host_protocol": "1",
            "generation_id": "generation-1",
            "plugin_release_id": plugin_release_id,
            "data_generation_id": "data-generation-1",
        },
        build_meta(
            "control",
            generation_id="generation-1",
            plugin_release_id=plugin_release_id,
            deadline_at="2026-09-04T00:00:00Z",
            operation_id="control-planner-1",
        ),
        request_id="123e4567-e89b-12d3-a456-426614174011",
    )
    start = build_request(
        "job.start",
        {
            "capability_id": PLANNER_CAPABILITY_ID,
            "run_snapshot_asset_id": "snapshot-asset-planner-1",
            "checkpoint_asset_id": None,
            "secrets": None,
        },
        build_meta(
            "attempt",
            generation_id="generation-1",
            plugin_release_id=plugin_release_id,
            deadline_at="2026-09-04T00:00:00Z",
            job_id="job-planner-1",
            step_id="step-planner-1",
            attempt_id="attempt-planner-1",
            lease_epoch=1,
            operation_id=operation_id,
        ),
        request_id="123e4567-e89b-12d3-a456-426614174012",
    )
    return (
        [handshake, start],
        {
            "snapshot-asset-planner-1": canonical_bytes(snapshot),
            "asset-run-parameters": parameters,
        },
        start["id"],
        operation_id,
    )


def _run_no_argument_worker(
    monkeypatch,
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> _Duplex:
    requests, assets, _start_id, _operation_id = _production_case(mutate)
    duplex = _Duplex(requests, assets, receipt_id="receipt-planner-production-1")
    monkeypatch.setattr(sys, "stdin", _TextStream(duplex))
    monkeypatch.setattr(sys, "stdout", _TextStream(duplex))
    planner_main()
    return duplex


def _job_response(duplex: _Duplex) -> dict[str, Any]:
    _requests, _assets, start_id, _operation_id = _production_case()
    responses = [
        message
        for message in duplex.messages
        if message.get("id") == start_id and ("result" in message or "error" in message)
    ]
    assert len(responses) == 1
    return responses[0]


def test_no_argument_planner_entrypoint_uses_shared_stdio_and_echoes_p2_worker_run_id(
    monkeypatch,
):
    duplex = _run_no_argument_worker(monkeypatch)
    response = _job_response(duplex)
    assert response["result"]["worker_run_id"] == "p2-lifecycle-planner-1"
    assert response["result"]["provenance_receipt_id"] == "receipt-planner-production-1"
    assert [
        message["method"] for message in duplex.messages if "method" in message
    ] == [
        "host.asset.read/v1",
        "host.asset.read/v1",
        "host.asset.create/v1",
        "host.asset.create/v1",
        "host.asset.create/v1",
        "host.asset.create/v1",
        "host.candidate.stage/v1",
        "host.job.complete/v1",
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["execution"].update(release_id="f" * 64),
        lambda value: value["execution"].update(package_hash="f" * 64),
        lambda value: value["selection"].update(
            planner_settings_revision_id="planner-settings-foreign"
        ),
        lambda value: value["execution"].update(
            data_generation_id="data-generation-foreign"
        ),
        lambda value: value["execution"].update(attempt_id="attempt-planner-foreign"),
        lambda value: value["run_snapshot"].update(snapshot_id="snapshot-planner-foreign"),
    ],
    ids=["release", "package", "settings", "generation", "attempt", "snapshot"],
)
def test_no_argument_planner_rejects_identity_drift_before_any_write_effect(
    monkeypatch, mutate
):
    duplex = _run_no_argument_worker(monkeypatch, mutate)
    response = _job_response(duplex)
    assert "error" in response
    methods = [message["method"] for message in duplex.messages if "method" in message]
    assert methods
    assert set(methods) == {"host.asset.read/v1"}
    assert not set(methods) & {
        "host.asset.create/v1",
        "host.candidate.stage/v1",
        "host.job.complete/v1",
    }


_HOST_READ_PARAMS = {"asset_id": "asset-stdio-small-1", "offset": 0, "length": 1}
_HOST_READ_RESULT = {
    "base64_chunk": "",
    "next_offset": None,
    "content_hash": sha256_hex(b""),
}


def _host_response_frame(request: dict[str, Any]) -> bytes:
    return encode_frame(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": _HOST_READ_RESULT,
        }
    )


class _Read1OnlyBufferedPipe:
    """Keep the writer open: only BufferedReader.read1 can return this small frame."""

    def __init__(self) -> None:
        reader_fd, self._writer_fd = os.pipe()
        self._reader = io.BufferedReader(os.fdopen(reader_fd, "rb", buffering=0))
        self.frame_lengths: list[int] = []
        self.read1_sizes: list[int] = []

    def queue_response(self, request: dict[str, Any]) -> None:
        frame = _host_response_frame(request)
        self.frame_lengths.append(len(frame))
        assert os.write(self._writer_fd, frame) == len(frame)

    def read1(self, size: int) -> bytes:
        self.read1_sizes.append(size)
        return self._reader.read1(size)

    def read(self, _size: int) -> bytes:
        raise AssertionError("read() must not be called when read1() is available")

    def close(self) -> None:
        os.close(self._writer_fd)
        self._reader.close()


class _ReadFallbackInput:
    def __init__(self) -> None:
        self._frames: deque[bytes] = deque()
        self.frame_lengths: list[int] = []
        self.read_sizes: list[int] = []

    def queue_response(self, request: dict[str, Any]) -> None:
        frame = _host_response_frame(request)
        self.frame_lengths.append(len(frame))
        self._frames.append(frame)

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self._frames.popleft()


class _HostResponseWriter:
    def __init__(self, input_stream: Any) -> None:
        self._decoder = FrameDecoder()
        self._input_stream = input_stream

    def write(self, data: bytes) -> int:
        for message in self._decoder.feed(bytes(data)):
            self._input_stream.queue_response(message)
        return len(data)

    def flush(self) -> None:
        return None


def _host_meta() -> dict[str, Any]:
    return build_meta(
        "attempt",
        generation_id="generation-stdio-1",
        plugin_release_id="a" * 64,
        deadline_at="2026-09-04T00:00:00Z",
        job_id="job-stdio-1",
        step_id="step-stdio-1",
        attempt_id="attempt-stdio-1",
        lease_epoch=1,
        operation_id="stdio-host-port-1",
    )


def test_stdio_host_port_uses_read1_for_small_frame_and_falls_back_to_read():
    pipe = _Read1OnlyBufferedPipe()
    try:
        port = StdioHostPort(pipe, _HostResponseWriter(pipe))
        port.bind_meta(_host_meta())
        assert port.call("host.asset.read/v1", _HOST_READ_PARAMS) == _HOST_READ_RESULT
        assert pipe.read1_sizes == [65536]
        assert pipe.frame_lengths and pipe.frame_lengths[0] < 65536
    finally:
        pipe.close()

    fallback = _ReadFallbackInput()
    port = StdioHostPort(fallback, _HostResponseWriter(fallback))
    port.bind_meta(_host_meta())
    assert port.call("host.asset.read/v1", _HOST_READ_PARAMS) == _HOST_READ_RESULT
    assert fallback.read_sizes == [65536]
    assert fallback.frame_lengths and fallback.frame_lengths[0] < 65536
