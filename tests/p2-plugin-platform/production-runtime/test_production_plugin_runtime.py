from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
BACKEND = str(ROOT / "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from plotpilot_core.assets import AssetStore
from plotpilot_core.bootstrap.production_plugin_runtime import (
    FIRST_PARTY_CAPABILITIES,
    ProductionPluginRuntime,
    build_production_plugin_runtime,
)
from plotpilot_core.plugins.lifecycle.repository import (
    initial_transition,
)
from plotpilot_core.repositories.authority import (
    CoreAuthorityRepository,
)
from plotpilot_core.repositories.migrations import (
    CORE_MIGRATIONS,
    PRODUCTION_CORE_MIGRATIONS,
    PRODUCTION_SUPERVISOR_MIGRATIONS,
    Migration,
    MigrationRunner,
)
from plotpilot_core.supervisor import (
    AttemptFence,
    InstallFence,
    IsolatedVenvProcessFactory,
    RouteAvailabilityReason,
)
from plotpilot_core.supervisor.venv import InterpreterIdentity
from plotpilot_plugin_sdk.canonical import canonical_bytes
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode
from plotpilot_plugin_sdk.package import build_files_sha256
from plotpilot_plugin_sdk.verifier import assert_valid

PLUGIN_ID = "com.plotpilot.synthetic"
CAPABILITY_ID = "synthetic.run/v1"
NOW = "2026-09-09T00:00:00Z"


@dataclass(slots=True)
class RecordedInterpreterProbe:
    calls: list[Path] = field(default_factory=list)

    def probe(self, python: Path) -> InterpreterIdentity:
        self.calls.append(python)
        return InterpreterIdentity(
            implementation="cpython",
            version="3.12.10",
            executable=python.resolve(strict=True),
        )


def _open_runtime(
    root: Path, probe: RecordedInterpreterProbe
) -> ProductionPluginRuntime:
    (root / "core").mkdir(parents=True, exist_ok=True)
    repository = CoreAuthorityRepository(root / "core" / "core.db")
    assets = AssetStore(root / "assets")
    core_runtime = SimpleNamespace(repository=repository, assets=assets)
    return build_production_plugin_runtime(
        core_runtime,
        data_root=root,
        interpreter_probe=probe,
    )


def _short_root(tmp_path: Path) -> Path:
    """Keep content-addressed Windows fixture paths below legacy MAX_PATH."""

    suffix = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:10]
    return tmp_path.parent / f"wu2a-{suffix}"


def _code_package_files(
    *,
    plugin_id: str = PLUGIN_ID,
    version: str = "1.0.0",
    include_wheelhouse: bool = True,
) -> dict[str, bytes]:
    wheel = f"backend/{plugin_id.rsplit('.', 1)[-1]}-{version}-py3-none-any.whl"
    manifest = {
        "schema": "plotpilot-plugin/v1",
        "plugin_id": plugin_id,
        "version": version,
        "display_name": "Synthetic Production Worker",
        "compatibility": {
            "core_api": ">=1.0 <2.0",
            "plugin_rpc": "1",
            "ui_host": "1",
            "python": "3.12.*",
        },
        "capabilities": [
            {
                "capability_id": CAPABILITY_ID,
                "operations": ["run"],
                "result_contract": "artifact-bundle/v1",
            }
        ],
        "settings": None,
        "needs": [],
        "kind": "code",
        "backend": {
            "entrypoint": "synthetic_worker:main",
            "wheel": wheel,
            "requirements_lock": "backend/requirements.lock",
            "wheelhouse": "backend/wheelhouse",
            "max_concurrency": 1,
        },
        "storage": {
            "schema_version": 1,
            "migration_policy": "transactional-shadow",
            "migration_manifest": "migrations/manifest.json",
        },
        "ui": None,
        "data": None,
    }
    ordinary = {
        "plugin.json": (json.dumps(manifest, separators=(",", ":")) + "\n").encode(),
        wheel: b"synthetic-wheel",
        "backend/requirements.lock": b"",
        "migrations/manifest.json": b"{}\n",
    }
    if include_wheelhouse:
        ordinary["backend/wheelhouse/dependency-1.0.0-py3-none-any.whl"] = (
            b"dependency-wheel"
        )
    return {**ordinary, "files.sha256": build_files_sha256(ordinary)}


def _generation(
    package: Any,
    *,
    generation_id: str = "generation-1",
    package_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": "plugin-generation/v1",
        "generation_id": generation_id,
        "core_api_version": "1.0.0",
        "members": [
            {
                "plugin_id": package.plugin_id,
                "release_id": package.release_id,
                "package_hash": package_hash or package.package_hash,
                "data_generation_id": None,
                "ui_bundle_hash": None,
                "global_settings_revision_id": None,
                "settings_schema_hash": None,
                "data_bundle_asset_id": None,
            }
        ],
        "created_reason": "WU-2A directed regression",
        "created_at": NOW,
        "health_result_asset_id": f"health-{generation_id}",
        "parent_generation_id": None,
        "base_generation_id": None,
    }


def _set_current(runtime: ProductionPluginRuntime, generation: dict[str, Any]) -> None:
    runtime.lifecycle.put_generation(generation)
    with runtime.lifecycle.transaction() as connection:
        connection.execute(
            """
            UPDATE p2_plugin_generation_pointer
               SET current_generation_id=?,safe_mode=0,revision=revision+1
             WHERE singleton=1
            """,
            (generation["generation_id"],),
        )


def _prepare_venv(runtime: ProductionPluginRuntime, package: Any) -> Path:
    root = runtime.data_root / "plugins" / "venvs" / package.plugin_id / package.package_hash
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"fixture-python")
    (root / "plotpilot-venv.json").write_text(
        json.dumps(
            {
                "schema": "plotpilot-venv/v1",
                "plugin_id": package.plugin_id,
                "release_id": package.release_id,
                "package_hash": package.package_hash,
                "python_implementation": "cpython",
                "python_version": "3.12.10",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return root


def _install_current(
    runtime: ProductionPluginRuntime,
    *,
    version: str = "1.0.0",
    generation_id: str = "generation-1",
    include_wheelhouse: bool = True,
    prepare_venv: bool = True,
    package_hash: str | None = None,
) -> Any:
    package = runtime.packages.publish(
        _code_package_files(
            version=version, include_wheelhouse=include_wheelhouse
        )
    )
    runtime.retirement.register_installed(package.release_id, at=NOW)
    _set_current(
        runtime,
        _generation(
            package,
            generation_id=generation_id,
            package_hash=package_hash,
        ),
    )
    if prepare_venv:
        _prepare_venv(runtime, package)
    return package


def _make_mutable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IWRITE)


def test_p1_empty_root_is_a_real_disabled_graph_without_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts: list[object] = []

    def forbidden_start(*args: object, **kwargs: object) -> object:
        starts.append((args, kwargs))
        raise AssertionError("a real process must not start for an empty installation")

    monkeypatch.setattr(IsolatedVenvProcessFactory, "start", forbidden_start)
    runtime = _open_runtime(_short_root(tmp_path), RecordedInterpreterProbe())
    try:
        plugins = runtime.plugin_availability()
        capabilities = runtime.capability_availability()
        assert len(plugins) == len(FIRST_PARTY_CAPABILITIES) == 8
        assert len(capabilities) == sum(map(len, FIRST_PARTY_CAPABILITIES.values()))
        assert all(item.status == "Disabled" for item in plugins)
        assert all(item.status == "Disabled" for item in capabilities)
        assert {
            item.reason for item in plugins
        } == {RouteAvailabilityReason.CURRENT_GENERATION_MISSING}
        assert all(runtime.routes.availability(item.plugin_id).route is None for item in plugins)
        assert runtime.authority.claim("com.plotpilot.autopilot", "owner-empty") is None
        assert isinstance(runtime.processes, IsolatedVenvProcessFactory)
        assert runtime.supervisor.status("com.plotpilot.autopilot") is None
        assert starts == []
    finally:
        runtime.repository.close()


def test_p2_route_uses_only_reverified_store_and_frozen_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _open_runtime(_short_root(tmp_path), RecordedInterpreterProbe())
    try:
        package = _install_current(runtime)
        donor = tmp_path / "source-donor" / package.plugin_id
        donor.mkdir(parents=True)
        (donor / "plugin.json").write_text("poison source donor", encoding="utf-8")
        original_read_bytes = Path.read_bytes

        def guarded_read_bytes(path: Path) -> bytes:
            try:
                path.resolve().relative_to(donor.resolve())
            except ValueError:
                return original_read_bytes(path)
            raise AssertionError("source donor was consulted")

        monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
        availability = runtime.routes.availability(package.plugin_id)
        route = availability.route
        assert availability.reason is RouteAvailabilityReason.ENABLED
        assert route is not None
        assert route.generation_id == "generation-1"
        assert route.release_id == package.release_id
        assert route.package_hash == package.package_hash
        assert route.package_root == runtime.packages.package_path(*package.identity).resolve()
        assert route.package_root != donor.resolve()
        assert runtime.packages.require_release(
            release_id=route.release_id,
            plugin_id=route.plugin_id,
            package_hash=route.package_hash,
        ).read_bytes("plugin.json") == package.read_bytes("plugin.json")
        fence = runtime.authority.claim(package.plugin_id, "owner-p2")
        assert fence is not None
        resolved = runtime.lookup.resolve_worker(fence, "owner-p2")
        assert resolved.route == route
        assert resolved.package_root == route.package_root
        assert resolved.capabilities == (CAPABILITY_ID,)
        assert runtime.authority.release(fence, "owner-p2")
    finally:
        runtime.repository.close()


@pytest.mark.parametrize(
    "case",
    [
        "missing_wheel",
        "missing_lock",
        "missing_wheelhouse",
        "corrupt_package",
        "generation_package_mismatch",
        "absent_prepared_venv",
    ],
)
def test_p3_missing_or_corrupt_artifacts_fail_closed(
    tmp_path: Path, case: str
) -> None:
    runtime = _open_runtime(_short_root(tmp_path), RecordedInterpreterProbe())
    try:
        include_wheelhouse = case != "missing_wheelhouse"
        mismatch = "f" * 64 if case == "generation_package_mismatch" else None
        package = _install_current(
            runtime,
            include_wheelhouse=include_wheelhouse,
            prepare_venv=case != "absent_prepared_venv",
            package_hash=mismatch,
        )
        package_root = runtime.packages.package_path(*package.identity)
        manifest = package.manifest
        backend = manifest["backend"]
        if case in {"missing_wheel", "missing_lock"}:
            field_name = "wheel" if case == "missing_wheel" else "requirements_lock"
            target = package_root.joinpath(*backend[field_name].split("/"))
            _make_mutable(target)
            target.unlink()
        elif case == "corrupt_package":
            target = package_root / "plugin.json"
            _make_mutable(target)
            target.write_bytes(b"{}")
        availability = runtime.routes.availability(package.plugin_id)
        assert availability.status == "Disabled"
        assert availability.reason is not RouteAvailabilityReason.ENABLED
        assert availability.reason in frozenset(RouteAvailabilityReason)
        assert availability.route is None
        assert runtime.routes.get_route(
            package.plugin_id,
            "generation-1",
            package.release_id,
            1,
        ) is None
        assert runtime.authority.claim(package.plugin_id, f"owner-{case}") is None
        assert runtime.supervisor.status(package.plugin_id) is None
    finally:
        runtime.repository.close()


def test_p4_claim_replay_release_restart_and_epoch_are_durable(tmp_path: Path) -> None:
    probe = RecordedInterpreterProbe()
    root = _short_root(tmp_path)
    first = _open_runtime(root, probe)
    second: ProductionPluginRuntime | None = None
    third: ProductionPluginRuntime | None = None
    try:
        package = _install_current(first)
        prospective = first.authority.snapshot(package.plugin_id)
        assert prospective is not None
        assert prospective.release_id == package.release_id
        fence = first.authority.claim(package.plugin_id, "owner-one")
        assert fence == prospective

        second = _open_runtime(root, probe)
        assert second.authority.claim(package.plugin_id, "owner-one") == fence
        assert second.authority.claim(package.plugin_id, "owner-conflict") is None
        assert second.authority.snapshot(package.plugin_id) == fence
        assert second.authority.release(fence, "owner-one")
        first.repository.close()
        second.repository.close()

        third = _open_runtime(root, probe)
        assert third.authority.release(fence, "owner-one")
        replacement = third.authority.claim(package.plugin_id, "owner-two")
        assert replacement is not None
        assert replacement.pin_epoch == fence.pin_epoch + 1
        assert replacement.pin_id != fence.pin_id
        assert not third.authority.holds(fence, "owner-one")
        assert not third.authority.release(fence, "owner-two")
        assert third.authority.holds(replacement, "owner-two")
        assert third.authority.release(replacement, "owner-two")
    finally:
        for runtime in (first, second, third):
            if runtime is not None:
                try:
                    runtime.repository.close()
                except sqlite3.ProgrammingError:
                    pass


def test_p5_generation_replacement_and_retire_epoch_fence_old_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _open_runtime(_short_root(tmp_path), RecordedInterpreterProbe())
    try:
        starts: list[object] = []

        def forbidden_start(*args: object, **kwargs: object) -> object:
            starts.append((args, kwargs))
            raise AssertionError("stale authority attempted a substituted spawn")

        monkeypatch.setattr(IsolatedVenvProcessFactory, "start", forbidden_start)
        old_package = _install_current(runtime)
        old = runtime.authority.claim(old_package.plugin_id, "owner-old")
        assert old is not None
        assert runtime.retirement.active_pins(old.release_id)[0]["pin_id"] == old.pin_id

        new_package = runtime.packages.publish(
            _code_package_files(version="2.0.0")
        )
        runtime.retirement.register_installed(new_package.release_id, at=NOW)
        _prepare_venv(runtime, new_package)
        _set_current(
            runtime,
            _generation(new_package, generation_id="generation-2"),
        )
        assert runtime.authority.snapshot(old.worker_id) is None
        assert not runtime.authority.holds(old, "owner-old")
        assert runtime.routes.get_route(
            old.worker_id,
            old.generation_id,
            old.release_id,
            old.retire_epoch,
        ) is None
        assert runtime.authority.claim(old.worker_id, "owner-new") is None
        with pytest.raises(ContractError) as caught:
            runtime.supervisor.acquire(
                old.worker_id, expected_release_id=new_package.release_id
            )
        assert caught.value.code == ErrorCode.STALE_LEASE
        assert starts == []
        assert runtime.authority.release(old, "owner-old")

        current = runtime.authority.claim(old.worker_id, "owner-new")
        assert current is not None
        retirement = copy.deepcopy(runtime.retirement.get(current.release_id))
        retirement.update(
            state="retiring",
            retire_epoch=current.retire_epoch + 1,
            started_at="2026-09-09T00:00:01Z",
            completed_at=None,
        )
        assert_valid("release-retirement/v1", retirement)
        with runtime.lifecycle.transaction() as connection:
            connection.execute(
                """
                UPDATE p2_plugin_release_retirement
                   SET retirement_json=?,revision=revision+1
                 WHERE release_id=?
                """,
                (
                    canonical_bytes(retirement).decode("utf-8"),
                    current.release_id,
                ),
            )
        assert runtime.authority.snapshot(current.worker_id) is None
        assert not runtime.authority.holds(current, "owner-new")
        assert runtime.routes.get_route(
            current.worker_id,
            current.generation_id,
            current.release_id,
            current.retire_epoch,
        ) is None
        assert runtime.supervisor.status(current.worker_id) is None
        assert runtime.authority.release(current, "owner-new")
    finally:
        runtime.repository.close()


def _insert_execution_job(runtime: ProductionPluginRuntime) -> None:
    with runtime.repository.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workspace(
                workspace_id,workspace_kind,title,status,current_plan_revision_id,
                metadata_json,created_at,updated_at,revision
            ) VALUES('workspace-1','novel','Novel','active',NULL,'{}',?,?,0)
            """,
            (NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO execution_job(
                job_id,workspace_id,request_key,run_intent_id,run_snapshot_hash,
                run_snapshot_asset_id,run_snapshot_json,job_state,job_revision,
                result_bundle_asset_id,provenance_receipt_id,created_at,updated_at
            ) VALUES(
                'job-1','workspace-1','request-1','intent-1',?,NULL,'{}',
                'queued',1,NULL,NULL,?,?
            )
            """,
            ("a" * 64, NOW, NOW),
        )


def _insert_active_install(
    runtime: ProductionPluginRuntime, package: Any
) -> InstallFence:
    operation_id = "install-1"
    runtime.retirement.acquire_pin(
        package.release_id,
        pin_kind="install",
        owner_id=operation_id,
        pin_id="pin-install-1",
        at=NOW,
    )
    transition = initial_transition(
        operation_id,
        base_generation_id=None,
        base_lkg_generation_id=None,
        target_generation_id="generation-install",
        created_at=NOW,
    )
    request = {
        "plugin_id": package.plugin_id,
        "release_id": package.release_id,
        "package_hash": package.package_hash,
    }
    runtime.lifecycle.create_or_recover_attempt(transition, request=request)
    for expected, target, updates in (
        ("selected", "staged", {"package_store_status": "staged"}),
        ("staged", "package_published", {"package_store_status": "published"}),
        ("package_published", "env_prepared", {}),
        (
            "env_prepared",
            "shadow_prepared",
            {"shadow_data_generation_id": "data-shadow-1"},
        ),
    ):
        runtime.lifecycle.advance_attempt(
            operation_id,
            expected_state=expected,
            target_state=target,
            updates=updates,
            at=NOW,
        )
    with runtime.lifecycle.transaction() as connection:
        connection.execute(
            """
            INSERT INTO p2_plugin_shadow_generation(
                shadow_data_generation_id,install_operation_id,release_id,state,
                db_lease_id,db_lease_epoch,owner_instance_id,issued_at,expires_at,
                renewed_at,lease_state
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "data-shadow-1",
                operation_id,
                package.release_id,
                "prepared",
                "shadow-lease-1",
                1,
                "installer-1",
                NOW,
                "9999-12-31T23:59:59Z",
                NOW,
                "active",
            ),
        )
    return InstallFence(operation_id, 1, "installer-1")


def test_p6_attempt_and_install_holds_require_exact_durable_fences(
    tmp_path: Path,
) -> None:
    runtime = _open_runtime(_short_root(tmp_path), RecordedInterpreterProbe())
    try:
        package = _install_current(runtime)
        owner = "worker-lifecycle-1"
        fence = runtime.authority.claim(package.plugin_id, owner)
        assert fence is not None
        _insert_execution_job(runtime)
        runtime.execution.start_attempt(
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            worker_run_id=owner,
            plugin_id=package.plugin_id,
            release_id=package.release_id,
            package_hash=package.package_hash,
            capability_id=CAPABILITY_ID,
            generation_id=fence.generation_id,
            lease_epoch=1,
            lease_expires_at="9999-12-31T23:59:59Z",
        )
        attempt = AttemptFence("job-1", "step-1", "attempt-1", 1, owner)
        assert runtime.authority.holds_attempt(fence, attempt, owner)
        assert not runtime.authority.holds_attempt(fence, attempt, "other-owner")
        assert not runtime.authority.holds_attempt(
            fence, replace(attempt, lease_epoch=2), owner
        )

        install = _insert_active_install(runtime, package)
        assert runtime.authority.holds_install(fence, install, owner)
        assert not runtime.authority.holds_install(fence, install, "other-owner")
        assert not runtime.authority.holds_install(
            fence, replace(install, install_lease_epoch=2), owner
        )
        assert not runtime.authority.holds_install(
            fence, replace(install, owner_instance_id="installer-other"), owner
        )

        assert runtime.authority.interrupt_attempt(
            fence, attempt, owner, "directed regression"
        )
        assert runtime.authority.interrupt_attempt(
            fence, attempt, owner, "idempotent replay"
        )
        assert not runtime.authority.holds_attempt(fence, attempt, owner)
        assert runtime.authority.release(fence, owner)
        assert not runtime.authority.holds_install(fence, install, owner)
    finally:
        runtime.repository.close()


def test_p7_migration_is_append_only_and_failure_or_hash_drift_rolls_back(
    tmp_path: Path,
) -> None:
    expected = (
        (
            "0001-core-authority",
            "4f34e809575161c942ee7951b607dace93891f472a694631be2de550fd3a1585",
        ),
        (
            "0002-candidate-publication",
            "4511dbebe334bdafe0d9b1b7c57c3d5df9669c702591ec867f38526ab95eabfa",
        ),
        (
            "0003-chapter-authority",
            "eedcacb30ff32f8d944a51f230afa04489186fd6060400dfa9a7363cbd645938",
        ),
        (
            "0004-chapter-generation-writer-mode-fence",
            "996b913bf88faab5d3a73d582857cd0f81cf6525cd5c2c6417c1bc178aaf6777",
        ),
    )
    assert tuple((item.migration_id, item.sha256) for item in CORE_MIGRATIONS[:4]) == expected
    assert len(CORE_MIGRATIONS) == 4
    assert all(
        production is original
        for production, original in zip(
            PRODUCTION_CORE_MIGRATIONS[:4], CORE_MIGRATIONS, strict=True
        )
    )
    assert (
        PRODUCTION_SUPERVISOR_MIGRATIONS[0].migration_id
        == "0005-production-supervisor-authority"
    )

    connection = sqlite3.connect(tmp_path / "migration.db", isolation_level=None)
    try:
        runner = MigrationRunner(connection)
        runner.apply(CORE_MIGRATIONS)
        supervisor_migration = PRODUCTION_SUPERVISOR_MIGRATIONS[0]
        broken = Migration(
            supervisor_migration.migration_id,
            supervisor_migration.sql + "BROKEN SQL;",
        )
        with pytest.raises(sqlite3.OperationalError):
            runner.apply((broken,))
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='plugin_supervisor_claim'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM schema_migration WHERE migration_id=?",
            (supervisor_migration.migration_id,),
        ).fetchone() is None

        runner.apply((supervisor_migration,))
        with pytest.raises(RuntimeError, match="migration hash mismatch"):
            runner.apply(
                (
                    Migration(
                        supervisor_migration.migration_id,
                        supervisor_migration.sql
                        + "CREATE TABLE forbidden_hash_drift(value TEXT);",
                    ),
                )
            )
        assert connection.execute(
            "SELECT sha256 FROM schema_migration WHERE migration_id=?",
            (supervisor_migration.migration_id,),
        ).fetchone()[0] == supervisor_migration.sha256
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='forbidden_hash_drift'"
        ).fetchone() is None
    finally:
        connection.close()
