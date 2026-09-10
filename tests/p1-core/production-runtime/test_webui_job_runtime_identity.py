from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend.plotpilot_core.api.v2.jobs.router import (
    ROUTE_ALLOWLIST,
    route_inventory,
)
from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.bootstrap.production_job_runtime import (
    build_production_job_runtime,
)
from backend.plotpilot_core.bootstrap.production_plugin_runtime import (
    build_production_plugin_runtime,
)
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.repositories.migrations import (
    PRODUCTION_CORE_MIGRATIONS,
)


def test_j10_exact_webui_authority_identity_and_jobs_v2_router_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "product"
    (root / "core").mkdir(parents=True)
    repository = CoreAuthorityRepository(root / "core" / "core.db")
    assets = AssetStore(root / "assets")
    core_runtime = SimpleNamespace(repository=repository, assets=assets)
    plugin_runtime = build_production_plugin_runtime(core_runtime, data_root=root)
    runtime = build_production_job_runtime(plugin_runtime)
    try:
        assert runtime.core_runtime is core_runtime
        assert runtime.plugin_runtime is plugin_runtime
        assert runtime.repository is repository
        assert runtime.assets is assets
        assert runtime.execution_authority is plugin_runtime.execution_authority
        assert runtime.supervisor is plugin_runtime.supervisor
        assert runtime.composition.authority is runtime.execution_authority
        assert runtime.composition.repository is repository
        assert runtime.composition.assets is assets
        assert runtime.composition.supervisor is runtime.supervisor
        assert runtime.command_query is runtime.composition.command_query
        assert runtime.http is runtime.command_query
        assert runtime.router is runtime.jobs_router

        expected = tuple(
            sorted(
                ROUTE_ALLOWLIST,
                key=lambda item: (item["path"], item["method"]),
            )
        )
        assert route_inventory(runtime.router) == expected
        assert len(runtime.router.routes) == 8
        assert all(item["path"].startswith("/api/v2/jobs") for item in expected)
        assert all("/api/v1/plugins" not in item["path"] for item in expected)

        runtime.shutdown()
        runtime.shutdown()
        with repository.read_connection() as connection:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        runtime.shutdown()
        repository.close()


def test_0006_is_append_only_after_byte_stable_0001_through_0005() -> None:
    assert tuple(
        (migration.migration_id, migration.sha256)
        for migration in PRODUCTION_CORE_MIGRATIONS
    ) == (
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
        (
            "0005-production-supervisor-authority",
            "705ffe22ba46b7cf2f740df0daaa2abfcf1b8cb848ffce3d5f763a3759000345",
        ),
        (
            "0006-production-job-command-receipt",
            "00671952b7d4cd06618df900eedd991eab9be50095ff540fab8c6acd8ca13e28",
        ),
    )
