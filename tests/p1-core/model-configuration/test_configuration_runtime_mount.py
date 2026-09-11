from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

import pytest

from backend.plotpilot_core.api.v2.configuration import ROUTE_ALLOWLIST, route_inventory
from backend.plotpilot_core.bootstrap import webui_runtime as runtime_module
from backend.plotpilot_core.bootstrap.model_configuration_adapters import (
    build_model_configuration_adapters,
)
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository


def _api_routes(app: FastAPI) -> list[APIRoute]:
    result: list[APIRoute] = []

    def append(router) -> None:
        for route in router.routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                append(nested)
            elif isinstance(route, APIRoute):
                result.append(route)

    append(app.router)
    return result


def test_model_configuration_composition_reuses_exact_core_repository(tmp_path) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    try:
        adapters = build_model_configuration_adapters(repository)
        router = adapters.router()
        assert adapters.repository is repository
        assert adapters.configuration.repository is repository
        assert adapters.planning.repository is repository
        assert adapters.http.configuration is adapters.configuration
        assert adapters.http.planning is adapters.planning
        assert adapters.plan_registrar is adapters.planning
        assert route_inventory(router) == tuple(
            sorted(ROUTE_ALLOWLIST, key=lambda item: (item["path"], item["method"]))
        )
        assert len(router.routes) == 5
    finally:
        repository.close()


def test_webui_runtime_adds_26_6_5_8_2_graph_before_spa_without_starting(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from application import paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "runtime")
    app = FastAPI()
    runtime = runtime_module.mount_webui_runtime(app)
    try:
        assert tuple(len(router.routes) for router in runtime.routers) == (
            26,
            6,
            5,
            8,
            2,
        )
        assert runtime_module.WEBUI_RUNTIME_ROUTE_COUNTS == (26, 6, 5, 8, 2)
        assert runtime.model_configuration.repository is runtime.repository
        assert runtime.model_configuration.configuration.repository is runtime.repository
        assert runtime.model_configuration.planning.repository is runtime.repository
        assert runtime.plan_authority is runtime.model_configuration.planning
        assert runtime.pump_thread is None
        assert runtime.started is False

        @app.get("/{full_path:path}")
        def spa(full_path: str) -> dict[str, str]:
            return {"path": full_path}

        routes = _api_routes(app)
        fallback_index = next(
            index
            for index, route in enumerate(routes)
            if route.path == "/{full_path:path}"
        )
        configuration_routes = [
            route
            for route in routes
            if route.name
            in {
                "model-secret.put",
                "model-profile.revise",
                "workspace-plan.select",
                "project-planning.get",
                "project-planning.start",
            }
        ]
        assert len(configuration_routes) == 5
        assert all(routes.index(route) < fallback_index for route in configuration_routes)
        assert len(
            [route for route in routes if route.path.startswith("/api/v2/core")]
        ) == 11
    finally:
        runtime.close()


def test_configuration_build_failure_rolls_back_job_and_core_owners(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from application import paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "failure")
    captured: dict[str, object] = {}
    original_core = runtime_module.build_webui_core_runtime
    original_job = runtime_module.build_production_job_runtime

    def capture_core():
        runtime = original_core()
        captured["core"] = runtime
        return runtime

    def capture_job(plugin_runtime):
        runtime = original_job(plugin_runtime)
        captured["job"] = runtime
        return runtime

    def fail_configuration(_repository):
        raise RuntimeError("injected configuration composition failure")

    monkeypatch.setattr(runtime_module, "build_webui_core_runtime", capture_core)
    monkeypatch.setattr(runtime_module, "build_production_job_runtime", capture_job)
    monkeypatch.setattr(
        runtime_module,
        "build_model_configuration_adapters",
        fail_configuration,
    )

    with pytest.raises(RuntimeError, match="injected configuration composition failure"):
        runtime_module.build_webui_runtime()

    assert captured["core"].closed
    assert captured["job"]._shutdown_complete
