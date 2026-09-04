"""Production composition for the accepted backup data plane."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..api.v2.core.backup import CoreBackupAdapter
from ..assets import AssetStore
from ..jobs.backup import JobRuntimeBackupContributor
from ..repositories import CoreAuthorityRepository
from .ports import PluginDataBackupPort
from .runtime_ports import (
    JobRuntimePluginDataPort,
    RuntimeBackupBarrierPort,
    RuntimeCoreSnapshotPort,
    RuntimeGenerationBackupPort,
    _AssetMutationJournal,
)
from .service import BackupDataPlane

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


@dataclass(frozen=True, slots=True)
class BackupRuntimeComposition:
    """Every backup adapter joined over one active P1 authority."""

    library_root_id: str
    source_root: Path
    repository: CoreAuthorityRepository
    assets: AssetStore
    generation_source: Any
    job_backup: JobRuntimeBackupContributor
    barrier_port: RuntimeBackupBarrierPort
    core_snapshot_port: RuntimeCoreSnapshotPort
    generation_port: RuntimeGenerationBackupPort
    plugin_data_port: JobRuntimePluginDataPort
    backup: BackupDataPlane
    api: CoreBackupAdapter

    @property
    def data_plane(self) -> BackupDataPlane:
        return self.backup


def _job_backup_port(
    repository: CoreAuthorityRepository, job_runtime: Any | None
) -> JobRuntimeBackupContributor:
    candidate = (
        JobRuntimeBackupContributor(repository)
        if job_runtime is None
        else getattr(job_runtime, "backup", job_runtime)
    )
    if (
        getattr(candidate, "repository", None) is not repository
        or not callable(getattr(candidate, "hold_for_backup", None))
        or not callable(getattr(candidate, "capture_durable_generation", None))
    ):
        raise ValueError(
            "job backup port must use the exact CoreAuthorityRepository object"
        )
    return candidate


def _require_generation_barrier_binding(
    repository: CoreAuthorityRepository, generation_source: Any
) -> None:
    """Reject a durable P2 repository that can write outside P3's barrier.

    Protocol-only immutable readers have no ``connection`` attribute.  The
    accepted durable lifecycle repository does, and in production it must use
    the exact P1 connection plus ``CoreAuthorityRepository.transaction``.
    """

    if not hasattr(generation_source, "connection"):
        return
    if generation_source.connection is not repository._connection:
        raise ValueError(
            "generation source must use the exact CoreAuthorityRepository connection"
        )
    transaction_factory = getattr(generation_source, "_transaction_factory", None)
    if getattr(transaction_factory, "__self__", None) is not repository:
        raise ValueError(
            "generation source writes must use the CoreAuthorityRepository transaction"
        )


def compose_backup_runtime(
    repository: CoreAuthorityRepository,
    assets: AssetStore,
    generation_source: Any,
    *,
    source_root: str | Path,
    library_root_id: str,
    job_runtime: Any | None = None,
    plugin_data_contributors: Iterable[PluginDataBackupPort] = (),
) -> BackupRuntimeComposition:
    """Compose P1/P2/P3 without a second writer, ledger, or root pointer."""

    if not isinstance(repository, CoreAuthorityRepository):
        raise TypeError("repository must be CoreAuthorityRepository")
    if not isinstance(assets, AssetStore):
        raise TypeError("assets must be AssetStore")
    if not isinstance(library_root_id, str) or _ID.fullmatch(library_root_id) is None:
        raise ValueError("library_root_id is not a closed identifier")
    if not callable(getattr(generation_source, "generation_state", None)):
        raise TypeError("generation_source must expose generation_state")
    _require_generation_barrier_binding(repository, generation_source)

    job_backup = _job_backup_port(repository, job_runtime)
    contributors = tuple(plugin_data_contributors)
    journal = _AssetMutationJournal(assets)
    barrier = RuntimeBackupBarrierPort(job_backup, journal)
    core_snapshot = RuntimeCoreSnapshotPort(assets, journal)
    generation = RuntimeGenerationBackupPort(generation_source)
    plugin_data = JobRuntimePluginDataPort(job_backup, barrier, contributors)
    backup = BackupDataPlane(
        source_root=source_root,
        core_database=repository.database,
        asset_root=assets.root,
        barrier_port=barrier,
        core_snapshot_port=core_snapshot,
        generation_port=generation,
        plugin_data_port=plugin_data,
    )
    api = CoreBackupAdapter(backup, library_root_id)
    return BackupRuntimeComposition(
        library_root_id=library_root_id,
        source_root=backup.source_root,
        repository=repository,
        assets=assets,
        generation_source=generation_source,
        job_backup=job_backup,
        barrier_port=barrier,
        core_snapshot_port=core_snapshot,
        generation_port=generation,
        plugin_data_port=plugin_data,
        backup=backup,
        api=api,
    )


build_backup_runtime = compose_backup_runtime

__all__ = [
    "BackupRuntimeComposition",
    "build_backup_runtime",
    "compose_backup_runtime",
]
