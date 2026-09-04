from __future__ import annotations

import json
from pathlib import Path

import pytest
from plotpilot_plugin_sdk import ContractValidationError
from plotpilot_plugin_sdk.rpc import build_meta, build_request
from plotpilot_plugin_sdk.verifier import assert_valid
from plotpilot_project_planner import PlannerWorker
from plotpilot_story_state import StoryStateWorker

ROOT = Path(__file__).resolve().parents[3]
PACKAGES = (
    ("project-planner", "com.plotpilot.project-planner", PlannerWorker),
    ("story-state", "com.plotpilot.story-state", StoryStateWorker),
)


def _handshake(worker, plugin_id):
    request = build_request(
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
        request_id="123e4567-e89b-12d3-a456-426614174011",
    )
    response = worker.handle(request)
    assert response and response["result"]["plugin_id"] == plugin_id


def test_first_party_packages_are_installable_manifest_surfaces_and_launch_workers():
    for directory, plugin_id, worker_type in PACKAGES:
        root = ROOT / "first-party-plugins" / directory
        manifest = json.loads((root / "plugin.json").read_text("utf-8"))
        assert_valid("plotpilot-plugin/v1", manifest)
        assert manifest["plugin_id"] == plugin_id
        assert (root / manifest["backend"]["requirements_lock"]).is_file()
        assert (root / manifest["storage"]["migration_manifest"]).is_file()
        _handshake(worker_type(), plugin_id)


def test_manifest_or_settings_mismatch_is_rejected_before_any_worker_registration():
    root = ROOT / "first-party-plugins" / "project-planner"
    manifest = json.loads((root / "plugin.json").read_text("utf-8"))
    manifest["version"] = "not-semver"
    with pytest.raises(ContractValidationError):
        assert_valid("plotpilot-plugin/v1", manifest)
    defaults = json.loads((root / "settings/defaults.json").read_text("utf-8"))
    schema = json.loads((root / "settings/schema.json").read_text("utf-8"))
    assert defaults["schema"] == schema["properties"]["schema"]["const"]
