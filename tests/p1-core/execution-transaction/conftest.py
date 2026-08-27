from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
sys.path.insert(0, str(Path(__file__).parent))
from support import PACKAGE, RELEASE  # noqa: E402


@pytest.fixture
def execution_stack(tmp_path):
    database = tmp_path / "core.db"
    asset_root = tmp_path / "assets"
    repository = CoreAuthorityRepository(database)
    assets = AssetStore(asset_root)
    repository.create_workspace(Workspace("ws-1", "Novel"))
    repository.create_document(Document("doc-1", "ws-1", "Chapter"))
    base = repository.publish_revision(
        document_id="doc-1", content="old", expected_revision_id=None, created_by="user", revision_id="rev-base"
    )
    snapshot = json.loads(Path("contracts/golden/run-snapshot/snapshot.json").read_text(encoding="utf-8"))
    authority = ExecutionAuthority(repository, assets)
    authority.create_from_verified_snapshot("job-1", snapshot)
    authority.start_attempt(
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        worker_run_id="worker-run-1",
        plugin_id="com.plotpilot.demo",
        release_id=RELEASE,
        package_hash=PACKAGE,
        capability_id="writing.chapter.draft/v1",
        generation_id="generation-1",
        preallocated_receipt_id="receipt-1",
    )
    yield {
        "database": database,
        "asset_root": asset_root,
        "repository": repository,
        "assets": assets,
        "authority": authority,
        "snapshot": snapshot,
        "base": base,
    }
    repository.close()
