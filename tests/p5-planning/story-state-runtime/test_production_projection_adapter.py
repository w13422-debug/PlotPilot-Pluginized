from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest
from plotpilot_plugin_sdk import canonical_bytes, hash_jcs
from plotpilot_story_state import CoreProjectionAdapter, CoreProjectionAdapterError


class Assets:
    def __init__(self, values):
        self.values = values

    def read_asset(self, asset_id):
        return self.values[asset_id]


def _input(*, revision_id="state-revision-2", revision_number=2):
    payload = canonical_bytes(
        {
            "schema": "story-state-payload/v1",
            "state_kind": "character",
            "entity_id": "character-1",
            "body": {
                "name": "Lin",
                "role": "detective",
                "traits": ["calm"],
                "status": "active",
            },
            "references": [],
        }
    )
    payload_hash = hashlib.sha256(payload).hexdigest()
    root = {"receipt_id": "receipt-1", "parent_receipt_ids": []}
    root["receipt_hash"] = hash_jcs("story-state-receipt/v2", root)
    value = {
        "schema": "story-state-projection-input/v2",
        "projection_input_id": "projection-1",
        "workspace_id": "workspace-1",
        "publication": {
            "publication_id": "publication-1",
            "candidate_id": "candidate-1",
            "revision_id": revision_id,
            "revision_number": revision_number,
            "content_hash": payload_hash,
        },
        "candidate": {
            "candidate_id": "candidate-1",
            "target": {
                "workspace_id": "workspace-1",
                "entity_kind": "document",
                "entity_id": "character-1",
            },
            "payload_asset_id": "candidate-asset-1",
            "payload_hash": payload_hash,
            "status": "complete",
        },
        "current_revision": {
            "revision_id": revision_id,
            "content_asset_id": "revision-asset-1",
            "content_hash": payload_hash,
            "revision_number": revision_number,
        },
        "assets": [
            {
                "asset_id": "candidate-asset-1",
                "sha256": payload_hash,
                "role": "candidate.payload",
            },
            {
                "asset_id": "revision-asset-1",
                "sha256": payload_hash,
                "role": "revision.content",
            },
        ],
        "provenance": {
            "receipt_id": "receipt-1",
            "receipt_hash": root["receipt_hash"],
            "run_snapshot_hash": "a" * 64,
            "release_id": "b" * 64,
            "package_hash": "c" * 64,
            "parent_receipt_ids": [],
        },
        "receipt_closure": [root],
        "derived_at": "2026-09-04T00:00:00Z",
    }
    return value, Assets({"candidate-asset-1": payload, "revision-asset-1": payload})


def test_core_projection_adapter_rebuilds_byte_equivalent_disposable_projection():
    value, assets = _input()
    adapter = CoreProjectionAdapter(assets)
    first = adapter.rebuild((value,), expected_workspace_id="workspace-1")
    second = adapter.rebuild((deepcopy(value),), expected_workspace_id="workspace-1")
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.records[0].fact.revision_id == "state-revision-2"


def test_core_projection_adapter_rejects_stale_or_content_drift_before_projection():
    newer_value, assets = _input(revision_number=2)
    stale_value, _ = _input(revision_id="state-revision-1", revision_number=1)
    adapter = CoreProjectionAdapter(assets)
    newer = adapter.rebuild((newer_value,), expected_workspace_id="workspace-1")
    stale = adapter.rebuild((stale_value,), expected_workspace_id="workspace-1")
    with pytest.raises(CoreProjectionAdapterError, match="stale"):
        stale.assert_not_stale_after(newer)
    forged = deepcopy(newer_value)
    forged["assets"][0]["sha256"] = "f" * 64
    with pytest.raises(CoreProjectionAdapterError):
        adapter.rebuild((forged,), expected_workspace_id="workspace-1")
