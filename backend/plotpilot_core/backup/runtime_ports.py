"""Runtime adapters joining the accepted P1, P2, and P3 backup ports.

The data plane deliberately owns no runtime discovery.  This module supplies
only the thin bindings that are possible after the P2 Generation and P3 Job
runtime slices have been composed over the active P1 authority.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..jobs.backup import JobRuntimeBackupContributor
from ..plugins.backup import GenerationBackupContributor
from .adapters import SqliteCoreSnapshotAdapter
from .models import (
    BackupBarrier,
    BackupMode,
    CoreSnapshotCapture,
    GenerationSnapshot,
    PluginBackupFile,
    PluginDataSnapshot,
)
from .ports import PluginDataBackupPort

_HASH = re.compile(r"^[0-9a-f]{64}$")
_BACKUP_STAGE = re.compile(r"^\.b-[0-9a-f]{32}\.stage$")


class BackupRuntimePortError(RuntimeError):
    """A composed runtime port is absent, stale, or crosses an authority."""


class RuntimeCoreSnapshotPort:
    """Materialize transient Core state only in the operation-owned stage."""

    @staticmethod
    def _stage_asset_root(core_database: Path) -> Path:
        database = Path(core_database)
        stage = database.parent.parent
        if (
            not database.is_absolute()
            or database.name != "core.db"
            or database.parent.name != "core"
            or _BACKUP_STAGE.fullmatch(stage.name) is None
            or not stage.is_dir()
            or not database.is_file()
        ):
            raise BackupRuntimePortError(
                "Core snapshot database is not inside an operation-owned backup stage"
            )
        return stage / "assets"

    def capture_for_backup(
        self,
        *,
        barrier: BackupBarrier,
        core_database: Path,
        database_sha256: str,
        database_asset_ids: tuple[str, ...],
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
    ) -> CoreSnapshotCapture:
        delegate = SqliteCoreSnapshotAdapter(self._stage_asset_root(core_database))
        return delegate.capture_for_backup(
            barrier=barrier,
            core_database=core_database,
            database_sha256=database_sha256,
            database_asset_ids=database_asset_ids,
            mode=mode,
            workspace_ids=workspace_ids,
        )


@dataclass(frozen=True, slots=True)
class _BarrierBinding:
    public: BackupBarrier
    native: Any


class RuntimeBackupBarrierPort:
    """Expose P3's real Job barrier in the data plane's import namespace."""

    def __init__(self, jobs: JobRuntimeBackupContributor) -> None:
        self.jobs = jobs
        self._binding: ContextVar[_BarrierBinding | None] = ContextVar(
            f"plotpilot_backup_barrier_{id(self)}", default=None
        )

    @staticmethod
    def _public_barrier(value: Any) -> BackupBarrier:
        try:
            token = value.token
            backup_epoch = value.backup_epoch
            high_water = value.core_event_high_water
            created_at = value.created_at
        except AttributeError as exc:
            raise BackupRuntimePortError(
                "P3 Job runtime returned an invalid backup barrier"
            ) from exc
        if (
            not isinstance(token, str)
            or not token
            or type(backup_epoch) is not int
            or backup_epoch < 1
            or type(high_water) is not int
            or high_water < 0
            or not isinstance(created_at, str)
        ):
            raise BackupRuntimePortError(
                "P3 Job runtime returned an invalid backup barrier"
            )
        return BackupBarrier(token, backup_epoch, high_water, created_at)

    @contextmanager
    def hold_for_backup(
        self,
        *,
        backup_epoch: int,
        created_at: str,
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
    ) -> Iterator[BackupBarrier]:
        if self._binding.get() is not None:
            raise BackupRuntimePortError("nested runtime backup barrier")
        with self.jobs.hold_for_backup(
            backup_epoch=backup_epoch,
            created_at=created_at,
            mode=mode,
            workspace_ids=workspace_ids,
        ) as native:
            public = self._public_barrier(native)
            if public.backup_epoch != backup_epoch or public.created_at != created_at:
                raise BackupRuntimePortError(
                    "P3 Job barrier is not bound to the requested backup epoch"
                )
            token = self._binding.set(_BarrierBinding(public, native))
            try:
                yield public
            finally:
                self._binding.reset(token)

    def native_barrier(self, value: BackupBarrier) -> Any:
        binding = self._binding.get()
        if binding is None or value != binding.public:
            raise BackupRuntimePortError(
                "P3 contributor received a stale or foreign backup barrier"
            )
        return binding.native


def _backup_file(value: Any) -> PluginBackupFile:
    try:
        return PluginBackupFile(
            path=value.path,
            role=value.role,
            source_path=Path(value.source_path),
            sha256=value.sha256,
            size=value.size,
            release_id=value.release_id,
            package_hash=value.package_hash,
        )
    except (AttributeError, TypeError) as exc:
        raise BackupRuntimePortError(
            "contributor returned an invalid backup file"
        ) from exc


class RuntimeGenerationBackupPort:
    """Run the accepted P2 contributor and normalize its immutable result."""

    def __init__(self, source_or_contributor: Any) -> None:
        try:
            source_binding = source_or_contributor.core_authority_binding
        except AttributeError as exc:
            raise BackupRuntimePortError(
                "P2 source has no public Core authority binding"
            ) from exc
        if source_binding is None:
            raise BackupRuntimePortError("P2 source Core authority binding is null")
        capture = getattr(source_or_contributor, "capture_for_backup", None)
        if callable(capture):
            self.source = source_or_contributor
            self.contributor = source_or_contributor
        else:
            state_reader = getattr(source_or_contributor, "generation_state", None)
            if not callable(state_reader):
                raise TypeError("generation source must expose generation_state")
            self.source = source_or_contributor
            self.contributor = GenerationBackupContributor(source_or_contributor)
        try:
            contributor_binding = self.contributor.core_authority_binding
        except AttributeError as exc:
            raise BackupRuntimePortError(
                "P2 contributor has no public Core authority binding"
            ) from exc
        if contributor_binding is not source_binding:
            raise BackupRuntimePortError(
                "P2 contributor changed the Core authority binding"
            )
        self.core_authority_binding = contributor_binding

    def require_core_authority_binding(self) -> None:
        """Recheck the public object identity immediately before an attempt."""

        try:
            source_binding = self.source.core_authority_binding
            contributor_binding = self.contributor.core_authority_binding
        except AttributeError as exc:
            raise BackupRuntimePortError(
                "P2 authority binding disappeared after composition"
            ) from exc
        if (
            source_binding is not self.core_authority_binding
            or contributor_binding is not self.core_authority_binding
        ):
            raise BackupRuntimePortError(
                "P2 authority binding changed after composition"
            )

    def capture_for_backup(
        self,
        *,
        barrier: BackupBarrier,
        core_database: Path,
        core_snapshot_hash: str,
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
    ) -> GenerationSnapshot:
        value = self.contributor.capture_for_backup(
            barrier=barrier,
            core_database=core_database,
            core_snapshot_hash=core_snapshot_hash,
            mode=mode,
            workspace_ids=workspace_ids,
        )
        try:
            return GenerationSnapshot(
                barrier_token=value.barrier_token,
                bound_core_snapshot_hash=value.bound_core_snapshot_hash,
                current_generation_id=value.current_generation_id,
                lkg_generation_id=value.lkg_generation_id,
                compatible=value.compatible,
                asset_ids=tuple(value.asset_ids),
                files=tuple(_backup_file(item) for item in value.files),
                plugin_releases=tuple(dict(item) for item in value.plugin_releases),
                projection_rebuild_required=tuple(
                    dict(item) for item in value.projection_rebuild_required
                ),
            )
        except (AttributeError, TypeError) as exc:
            raise BackupRuntimePortError(
                "P2 contributor returned an invalid Generation snapshot"
            ) from exc


def _plugin_data_snapshot(value: Any) -> PluginDataSnapshot:
    try:
        return PluginDataSnapshot(
            barrier_token=value.barrier_token,
            bound_core_snapshot_hash=value.bound_core_snapshot_hash,
            compatible=value.compatible,
            asset_ids=tuple(value.asset_ids),
            files=tuple(_backup_file(item) for item in value.files),
        )
    except (AttributeError, TypeError) as exc:
        raise BackupRuntimePortError(
            "P3 contributor returned an invalid plugin-data snapshot"
        ) from exc


class JobRuntimePluginDataPort:
    """Bind P3's durable Job generation and optional data contributors.

    Job/checkpoint rows already live in the one P1 Core database, so the real
    Job contributor attests that generation but contributes no second ledger
    file.  Optional plugin-owned data snapshots may contribute files or Assets;
    every one must bind the exact same barrier and Core snapshot.
    """

    def __init__(
        self,
        jobs: JobRuntimeBackupContributor,
        barriers: RuntimeBackupBarrierPort,
        contributors: Iterable[PluginDataBackupPort] = (),
    ) -> None:
        self.jobs = jobs
        self.barriers = barriers
        self.contributors = tuple(contributors)
        if any(
            not callable(getattr(contributor, "capture_for_backup", None))
            for contributor in self.contributors
        ):
            raise TypeError("plugin-data contributors must expose capture_for_backup")
        if len({id(value) for value in self.contributors}) != len(self.contributors):
            raise ValueError("plugin-data contributors must be unique objects")

    def _attest_job_generation(
        self, native_barrier: Any, public: BackupBarrier
    ) -> None:
        generation = self.jobs.capture_durable_generation(barrier=native_barrier)
        try:
            token = generation.barrier_token
            epoch = generation.backup_epoch
            digest = generation.durable_generation_hash
        except AttributeError as exc:
            raise BackupRuntimePortError(
                "P3 Job runtime returned an invalid durable generation"
            ) from exc
        if (
            token != public.token
            or epoch != public.backup_epoch
            or not isinstance(digest, str)
            or _HASH.fullmatch(digest) is None
            or digest not in public.token
        ):
            raise BackupRuntimePortError(
                "P3 durable generation is not bound to the held backup barrier"
            )

    def capture_for_backup(
        self,
        *,
        barrier: BackupBarrier,
        core_database: Path,
        core_snapshot_hash: str,
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
    ) -> PluginDataSnapshot:
        if _HASH.fullmatch(core_snapshot_hash) is None:
            raise BackupRuntimePortError("Core snapshot hash is not lowercase SHA-256")
        native = self.barriers.native_barrier(barrier)
        asset_ids: set[str] = set()
        files: list[PluginBackupFile] = []
        for contributor in self.contributors:
            value = _plugin_data_snapshot(
                contributor.capture_for_backup(
                    barrier=barrier,
                    core_database=core_database,
                    core_snapshot_hash=core_snapshot_hash,
                    mode=mode,
                    workspace_ids=workspace_ids,
                )
            )
            if (
                value.barrier_token != barrier.token
                or value.bound_core_snapshot_hash != core_snapshot_hash
                or value.compatible is not True
            ):
                raise BackupRuntimePortError(
                    "P3 contributor snapshot crosses the held durable generation"
                )
            asset_ids.update(value.asset_ids)
            files.extend(value.files)
        paths = [item.path for item in files]
        if len(paths) != len(set(paths)):
            raise BackupRuntimePortError(
                "P3 contributors returned duplicate backup file paths"
            )
        self._attest_job_generation(native, barrier)
        return PluginDataSnapshot(
            barrier_token=barrier.token,
            bound_core_snapshot_hash=core_snapshot_hash,
            compatible=True,
            asset_ids=tuple(sorted(asset_ids, key=lambda item: item.encode("utf-8"))),
            files=tuple(sorted(files, key=lambda item: item.path.encode("utf-8"))),
        )


__all__ = [
    "BackupRuntimePortError",
    "JobRuntimePluginDataPort",
    "RuntimeBackupBarrierPort",
    "RuntimeCoreSnapshotPort",
    "RuntimeGenerationBackupPort",
]
