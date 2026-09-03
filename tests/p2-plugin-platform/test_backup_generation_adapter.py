from __future__ import annotations

import copy
from dataclasses import dataclass

import pytest
from plotpilot_core.backup.models import BackupBarrier
from plotpilot_core.plugins.backup import (
    GenerationBackupContributor,
    GenerationBackupError,
)
from plotpilot_core.plugins.generation import GenerationState
from plotpilot_plugin_sdk.errors import ContractError

RELEASE_A = "a" * 64
RELEASE_B = "b" * 64
PACKAGE_A = "c" * 64
PACKAGE_B = "d" * 64
CORE_HASH = "e" * 64


@dataclass
class FakeGenerationSource:
    state: GenerationState

    def generation_state(self) -> GenerationState:
        return self.state


def _generation(generation_id: str, *, release_id: str, package_hash: str) -> dict:
    return {
        "schema": "plugin-generation/v1",
        "generation_id": generation_id,
        "core_api_version": "1.2.0",
        "health_result_asset_id": f"health-{generation_id}",
        "created_reason": "backup projection test",
        "created_at": "2026-09-01T00:00:00Z",
        "parent_generation_id": None,
        "base_generation_id": None,
        "members": [
            {
                "plugin_id": "com.plotpilot.test",
                "release_id": release_id,
                "package_hash": package_hash,
                "data_generation_id": "data-generation-1",
                "ui_bundle_hash": None,
                "global_settings_revision_id": None,
                "settings_schema_hash": None,
                "data_bundle_asset_id": "data-generation-1",
            }
        ],
    }


def _barrier() -> BackupBarrier:
    return BackupBarrier(
        token="backup-barrier-1",
        backup_epoch=1,
        core_event_high_water=7,
        created_at="2026-09-01T00:00:00Z",
    )


def test_generation_contributor_keeps_current_and_lkg_release_lineage() -> None:
    source = FakeGenerationSource(
        GenerationState(
            current=_generation(
                "generation-current",
                release_id=RELEASE_A,
                package_hash=PACKAGE_A,
            ),
            lkg=_generation(
                "generation-lkg",
                release_id=RELEASE_B,
                package_hash=PACKAGE_B,
            ),
        )
    )

    snapshot = GenerationBackupContributor(source).capture_for_backup(
        barrier=_barrier(),
        core_database=object(),
        core_snapshot_hash=CORE_HASH,
        mode="full",
        workspace_ids=("workspace-1",),
    )

    assert snapshot.barrier_token == "backup-barrier-1"
    assert snapshot.bound_core_snapshot_hash == CORE_HASH
    assert snapshot.current_generation_id == "generation-current"
    assert snapshot.lkg_generation_id == "generation-lkg"
    assert snapshot.plugin_releases == (
        {
            "plugin_id": "com.plotpilot.test",
            "release_id": RELEASE_A,
            "package_hash": PACKAGE_A,
            "package_present": False,
        },
        {
            "plugin_id": "com.plotpilot.test",
            "release_id": RELEASE_B,
            "package_hash": PACKAGE_B,
            "package_present": False,
        },
    )
    assert snapshot.asset_ids == (
        "data-generation-1",
        "health-generation-current",
        "health-generation-lkg",
    )


def test_generation_contributor_refuses_to_invent_a_missing_package_identity() -> None:
    generation = _generation(
        "generation-current",
        release_id=RELEASE_A,
        package_hash=PACKAGE_A,
    )
    del generation["members"][0]["package_hash"]
    contributor = GenerationBackupContributor(
        FakeGenerationSource(GenerationState(current=generation))
    )

    with pytest.raises(GenerationBackupError) as caught:
        contributor.capture_for_backup(
            barrier=_barrier(),
            core_database=object(),
            core_snapshot_hash=CORE_HASH,
            mode="full",
            workspace_ids=("workspace-1",),
        )
    assert isinstance(caught.value.__cause__, ContractError)


def test_generation_contributor_rejects_duplicate_plugin_members() -> None:
    generation = _generation(
        "generation-current",
        release_id=RELEASE_A,
        package_hash=PACKAGE_A,
    )
    generation["members"].append(copy.deepcopy(generation["members"][0]))

    with pytest.raises(GenerationBackupError) as caught:
        GenerationBackupContributor(
            FakeGenerationSource(GenerationState(current=generation))
        ).capture_for_backup(
            barrier=_barrier(),
            core_database=object(),
            core_snapshot_hash=CORE_HASH,
            mode="full",
            workspace_ids=("workspace-1",),
        )

    assert isinstance(caught.value.__cause__, ContractError)


def test_generation_contributor_rejects_members_out_of_utf8_order() -> None:
    generation = _generation(
        "generation-current",
        release_id=RELEASE_A,
        package_hash=PACKAGE_A,
    )
    second = copy.deepcopy(generation["members"][0])
    generation["members"][0]["plugin_id"] = "com.plotpilot.z"
    second.update(
        plugin_id="com.plotpilot.a",
        release_id=RELEASE_B,
        package_hash=PACKAGE_B,
    )
    generation["members"].append(second)

    with pytest.raises(GenerationBackupError) as caught:
        GenerationBackupContributor(
            FakeGenerationSource(GenerationState(current=generation))
        ).capture_for_backup(
            barrier=_barrier(),
            core_database=object(),
            core_snapshot_hash=CORE_HASH,
            mode="full",
            workspace_ids=("workspace-1",),
        )

    assert isinstance(caught.value.__cause__, ContractError)


def test_generation_contributor_rejects_noncontract_asset_pairing() -> None:
    generation = _generation(
        "generation-current",
        release_id=RELEASE_A,
        package_hash=PACKAGE_A,
    )
    generation["qualification_asset_id"] = "qualification-for-another-generation"

    with pytest.raises(GenerationBackupError) as caught:
        GenerationBackupContributor(
            FakeGenerationSource(GenerationState(current=generation))
        ).capture_for_backup(
            barrier=_barrier(),
            core_database=object(),
            core_snapshot_hash=CORE_HASH,
            mode="full",
            workspace_ids=("workspace-1",),
        )

    assert isinstance(caught.value.__cause__, ContractError)


def test_workspace_backup_does_not_leak_global_generation_or_assets() -> None:
    source = FakeGenerationSource(
        GenerationState(
            current=_generation(
                "generation-current",
                release_id=RELEASE_A,
                package_hash=PACKAGE_A,
            )
        )
    )

    snapshot = GenerationBackupContributor(source).capture_for_backup(
        barrier=_barrier(),
        core_database=object(),
        core_snapshot_hash=CORE_HASH,
        mode="workspace",
        workspace_ids=("workspace-1",),
    )

    assert snapshot.current_generation_id is None
    assert snapshot.lkg_generation_id is None
    assert snapshot.asset_ids == ()
    assert snapshot.plugin_releases == ()
