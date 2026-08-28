from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BackupMode = Literal["full", "data", "workspace"]
BackupFileRole = Literal["plugin_db", "package", "metadata"]


@dataclass(frozen=True, slots=True)
class BackupRequest:
    backup_id: str
    library_root_id: str
    backup_epoch: int
    mode: BackupMode
    workspace_ids: tuple[str, ...]
    created_at: str
    verified_at: str
    core_contract_version: str = "1.2.0"


@dataclass(frozen=True, slots=True)
class BackupBarrier:
    """P3/P0-held barrier spanning every authority snapshot participating in a bundle."""

    token: str
    backup_epoch: int
    core_event_high_water: int
    created_at: str


@dataclass(frozen=True, slots=True)
class CoreSnapshotCapture:
    """Accepted CoreSnapshot plus an out-of-band binding to the frozen SQLite image."""

    barrier_token: str
    bound_database_sha256: str
    bound_core_event_high_water: int
    bound_asset_ids: tuple[str, ...]
    bound_workspace_ids: tuple[str, ...]
    core_contract_version: str
    workspace_snapshot_hash: str | None
    required_asset_ids: tuple[str, ...]
    compatible: bool
    snapshot: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PluginBackupFile:
    """A contributor-produced frozen file; Core verifies bytes before admitting it."""

    path: str
    role: BackupFileRole
    source_path: Path
    sha256: str
    size: int
    release_id: str | None = None
    package_hash: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationSnapshot:
    """P2-owned immutable generation view bound to one Core snapshot."""

    barrier_token: str
    bound_core_snapshot_hash: str
    current_generation_id: str | None
    lkg_generation_id: str | None
    compatible: bool
    asset_ids: tuple[str, ...] = ()
    files: tuple[PluginBackupFile, ...] = ()
    plugin_releases: tuple[Mapping[str, object], ...] = ()
    projection_rebuild_required: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class PluginDataSnapshot:
    """P3-owned immutable data view bound to one Core snapshot."""

    barrier_token: str
    bound_core_snapshot_hash: str
    compatible: bool
    asset_ids: tuple[str, ...] = ()
    files: tuple[PluginBackupFile, ...] = ()


@dataclass(frozen=True, slots=True)
class BackupResult:
    bundle_root: Path
    manifest: Mapping[str, object]
    receipt: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RestoreRequest:
    restore_id: str
    source_root_id: str
    target_root_id: str
    created_at: str
    completed_at: str


@dataclass(frozen=True, slots=True)
class RestoreResult:
    target_root: Path
    report: Mapping[str, object]
    reused: bool
