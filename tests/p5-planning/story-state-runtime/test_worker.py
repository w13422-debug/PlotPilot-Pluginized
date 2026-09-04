from __future__ import annotations

import base64
import hashlib
import json
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
from plotpilot_story_state import StoryStateRuntime, StoryStateWorker
from plotpilot_story_state.worker import CAPABILITY_ID
from plotpilot_story_state.worker import main as story_state_main

from .support import request

ROOT = Path(__file__).resolve().parents[3]
HASHES = [char * 64 for char in "abcdef123456789"]


class Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "host.asset.create/v1":
            content = base64.b64decode(params["base64_chunk"])
            digest = hashlib.sha256(content).hexdigest()
            assert digest == params["expected_hash"] == params["chunk_hash"]
            return {
                "upload_id": params["upload_id"],
                "accepted_bytes": len(content),
                "completed": True,
                "asset_id": f"asset-sha256-{digest}",
            }
        if method == "host.candidate.stage/v1":
            return {
                "accepted": True,
                "staged_items": [
                    {
                        "item_id": "item-story-1",
                        "candidate_id": "candidate-story-1",
                        "stage_status": "created",
                        "publication_eligibility": "eligible",
                    }
                ],
                "job_event_seq": 1,
            }
        if method == "host.job.complete/v1":
            return {
                "accepted": True,
                "attempt_state": "succeeded",
                "step_state": "succeeded",
                "job_state": "succeeded",
                "provenance_receipt_id": "receipt-story-1",
                "job_event_seq": 2,
                "core_event_high_water": 2,
            }
        raise AssertionError(method)


def _handshake(worker):
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
            request_id="123e4567-e89b-12d3-a456-426614174021",
        )
    )
    assert response and response["result"]["plugin_id"] == "com.plotpilot.story-state"


def test_story_state_worker_uses_only_host_candidate_and_terminal_ports_and_replays():
    host = Host()
    worker = StoryStateWorker(
        host,
        execute=lambda _params, _meta, terminal: StoryStateRuntime(terminal).execute(
            request()
        ),
        receipt_reader=lambda receipt_id, expected: {
            **expected,
            "receipt_id": receipt_id,
        },
    )
    _handshake(worker)
    start = build_request(
        "job.start",
        {
            "capability_id": CAPABILITY_ID,
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
            operation_id="p2-lifecycle-story-legacy-1",
        ),
        request_id="123e4567-e89b-12d3-a456-426614174022",
    )
    first = worker.handle(start)
    replay = worker.handle({**start, "id": "123e4567-e89b-12d3-a456-426614174023"})
    assert first and replay and first["result"] == replay["result"]
    assert first["result"]["worker_run_id"] == "p2-lifecycle-story-legacy-1"
    assert [name for name, _params in host.calls] == [
        "host.asset.create/v1",
        "host.asset.create/v1",
        "host.candidate.stage/v1",
        "host.job.complete/v1",
    ]
    assert all("publication" not in name for name, _params in host.calls)


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
            return {
                "accepted": True,
                "staged_items": [
                    {
                        "item_id": "item-story-production-1",
                        "candidate_id": "candidate-story-production-1",
                        "stage_status": "created",
                        "publication_eligibility": "eligible",
                    }
                ],
                "job_event_seq": 1,
            }
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


def _story_snapshot() -> dict[str, Any]:
    value = json.loads((ROOT / "contracts/examples/run-snapshot.json").read_text("utf-8"))
    value.update(
        {
            "snapshot_id": "snapshot-story-production-1",
            "workspace_id": "workspace-story-production",
            "scope": {
                "document_id": "character-document-story-1",
                "node_id": None,
                "operation": CAPABILITY_ID,
            },
            "input_revisions": [
                {
                    "document_id": "character-document-story-1",
                    "revision_id": "character-revision-story-1",
                    "content_hash": HASHES[0],
                }
            ],
            "plan_revision_id": "plan-story-production-1",
            "model_profile_revision_id": "model-story-production-1",
            "data_bindings": [],
            "skill_releases": [],
            "plugin_settings_revisions": [],
            "parameters_asset_id": "asset-story-runtime-parameters",
            "run_intent_id": "story-production-intent-1",
            "created_at": "2026-09-04T00:00:00Z",
        }
    )
    return value


def _production_case(
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, bytes], str, str]:
    operation_id = "p2-lifecycle-story-production-1"
    package_hash = HASHES[4]
    plugin_release_id = release_id("com.plotpilot.story-state", "1.0.0", package_hash)
    snapshot = _story_snapshot()
    snapshot["plugin_releases"] = [
        {
            "plugin_id": "com.plotpilot.story-state",
            "release_id": plugin_release_id,
            "package_hash": package_hash,
            "data_generation_id": "data-generation-story-production-1",
        }
    ]
    runtime_input: dict[str, Any] = {
        "schema": "story-state-runtime-input/v1",
        "run_snapshot": {
            "asset_id": "snapshot-asset-story-production-1",
            "snapshot_id": "snapshot-story-production-1",
            "workspace_id": "workspace-story-production",
            "parameters_asset_id": "asset-story-runtime-parameters",
        },
        "execution": {
            "plugin_id": "com.plotpilot.story-state",
            "release_id": plugin_release_id,
            "package_hash": package_hash,
            "data_generation_id": "data-generation-story-production-1",
            "capability_id": CAPABILITY_ID,
            "settings_revision_id": None,
            "job_id": "job-story-production-1",
            "step_id": "step-story-production-1",
            "attempt_id": "attempt-story-production-1",
            "lease_epoch": 1,
            "worker_run_id": operation_id,
        },
        "request": {
            "bundle_id": "bundle-story-production-1",
            "receipt_id": "receipt-story-production-1",
            "created_at": "2026-09-04T00:00:00Z",
            "proposals": [
                {
                    "proposal_id": "proposal-story-production-1",
                    "operation_id": operation_id,
                    "item_id": "item-story-production-1",
                    "retry_id": "retry-story-production-1",
                    "target": {
                        "workspace_id": "workspace-story-production",
                        "entity_kind": "character",
                        "entity_id": "character-document-story-1",
                        "revision_id": "character-revision-story-1",
                        "content_hash": HASHES[0],
                    },
                    "payload": {
                        "schema": "story-state-payload/v1",
                        "state_kind": "character",
                        "entity_id": "character-document-story-1",
                        "body": {
                            "name": "Lin",
                            "role": "detective",
                            "traits": ["calm"],
                            "status": "active",
                        },
                        "references": [],
                    },
                    "parent_candidate_ids": [],
                    "outcome": "success",
                    "error": None,
                    "terminal_seq": 1,
                }
            ],
            "known_entity_ids": ["character-document-story-1"],
            "parent_receipt_ids": [],
            "terminal_detail_asset_id": None,
            "local_seq": 1,
        },
        "reference_bindings": [],
    }
    if mutate is not None:
        mutate(runtime_input)
    parameters = canonical_bytes(runtime_input)
    snapshot["asset_hashes"] = [
        {
            "asset_id": "asset-story-runtime-parameters",
            "sha256": sha256_hex(parameters),
        }
    ]
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    handshake = build_request(
        "runtime.handshake",
        {
            "host_protocol": "1",
            "generation_id": "generation-story-production-1",
            "plugin_release_id": plugin_release_id,
            "data_generation_id": "data-generation-story-production-1",
        },
        build_meta(
            "control",
            generation_id="generation-story-production-1",
            plugin_release_id=plugin_release_id,
            deadline_at="2026-09-04T00:00:00Z",
            operation_id="control-story-production-1",
        ),
        request_id="123e4567-e89b-12d3-a456-426614174031",
    )
    start = build_request(
        "job.start",
        {
            "capability_id": CAPABILITY_ID,
            "run_snapshot_asset_id": "snapshot-asset-story-production-1",
            "checkpoint_asset_id": None,
            "secrets": None,
        },
        build_meta(
            "attempt",
            generation_id="generation-story-production-1",
            plugin_release_id=plugin_release_id,
            deadline_at="2026-09-04T00:00:00Z",
            job_id="job-story-production-1",
            step_id="step-story-production-1",
            attempt_id="attempt-story-production-1",
            lease_epoch=1,
            operation_id=operation_id,
        ),
        request_id="123e4567-e89b-12d3-a456-426614174032",
    )
    return (
        [handshake, start],
        {
            "snapshot-asset-story-production-1": canonical_bytes(snapshot),
            "asset-story-runtime-parameters": parameters,
        },
        start["id"],
        operation_id,
    )


def _run_no_argument_worker(
    monkeypatch,
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> _Duplex:
    requests, assets, _start_id, _operation_id = _production_case(mutate)
    duplex = _Duplex(requests, assets, receipt_id="receipt-story-production-1")
    monkeypatch.setattr(sys, "stdin", _TextStream(duplex))
    monkeypatch.setattr(sys, "stdout", _TextStream(duplex))
    story_state_main()
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


def test_no_argument_story_state_entrypoint_uses_shared_stdio_and_p2_worker_run_id(
    monkeypatch,
):
    duplex = _run_no_argument_worker(monkeypatch)
    response = _job_response(duplex)
    assert response["result"]["worker_run_id"] == "p2-lifecycle-story-production-1"
    assert response["result"]["provenance_receipt_id"] == "receipt-story-production-1"
    assert [message["method"] for message in duplex.messages if "method" in message] == [
        "host.asset.read/v1",
        "host.asset.read/v1",
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
        lambda value: value["execution"].update(
            settings_revision_id="story-settings-foreign"
        ),
        lambda value: value["execution"].update(
            data_generation_id="data-generation-story-foreign"
        ),
        lambda value: value["execution"].update(
            attempt_id="attempt-story-production-foreign"
        ),
        lambda value: value["run_snapshot"].update(
            snapshot_id="snapshot-story-production-foreign"
        ),
    ],
    ids=["release", "package", "settings", "generation", "attempt", "snapshot"],
)
def test_no_argument_story_state_rejects_identity_drift_before_any_write_effect(
    monkeypatch,
    mutate,
):
    duplex = _run_no_argument_worker(monkeypatch, mutate)
    response = _job_response(duplex)
    assert "error" in response
    methods = [message["method"] for message in duplex.messages if "method" in message]
    assert methods
    assert set(methods) == {"host.asset.read/v1"}
