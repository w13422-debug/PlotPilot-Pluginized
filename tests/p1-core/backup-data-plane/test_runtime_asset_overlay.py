from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.backup import (
    BackupRequest,
    BackupValidationError,
    compose_backup_runtime,
)
from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.plugins.generation import GenerationState
from backend.plotpilot_core.repositories import CoreAuthorityRepository

CREATED_AT = "2026-09-04T00:00:00Z"
VERIFIED_AT = "2026-09-04T00:00:01Z"


@dataclass
class GenerationSource:
    state: GenerationState
    core_authority_binding: object
    failure: BaseException | None = None

    def generation_state(self) -> GenerationState:
        if self.failure is not None:
            raise self.failure
        return self.state


@dataclass
class RuntimeFixture:
    root: Path
    repository: CoreAuthorityRepository
    assets: AssetStore
    source: GenerationSource
    runtime: Any


def _generation(asset_id: str) -> GenerationState:
    return GenerationState(
        current={
            "schema": "plugin-generation/v1",
            "generation_id": "generation-current",
            "core_api_version": "1.2.0",
            "health_result_asset_id": asset_id,
            "created_reason": "runtime overlay proof",
            "created_at": CREATED_AT,
            "parent_generation_id": None,
            "base_generation_id": None,
            "members": [],
        }
    )


@contextmanager
def _fixture(
    tmp_path: Path,
    *,
    state_builder: Callable[[AssetStore], GenerationState] | None = None,
    failure: BaseException | None = None,
) -> Iterator[RuntimeFixture]:
    root = tmp_path / "active"
    (root / "core").mkdir(parents=True)
    repository = CoreAuthorityRepository(root / "core" / "core.db")
    assets = AssetStore(root / "assets")
    try:
        repository.create_workspace(Workspace("workspace-1", "Novel"))
        state = GenerationState() if state_builder is None else state_builder(assets)
        source = GenerationSource(state, repository, failure)
        runtime = compose_backup_runtime(
            repository,
            assets,
            source,
            source_root=root,
            library_root_id="root-active",
        )
        yield RuntimeFixture(root, repository, assets, source, runtime)
    finally:
        repository.close()


def _request() -> BackupRequest:
    return BackupRequest(
        backup_id="backup-overlay",
        library_root_id="root-active",
        backup_epoch=17,
        mode="full",
        workspace_ids=("workspace-1",),
        created_at=CREATED_AT,
        verified_at=VERIFIED_AT,
    )


def _tree_bytes(root: Path) -> tuple[tuple[str, str, bytes | None], ...]:
    values: list[tuple[str, str, bytes | None]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            values.append(("file", relative, path.read_bytes()))
        elif path.is_dir():
            values.append(("directory", relative, None))
    return tuple(sorted(values))


def _active_proof(value: RuntimeFixture) -> tuple[Any, ...]:
    with value.repository.read_connection() as connection:
        rows = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT workspace_id,title,revision FROM workspace ORDER BY workspace_id"
            )
        )
    return (
        rows,
        Path(value.repository.database).read_bytes(),
        _tree_bytes(value.assets.root),
        _tree_bytes(value.root),
    )


def _asset_paths(root: Path, asset_id: str) -> tuple[Path, Path]:
    digest = asset_id.removeprefix("asset-sha256-")
    return (
        root / "objects" / digest[:2] / digest,
        root / "metadata" / f"{digest}.json",
    )


def test_snapshot_assets_are_stage_owned_and_active_store_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def state_builder(assets: AssetStore) -> GenerationState:
        health = assets.put(
            b'{"healthy":true}',
            mime="application/json",
            logical_role="plugin_health",
            provenance="test:active-generation",
        )
        return _generation(health.asset_id)

    with _fixture(tmp_path, state_builder=state_builder) as value:
        before = _active_proof(value)
        original_put = AssetStore.put
        write_roots: list[Path] = []

        def tracked_put(store: AssetStore, content: Any, **kwargs: Any) -> Any:
            write_roots.append(store.root)
            return original_put(store, content, **kwargs)

        monkeypatch.setattr(AssetStore, "put", tracked_put)
        bundle = value.runtime.api.create_backup(tmp_path / "bundle", _request())

        snapshot = json.loads((bundle.bundle_root / "core/snapshot.json").read_bytes())
        snapshot_ids = tuple(
            item["state_asset_id"] for item in snapshot["covered_aggregates"]
        )
        assert snapshot_ids
        assert all(root != value.assets.root for root in write_roots)
        assert all(
            root.name == "assets" and root.parent.name.endswith(".stage")
            for root in write_roots
        )
        for asset_id in snapshot_ids:
            active_object, active_metadata = _asset_paths(value.assets.root, asset_id)
            bundle_object, bundle_metadata = _asset_paths(
                bundle.bundle_root / "assets", asset_id
            )
            assert not active_object.exists()
            assert not active_metadata.exists()
            assert bundle_object.is_file()
            assert bundle_metadata.is_file()
        assert _active_proof(value) == before


@pytest.mark.parametrize("failure_point", ["p2", "p3", "closure"])
def test_failure_matrix_preserves_active_rows_database_root_and_asset_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    missing_id = "asset-sha256-" + "f" * 64
    state_builder = (
        (lambda _assets: _generation(missing_id))
        if failure_point == "closure"
        else None
    )
    failure = RuntimeError("P2 injected failure") if failure_point == "p2" else None
    with _fixture(tmp_path, state_builder=state_builder, failure=failure) as value:
        if failure_point == "p3":

            def fail_p3(*, barrier: Any) -> Any:
                del barrier
                raise RuntimeError("P3 injected failure")

            monkeypatch.setattr(
                value.runtime.job_backup, "capture_durable_generation", fail_p3
            )
        before = _active_proof(value)
        destination = tmp_path / f"failed-{failure_point}"

        with pytest.raises((RuntimeError, BackupValidationError)):
            value.runtime.api.create_backup(destination, _request())

        assert not destination.exists()
        assert not list(tmp_path.glob(".b-*.stage"))
        assert _active_proof(value) == before


def test_cleanup_failure_never_replaces_primary_p2_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = RuntimeError("primary P2 failure")
    with _fixture(tmp_path, failure=primary) as value:
        before = _active_proof(value)

        def fail_cleanup(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("injected stage cleanup failure")

        monkeypatch.setattr(value.runtime.backup, "_remove_owned_stage", fail_cleanup)
        with pytest.raises(RuntimeError, match="primary P2 failure") as raised:
            value.runtime.api.create_backup(tmp_path / "failed-cleanup", _request())

        assert raised.value is primary
        assert any("cleanup failed" in note for note in raised.value.__notes__)
        assert _active_proof(value) == before


def test_complete_stage_local_asset_pair_takes_precedence_over_active_metadata(
    tmp_path: Path,
) -> None:
    with _fixture(tmp_path) as value:
        content = b"same content, different metadata authority"
        active = value.assets.put(
            content,
            mime="application/octet-stream",
            logical_role="active-role",
            provenance="test:active",
        )
        stage = tmp_path / (".b-" + "a" * 32 + ".stage")
        stage.mkdir()
        stage_store = AssetStore(stage / "assets")
        staged = stage_store.put(
            content,
            mime="application/vnd.plotpilot.stage",
            logical_role="stage-role",
            provenance="test:stage",
        )
        assert staged.asset_id == active.asset_id
        _, active_metadata = _asset_paths(value.assets.root, active.asset_id)
        _, stage_metadata = _asset_paths(stage_store.root, staged.asset_id)
        active_before = _tree_bytes(value.assets.root)
        staged_metadata_bytes = stage_metadata.read_bytes()

        files, asset_ids, _closure = value.runtime.backup._copy_asset_closure(
            root_asset_ids=(staged.asset_id,), stage=stage
        )

        assert asset_ids == (staged.asset_id,)
        assert stage_metadata.read_bytes() == staged_metadata_bytes
        assert stage_metadata.read_bytes() != active_metadata.read_bytes()
        assert _tree_bytes(value.assets.root) == active_before
        metadata_file = next(item for item in files if item["path"].endswith(".json"))
        assert (
            metadata_file["sha256"] == hashlib.sha256(staged_metadata_bytes).hexdigest()
        )


@pytest.mark.parametrize("present_half", ["object", "metadata"])
def test_stage_local_half_pair_is_fatal_and_never_mixes_active_bytes(
    tmp_path: Path, present_half: str
) -> None:
    with _fixture(tmp_path) as value:
        active = value.assets.put(
            b"authoritative active bytes",
            mime="application/octet-stream",
            logical_role="active-role",
            provenance="test:active",
        )
        active_object, active_metadata = _asset_paths(
            value.assets.root, active.asset_id
        )
        stage = tmp_path / (".b-" + "b" * 32 + ".stage")
        stage.mkdir()
        stage_object, stage_metadata = _asset_paths(stage / "assets", active.asset_id)
        selected_source, selected_target = (
            (active_object, stage_object)
            if present_half == "object"
            else (active_metadata, stage_metadata)
        )
        selected_target.parent.mkdir(parents=True)
        selected_target.write_bytes(selected_source.read_bytes())
        active_before = _tree_bytes(value.assets.root)

        with pytest.raises(BackupValidationError, match="pair is incomplete"):
            value.runtime.backup._copy_asset_closure(
                root_asset_ids=(active.asset_id,), stage=stage
            )

        assert stage_object.exists() is (present_half == "object")
        assert stage_metadata.exists() is (present_half == "metadata")
        assert _tree_bytes(value.assets.root) == active_before


def test_deterministic_cas_interleaving_preserves_concurrent_authoritative_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _fixture(tmp_path, failure=RuntimeError("stop after snapshot")) as value:
        original_put = AssetStore.put
        concurrent: list[tuple[str, bytes]] = []

        def interleaved_put(store: AssetStore, content: Any, **kwargs: Any) -> Any:
            data = bytes(content)
            if store.root != value.assets.root and not concurrent:
                published = original_put(
                    value.assets,
                    data,
                    mime=kwargs["mime"],
                    logical_role="concurrent-authoritative",
                    provenance="test:deterministic-interleaving",
                    rebuildable=kwargs.get("rebuildable", False),
                )
                concurrent.append((published.asset_id, data))
            return original_put(store, data, **kwargs)

        monkeypatch.setattr(AssetStore, "put", interleaved_put)
        with pytest.raises(RuntimeError, match="stop after snapshot"):
            value.runtime.api.create_backup(tmp_path / "failed", _request())

        assert len(concurrent) == 1
        asset_id, expected = concurrent[0]
        assert value.assets.read(asset_id) == expected
        assert (
            value.assets.describe(asset_id).logical_role == "concurrent-authoritative"
        )
        assert all(path.is_file() for path in _asset_paths(value.assets.root, asset_id))
        assert not list(tmp_path.glob(".b-*.stage"))
