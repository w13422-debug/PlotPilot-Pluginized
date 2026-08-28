from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.plotpilot_core.api.v1.jobs.rpc import (
    AttemptStartBinding,
    JobSnapshotExtensions,
)
from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority


class NoMountedSnapshotExtensions:
    """Test composition for the accepted baseline's empty extension sources."""

    def read(self, connection, *, job_id, workspace_id):
        del connection, job_id, workspace_id
        return JobSnapshotExtensions(None, ())


class AuthorityAttemptStartFixture:
    """Test-only bridge that observes the row written by the accepted P1 code."""

    def __init__(self, authority, repository):
        self.authority = authority
        self.repository = repository

    def start_attempt(self, **command):
        self.authority.start_attempt(**command)
        with self.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_attempt WHERE attempt_id=?",
                (command["attempt_id"],),
            ).fetchone()
        return AttemptStartBinding(
            job_id=row["job_id"],
            step_id=row["step_id"],
            attempt_id=row["attempt_id"],
            worker_run_id=row["worker_run_id"],
            plugin_id=row["plugin_id"],
            release_id=row["release_id"],
            package_hash=row["package_hash"],
            capability_id=row["capability_id"],
            generation_id=row["generation_id"],
            lease_epoch=row["lease_epoch"],
            preallocated_receipt_id=row["preallocated_receipt_id"],
            expected_result_contract=row["expected_result_contract"],
        )


@pytest.fixture
def snapshot_extensions():
    return NoMountedSnapshotExtensions()


@pytest.fixture
def attempt_starter(job_stack):
    return AuthorityAttemptStartFixture(job_stack["authority"], job_stack["repository"])


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
