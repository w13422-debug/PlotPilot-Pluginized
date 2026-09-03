from __future__ import annotations

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.bootstrap.m4_authority_adapters import (
    build_m4_authority_adapters,
)
from backend.plotpilot_core.candidates import CandidateError, CandidateService
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.repositories import CoreAuthorityRepository


def _partial_stream(base, payload):
    target = {
        "workspace_id": "ws-1",
        "entity_kind": "document",
        "entity_id": "doc-1",
    }
    return {
        "schema": "candidate-item/v1",
        "item_id": "stream-direct",
        "item_kind": "incomplete_stream",
        "target": target,
        "mutation": {
            "mode": "replace",
            "payload_schema": "core/document-text/v1",
            "payload_hash": payload.sha256,
        },
        "payload_asset_id": payload.asset_id,
        "base": {
            "revision_id": base.revision_id,
            "content_hash": base.content_hash,
        },
        "write_set": [
            {
                **target,
                "revision_id": base.revision_id,
                "content_hash": base.content_hash,
            }
        ],
        "parent_candidate_ids": [],
        "source_refs": [],
        "status": "partial",
    }


def test_lifecycle_adapter_mounts_only_the_six_frozen_core_v2_routes(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    try:
        adapters = build_m4_authority_adapters(repository, assets)
        router = adapters.router()
        paths = {route.path for route in router.routes}

        assert paths == {
            "/api/v2/core/workspaces/{workspace_id}/candidates",
            "/api/v2/core/workspaces/{workspace_id}/candidates/{candidate_id}",
            "/api/v2/core/workspaces/{workspace_id}/candidates/{candidate_id}/review",
            "/api/v2/core/workspaces/{workspace_id}/candidates/{candidate_id}/preview",
            "/api/v2/core/workspaces/{workspace_id}/publications:accept",
            "/api/v2/core/workspaces/{workspace_id}/story-state/projection-input",
        }
        assert all("/api/v1/" not in path for path in paths)
    finally:
        repository.close()


def test_direct_or_plugin_candidate_staging_cannot_use_incomplete_stream(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    try:
        repository.create_workspace(Workspace("ws-1", "Novel"))
        repository.create_document(Document("doc-1", "ws-1", "Chapter"))
        base = repository.publish_revision(
            document_id="doc-1",
            content="old",
            expected_revision_id=None,
            created_by="user",
            revision_id="rev-base",
        )
        payload = assets.put(
            b"partial",
            mime="text/plain",
            logical_role="candidate_payload",
            provenance="test:lifecycle",
        )

        with pytest.raises(CandidateError, match="durable-stream authority"):
            CandidateService(repository, assets).stage(
                "plugin-or-direct", _partial_stream(base, payload)
            )
        assert repository._connection.execute("SELECT count(*) FROM candidate").fetchone()[0] == 0
    finally:
        repository.close()
