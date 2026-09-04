"""Verified SQLite/Asset backup and restore-to-new-root staging."""

from .adapters import (
    CoreSnapshotAdapterError,
    PackageStoreArchiveAdapter,
    SqliteCoreSnapshotAdapter,
    SqliteWorkspaceDatabaseProjector,
    WorkspaceProjectionError,
    deterministic_package_archive,
)
from .composition import (
    BackupRuntimeComposition,
    build_backup_runtime,
    compose_backup_runtime,
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
from .runtime_ports import (
    BackupRuntimePortError,
    JobRuntimePluginDataPort,
    RuntimeBackupBarrierPort,
    RuntimeCoreSnapshotPort,
    RuntimeGenerationBackupPort,
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
    "BackupRuntimeComposition",
    "BackupRuntimePortError",
    "BackupValidationError",
    "CoreSnapshotAdapterError",
    "CoreSnapshotCapture",
    "CoreSnapshotPort",
    "GenerationBackupPort",
    "GenerationSnapshot",
    "JobRuntimePluginDataPort",
    "PackageStoreArchiveAdapter",
    "PluginBackupFile",
    "PluginDataBackupPort",
    "PluginDataSnapshot",
    "RestoreRequest",
    "RestoreResult",
    "RuntimeBackupBarrierPort",
    "RuntimeCoreSnapshotPort",
    "RuntimeGenerationBackupPort",
    "SqliteAssetReferenceScanner",
    "SqliteCoreSnapshotAdapter",
    "SqliteWorkspaceDatabaseProjector",
    "WorkspaceProjectionError",
    "build_backup_runtime",
    "compose_backup_runtime",
    "deterministic_package_archive",
]
