from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from .models import (
    BackupBarrier,
    BackupMode,
    CoreSnapshotCapture,
    GenerationSnapshot,
    PluginDataSnapshot,
)


class BackupBarrierPort(Protocol):
    """Injected P3/P0 seam that freezes cross-authority pointers for one epoch."""

    def hold_for_backup(
        self,
        *,
        backup_epoch: int,
        created_at: str,
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
    ) -> AbstractContextManager[BackupBarrier]: ...


class CoreSnapshotPort(Protocol):
    """Build the accepted logical core-snapshot/v1 from the frozen Core database."""

    def capture_for_backup(
        self,
        *,
        barrier: BackupBarrier,
        core_database: Path,
        database_sha256: str,
        database_asset_ids: tuple[str, ...],
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
    ) -> CoreSnapshotCapture: ...


class GenerationBackupPort(Protocol):
    """Injected P2 seam. Implementations must return an immutable bound view."""

    def capture_for_backup(
        self,
        *,
        barrier: BackupBarrier,
        core_database: Path,
        core_snapshot_hash: str,
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
    ) -> GenerationSnapshot: ...


class PluginDataBackupPort(Protocol):
    """Injected P3 seam. Implementations freeze files before returning."""

    def capture_for_backup(
        self,
        *,
        barrier: BackupBarrier,
        core_database: Path,
        core_snapshot_hash: str,
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
    ) -> PluginDataSnapshot: ...
