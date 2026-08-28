"""Verified SQLite/Asset backup and restore-to-new-root staging."""

from .adapters import (
    CoreSnapshotAdapterError,
    PackageStoreArchiveAdapter,
    SqliteCoreSnapshotAdapter,
    deterministic_package_archive,
)
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
    "CoreSnapshotAdapterError",
    "CoreSnapshotCapture",
    "CoreSnapshotPort",
    "GenerationBackupPort",
    "GenerationSnapshot",
    "PackageStoreArchiveAdapter",
    "PluginBackupFile",
    "PluginDataBackupPort",
    "PluginDataSnapshot",
    "RestoreRequest",
    "RestoreResult",
    "SqliteAssetReferenceScanner",
    "SqliteCoreSnapshotAdapter",
    "deterministic_package_archive",
]
