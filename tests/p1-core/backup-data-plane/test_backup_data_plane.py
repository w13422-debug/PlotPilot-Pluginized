from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.backup import (
    BackupBarrier,
    BackupConflictError,
    BackupDataPlane,
    BackupRequest,
    BackupValidationError,
    CoreSnapshotCapture,
    GenerationSnapshot,
    PackageStoreArchiveAdapter,
    PluginBackupFile,
    PluginDataSnapshot,
    RestoreRequest,
    SqliteAssetReferenceScanner,
    SqliteCoreSnapshotAdapter,
)
from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.plugins.package import verify_package
from backend.plotpilot_core.plugins.store import PackageStore
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_plugin_sdk.canonical import canonical_bytes, hash_jcs
from backend.plotpilot_plugin_sdk.package import build_files_sha256

NOW = "2026-08-28T01:00:00Z"
RELEASE_ID = "1" * 64


class BoundBarrierPort:
    def __init__(self, *, token: str | None = None, high_water: int = 13) -> None:
        self.token = token
        self.high_water = high_water

    @contextmanager
    def hold_for_backup(self, **kwargs: Any):
        yield BackupBarrier(
            token=self.token or f"barrier-{kwargs['backup_epoch']}",
            backup_epoch=kwargs["backup_epoch"],
            core_event_high_water=self.high_water,
            created_at=kwargs["created_at"],
        )


class BoundCoreSnapshotPort:
    def __init__(
        self,
        *,
        state_asset_ids: tuple[str, ...] = (),
        required_asset_ids: tuple[str, ...] = (),
        wrong_binding: bool = False,
        compatible: bool = True,
        workspace_mismatch: bool = False,
        high_water_delta: int = 0,
        wrong_state_hash: bool = False,
        force_scope: str | None = None,
        core_contract_version: str = "1.2.0",
    ) -> None:
        self.state_asset_ids = tuple(sorted(state_asset_ids))
        self.required_asset_ids = tuple(sorted(required_asset_ids))
        self.wrong_binding = wrong_binding
        self.compatible = compatible
        self.workspace_mismatch = workspace_mismatch
        self.high_water_delta = high_water_delta
        self.wrong_state_hash = wrong_state_hash
        self.force_scope = force_scope
        self.core_contract_version = core_contract_version

    def capture_for_backup(self, **kwargs: Any) -> CoreSnapshotCapture:
        barrier = kwargs["barrier"]
        database_sha256 = kwargs["database_sha256"]
        workspace_ids = kwargs["workspace_ids"]
        snapshot: dict[str, object] = {
            "schema": "core-snapshot/v1",
            "snapshot_id": "core-snapshot-1",
            "subscription_scope": {
                "workspace_id": (
                    self.force_scope
                    if self.force_scope is not None
                    else workspace_ids[0] if kwargs["mode"] == "workspace" else None
                ),
                "event_types": [],
            },
            "core_snapshot_revision": barrier.backup_epoch,
            "core_event_high_water": barrier.core_event_high_water + self.high_water_delta,
            "coverage_complete": True,
            "covered_aggregates": [
                {
                    "aggregate_type": "workspace",
                    "aggregate_id": f"aggregate-{index}",
                    "aggregate_revision": 1,
                    "state_asset_id": asset_id,
                    "state_hash": (
                        "0" * 64
                        if self.wrong_state_hash
                        else asset_id.removeprefix("asset-sha256-")
                    ),
                }
                for index, asset_id in enumerate(self.state_asset_ids)
            ],
            "created_at": NOW,
        }
        snapshot["snapshot_hash"] = hash_jcs("core-snapshot/v1", snapshot)
        workspace_hash = snapshot["snapshot_hash"] if kwargs["mode"] == "workspace" else None
        if self.workspace_mismatch:
            workspace_hash = "f" * 64
        return CoreSnapshotCapture(
            barrier_token="wrong" if self.wrong_binding else barrier.token,
            bound_database_sha256="0" * 64 if self.wrong_binding else database_sha256,
            bound_core_event_high_water=barrier.core_event_high_water,
            bound_asset_ids=kwargs["database_asset_ids"],
            bound_workspace_ids=workspace_ids,
            core_contract_version=self.core_contract_version,
            workspace_snapshot_hash=workspace_hash,
            required_asset_ids=self.required_asset_ids,
            compatible=self.compatible,
            snapshot=snapshot,
        )


class BoundGenerationPort:
    def __init__(
        self,
        *,
        asset_ids: tuple[str, ...] = (),
        files: tuple[PluginBackupFile, ...] = (),
        plugin_releases: tuple[dict[str, object], ...] = (),
        projection_rebuild_required: tuple[dict[str, object], ...] = (),
        wrong_binding: bool = False,
        compatible: bool = True,
    ) -> None:
        self.asset_ids = tuple(sorted(asset_ids))
        self.files = files
        self.plugin_releases = plugin_releases
        self.projection_rebuild_required = projection_rebuild_required
        self.wrong_binding = wrong_binding
        self.compatible = compatible

    def capture_for_backup(self, **kwargs: Any) -> GenerationSnapshot:
        core_hash = "0" * 64 if self.wrong_binding else kwargs["core_snapshot_hash"]
        return GenerationSnapshot(
            barrier_token=kwargs["barrier"].token,
            bound_core_snapshot_hash=core_hash,
            current_generation_id="generation-current",
            lkg_generation_id="generation-lkg",
            asset_ids=self.asset_ids,
            files=self.files,
            compatible=self.compatible,
            plugin_releases=self.plugin_releases,
            projection_rebuild_required=self.projection_rebuild_required,
        )


class BoundPluginDataPort:
    def __init__(
        self,
        *,
        asset_ids: tuple[str, ...] = (),
        files: tuple[PluginBackupFile, ...] = (),
        wrong_binding: bool = False,
        compatible: bool = True,
    ) -> None:
        self.asset_ids = tuple(sorted(asset_ids))
        self.files = files
        self.wrong_binding = wrong_binding
        self.compatible = compatible

    def capture_for_backup(self, **kwargs: Any) -> PluginDataSnapshot:
        core_hash = "f" * 64 if self.wrong_binding else kwargs["core_snapshot_hash"]
        return PluginDataSnapshot(
            barrier_token=kwargs["barrier"].token,
            bound_core_snapshot_hash=core_hash,
            asset_ids=self.asset_ids,
            files=self.files,
            compatible=self.compatible,
        )


def _request(
    backup_id: str = "backup-1",
    *,
    mode: str = "workspace",
    core_contract_version: str = "1.2.0",
) -> BackupRequest:
    return BackupRequest(
        backup_id=backup_id,
        library_root_id="library-source",
        backup_epoch=7,
        mode=mode,  # type: ignore[arg-type]
        workspace_ids=("ws-1",),
        created_at=NOW,
        verified_at=NOW,
        core_contract_version=core_contract_version,
    )


def _restore_request(restore_id: str = "restore-1") -> RestoreRequest:
    return RestoreRequest(
        restore_id=restore_id,
        source_root_id="library-source",
        target_root_id="library-restored",
        created_at=NOW,
        completed_at=NOW,
    )


def _source(
    tmp_path: Path, *, asset_content: bytes | None = b"cover"
) -> tuple[Path, Path, Path, str | None]:
    source = tmp_path / "source"
    source.mkdir()
    assets = source / "assets"
    store = AssetStore(assets)
    asset_id = None
    metadata: dict[str, object] = {}
    if asset_content is not None:
        asset = store.put(
            asset_content,
            mime="application/octet-stream",
            logical_role="workspace_cover",
            provenance="test",
        )
        asset_id = asset.asset_id
        metadata["cover_asset_id"] = asset_id
    database = source / "core.db"
    repo = CoreAuthorityRepository(database)
    repo.create_workspace(
        Workspace(
            "ws-1",
            "Novel",
            metadata=metadata,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    repo.close()
    return source, database, assets, asset_id


def _plane(source: Path, database: Path, assets: Path, **kwargs: Any) -> BackupDataPlane:
    return BackupDataPlane(
        source_root=source,
        core_database=database,
        asset_root=assets,
        barrier_port=kwargs.pop("barrier_port", BoundBarrierPort()),
        core_snapshot_port=kwargs.pop("core_snapshot_port", SqliteCoreSnapshotAdapter(assets)),
        generation_port=kwargs.pop("generation_port", BoundGenerationPort()),
        plugin_data_port=kwargs.pop("plugin_data_port", BoundPluginDataPort()),
        **kwargs,
    )


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _file_descriptor(
    path: Path,
    relative: str,
    role: str,
    *,
    release_id: str | None = None,
    package_hash: str | None = None,
) -> PluginBackupFile:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    return PluginBackupFile(
        path=relative,
        role=role,  # type: ignore[arg-type]
        source_path=path,
        sha256=digest,
        size=len(raw),
        release_id=release_id,
        package_hash=package_hash,
    )


def _write_canonical(path: Path, value: dict[str, object]) -> bytes:
    raw = canonical_bytes(value) + b"\n"
    path.write_bytes(raw)
    return raw


def _rehash_bundle(bundle: Path, manifest: dict[str, object]) -> None:
    unsigned_manifest = {key: value for key, value in manifest.items() if key != "bundle_hash"}
    manifest["bundle_hash"] = hash_jcs("plotpilot-backup/v1", unsigned_manifest)
    manifest_raw = _write_canonical(bundle / "backup.json", manifest)
    receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    receipt["bundle_hash"] = manifest["bundle_hash"]
    receipt["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    unsigned_receipt = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    receipt["receipt_hash"] = hash_jcs("plotpilot-backup-receipt/v1", unsigned_receipt)
    _write_canonical(bundle / "receipt.json", receipt)


def _release(package_hash: str, *, present: bool) -> dict[str, object]:
    return {
        "plugin_id": "com.plotpilot.test",
        "release_id": RELEASE_ID,
        "package_hash": package_hash,
        "package_present": present,
    }


def _valid_package(path: Path) -> tuple[Path, str, str]:
    manifest = {
        "capabilities": [],
        "compatibility": {
            "core_api": ">=1.0 <2.0",
            "plugin_rpc": "1",
            "ui_host": "1",
        },
        "data": {"format": "test/v1", "root": "data/value.json"},
        "display_name": "Backup Test",
        "kind": "data",
        "needs": [],
        "plugin_id": "com.plotpilot.test",
        "schema": "plotpilot-plugin/v1",
        "version": "1.0.0",
    }
    ordinary = {
        "plugin.json": (json.dumps(manifest, separators=(",", ":")) + "\n").encode(),
        "data/value.json": b'{"value":1}\n',
    }
    files = {**ordinary, "files.sha256": build_files_sha256(ordinary)}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(files):
            archive.writestr(name, files[name])
    path.write_bytes(output.getvalue())
    verified = verify_package(path)
    return path, verified.package_hash, verified.release_id


def test_backup_manifest_receipt_and_restore_are_deterministic_and_idempotent(
    tmp_path: Path,
) -> None:
    source, database, assets, asset_id = _source(tmp_path)
    ambient = AssetStore(assets).put(
        b"ambient", mime="application/octet-stream", logical_role="unreferenced", provenance="test"
    )
    plane = _plane(source, database, assets)

    first = plane.create_backup(tmp_path / "backup-a", _request())
    second = plane.create_backup(tmp_path / "backup-b", _request())

    assert first.manifest == second.manifest
    assert first.receipt == second.receipt
    assert (first.bundle_root / "backup.json").read_bytes() == (
        second.bundle_root / "backup.json"
    ).read_bytes()
    assert first.manifest["files"] == sorted(
        first.manifest["files"], key=lambda item: item["path"].encode("utf-8")
    )
    assert asset_id in first.receipt["asset_ids"]
    assert ambient.asset_id not in first.receipt["asset_ids"]
    assert first.manifest["workspace_snapshot_hash"] == first.manifest["core_snapshot_hash"]
    plane.verify_backup(first.bundle_root)

    live_hashes = _tree_hashes(source)
    restored = plane.stage_restore(first.bundle_root, tmp_path / "restored", _restore_request())
    ready_hashes = _tree_hashes(restored.target_root)
    repeated = plane.stage_restore(first.bundle_root, tmp_path / "restored", _restore_request())

    assert restored.report["state"] == "restore_ready"
    assert restored.reused is False
    assert repeated.reused is True
    assert repeated.report == restored.report
    assert _tree_hashes(restored.target_root) == ready_hashes
    assert _tree_hashes(source) == live_hashes


def test_online_backup_never_splices_asset_closure_across_commits(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path, asset_content=None)
    store = AssetStore(assets)
    first = store.put(b"before", mime="text/plain", logical_role="binding", provenance="test")
    second = store.put(b"after", mime="text/plain", logical_role="binding", provenance="test")
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("CREATE TABLE asset_binding(version INTEGER NOT NULL, asset_id TEXT NOT NULL)")
    connection.execute("INSERT INTO asset_binding VALUES(1,?)", (first.asset_id,))
    connection.execute("CREATE TABLE backup_padding(payload BLOB NOT NULL)")
    for _ in range(24):
        connection.execute("INSERT INTO backup_padding VALUES(zeroblob(524288))")
    connection.close()
    changed = False

    def commit_during_backup(status: int, remaining: int, total: int) -> None:
        nonlocal changed
        if changed:
            return
        writer = sqlite3.connect(database, isolation_level=None, timeout=10)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE asset_binding SET version=2,asset_id=?", (second.asset_id,))
            writer.commit()
            changed = True
        finally:
            writer.close()

    plane = _plane(
        source,
        database,
        assets,
        backup_pages=1,
        on_backup_progress=commit_during_backup,
    )
    result = plane.create_backup(tmp_path / "backup", _request())

    assert changed is True
    snapshot_db = sqlite3.connect(result.bundle_root / "core/core.db")
    version, asset_id = snapshot_db.execute("SELECT version,asset_id FROM asset_binding").fetchone()
    snapshot_db.close()
    assert (version, asset_id) in {(1, first.asset_id), (2, second.asset_id)}
    digest = asset_id.removeprefix("asset-sha256-")
    assert (result.bundle_root / f"assets/objects/{digest[:2]}/{digest}").read_bytes() in {
        b"before",
        b"after",
    }
    assert result.receipt["asset_roots"]["core_database"] == [asset_id]


def test_all_contributor_and_plugin_database_asset_roots_enter_closure(tmp_path: Path) -> None:
    source, database, assets, core_db_asset = _source(tmp_path)
    store = AssetStore(assets)
    state = store.put(b"state", mime="application/json", logical_role="state", provenance="test")
    core_declared = store.put(b"core-extra", mime="text/plain", logical_role="core", provenance="test")
    p2 = store.put(b"p2", mime="text/plain", logical_role="p2", provenance="test")
    p3 = store.put(b"p3", mime="text/plain", logical_role="p3", provenance="test")
    p3_db_asset = store.put(b"p3-db", mime="text/plain", logical_role="p3-db", provenance="test")
    plugin_db = tmp_path / "plugin.db"
    connection = sqlite3.connect(plugin_db)
    connection.execute("CREATE TABLE binding(asset_id TEXT NOT NULL)")
    connection.execute("INSERT INTO binding VALUES(?)", (p3_db_asset.asset_id,))
    connection.commit()
    connection.close()
    plugin_file = _file_descriptor(plugin_db, "plugins/data/plugin.db", "plugin_db")
    plane = _plane(
        source,
        database,
        assets,
        core_snapshot_port=BoundCoreSnapshotPort(
            state_asset_ids=(state.asset_id,), required_asset_ids=(core_declared.asset_id,)
        ),
        generation_port=BoundGenerationPort(asset_ids=(p2.asset_id,)),
        plugin_data_port=BoundPluginDataPort(
            asset_ids=(p3.asset_id,), files=(plugin_file,)
        ),
    )

    result = plane.create_backup(tmp_path / "backup", _request(mode="data"))

    assert set(result.receipt["asset_ids"]) == {
        core_db_asset,
        state.asset_id,
        core_declared.asset_id,
        p2.asset_id,
        p3.asset_id,
        p3_db_asset.asset_id,
    }
    assert result.receipt["asset_roots"]["core_snapshot"] == [state.asset_id]
    assert result.receipt["asset_roots"]["plugin_databases"] == [p3_db_asset.asset_id]
    plane.verify_backup(result.bundle_root)


@pytest.mark.parametrize("contributor", ["core", "p2", "p3", "plugin_db"])
def test_missing_contributor_asset_fails_closed(tmp_path: Path, contributor: str) -> None:
    source, database, assets, _ = _source(tmp_path)
    missing = "asset-sha256-" + "f" * 64
    mode = "workspace"
    kwargs: dict[str, object] = {}
    if contributor == "core":
        kwargs["core_snapshot_port"] = BoundCoreSnapshotPort(state_asset_ids=(missing,))
    elif contributor == "p2":
        kwargs["generation_port"] = BoundGenerationPort(asset_ids=(missing,))
    elif contributor == "p3":
        kwargs["plugin_data_port"] = BoundPluginDataPort(asset_ids=(missing,))
    else:
        mode = "data"
        plugin_db = tmp_path / "missing-asset-plugin.db"
        connection = sqlite3.connect(plugin_db)
        connection.execute("CREATE TABLE binding(asset_id TEXT NOT NULL)")
        connection.execute("INSERT INTO binding VALUES(?)", (missing,))
        connection.commit()
        connection.close()
        kwargs["plugin_data_port"] = BoundPluginDataPort(
            files=(_file_descriptor(plugin_db, "plugins/data/missing.db", "plugin_db"),)
        )
    plane = _plane(source, database, assets, **kwargs)

    with pytest.raises(BackupValidationError, match="required Asset"):
        plane.create_backup(tmp_path / "backup", _request(mode=mode))

    assert not (tmp_path / "backup").exists()


def test_asset_closure_is_read_from_frozen_database_not_live_authority(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path, asset_content=None)
    store = AssetStore(assets)
    first = store.put(b"frozen", mime="text/plain", logical_role="binding", provenance="test")
    second = store.put(b"live-later", mime="text/plain", logical_role="binding", provenance="test")
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("CREATE TABLE asset_binding(version INTEGER NOT NULL, asset_id TEXT NOT NULL)")
    connection.execute("INSERT INTO asset_binding VALUES(1,?)", (first.asset_id,))
    connection.close()

    def mutate_only_after_copy(snapshot_database: Path) -> None:
        frozen = sqlite3.connect(
            f"{snapshot_database.resolve().as_uri()}?mode=ro&immutable=1", uri=True
        )
        assert frozen.execute("SELECT version,asset_id FROM asset_binding").fetchone() == (
            1,
            first.asset_id,
        )
        frozen.close()
        writer = sqlite3.connect(database, isolation_level=None)
        writer.execute("UPDATE asset_binding SET version=2,asset_id=?", (second.asset_id,))
        writer.close()

    plane = _plane(source, database, assets, on_core_snapshot_copied=mutate_only_after_copy)
    result = plane.create_backup(tmp_path / "backup", _request())

    assert first.asset_id in result.receipt["asset_ids"]
    assert second.asset_id not in result.receipt["asset_ids"]


def test_missing_asset_fails_closed_and_cleans_only_current_backup_generation(
    tmp_path: Path,
) -> None:
    source, database, assets, asset_id = _source(tmp_path)
    assert asset_id is not None
    digest = asset_id.removeprefix("asset-sha256-")
    (assets / f"objects/{digest[:2]}/{digest}").unlink()
    plane = _plane(source, database, assets)

    with pytest.raises(BackupValidationError, match="required Asset"):
        plane.create_backup(tmp_path / "backup", _request())

    assert not (tmp_path / "backup").exists()
    assert not list(tmp_path.glob(".b-*.stage"))
    assert database.exists()


@pytest.mark.parametrize("damage", ["hash", "missing"])
def test_restore_rejects_corrupt_or_missing_asset_before_creating_target(
    tmp_path: Path, damage: str
) -> None:
    source, database, assets, asset_id = _source(tmp_path)
    plane = _plane(source, database, assets)
    backup = plane.create_backup(tmp_path / "backup", _request())
    assert asset_id is not None
    digest = asset_id.removeprefix("asset-sha256-")
    object_path = backup.bundle_root / f"assets/objects/{digest[:2]}/{digest}"
    if damage == "hash":
        object_path.write_bytes(b"corrupt")
    else:
        object_path.unlink()
    live_hashes = _tree_hashes(source)

    with pytest.raises(BackupValidationError):
        plane.stage_restore(backup.bundle_root, tmp_path / "restored", _restore_request())

    assert not (tmp_path / "restored").exists()
    assert _tree_hashes(source) == live_hashes


def test_content_address_binding_survives_outer_hash_recomputation(tmp_path: Path) -> None:
    source, database, assets, asset_id = _source(tmp_path)
    plane = _plane(source, database, assets)
    backup = plane.create_backup(tmp_path / "backup", _request())
    assert asset_id is not None
    digest = asset_id.removeprefix("asset-sha256-")
    object_path = backup.bundle_root / f"assets/objects/{digest[:2]}/{digest}"
    replacement = b"same outer hashes are recomputed"
    object_path.write_bytes(replacement)
    manifest = json.loads((backup.bundle_root / "backup.json").read_text(encoding="utf-8"))
    object_item = next(item for item in manifest["files"] if item["path"].endswith(digest))
    object_item["size"] = len(replacement)
    object_item["sha256"] = hashlib.sha256(replacement).hexdigest()
    _rehash_bundle(backup.bundle_root, manifest)

    with pytest.raises(BackupValidationError, match="content-addressed ID"):
        plane.verify_backup(backup.bundle_root)


@pytest.mark.parametrize("case", ["missing", "wrong_hash", "orphan", "wrong_mode"])
def test_package_release_binding_failures_are_closed(tmp_path: Path, case: str) -> None:
    source, database, assets, _ = _source(tmp_path)
    package, package_hash, release_id = _valid_package(tmp_path / "plugin.ppkg")
    descriptor = _file_descriptor(
        package,
        "packages/com.plotpilot.test/plugin.ppkg",
        "package",
        release_id=release_id,
        package_hash=package_hash,
    )
    releases: tuple[dict[str, object], ...] = (
        {
            "plugin_id": "com.plotpilot.test",
            "release_id": release_id,
            "package_hash": package_hash,
            "package_present": True,
        },
    )
    files = (descriptor,)
    mode = "full"
    if case == "missing":
        files = ()
    elif case == "wrong_hash":
        releases = ({**releases[0], "package_hash": "2" * 64},)
    elif case == "orphan":
        releases = ()
    else:
        mode = "data"
        releases = ({**releases[0], "package_present": False},)
    plane = _plane(
        source,
        database,
        assets,
        generation_port=BoundGenerationPort(files=files, plugin_releases=releases),
    )

    with pytest.raises(BackupValidationError):
        plane.create_backup(tmp_path / "backup", _request(mode=mode))


def test_full_backup_binds_one_package_to_one_release(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    package, package_hash, release_id = _valid_package(tmp_path / "plugin.ppkg")
    archive_digest = hashlib.sha256(package.read_bytes()).hexdigest()
    descriptor = _file_descriptor(
        package,
        "packages/com.plotpilot.test/plugin.ppkg",
        "package",
        release_id=release_id,
        package_hash=package_hash,
    )
    plane = _plane(
        source,
        database,
        assets,
        generation_port=BoundGenerationPort(
            files=(descriptor,),
            plugin_releases=(
                {
                    "plugin_id": "com.plotpilot.test",
                    "release_id": release_id,
                    "package_hash": package_hash,
                    "package_present": True,
                },
            ),
        ),
    )

    backup = plane.create_backup(tmp_path / "backup", _request(mode="full"))

    assert backup.receipt["package_bindings"] == [
        {
            "plugin_id": "com.plotpilot.test",
            "release_id": release_id,
            "package_hash": package_hash,
            "path": descriptor.path,
            "archive_sha256": archive_digest,
        }
    ]
    assert package_hash != archive_digest
    plane.verify_backup(backup.bundle_root)


def test_interrupted_restore_uses_new_generation_and_explicit_cleanup(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    interrupted_stages: list[Path] = []

    def interrupt_once(step: str, stage: Path) -> None:
        if step == "core/core.db" and not interrupted_stages:
            interrupted_stages.append(stage)
            raise KeyboardInterrupt("simulated process stop")

    plane = _plane(source, database, assets, on_restore_stage=interrupt_once)
    backup = plane.create_backup(tmp_path / "backup", _request())
    target = tmp_path / "restored"

    with pytest.raises(KeyboardInterrupt):
        plane.stage_restore(backup.bundle_root, target, _restore_request())
    old_stage = interrupted_stages[0]
    assert old_stage.is_dir()
    assert not target.exists()

    result = plane.stage_restore(backup.bundle_root, target, _restore_request())
    assert result.report["state"] == "restore_ready"
    assert old_stage.is_dir()
    plane.cleanup_restore_staging(
        old_stage,
        target_root=target,
        restore_id="restore-1",
        bundle_hash=str(backup.manifest["bundle_hash"]),
    )
    assert not old_stage.exists()
    assert source.exists()


def test_concurrent_same_restore_id_never_deletes_another_generation(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    first_prepared = threading.Event()
    release_first = threading.Event()

    def pause_first(step: str, stage: Path) -> None:
        if step == "prepared" and threading.current_thread().name == "first":
            first_prepared.set()
            assert release_first.wait(10)

    plane = _plane(source, database, assets, on_restore_stage=pause_first)
    backup = plane.create_backup(tmp_path / "backup", _request())
    target = tmp_path / "restored"
    outcomes: list[object] = []

    def restore() -> None:
        try:
            outcomes.append(plane.stage_restore(backup.bundle_root, target, _restore_request()))
        except BackupConflictError as exc:
            outcomes.append(exc)

    first = threading.Thread(target=restore, name="first")
    second = threading.Thread(target=restore, name="second")
    first.start()
    assert first_prepared.wait(10)
    second.start()
    second.join(10)
    release_first.set()
    first.join(10)

    assert not first.is_alive() and not second.is_alive()
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, BackupConflictError) for item in outcomes) == 1
    assert target.is_dir()
    assert not list(tmp_path.glob(".r-*.stage"))
    plane.stage_restore(backup.bundle_root, target, _restore_request())


def test_publish_window_interruption_is_replayable(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    interrupted = False

    def interrupt_after_publish(step: str, root: Path) -> None:
        nonlocal interrupted
        if step == "published" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("crash after nonreplace publish")

    plane = _plane(source, database, assets, on_restore_stage=interrupt_after_publish)
    backup = plane.create_backup(tmp_path / "backup", _request())
    target = tmp_path / "restored"

    with pytest.raises(KeyboardInterrupt):
        plane.stage_restore(backup.bundle_root, target, _restore_request())

    assert target.is_dir()
    assert (target / ".plotpilot-stage.json").is_file()
    replay = plane.stage_restore(backup.bundle_root, target, _restore_request())
    assert replay.reused is True


def test_target_creation_race_preserves_foreign_target(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    target = tmp_path / "restored"

    def create_target(step: str, stage: Path) -> None:
        if step == "verified":
            target.mkdir()
            (target / "foreign.txt").write_text("keep", encoding="utf-8")

    plane = _plane(source, database, assets, on_restore_stage=create_target)
    backup = plane.create_backup(tmp_path / "backup", _request())

    with pytest.raises(BackupConflictError):
        plane.stage_restore(backup.bundle_root, target, _restore_request())

    assert (target / "foreign.txt").read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob(".r-*.stage"))


def test_cleanup_rejects_reparse_content_and_preserves_external_file(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    stages: list[Path] = []

    def interrupt(step: str, stage: Path) -> None:
        if step == "prepared" and not stages:
            stages.append(stage)
            raise KeyboardInterrupt

    plane = _plane(source, database, assets, on_restore_stage=interrupt)
    backup = plane.create_backup(tmp_path / "backup", _request())
    target = tmp_path / "restored"
    with pytest.raises(KeyboardInterrupt):
        plane.stage_restore(backup.bundle_root, target, _restore_request())
    external = tmp_path / "external.txt"
    external.write_text("keep", encoding="utf-8")
    link = stages[0] / "external-link"
    try:
        os.symlink(external, link)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(BackupConflictError, match="reparse"):
        plane.cleanup_restore_staging(
            stages[0],
            target_root=target,
            restore_id="restore-1",
            bundle_hash=str(backup.manifest["bundle_hash"]),
        )

    assert external.read_text(encoding="utf-8") == "keep"


def test_restore_replay_rejects_same_backup_id_with_different_bundle(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    plane = _plane(source, database, assets)
    first = plane.create_backup(tmp_path / "backup-a", _request())
    target = tmp_path / "restored"
    plane.stage_restore(first.bundle_root, target, _restore_request())
    connection = sqlite3.connect(database)
    connection.execute("UPDATE workspace SET title='Changed' WHERE workspace_id='ws-1'")
    connection.commit()
    connection.close()
    second = plane.create_backup(tmp_path / "backup-b", _request())
    assert first.manifest["bundle_hash"] != second.manifest["bundle_hash"]

    with pytest.raises(BackupConflictError, match="different bundle"):
        plane.stage_restore(second.bundle_root, target, _restore_request())


def test_restore_replay_rejects_canonical_tampered_report(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    plane = _plane(source, database, assets)
    backup = plane.create_backup(tmp_path / "backup", _request())
    target = tmp_path / "restored"
    plane.stage_restore(backup.bundle_root, target, _restore_request())
    report_path = target / ".plotpilot/restore-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["verified_files"] = []
    _write_canonical(report_path, report)

    with pytest.raises(BackupConflictError, match="different bundle, receipt, or report"):
        plane.stage_restore(backup.bundle_root, target, _restore_request())


@pytest.mark.parametrize("bad_port", ["core", "generation", "plugin"])
def test_unbound_p2_p3_snapshot_hooks_fail_closed(tmp_path: Path, bad_port: str) -> None:
    source, database, assets, _ = _source(tmp_path)
    kwargs: dict[str, object] = {}
    if bad_port == "core":
        kwargs["core_snapshot_port"] = BoundCoreSnapshotPort(wrong_binding=True)
    elif bad_port == "generation":
        kwargs["generation_port"] = BoundGenerationPort(wrong_binding=True)
    else:
        kwargs["plugin_data_port"] = BoundPluginDataPort(wrong_binding=True)
    plane = _plane(source, database, assets, **kwargs)

    with pytest.raises(BackupValidationError, match="not bound"):
        plane.create_backup(tmp_path / "backup", _request())

    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("bad_port", ["core", "generation", "plugin"])
def test_incompatible_authority_fails_closed(tmp_path: Path, bad_port: str) -> None:
    source, database, assets, _ = _source(tmp_path)
    kwargs: dict[str, object] = {}
    if bad_port == "core":
        kwargs["core_snapshot_port"] = BoundCoreSnapshotPort(compatible=False)
    elif bad_port == "generation":
        kwargs["generation_port"] = BoundGenerationPort(compatible=False)
    else:
        kwargs["plugin_data_port"] = BoundPluginDataPort(compatible=False)
    plane = _plane(source, database, assets, **kwargs)

    with pytest.raises(BackupValidationError, match="incompatible|compatibility"):
        plane.create_backup(tmp_path / "backup", _request())


def test_unknown_core_version_and_workspace_snapshot_mismatch_fail_closed(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    plane = _plane(source, database, assets)
    with pytest.raises(BackupValidationError, match="unsupported Core contract"):
        plane.create_backup(
            tmp_path / "unknown-version",
            _request(core_contract_version="9.0.0"),
        )
    wrong_authority_version = _plane(
        source,
        database,
        assets,
        core_snapshot_port=BoundCoreSnapshotPort(core_contract_version="1.3.0"),
    )
    with pytest.raises(BackupValidationError, match="compatibility authority"):
        wrong_authority_version.create_backup(tmp_path / "wrong-authority-version", _request())
    mismatched = _plane(
        source,
        database,
        assets,
        core_snapshot_port=BoundCoreSnapshotPort(workspace_mismatch=True),
    )
    with pytest.raises(BackupValidationError, match="scope does not match"):
        mismatched.create_backup(tmp_path / "mismatch", _request())


def test_restore_rejects_existing_foreign_target_without_modifying_it(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    plane = _plane(source, database, assets)
    backup = plane.create_backup(tmp_path / "backup", _request())
    target = tmp_path / "restored"
    target.mkdir()
    sentinel = target / "foreign.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(BackupValidationError):
        plane.stage_restore(backup.bundle_root, target, _restore_request())

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_production_core_snapshot_adapter_materializes_authority_state_asset(
    tmp_path: Path,
) -> None:
    source, database, assets, _ = _source(tmp_path)
    backup = _plane(source, database, assets).create_backup(
        tmp_path / "backup", _request()
    )
    snapshot = json.loads((backup.bundle_root / "core/snapshot.json").read_text())

    assert snapshot["core_event_high_water"] == 13
    assert snapshot["subscription_scope"]["workspace_id"] == "ws-1"
    aggregate = snapshot["covered_aggregates"][0]
    assert aggregate["aggregate_id"] == "ws-1"
    assert aggregate["state_hash"] == aggregate["state_asset_id"].removeprefix(
        "asset-sha256-"
    )
    assert b'"workspace_id":"ws-1"' in AssetStore(assets).read(
        aggregate["state_asset_id"]
    )


@pytest.mark.parametrize("delta", [-1, 1])
def test_core_snapshot_high_water_must_equal_durable_barrier(
    tmp_path: Path, delta: int
) -> None:
    source, database, assets, _ = _source(tmp_path)
    plane = _plane(
        source,
        database,
        assets,
        core_snapshot_port=BoundCoreSnapshotPort(high_water_delta=delta),
    )

    with pytest.raises(BackupValidationError, match="high-water"):
        plane.create_backup(tmp_path / "backup", _request())


def test_package_semantic_identity_rejects_repacked_bytes_after_outer_rehash(
    tmp_path: Path,
) -> None:
    source, database, assets, _ = _source(tmp_path)
    package, package_hash, release_id = _valid_package(tmp_path / "plugin.ppkg")
    descriptor = _file_descriptor(
        package,
        "packages/com.plotpilot.test/plugin.ppkg",
        "package",
        release_id=release_id,
        package_hash=package_hash,
    )
    release = {
        "plugin_id": "com.plotpilot.test",
        "release_id": release_id,
        "package_hash": package_hash,
        "package_present": True,
    }
    plane = _plane(
        source,
        database,
        assets,
        generation_port=BoundGenerationPort(
            files=(descriptor,), plugin_releases=(release,)
        ),
    )
    backup = plane.create_backup(tmp_path / "backup", _request(mode="full"))
    package_path = backup.bundle_root / descriptor.path
    raw = bytearray(package_path.read_bytes())
    raw[len(raw) // 2] ^= 1
    package_path.write_bytes(raw)
    manifest = json.loads((backup.bundle_root / "backup.json").read_text())
    item = next(entry for entry in manifest["files"] if entry["path"] == descriptor.path)
    item["sha256"] = hashlib.sha256(raw).hexdigest()
    item["size"] = len(raw)
    receipt = json.loads((backup.bundle_root / "receipt.json").read_text())
    receipt["package_bindings"][0]["archive_sha256"] = item["sha256"]
    manifest["bundle_hash"] = hash_jcs(
        "plotpilot-backup/v1",
        {key: value for key, value in manifest.items() if key != "bundle_hash"},
    )
    manifest_raw = _write_canonical(backup.bundle_root / "backup.json", manifest)
    receipt["bundle_hash"] = manifest["bundle_hash"]
    receipt["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    receipt["receipt_hash"] = hash_jcs(
        "plotpilot-backup-receipt/v1",
        {key: value for key, value in receipt.items() if key != "receipt_hash"},
    )
    _write_canonical(backup.bundle_root / "receipt.json", receipt)

    with pytest.raises(BackupValidationError, match="semantic verification"):
        plane.verify_backup(backup.bundle_root)


def test_package_store_archive_adapter_is_deterministic_and_keeps_semantic_identity(
    tmp_path: Path,
) -> None:
    package, package_hash, release_id = _valid_package(tmp_path / "source.ppkg")
    store = PackageStore(tmp_path / "package-store")
    verified = store.publish(
        package,
        expected_package_hash=package_hash,
        expected_release_id=release_id,
    )
    adapter = PackageStoreArchiveAdapter(store)
    first = adapter.backup_file(
        plugin_id=verified.plugin_id,
        version=verified.version,
        destination=tmp_path / "first.ppkg",
        bundle_path="packages/test.ppkg",
        package_hash=verified.package_hash,
    )
    second = adapter.backup_file(
        plugin_id=verified.plugin_id,
        version=verified.version,
        destination=tmp_path / "second.ppkg",
        bundle_path="packages/test.ppkg",
        package_hash=verified.package_hash,
    )

    assert first.sha256 == second.sha256
    assert first.package_hash == second.package_hash == package_hash
    assert first.release_id == second.release_id == release_id
    assert first.sha256 != first.package_hash


def test_p3_cannot_contribute_a_second_core_database_role(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    forged = _file_descriptor(database, "plugins/forged-core.db", "core_db")
    plane = _plane(
        source,
        database,
        assets,
        plugin_data_port=BoundPluginDataPort(files=(forged,)),
    )

    with pytest.raises(BackupValidationError, match="P3 may contribute only"):
        plane.create_backup(tmp_path / "backup", _request(mode="data"))


def test_core_snapshot_state_hash_must_bind_state_asset_id(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    state = AssetStore(assets).put(
        b"state", mime="application/json", logical_role="state", provenance="test"
    )
    plane = _plane(
        source,
        database,
        assets,
        core_snapshot_port=BoundCoreSnapshotPort(
            state_asset_ids=(state.asset_id,), wrong_state_hash=True
        ),
    )

    with pytest.raises(BackupValidationError, match="state hash"):
        plane.create_backup(tmp_path / "backup", _request())


def test_workspace_scope_rebuild_rejects_multi_workspace_frozen_database(
    tmp_path: Path,
) -> None:
    source, database, assets, _ = _source(tmp_path)
    repository = CoreAuthorityRepository(database)
    repository.create_workspace(
        Workspace("ws-2", "Second", created_at=NOW, updated_at=NOW)
    )
    repository.close()
    request = BackupRequest(
        backup_id="backup-multi",
        library_root_id="library-source",
        backup_epoch=7,
        mode="workspace",
        workspace_ids=("ws-1", "ws-2"),
        created_at=NOW,
        verified_at=NOW,
    )

    with pytest.raises(BackupValidationError, match="could not capture"):
        _plane(source, database, assets).create_backup(tmp_path / "backup", request)


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        ("asset_id", "not-an-asset", None),
        ("payload_json", '{"asset-sha256-' + "a" * 64 + '":1}', "asset"),
        ("payload_json", '{"asset-sha256-bad":1}', None),
        (
            "payload_json",
            '{"x":1,"x":"asset-sha256-' + "a" * 64 + '"}',
            None,
        ),
    ],
)
def test_asset_scanner_is_closed_for_direct_values_keys_and_json(
    tmp_path: Path, column: str, value: str, expected: str | None
) -> None:
    database = tmp_path / "scan.db"
    connection = sqlite3.connect(database)
    connection.execute(f"CREATE TABLE binding({column} TEXT)")
    connection.execute("INSERT INTO binding VALUES(?)", (value,))
    connection.commit()
    connection.close()
    scanner = SqliteAssetReferenceScanner()

    if expected == "asset":
        assert scanner.scan(database) == ("asset-sha256-" + "a" * 64,)
    else:
        with pytest.raises(BackupValidationError):
            scanner.scan(database)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "unexpected": True},
        lambda value: {**value, "size": True},
        lambda value: {key: item for key, item in value.items() if key != "mime"},
    ],
)
def test_asset_metadata_uses_exact_assetstore_schema(
    tmp_path: Path, mutation: Any
) -> None:
    source, database, assets, asset_id = _source(tmp_path)
    assert asset_id is not None
    digest = asset_id.removeprefix("asset-sha256-")
    path = assets / "metadata" / f"{digest}.json"
    value = json.loads(path.read_text())
    path.write_text(json.dumps(mutation(value), separators=(",", ":")), encoding="utf-8")

    with pytest.raises(BackupValidationError, match="Asset|asset"):
        _plane(source, database, assets).create_backup(tmp_path / "backup", _request())


def test_restore_source_identity_is_bound_before_target_replay_or_copy(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    plane = _plane(source, database, assets)
    backup = plane.create_backup(tmp_path / "backup", _request())
    wrong = RestoreRequest(
        restore_id="restore-wrong-source",
        source_root_id="library-wrong",
        target_root_id="library-restored",
        created_at=NOW,
        completed_at=NOW,
    )

    with pytest.raises(BackupValidationError, match="source root ID"):
        plane.stage_restore(backup.bundle_root, tmp_path / "restored", wrong)
    assert not (tmp_path / "restored").exists()
    assert not list(tmp_path.glob(".r-*.stage"))


def test_cleanup_restore_staging_never_deletes_published_root(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    plane = _plane(source, database, assets)
    backup = plane.create_backup(tmp_path / "backup", _request())
    target = tmp_path / "restored"
    plane.stage_restore(backup.bundle_root, target, _restore_request())
    before = _tree_hashes(target)

    with pytest.raises(BackupConflictError):
        plane.cleanup_restore_staging(
            target,
            target_root=target,
            restore_id="restore-1",
            bundle_hash=str(backup.manifest["bundle_hash"]),
        )
    assert _tree_hashes(target) == before
    assert plane.stage_restore(backup.bundle_root, target, _restore_request()).reused


def test_restore_cleanup_binds_complete_target_parent(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    stages: list[Path] = []

    def stop(step: str, stage: Path) -> None:
        if step == "prepared":
            stages.append(stage)
            raise KeyboardInterrupt

    plane = _plane(source, database, assets, on_restore_stage=stop)
    backup = plane.create_backup(tmp_path / "backup", _request())
    target = tmp_path / "parent-a" / "restored"
    target.parent.mkdir()
    with pytest.raises(KeyboardInterrupt):
        plane.stage_restore(backup.bundle_root, target, _restore_request())
    stage = stages[0]
    wrong_target = tmp_path / "parent-b" / "restored"
    wrong_target.parent.mkdir()

    with pytest.raises(BackupConflictError):
        plane.cleanup_restore_staging(
            stage,
            target_root=wrong_target,
            restore_id="restore-1",
            bundle_hash=str(backup.manifest["bundle_hash"]),
        )
    assert stage.exists()
    plane.cleanup_restore_staging(
        stage,
        target_root=target,
        restore_id="restore-1",
        bundle_hash=str(backup.manifest["bundle_hash"]),
    )
    assert not stage.exists()


def test_verify_rejects_reparse_control_file_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.plotpilot_core.backup.service as backup_service

    source, database, assets, _ = _source(tmp_path)
    plane = _plane(source, database, assets)
    backup = plane.create_backup(tmp_path / "backup", _request())
    control = backup.bundle_root / "backup.json"
    original = backup_service._is_reparse_point
    original_read_bytes = Path.read_bytes
    control_reads: list[Path] = []

    def tracked_read_bytes(path: Path) -> bytes:
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(control)
        ):
            control_reads.append(path)
        return original_read_bytes(path)

    def is_reparse(path: Path) -> bool:
        same = os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(control)
        )
        return same or original(path)

    monkeypatch.setattr(backup_service, "_is_reparse_point", is_reparse)
    monkeypatch.setattr(Path, "read_bytes", tracked_read_bytes)
    with pytest.raises(BackupValidationError, match="reparse"):
        plane.verify_backup(backup.bundle_root)
    assert control_reads == []


def test_restore_rejects_reparse_target_component_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.plotpilot_core.backup.service as backup_service

    source, database, assets, _ = _source(tmp_path)
    plane = _plane(source, database, assets)
    backup = plane.create_backup(tmp_path / "backup", _request())
    target_parent = tmp_path / "reparse-parent"
    target_parent.mkdir()
    target = target_parent / "restored"
    original = backup_service._is_reparse_point
    monkeypatch.setattr(
        backup_service,
        "_is_reparse_point",
        lambda path: (
            os.path.normcase(os.path.abspath(path))
            == os.path.normcase(os.path.abspath(target_parent))
        )
        or original(path),
    )

    with pytest.raises(BackupValidationError, match="reparse"):
        plane.stage_restore(backup.bundle_root, target, _restore_request())
    assert not target.exists()
    assert not list(target_parent.glob(".r-*.stage"))


def test_restore_revalidates_after_verified_callback_before_publish(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)

    def mutate(step: str, stage: Path) -> None:
        if step == "verified":
            (stage / "backup.json").write_bytes(b"{}\n")

    plane = _plane(source, database, assets, on_restore_stage=mutate)
    backup = plane.create_backup(tmp_path / "backup", _request())
    target = tmp_path / "restored"

    with pytest.raises(BackupValidationError):
        plane.stage_restore(backup.bundle_root, target, _restore_request())
    assert not target.exists()
    assert not list(tmp_path.glob(".r-*.stage"))


def test_random_barrier_tokens_do_not_change_receipt_or_restore_replay(
    tmp_path: Path,
) -> None:
    source, database, assets, _ = _source(tmp_path)
    first_plane = _plane(
        source, database, assets, barrier_port=BoundBarrierPort(token="random-token-a")
    )
    second_plane = _plane(
        source, database, assets, barrier_port=BoundBarrierPort(token="random-token-b")
    )
    first = first_plane.create_backup(tmp_path / "backup-a", _request())
    second = second_plane.create_backup(tmp_path / "backup-b", _request())

    assert first.manifest == second.manifest
    assert first.receipt == second.receipt
    target = tmp_path / "restored"
    first_plane.stage_restore(first.bundle_root, target, _restore_request())
    assert second_plane.stage_restore(second.bundle_root, target, _restore_request()).reused


@pytest.mark.parametrize("case", ["top", "nested", "created_at"])
def test_receipt_v1_is_closed_and_time_bound(tmp_path: Path, case: str) -> None:
    source, database, assets, _ = _source(tmp_path)
    plane = _plane(source, database, assets)
    backup = plane.create_backup(tmp_path / "backup", _request())
    receipt_path = backup.bundle_root / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    if case == "top":
        receipt["unexpected_extension"] = True
    elif case == "nested":
        receipt["compatibility_evidence"]["unexpected_extension"] = True
    else:
        receipt["created_at"] = "2026-08-28T01:00:01Z"
    receipt["receipt_hash"] = hash_jcs(
        "plotpilot-backup-receipt/v1",
        {key: value for key, value in receipt.items() if key != "receipt_hash"},
    )
    _write_canonical(receipt_path, receipt)

    with pytest.raises(BackupValidationError, match="schema is not closed|not bound"):
        plane.verify_backup(backup.bundle_root)


def test_interrupted_backup_has_exact_cleanup_retry(tmp_path: Path) -> None:
    source, database, assets, _ = _source(tmp_path)
    interrupted: list[Path] = []

    def stop(snapshot: Path) -> None:
        interrupted.append(snapshot.parents[1])
        raise KeyboardInterrupt("backup interrupted")

    plane = _plane(source, database, assets, on_core_snapshot_copied=stop)
    destination = tmp_path / "backup"
    with pytest.raises(KeyboardInterrupt):
        plane.create_backup(destination, _request())
    stage = interrupted[0]
    assert stage.name.startswith(".b-") and stage.is_dir()
    plane.on_core_snapshot_copied = None
    published = plane.create_backup(destination, _request())
    with pytest.raises(BackupConflictError):
        plane.cleanup_backup_staging(
            stage,
            destination=tmp_path / "other-parent" / "backup",
            backup_id="backup-1",
            backup_epoch=7,
        )
    assert stage.exists()
    plane.cleanup_backup_staging(
        stage,
        destination=destination,
        backup_id="backup-1",
        backup_epoch=7,
    )
    assert not stage.exists()
    assert plane.create_backup(destination, _request()).manifest == published.manifest


def test_backup_publish_window_interruption_recovers_existing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, database, assets, _ = _source(tmp_path)
    plane = _plane(source, database, assets)
    destination = tmp_path / "backup-window"
    original = plane._require_publication_marker
    stopped = False

    def stop_after_rename(root: Path, **kwargs: Any):
        nonlocal stopped
        result = original(root, **kwargs)
        if root == destination and not stopped:
            stopped = True
            raise KeyboardInterrupt("published before acknowledgement")
        return result

    monkeypatch.setattr(plane, "_require_publication_marker", stop_after_rename)
    request = _request(backup_id="backup-window")
    with pytest.raises(KeyboardInterrupt):
        plane.create_backup(destination, request)
    recovered = plane.create_backup(destination, request)
    assert recovered.bundle_root == destination
