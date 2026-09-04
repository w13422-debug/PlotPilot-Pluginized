from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from plotpilot_core.backup.models import BackupBarrier
from plotpilot_core.plugins.backup import (
    GenerationBackupContributor,
    GenerationBackupError,
)
from plotpilot_core.plugins.generation import GenerationState
from plotpilot_core.plugins.lifecycle import (
    LIFECYCLE_MIGRATIONS,
    LifecycleError,
    LifecycleRepository,
)
from plotpilot_core.repositories import CoreAuthorityRepository, MigrationRunner

from backend.plotpilot_core.backup.models import BackupBarrier as AlternateBackupBarrier
from backend.plotpilot_core.plugins.generation import (
    GenerationState as AlternateGenerationState,
)

CORE_HASH = "e" * 64


@dataclass
class CountingGenerationSource:
    core_authority_binding: object | None
    state: object
    reads: int = 0

    def generation_state(self) -> object:
        self.reads += 1
        return self.state


@dataclass
class GenerationOnlyFacade:
    state: GenerationState
    reads: int = 0

    def generation_state(self) -> GenerationState:
        self.reads += 1
        return self.state


def _barrier() -> BackupBarrier:
    return BackupBarrier(
        token="backup-barrier-1",
        backup_epoch=1,
        core_event_high_water=0,
        created_at="2026-09-04T00:00:00Z",
    )


def _compose_lifecycle(repository: CoreAuthorityRepository) -> LifecycleRepository:
    with repository.read_connection() as connection:
        MigrationRunner(connection).apply(LIFECYCLE_MIGRATIONS)
        return LifecycleRepository(
            connection,
            transaction_factory=repository.transaction,
            core_authority_binding=repository,
        )


def _dependent_identity_fence(
    expected: CoreAuthorityRepository,
    contributor: GenerationBackupContributor,
) -> None:
    if contributor.core_authority_binding is not expected:
        raise GenerationBackupError(
            "generation contributor belongs to another Core authority"
        )


def test_exact_lifecycle_binding_is_forwarded_with_durable_generation_state(
    tmp_path: Path,
) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    try:
        lifecycle = _compose_lifecycle(repository)
        contributor = GenerationBackupContributor(lifecycle)

        assert lifecycle.core_authority_binding is repository
        assert contributor.core_authority_binding is repository
        assert isinstance(lifecycle.generation_state(), GenerationState)

        snapshot = contributor.capture_for_backup(
            barrier=_barrier(),
            core_database=Path(repository.database),
            core_snapshot_hash=CORE_HASH,
            mode="full",
            workspace_ids=(),
        )
        assert snapshot.barrier_token == "backup-barrier-1"
        assert snapshot.current_generation_id is None
        assert snapshot.lkg_generation_id is None
    finally:
        repository.close()


@pytest.mark.parametrize("explicit_null", [False, True])
def test_shared_lifecycle_construction_rejects_missing_or_null_binding_before_write(
    explicit_null: bool,
) -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)

    @contextmanager
    def transaction():
        yield connection

    try:
        before = connection.total_changes
        kwargs: dict[str, Any] = {"transaction_factory": transaction}
        if explicit_null:
            kwargs["core_authority_binding"] = None
        with pytest.raises(LifecycleError, match="authority binding"):
            LifecycleRepository(connection, **kwargs)
        assert connection.total_changes == before
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_missing_null_and_generation_only_sources_fail_before_state_read() -> None:
    missing = GenerationOnlyFacade(GenerationState())
    null = CountingGenerationSource(None, GenerationState())

    with pytest.raises(GenerationBackupError, match="authority binding"):
        GenerationBackupContributor(missing)
    with pytest.raises(GenerationBackupError, match="authority binding"):
        GenerationBackupContributor(null)

    assert missing.reads == 0
    assert null.reads == 0


def test_contributor_binding_is_live_and_read_only_before_generation_read() -> None:
    initial = object()
    replacement = object()
    source = CountingGenerationSource(initial, GenerationState())
    contributor = GenerationBackupContributor(source)

    source.core_authority_binding = replacement

    assert contributor.core_authority_binding is replacement
    with pytest.raises(AttributeError):
        contributor.core_authority_binding = initial
    assert contributor.core_authority_binding is replacement
    assert source.reads == 0


@pytest.mark.parametrize("drift", ["missing", "null"])
def test_live_contributor_rejects_missing_or_null_drift_before_generation_read(
    drift: str,
) -> None:
    source = CountingGenerationSource(object(), GenerationState())
    contributor = GenerationBackupContributor(source)

    if drift == "missing":
        del source.core_authority_binding
    else:
        source.core_authority_binding = None

    with pytest.raises(GenerationBackupError, match="authority binding"):
        _ = contributor.core_authority_binding
    assert source.reads == 0


@pytest.mark.parametrize("same_database", [False, True])
def test_post_construction_repository_drift_is_rejected_before_generation_read(
    tmp_path: Path,
    same_database: bool,
) -> None:
    database = tmp_path / "core.db"
    repository = CoreAuthorityRepository(database)
    foreign_database = database if same_database else tmp_path / "foreign.db"
    foreign = CoreAuthorityRepository(foreign_database)
    try:
        source = CountingGenerationSource(repository, GenerationState())
        contributor = GenerationBackupContributor(source)

        assert contributor.core_authority_binding is repository
        assert repository is not foreign
        if same_database:
            assert Path(repository.database) == Path(foreign.database)
        source.core_authority_binding = foreign

        assert contributor.core_authority_binding is foreign
        with pytest.raises(GenerationBackupError, match="another Core authority"):
            _dependent_identity_fence(repository, contributor)
        assert source.reads == 0
    finally:
        foreign.close()
        repository.close()


def test_public_types_normalize_across_import_namespaces() -> None:
    authority = object()
    source = CountingGenerationSource(authority, AlternateGenerationState())
    contributor = GenerationBackupContributor(source)
    barrier = AlternateBackupBarrier(
        token="backup-barrier-1",
        backup_epoch=1,
        core_event_high_water=0,
        created_at="2026-09-04T00:00:00Z",
    )

    assert type(source.state) is not GenerationState
    assert type(barrier) is not BackupBarrier
    snapshot = contributor.capture_for_backup(
        barrier=barrier,
        core_database=object(),
        core_snapshot_hash=CORE_HASH,
        mode="full",
        workspace_ids=(),
    )

    assert contributor.core_authority_binding is authority
    assert source.reads == 1
    assert snapshot.barrier_token == "backup-barrier-1"
