"""Runtime adapters joining the accepted P1, P2, and P3 backup ports.

The data plane deliberately owns no runtime discovery.  This module supplies
only the thin bindings that are possible after the P2 Generation and P3 Job
runtime slices have been composed over the active P1 authority.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..assets import AssetStore
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


class BackupRuntimePortError(RuntimeError):
    """A composed runtime port is absent, stale, or crosses an authority."""


@dataclass(slots=True)
class _AssetMutation:
    files: dict[Path, bytes] = field(default_factory=dict)
    directories: set[Path] = field(default_factory=set)


class _AssetMutationJournal:
    """Remove only state Assets created by the current held backup barrier.

    ``SqliteCoreSnapshotAdapter`` materializes deterministic aggregate state in
    an ``AssetStore``.  Those Assets are needed until the data plane copies its
    closure, but a backup operation must not leave the active root changed.
    The journal records only paths that did not exist before the exact ``put``
    and removes them while the P3 writer barrier is still held.
    """

    def __init__(self, store: AssetStore) -> None:
        self.store = store
        self._active: ContextVar[_AssetMutation | None] = ContextVar(
            f"plotpilot_backup_asset_mutation_{id(self)}", default=None
        )

    def current(self) -> _AssetMutation:
        mutation = self._active.get()
        if mutation is None:
            raise BackupRuntimePortError(
                "Core snapshot Asset write occurred outside the held backup barrier"
            )
        return mutation

    @contextmanager
    def operation(self) -> Iterator[None]:
        if self._active.get() is not None:
            raise BackupRuntimePortError("nested backup Asset mutation journal")
        mutation = _AssetMutation()
        token = self._active.set(mutation)
        try:
            yield
        finally:
            try:
                self._rollback(mutation)
            finally:
                self._active.reset(token)

    @staticmethod
    def _missing_parents(path: Path, root: Path) -> set[Path]:
        result: set[Path] = set()
        current = path.parent
        while current != root and current.is_relative_to(root):
            if current.exists():
                break
            result.add(current)
            current = current.parent
        return result

    def record_after_put(
        self,
        *,
        paths: tuple[Path, ...],
        existed: Mapping[Path, bool],
        missing_directories: set[Path],
    ) -> None:
        mutation = self.current()
        for path in paths:
            if not existed[path] and path.is_file():
                mutation.files.setdefault(path, path.read_bytes())
        mutation.directories.update(
            path for path in missing_directories if path.is_dir()
        )

    @staticmethod
    def _rollback(mutation: _AssetMutation) -> None:
        failures: list[str] = []
        for path, expected in sorted(
            mutation.files.items(), key=lambda item: len(item[0].parts), reverse=True
        ):
            try:
                if not path.exists():
                    continue
                if not path.is_file() or path.read_bytes() != expected:
                    failures.append(f"created Asset changed before cleanup: {path}")
                    continue
                path.unlink()
            except OSError as exc:
                failures.append(f"could not remove created Asset {path}: {exc}")
        for path in sorted(
            mutation.directories, key=lambda item: len(item.parts), reverse=True
        ):
            try:
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError as exc:
                failures.append(
                    f"could not remove created Asset directory {path}: {exc}"
                )
        if failures:
            raise BackupRuntimePortError("; ".join(failures))


class _JournaledAssetStore:
    """The exact active AssetStore with operation-owned ``put`` tracking."""

    def __init__(self, store: AssetStore, journal: _AssetMutationJournal) -> None:
        self._store = store
        self._journal = journal
        self.root = store.root
        self.objects = store.objects
        self.metadata = store.metadata

    def put(
        self,
        content: bytes | Any,
        *,
        mime: str,
        logical_role: str,
        provenance: str,
        rebuildable: bool = False,
    ) -> Any:
        self._journal.current()
        data = (
            bytes(content)
            if isinstance(content, (bytes, bytearray))
            else content.read()
        )
        if not isinstance(data, bytes):
            raise TypeError("asset stream must return bytes")
        digest = hashlib.sha256(data).hexdigest()
        object_path = self.objects / digest[:2] / digest
        metadata_path = self.metadata / f"{digest}.json"
        paths = (object_path, metadata_path)
        existed = {path: path.exists() for path in paths}
        missing_directories = set().union(
            *(self._journal._missing_parents(path, self.root) for path in paths)
        )
        try:
            return self._store.put(
                data,
                mime=mime,
                logical_role=logical_role,
                provenance=provenance,
                rebuildable=rebuildable,
            )
        finally:
            self._journal.record_after_put(
                paths=paths,
                existed=existed,
                missing_directories=missing_directories,
            )


class RuntimeCoreSnapshotPort:
    """P1 snapshot adapter whose transient state Assets are source-neutral."""

    def __init__(self, assets: AssetStore, journal: _AssetMutationJournal) -> None:
        self.assets = assets
        self._delegate = SqliteCoreSnapshotAdapter(assets.root)
        self._delegate.asset_store = _JournaledAssetStore(assets, journal)  # type: ignore[assignment]

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
        return self._delegate.capture_for_backup(
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

    def __init__(
        self,
        jobs: JobRuntimeBackupContributor,
        journal: _AssetMutationJournal,
    ) -> None:
        self.jobs = jobs
        self._journal = journal
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
                # The journal exits before P3 releases the sole Core writer.
                with self._journal.operation():
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


class _GenerationStateBridge:
    """Normalize the repository's dual source/import spelling for P2."""

    def __init__(self, source: Any, state_type: type[Any]) -> None:
        self.source = source
        self._state_type = state_type

    def generation_state(self) -> Any:
        value = self.source.generation_state()
        if isinstance(value, self._state_type):
            return value
        try:
            return self._state_type(
                current=value.current,
                lkg=value.lkg,
                safe_mode=value.safe_mode,
                rollback_consumed=value.rollback_consumed,
            )
        except (AttributeError, TypeError) as exc:
            raise BackupRuntimePortError(
                "P2 generation source returned an untyped state"
            ) from exc


def _method_global(instance: Any, name: str, global_name: str) -> Any | None:
    method = getattr(type(instance), name, None)
    globals_map = getattr(method, "__globals__", None)
    if isinstance(globals_map, dict):
        return globals_map.get(global_name)
    return None


def _barrier_for(value: BackupBarrier, target_type: type[Any] | None) -> Any:
    if target_type is None or isinstance(value, target_type):
        return value
    return target_type(
        token=value.token,
        backup_epoch=value.backup_epoch,
        core_event_high_water=value.core_event_high_water,
        created_at=value.created_at,
    )


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
        capture = getattr(source_or_contributor, "capture_for_backup", None)
        if callable(capture):
            self.source = getattr(source_or_contributor, "_generations", None)
            self.contributor = source_or_contributor
        else:
            state_reader = getattr(source_or_contributor, "generation_state", None)
            if not callable(state_reader):
                raise TypeError("generation source must expose generation_state")
            probe = GenerationBackupContributor(source_or_contributor)
            state_type = _method_global(probe, "capture_for_backup", "GenerationState")
            if not isinstance(state_type, type):
                raise BackupRuntimePortError(
                    "accepted P2 contributor does not expose GenerationState"
                )
            self.source = source_or_contributor
            self.contributor = GenerationBackupContributor(
                _GenerationStateBridge(source_or_contributor, state_type)
            )
        barrier_type = _method_global(
            self.contributor, "capture_for_backup", "BackupBarrier"
        )
        self._barrier_type = barrier_type if isinstance(barrier_type, type) else None

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
            barrier=_barrier_for(barrier, self._barrier_type),
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
