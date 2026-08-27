"""Verified SQLite/Asset backup and restore-to-new-root staging."""

from .models import (
    BackupBarrier,
    BackupRequest,
    BackupResult,
    CoreSnapshotCapture,
    GenerationSnapshot,
    PluginBackupFile,
    PluginDataSnapshot,
    RestoreRequest,
    RestoreResult,
)
from .ports import (
    BackupBarrierPort,
    CoreSnapshotPort,
    GenerationBackupPort,
    PluginDataBackupPort,
)
from .service import (
    BackupConflictError,
    BackupDataError,
    BackupDataPlane,
    BackupValidationError,
    SqliteAssetReferenceScanner,
)

__all__ = [
    "BackupBarrier",
    "BackupBarrierPort",
    "BackupConflictError",
    "BackupDataError",
    "BackupDataPlane",
    "BackupRequest",
    "BackupResult",
    "BackupValidationError",
    "CoreSnapshotCapture",
    "CoreSnapshotPort",
    "GenerationBackupPort",
    "GenerationSnapshot",
    "PluginBackupFile",
    "PluginDataBackupPort",
    "PluginDataSnapshot",
    "RestoreRequest",
    "RestoreResult",
    "SqliteAssetReferenceScanner",
]
