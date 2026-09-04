from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.backup import (
    BackupConflictError,
    BackupDataError,
    BackupRequest,
    RestoreRequest,
    compose_backup_runtime,
)
from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.plugins.generation import GenerationState
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_plugin_sdk.canonical import canonical_bytes, hash_jcs

CREATED_AT = "2026-09-04T00:00:00Z"
VERIFIED_AT = "2026-09-04T00:00:01Z"
COMPLETED_AT = "2026-09-04T00:00:02Z"


@dataclass
class EmptyGenerationSource:
    core_authority_binding: object

    def generation_state(self) -> GenerationState:
        return GenerationState()


@dataclass
class RestoreFixture:
    root: Path
    repository: CoreAuthorityRepository
    runtime: Any


@contextmanager
def _fixture(tmp_path: Path) -> Iterator[RestoreFixture]:
    root = tmp_path / "active"
    (root / "core").mkdir(parents=True)
    repository = CoreAuthorityRepository(root / "core" / "core.db")
    assets = AssetStore(root / "assets")
    try:
        repository.create_workspace(Workspace("workspace-1", "Novel"))
        runtime = compose_backup_runtime(
            repository,
            assets,
            EmptyGenerationSource(repository),
            source_root=root,
            library_root_id="root-active",
        )
        yield RestoreFixture(root, repository, runtime)
    finally:
        repository.close()


def _backup_request() -> BackupRequest:
    return BackupRequest(
        backup_id="backup-restore-quarantine",
        library_root_id="root-active",
        backup_epoch=23,
        mode="full",
        workspace_ids=("workspace-1",),
        created_at=CREATED_AT,
        verified_at=VERIFIED_AT,
    )


def _restore_request(restore_id: str = "restore-1") -> RestoreRequest:
    return RestoreRequest(
        restore_id=restore_id,
        source_root_id="root-active",
        target_root_id="root-restored",
        created_at=VERIFIED_AT,
        completed_at=COMPLETED_AT,
    )


def _tree_bytes(root: Path) -> tuple[tuple[str, str, bytes | None], ...]:
    result: list[tuple[str, str, bytes | None]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            result.append(("file", relative, path.read_bytes()))
        elif path.is_dir():
            result.append(("directory", relative, None))
    return tuple(sorted(result))


def test_valid_commit_unknown_publication_is_retained_and_replayed(
    tmp_path: Path,
) -> None:
    with _fixture(tmp_path) as value:
        bundle = value.runtime.api.create_backup(tmp_path / "bundle", _backup_request())
        target = tmp_path / "restored"
        raised = False

        def lose_acknowledgement(step: str, root: Path) -> None:
            nonlocal raised
            if step == "published" and not raised:
                raised = True
                assert root == target
                raise RuntimeError("publication acknowledgement lost")

        value.runtime.backup.on_restore_stage = lose_acknowledgement
        with pytest.raises(RuntimeError, match="acknowledgement lost"):
            value.runtime.api.restore_to_new_root(
                bundle.bundle_root, target, _restore_request()
            )

        assert target.is_dir()
        assert not list(tmp_path.glob(".r-*.quarantine"))
        published_bytes = _tree_bytes(target)
        value.runtime.backup.on_restore_stage = None
        replay = value.runtime.api.restore_to_new_root(
            bundle.bundle_root, target, _restore_request()
        )
        assert replay.reused is True
        assert _tree_bytes(target) == published_bytes


def test_invalid_exact_marker_bound_postrename_target_is_quarantined(
    tmp_path: Path,
) -> None:
    with _fixture(tmp_path) as value:
        bundle = value.runtime.api.create_backup(tmp_path / "bundle", _backup_request())
        active_before = _tree_bytes(value.root)
        bundle_before = _tree_bytes(bundle.bundle_root)
        target = tmp_path / "restored"
        marker_bytes: list[bytes] = []

        def corrupt_after_rename(step: str, root: Path) -> None:
            if step == "published":
                marker_bytes.append((root / ".plotpilot-stage.json").read_bytes())
                (root / "backup.json").write_bytes(b"{}\n")

        value.runtime.backup.on_restore_stage = corrupt_after_rename
        with pytest.raises(BackupDataError):
            value.runtime.api.restore_to_new_root(
                bundle.bundle_root, target, _restore_request()
            )

        quarantines = list(tmp_path.glob(".r-*.quarantine"))
        assert not target.exists()
        assert len(quarantines) == 1
        quarantine = quarantines[0]
        marker_path = quarantine / ".plotpilot-stage.json"
        assert marker_path.read_bytes() == marker_bytes[0]
        marker = json.loads(marker_path.read_bytes())
        assert marker["operation"] == "restore"
        assert marker["operation_id"] == "restore-1"
        assert marker["state"] == "published"
        assert marker["target_name"] == target.name
        assert marker["target_parent_id"] == os.path.normcase(
            os.path.normpath(os.path.abspath(target.parent))
        )
        assert marker["binding"] == bundle.manifest["bundle_hash"]
        assert quarantine.name == f".r-{marker['generation_id']}.quarantine"
        assert _tree_bytes(value.root) == active_before
        assert _tree_bytes(bundle.bundle_root) == bundle_before


def test_existing_quarantine_is_never_replaced_and_invalid_target_is_not_moved(
    tmp_path: Path,
) -> None:
    with _fixture(tmp_path) as value:
        bundle = value.runtime.api.create_backup(tmp_path / "bundle", _backup_request())
        target = tmp_path / "restored"
        value.runtime.api.restore_to_new_root(
            bundle.bundle_root, target, _restore_request()
        )
        (target / ".plotpilot/restore-report.json").write_bytes(b"{}\n")
        marker = json.loads((target / ".plotpilot-stage.json").read_bytes())
        quarantine = target.with_name(f".r-{marker['generation_id']}.quarantine")
        quarantine.mkdir()
        (quarantine / "sentinel.bin").write_bytes(b"existing quarantine")
        target_before = _tree_bytes(target)
        quarantine_before = _tree_bytes(quarantine)

        with pytest.raises(BackupConflictError):
            value.runtime.api.restore_to_new_root(
                bundle.bundle_root, target, _restore_request()
            )

        assert _tree_bytes(target) == target_before
        assert _tree_bytes(quarantine) == quarantine_before


@pytest.mark.parametrize("target_kind", ["unbound", "foreign_marker", "uncertain"])
def test_unowned_or_identity_uncertain_restore_target_is_never_moved(
    tmp_path: Path, target_kind: str
) -> None:
    with _fixture(tmp_path) as value:
        bundle = value.runtime.api.create_backup(tmp_path / "bundle", _backup_request())
        target = tmp_path / "restored"
        request = _restore_request()
        if target_kind == "unbound":
            target.mkdir()
            (target / "sentinel.bin").write_bytes(b"foreign target")
        else:
            value.runtime.api.restore_to_new_root(bundle.bundle_root, target, request)
            if target_kind == "foreign_marker":
                request = _restore_request("restore-foreign")
            else:
                marker_path = target / ".plotpilot-stage.json"
                marker = json.loads(marker_path.read_bytes())
                marker["generation_id"] = "z" * 32
                marker["marker_hash"] = hash_jcs(
                    "plotpilot-owned-stage/v2",
                    {key: item for key, item in marker.items() if key != "marker_hash"},
                )
                marker_path.write_bytes(canonical_bytes(marker) + b"\n")
        before = _tree_bytes(target)

        with pytest.raises(BackupDataError):
            value.runtime.api.restore_to_new_root(bundle.bundle_root, target, request)

        assert target.is_dir()
        assert _tree_bytes(target) == before
        assert not list(tmp_path.glob(".r-*.quarantine"))


def test_reparse_restore_target_is_rejected_without_moving_foreign_bytes(
    tmp_path: Path,
) -> None:
    with _fixture(tmp_path) as value:
        bundle = value.runtime.api.create_backup(tmp_path / "bundle", _backup_request())
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        (foreign / "sentinel.bin").write_bytes(b"foreign reparse bytes")
        foreign_before = _tree_bytes(foreign)
        target = tmp_path / "restored"
        try:
            target.symlink_to(foreign, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlink creation is unavailable")

        with pytest.raises(BackupDataError, match="reparse"):
            value.runtime.api.restore_to_new_root(
                bundle.bundle_root, target, _restore_request()
            )

        assert target.is_symlink()
        assert _tree_bytes(foreign) == foreign_before
        assert not list(tmp_path.glob(".r-*.quarantine"))
