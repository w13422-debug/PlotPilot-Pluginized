from __future__ import annotations

import json
from pathlib import Path

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
