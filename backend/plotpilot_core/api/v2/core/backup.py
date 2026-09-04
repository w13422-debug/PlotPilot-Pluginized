"""Typed additive v2 seam for the accepted backup/restore data plane.

This module intentionally mounts no HTTP route: no frozen public route family
exists for backup yet.  P0/P4 may consume this typed seam without reopening
``api/v1`` or gaining a root-switch operation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ....backup.models import BackupRequest, BackupResult, RestoreRequest, RestoreResult
from ....backup.service import BackupDataPlane, BackupValidationError

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


@dataclass(frozen=True, slots=True)
class CoreBackupStatus:
    """Stable read-only identity of the active backup runtime."""

    library_root_id: str
    current_root: Path
    core_database: Path
    asset_root: Path


@dataclass(frozen=True, slots=True)
class CoreBackupAdapter:
    """Bind requests to one active root while exposing no root switch."""

    data_plane: BackupDataPlane
    library_root_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.data_plane, BackupDataPlane):
            raise TypeError("data_plane must be BackupDataPlane")
        if (
            not isinstance(self.library_root_id, str)
            or _ID.fullmatch(self.library_root_id) is None
        ):
            raise ValueError("library_root_id is not a closed identifier")

    @property
    def current_root(self) -> Path:
        return self.data_plane.source_root

    def status(self) -> CoreBackupStatus:
        """Return immutable identity only; do not inspect or mutate a target."""

        return CoreBackupStatus(
            library_root_id=self.library_root_id,
            current_root=self.data_plane.source_root,
            core_database=self.data_plane.core_database,
            asset_root=self.data_plane.asset_root,
        )

    def create_backup(
        self, destination: str | Path, request: BackupRequest
    ) -> BackupResult:
        if not isinstance(request, BackupRequest):
            raise TypeError("request must be BackupRequest")
        if request.library_root_id != self.library_root_id:
            raise BackupValidationError(
                "backup request library root differs from the active root identity"
            )
        return self.data_plane.create_backup(destination, request)

    def verify_backup(self, bundle_root: str | Path) -> BackupResult:
        return self.data_plane.verify_backup(bundle_root)

    def restore_to_new_root(
        self,
        bundle_root: str | Path,
        target_root: str | Path,
        request: RestoreRequest,
    ) -> RestoreResult:
        if not isinstance(request, RestoreRequest):
            raise TypeError("request must be RestoreRequest")
        if request.source_root_id != self.library_root_id:
            raise BackupValidationError(
                "restore source root differs from the active root identity"
            )
        if request.target_root_id == self.library_root_id:
            raise BackupValidationError(
                "restore target root identity must differ from the active root"
            )
        return self.data_plane.stage_restore(bundle_root, target_root, request)

    def create(self, destination: str | Path, request: BackupRequest) -> BackupResult:
        return self.create_backup(destination, request)

    def verify(self, bundle_root: str | Path) -> BackupResult:
        return self.verify_backup(bundle_root)

    def restore(
        self,
        bundle_root: str | Path,
        target_root: str | Path,
        request: RestoreRequest,
    ) -> RestoreResult:
        return self.restore_to_new_root(bundle_root, target_root, request)


CoreBackupApi = CoreBackupAdapter

__all__ = ["CoreBackupAdapter", "CoreBackupApi", "CoreBackupStatus"]
