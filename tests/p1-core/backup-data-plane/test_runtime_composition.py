from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.backup import (
    BackupDataError,
    BackupRequest,
    BackupRuntimePortError,
    BackupValidationError,
    RestoreRequest,
    compose_backup_runtime,
)
from backend.plotpilot_core.backup.models import PluginDataSnapshot
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.jobs.backup import JobRuntimeBackupContributor
from backend.plotpilot_core.plugins.backup import (
    GenerationBackupContributor,
    GenerationBackupError,
)
from backend.plotpilot_core.plugins.generation import GenerationState
from backend.plotpilot_core.plugins.lifecycle import LifecycleRepository
from backend.plotpilot_core.repositories import CoreAuthorityRepository

CREATED_AT = "2026-09-04T00:00:00Z"
VERIFIED_AT = "2026-09-04T00:00:01Z"
COMPLETED_AT = "2026-09-04T00:00:02Z"


@dataclass
class StaticGenerationSource:
    state: GenerationState
    core_authority_binding: object | None
    failure: BaseException | None = None
    reads: int = 0

    def generation_state(self) -> GenerationState:
        self.reads += 1
        if self.failure is not None:
            raise self.failure
        return self.state


@dataclass
class RuntimeStack:
    source_root: Path
    repository: CoreAuthorityRepository
    assets: AssetStore
    generations: Any
    runtime: Any


def _generation(assets: AssetStore) -> GenerationState:
    health = assets.put(
        b'{"healthy":true}',
        mime="application/json",
        logical_role="plugin_health",
        provenance="test:runtime-composition",
    )
    data = assets.put(
        b'{"data":"frozen"}',
        mime="application/json",
        logical_role="plugin_data",
        provenance="test:runtime-composition",
    )
    return GenerationState(
        current={
            "schema": "plugin-generation/v1",
            "generation_id": "generation-current",
            "core_api_version": "1.2.0",
            "health_result_asset_id": health.asset_id,
            "created_reason": "runtime composition test",
            "created_at": CREATED_AT,
            "parent_generation_id": None,
            "base_generation_id": None,
            "members": [
                {
                    "plugin_id": "com.plotpilot.test",
                    "release_id": "a" * 64,
                    "package_hash": "b" * 64,
                    "data_generation_id": "data-generation-1",
                    "ui_bundle_hash": None,
                    "global_settings_revision_id": None,
                    "settings_schema_hash": None,
                    "data_bundle_asset_id": data.asset_id,
                }
            ],
        }
    )


@contextmanager
def _stack(
    tmp_path: Path,
    *,
    state_builder: Callable[[AssetStore], GenerationState] | None = None,
    failure: BaseException | None = None,
    contributors: tuple[Any, ...] = (),
    with_document: bool = True,
    preconstructed_generation: bool = False,
) -> Iterator[RuntimeStack]:
    source_root = tmp_path / "active-root"
    (source_root / "core").mkdir(parents=True)
    repository = CoreAuthorityRepository(source_root / "core" / "core.db")
    assets = AssetStore(source_root / "assets")
    try:
        repository.create_workspace(Workspace("workspace-1", "Novel"))
        if with_document:
            repository.create_document(
                Document("document-1", "workspace-1", "Chapter 1")
            )
            repository.publish_revision(
                document_id="document-1",
                content="original bytes",
                expected_revision_id=None,
                created_by="user",
                revision_id="revision-1",
            )
        state = GenerationState() if state_builder is None else state_builder(assets)
        generations = StaticGenerationSource(state, repository, failure)
        generation_input = (
            GenerationBackupContributor(generations)
            if preconstructed_generation
            else generations
        )
        runtime = compose_backup_runtime(
            repository,
            assets,
            generation_input,
            source_root=source_root,
            library_root_id="root-active",
            plugin_data_contributors=contributors,
        )
        yield RuntimeStack(source_root, repository, assets, generations, runtime)
    finally:
        repository.close()


def _backup_request(
    *,
    backup_id: str = "backup-1",
    root_id: str = "root-active",
    epoch: int = 7,
) -> BackupRequest:
    return BackupRequest(
        backup_id=backup_id,
        library_root_id=root_id,
        backup_epoch=epoch,
        mode="full",
        workspace_ids=("workspace-1",),
        created_at=CREATED_AT,
        verified_at=VERIFIED_AT,
    )


def _restore_request(
    *,
    restore_id: str = "restore-1",
    source_root_id: str = "root-active",
    target_root_id: str = "root-restored",
) -> RestoreRequest:
    return RestoreRequest(
        restore_id=restore_id,
        source_root_id=source_root_id,
        target_root_id=target_root_id,
        created_at=VERIFIED_AT,
        completed_at=COMPLETED_AT,
    )


def _root_tree(root: Path) -> tuple[tuple[str, str, str | None], ...]:
    result: list[tuple[str, str, str | None]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            result.append(
                ("file", relative, hashlib.sha256(path.read_bytes()).hexdigest())
            )
        elif path.is_dir():
            result.append(("directory", relative, None))
    return tuple(sorted(result))


def _workspace_rows(repository: CoreAuthorityRepository) -> tuple[tuple[Any, ...], ...]:
    with repository.read_connection() as connection:
        return tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT workspace_id,title,revision FROM workspace ORDER BY workspace_id"
            )
        )


def _active_proof(stack: RuntimeStack) -> tuple[Any, ...]:
    return (
        stack.source_root.resolve(),
        _workspace_rows(stack.repository),
        Path(stack.repository.database).read_bytes(),
        _root_tree(stack.assets.root),
        _root_tree(stack.source_root),
    )


def _read_restored_workspace(root: Path) -> tuple[str, str]:
    database = root / "core" / "core.db"
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT workspace_id,title FROM workspace WHERE workspace_id='workspace-1'"
        ).fetchone()
        assert row is not None
        return str(row[0]), str(row[1])
    finally:
        connection.close()


def test_real_runtime_composes_one_barrier_and_restores_without_switching(
    tmp_path: Path,
) -> None:
    with _stack(tmp_path) as stack:
        runtime = stack.runtime
        assert runtime.repository is stack.repository
        assert runtime.assets is stack.assets
        assert runtime.job_backup.repository is stack.repository
        assert runtime.barrier_port.jobs is runtime.job_backup
        assert runtime.plugin_data_port.jobs is runtime.job_backup
        assert runtime.generation_port.core_authority_binding is stack.repository
        assert (
            runtime.generation_port.contributor.core_authority_binding
            is stack.repository
        )
        assert runtime.backup.barrier_port is runtime.barrier_port
        assert runtime.backup.core_snapshot_port is runtime.core_snapshot_port
        assert runtime.backup.generation_port is runtime.generation_port
        assert runtime.backup.plugin_data_port is runtime.plugin_data_port

        status = runtime.api.status()
        assert runtime.api.status() == status
        assert status.current_root == stack.source_root.resolve()
        assert "switch_root" not in dir(runtime.api)
        before = _active_proof(stack)

        bundle = runtime.api.create_backup(tmp_path / "bundle", _backup_request())
        assert (
            runtime.api.create(tmp_path / "bundle", _backup_request()).manifest
            == bundle.manifest
        )
        assert runtime.api.verify(bundle.bundle_root).manifest == bundle.manifest

        restored = runtime.api.restore_to_new_root(
            bundle.bundle_root,
            tmp_path / "restored-root",
            _restore_request(),
        )
        replay = runtime.api.restore(
            bundle.bundle_root,
            tmp_path / "restored-root",
            _restore_request(),
        )
        assert restored.reused is False
        assert replay.reused is True
        assert restored.report["state"] == "restore_ready"
        assert _read_restored_workspace(restored.target_root) == (
            "workspace-1",
            "Novel",
        )
        assert runtime.api.status() == status
        assert _active_proof(stack) == before


def test_real_p2_generation_and_assets_are_bound_without_source_mutation(
    tmp_path: Path,
) -> None:
    with _stack(tmp_path, state_builder=_generation) as stack:
        before = _active_proof(stack)
        bundle = stack.runtime.api.create_backup(
            tmp_path / "generation-bundle", _backup_request()
        )

        assert bundle.manifest["current_generation_id"] == "generation-current"
        assert bundle.manifest["lkg_generation_id"] is None
        assert bundle.manifest["plugin_releases"] == [
            {
                "plugin_id": "com.plotpilot.test",
                "release_id": "a" * 64,
                "package_hash": "b" * 64,
                "package_present": False,
            }
        ]
        assert len(bundle.receipt["asset_roots"]["generation_declared"]) == 2
        restored = stack.runtime.api.restore_to_new_root(
            bundle.bundle_root,
            tmp_path / "generation-restored",
            _restore_request(),
        )
        assert restored.report["missing_release_ids"] == ["a" * 64]
        assert _active_proof(stack) == before


def test_durable_p2_repository_uses_same_p1_transaction_and_restores(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "durable-active-root"
    (source_root / "core").mkdir(parents=True)
    repository = CoreAuthorityRepository(source_root / "core" / "core.db")
    assets = AssetStore(source_root / "assets")
    try:
        repository.create_workspace(Workspace("workspace-1", "Novel"))
        with repository.read_connection() as connection:
            LifecycleRepository.initialize_standalone_schema_for_tests(connection)
            lifecycle = LifecycleRepository(
                connection,
                transaction_factory=repository.transaction,
                core_authority_binding=repository,
            )
        state = _generation(assets)
        assert state.current is not None
        lifecycle.put_generation(state.current)
        with repository.transaction() as connection:
            connection.execute(
                "UPDATE p2_plugin_generation_pointer "
                "SET current_generation_id='generation-current'"
            )
        runtime = compose_backup_runtime(
            repository,
            assets,
            lifecycle,
            source_root=source_root,
            library_root_id="root-active",
        )
        stack = RuntimeStack(source_root, repository, assets, lifecycle, runtime)
        before = _active_proof(stack)

        bundle = runtime.api.create_backup(
            tmp_path / "durable-generation-bundle", _backup_request()
        )
        restored = runtime.api.restore_to_new_root(
            bundle.bundle_root,
            tmp_path / "durable-generation-restored",
            _restore_request(),
        )
        reopened = CoreAuthorityRepository(restored.target_root / "core" / "core.db")
        try:
            with reopened.read_connection() as connection:
                restored_lifecycle = LifecycleRepository(
                    connection,
                    transaction_factory=reopened.transaction,
                    core_authority_binding=reopened,
                )
            assert (
                restored_lifecycle.generation_state().current["generation_id"]
                == "generation-current"
            )
        finally:
            reopened.close()
        assert bundle.manifest["current_generation_id"] == "generation-current"
        assert _active_proof(stack) == before
    finally:
        repository.close()


def test_empty_workspace_and_zero_optional_contributors_are_deterministic(
    tmp_path: Path,
) -> None:
    with _stack(tmp_path, with_document=False) as stack:
        before = _active_proof(stack)
        first_status = stack.runtime.api.status()
        second_status = stack.runtime.api.status()
        bundle = stack.runtime.api.create_backup(
            tmp_path / "empty-workspace-bundle", _backup_request()
        )

        assert first_status == second_status
        assert bundle.manifest["current_generation_id"] is None
        assert bundle.manifest["lkg_generation_id"] is None
        assert bundle.manifest["plugin_releases"] == []
        assert bundle.receipt["asset_roots"]["generation_declared"] == []
        assert bundle.receipt["asset_roots"]["plugin_declared"] == []
        assert _active_proof(stack) == before


def test_p2_failure_removes_partial_backup_and_restores_source_bytes(
    tmp_path: Path,
) -> None:
    with _stack(tmp_path, failure=TimeoutError("P2 barrier timeout")) as stack:
        before = _active_proof(stack)
        destination = tmp_path / "failed-p2-backup"

        with pytest.raises(TimeoutError, match="P2 barrier timeout"):
            stack.runtime.api.create_backup(destination, _backup_request())

        assert not destination.exists()
        assert not list(tmp_path.glob(".b-*.stage"))
        assert _active_proof(stack) == before


def test_p3_failure_removes_partial_backup_and_restores_source_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _stack(tmp_path) as stack:
        before = _active_proof(stack)

        def fail_p3(*, barrier: Any) -> None:
            del barrier
            raise RuntimeError("P3 contributor failed")

        monkeypatch.setattr(
            stack.runtime.job_backup, "capture_durable_generation", fail_p3
        )
        destination = tmp_path / "failed-p3-backup"
        with pytest.raises(RuntimeError, match="P3 contributor failed"):
            stack.runtime.api.create_backup(destination, _backup_request())

        assert not destination.exists()
        assert not list(tmp_path.glob(".b-*.stage"))
        assert _active_proof(stack) == before


def test_barrier_timeout_never_publishes_or_mutates_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _stack(tmp_path) as stack:
        before = _active_proof(stack)

        @contextmanager
        def timeout(**_kwargs: Any) -> Iterator[None]:
            raise TimeoutError("job barrier timeout")
            yield  # pragma: no cover

        monkeypatch.setattr(stack.runtime.job_backup, "hold_for_backup", timeout)
        destination = tmp_path / "failed-barrier-backup"
        with pytest.raises(TimeoutError, match="job barrier timeout"):
            stack.runtime.api.create_backup(destination, _backup_request())

        assert not destination.exists()
        assert not list(tmp_path.glob(".b-*.stage"))
        assert _active_proof(stack) == before


class ForeignGenerationContributor:
    def capture_for_backup(self, **kwargs: Any) -> PluginDataSnapshot:
        return PluginDataSnapshot(
            barrier_token="foreign-barrier",
            bound_core_snapshot_hash=kwargs["core_snapshot_hash"],
            compatible=True,
        )


def test_mixed_generation_optional_contributor_fails_closed(tmp_path: Path) -> None:
    with _stack(tmp_path, contributors=(ForeignGenerationContributor(),)) as stack:
        before = _active_proof(stack)
        destination = tmp_path / "mixed-generation-backup"

        with pytest.raises(
            BackupRuntimePortError, match="crosses the held durable generation"
        ):
            stack.runtime.api.create_backup(destination, _backup_request())

        assert not destination.exists()
        assert _active_proof(stack) == before


def test_stale_barrier_cannot_be_replayed_into_p3_contributor(tmp_path: Path) -> None:
    with _stack(tmp_path) as stack:
        with stack.runtime.barrier_port.hold_for_backup(
            backup_epoch=7,
            created_at=CREATED_AT,
            mode="full",
            workspace_ids=("workspace-1",),
        ) as barrier:
            pass

        with pytest.raises(BackupRuntimePortError, match="stale or foreign"):
            stack.runtime.plugin_data_port.capture_for_backup(
                barrier=barrier,
                core_database=stack.runtime.backup.core_database,
                core_snapshot_hash="c" * 64,
                mode="full",
                workspace_ids=("workspace-1",),
            )


def test_api_rejects_root_identity_redirection_before_writing(tmp_path: Path) -> None:
    with _stack(tmp_path) as stack:
        before = _active_proof(stack)
        with pytest.raises(BackupValidationError, match="active root identity"):
            stack.runtime.api.create_backup(
                tmp_path / "wrong-root-bundle",
                _backup_request(root_id="root-foreign"),
            )
        assert not (tmp_path / "wrong-root-bundle").exists()

        bundle = stack.runtime.api.create_backup(
            tmp_path / "identity-bundle", _backup_request()
        )
        with pytest.raises(BackupValidationError, match="active root identity"):
            stack.runtime.api.restore_to_new_root(
                bundle.bundle_root,
                tmp_path / "wrong-source-restore",
                _restore_request(source_root_id="root-foreign"),
            )
        with pytest.raises(BackupValidationError, match="must differ"):
            stack.runtime.api.restore_to_new_root(
                bundle.bundle_root,
                tmp_path / "same-id-restore",
                _restore_request(target_root_id="root-active"),
            )
        assert not (tmp_path / "wrong-source-restore").exists()
        assert not (tmp_path / "same-id-restore").exists()
        assert _active_proof(stack) == before


@pytest.mark.parametrize("target_kind", ["current", "nested", "existing"])
def test_restore_rejects_non_new_roots_without_changing_active_workspace(
    tmp_path: Path, target_kind: str
) -> None:
    with _stack(tmp_path) as stack:
        bundle = stack.runtime.api.create_backup(
            tmp_path / "new-root-bundle", _backup_request()
        )
        before = _active_proof(stack)
        if target_kind == "current":
            target = stack.source_root
        elif target_kind == "nested":
            target = stack.source_root / "nested"
        else:
            target = tmp_path / "foreign-existing"
            target.mkdir()
            (target / "sentinel.bin").write_bytes(b"foreign")
        target_before = _root_tree(target) if target.exists() else ()

        with pytest.raises(BackupDataError):
            stack.runtime.api.restore_to_new_root(
                bundle.bundle_root, target, _restore_request()
            )

        assert _active_proof(stack) == before
        if target_kind == "existing":
            assert _root_tree(target) == target_before
        elif target_kind == "nested":
            assert not target.exists()


def test_corrupt_bundle_restore_failure_leaves_current_root_unchanged(
    tmp_path: Path,
) -> None:
    with _stack(tmp_path) as stack:
        bundle = stack.runtime.api.create_backup(
            tmp_path / "corrupt-bundle", _backup_request()
        )
        before = _active_proof(stack)
        (bundle.bundle_root / "backup.json").write_bytes(b"{}\n")
        target = tmp_path / "corrupt-restore"

        with pytest.raises(BackupValidationError):
            stack.runtime.api.restore_to_new_root(
                bundle.bundle_root, target, _restore_request()
            )

        assert not target.exists()
        assert not list(tmp_path.glob(".r-*.stage"))
        assert _active_proof(stack) == before


def test_restore_rename_failure_cleans_only_new_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _stack(tmp_path) as stack:
        bundle = stack.runtime.api.create_backup(
            tmp_path / "rename-bundle", _backup_request()
        )
        before = _active_proof(stack)
        target = tmp_path / "rename-failed-restore"
        original_rename = Path.rename

        def fail_target_rename(path: Path, destination: str | Path) -> Path:
            if Path(destination) == target:
                raise OSError("injected rename failure")
            return original_rename(path, destination)

        monkeypatch.setattr(Path, "rename", fail_target_rename)
        with pytest.raises(BackupDataError, match="without replacement"):
            stack.runtime.api.restore_to_new_root(
                bundle.bundle_root, target, _restore_request()
            )

        assert not target.exists()
        assert not list(tmp_path.glob(".r-*.stage"))
        assert bundle.bundle_root.is_dir()
        assert _active_proof(stack) == before


def test_composition_rejects_job_port_from_another_core_authority(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "active-root"
    other_root = tmp_path / "other-root"
    (source_root / "core").mkdir(parents=True)
    (other_root / "core").mkdir(parents=True)
    repository = CoreAuthorityRepository(source_root / "core" / "core.db")
    other = CoreAuthorityRepository(other_root / "core" / "core.db")
    assets = AssetStore(source_root / "assets")
    try:
        with pytest.raises(ValueError, match="exact CoreAuthorityRepository"):
            compose_backup_runtime(
                repository,
                assets,
                StaticGenerationSource(GenerationState(), repository),
                source_root=source_root,
                library_root_id="root-active",
                job_runtime=JobRuntimeBackupContributor(other),
            )
    finally:
        repository.close()
        other.close()


class GenerationOnlyFacade:
    def generation_state(self) -> GenerationState:
        return GenerationState()


class EffectSentinelJobRuntime:
    def __init__(self) -> None:
        self.accesses = 0

    @property
    def backup(self) -> Any:
        self.accesses += 1
        raise AssertionError("authority rejection happened after runtime access")


@pytest.mark.parametrize(
    "case",
    ["missing", "generation_only", "null", "foreign", "same_database"],
)
def test_generation_authority_is_rejected_before_any_dependent_effect(
    tmp_path: Path, case: str
) -> None:
    source_root = tmp_path / "active-root"
    (source_root / "core").mkdir(parents=True)
    repository = CoreAuthorityRepository(source_root / "core" / "core.db")
    assets = AssetStore(source_root / "assets")
    other: CoreAuthorityRepository | None = None
    sentinel = EffectSentinelJobRuntime()
    try:
        if case == "missing":
            generation_source: Any = object()
        elif case == "generation_only":
            generation_source = GenerationOnlyFacade()
        elif case == "null":
            generation_source = StaticGenerationSource(GenerationState(), None)
        elif case == "foreign":
            other_root = tmp_path / "foreign-root"
            (other_root / "core").mkdir(parents=True)
            other = CoreAuthorityRepository(other_root / "core" / "core.db")
            generation_source = StaticGenerationSource(GenerationState(), other)
        else:
            other = CoreAuthorityRepository(source_root / "core" / "core.db")
            generation_source = StaticGenerationSource(GenerationState(), other)
        before = _root_tree(source_root)

        with pytest.raises(ValueError, match="Core authority binding|exact Core"):
            compose_backup_runtime(
                repository,
                assets,
                generation_source,
                source_root=source_root,
                library_root_id="root-active",
                job_runtime=sentinel,
            )

        assert sentinel.accesses == 0
        assert _root_tree(source_root) == before
        assert not list(tmp_path.glob(".b-*.stage"))
    finally:
        if other is not None:
            other.close()
        repository.close()


def test_accepted_generation_contributor_forwards_exact_authority_identity(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "active-root"
    (source_root / "core").mkdir(parents=True)
    repository = CoreAuthorityRepository(source_root / "core" / "core.db")
    assets = AssetStore(source_root / "assets")
    try:
        source = StaticGenerationSource(GenerationState(), repository)
        contributor = GenerationBackupContributor(source)

        runtime = compose_backup_runtime(
            repository,
            assets,
            contributor,
            source_root=source_root,
            library_root_id="root-active",
        )

        assert runtime.generation_source is contributor
        assert runtime.generation_port.source is contributor
        assert runtime.generation_port.contributor is contributor
        assert runtime.generation_port.core_authority_binding is repository
        assert contributor.core_authority_binding is repository
    finally:
        repository.close()


@pytest.mark.parametrize("source_shape", ["raw", "preconstructed"])
@pytest.mark.parametrize("drift", ["foreign", "missing", "null", "same_database"])
def test_live_generation_authority_drift_is_rejected_before_any_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_shape: str,
    drift: str,
) -> None:
    with _stack(
        tmp_path,
        preconstructed_generation=source_shape == "preconstructed",
    ) as stack:
        other: CoreAuthorityRepository | None = None
        try:
            if drift == "foreign":
                other = CoreAuthorityRepository(tmp_path / "foreign-core.db")
                assert Path(other.database) != Path(stack.repository.database)
                stack.generations.core_authority_binding = other
            elif drift == "same_database":
                other = CoreAuthorityRepository(stack.repository.database)
                assert other is not stack.repository
                assert Path(other.database) == Path(stack.repository.database)
                stack.generations.core_authority_binding = other
            elif drift == "missing":
                del stack.generations.core_authority_binding
            else:
                stack.generations.core_authority_binding = None

            before = _active_proof(stack)
            barrier_entries: list[bool] = []

            @contextmanager
            def unexpected_barrier(**_kwargs: Any) -> Iterator[None]:
                barrier_entries.append(True)
                yield

            monkeypatch.setattr(
                stack.runtime.job_backup, "hold_for_backup", unexpected_barrier
            )
            destination = tmp_path / f"{source_shape}-{drift}-authority"

            with pytest.raises(
                (BackupRuntimePortError, GenerationBackupError),
                match="authority binding",
            ):
                stack.runtime.api.create_backup(destination, _backup_request())

            assert stack.generations.reads == 0
            assert barrier_entries == []
            assert not destination.exists()
            assert not list(tmp_path.glob(".b-*.stage"))
            assert _active_proof(stack) == before
        finally:
            if other is not None:
                other.close()
