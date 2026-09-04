from __future__ import annotations

import base64
import hashlib

from plotpilot_plugin_sdk.rpc import build_meta, build_request
from plotpilot_story_state import StoryStateRuntime, StoryStateWorker

from .support import request


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
            "capability_id": "planning.story-state.settle/v1",
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
        ),
        request_id="123e4567-e89b-12d3-a456-426614174022",
    )
    first = worker.handle(start)
    replay = worker.handle({**start, "id": "123e4567-e89b-12d3-a456-426614174023"})
    assert first and replay and first["result"] == replay["result"]
    assert [name for name, _params in host.calls] == [
        "host.asset.create/v1",
        "host.asset.create/v1",
        "host.candidate.stage/v1",
        "host.job.complete/v1",
    ]
    assert all("publication" not in name for name, _params in host.calls)
