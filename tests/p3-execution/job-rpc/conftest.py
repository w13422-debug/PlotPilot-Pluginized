from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.plotpilot_core.api.v1.jobs.rpc import JobSnapshotExtensions
from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority


class NoMountedSnapshotExtensions:
    """Test composition for the accepted baseline's empty extension sources."""

    def read(self, connection, *, job_id, workspace_id):
        del connection, job_id, workspace_id
        return JobSnapshotExtensions(None, ())


@pytest.fixture
def snapshot_extensions():
    return NoMountedSnapshotExtensions()


@pytest.fixture
def job_stack(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    repository.create_workspace(Workspace("ws-1", "Novel"))
    repository.create_workspace(Workspace("ws-2", "Other"))
    snapshot = json.loads(
        Path("contracts/golden/run-snapshot/snapshot.json").read_text(encoding="utf-8")
    )
    authority = ExecutionAuthority(repository, assets)
    try:
        yield {
            "repository": repository,
            "assets": assets,
            "authority": authority,
            "snapshot": snapshot,
        }
    finally:
        repository.close()
