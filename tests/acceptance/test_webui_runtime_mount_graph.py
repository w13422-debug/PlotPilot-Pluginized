from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.plotpilot_core.bootstrap import (
    production_job_runtime as job_runtime_module,
)
from backend.plotpilot_core.bootstrap import webui_runtime as runtime_module
from backend.plotpilot_core.domain import Document, Workspace
from interfaces.api.settings import BackendSettings


def _settings(tmp_path: Path, name: str) -> BackendSettings:
    frontend_dir = tmp_path / name
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html>WebUI</html>", encoding="utf-8")
    return BackendSettings(
        disable_auto_daemon=True,
        frontend_dir=frontend_dir,
        log_file=str(tmp_path / f"{name}.log"),
    )


def _api_routes(app: FastAPI) -> list[APIRoute]:
    result: list[APIRoute] = []

    def append_routes(router) -> None:
        for route in router.routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                append_routes(nested)
            elif isinstance(route, APIRoute):
                result.append(route)

    append_routes(app.router)
    return result


def _table_counts(runtime) -> dict[str, int]:
    tables = (
        "chapter_generation_writer_fence",
        "execution_job",
        "candidate",
        "revision",
        "plugin_supervisor_claim",
    )
    with runtime.repository.read_connection() as connection:
        return {
            table: int(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            )
            for table in tables
        }


def test_mounts_exact_26_6_5_8_2_route_graph_with_shared_authorities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from application import paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "route-graph")
    app = FastAPI()
    runtime = runtime_module.mount_webui_runtime(app)
    try:
        routes = _api_routes(app)
        configuration_route_names = {
            "model-secret.put",
            "model-profile.revise",
            "workspace-plan.select",
            "project-planning.get",
            "project-planning.start",
        }
        groups = (
            [route for route in routes if route.path.startswith("/api/v1/core")],
            [
                route
                for route in routes
                if route.path.startswith("/api/v2/core")
                and route.name not in configuration_route_names
            ],
            [route for route in routes if route.name in configuration_route_names],
            [route for route in routes if route.path.startswith("/api/v2/jobs")],
            [
                route
                for route in routes
                if route.path.startswith("/api/v1/webui/workspaces/")
            ],
        )
        assert tuple(len(group) for group in groups) == (26, 6, 5, 8, 2)
        assert not any(
            route.path == "/api/v1/plugins"
            or route.path.startswith("/api/v1/plugins/")
            for route in routes
        )

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str) -> dict[str, str]:
            return {"path": full_path}

        ordered = _api_routes(app)
        fallback_index = next(
            index
            for index, route in enumerate(ordered)
            if route.path == "/{full_path:path}"
        )
        assert all(
            ordered.index(route) < fallback_index for group in groups for route in group
        )

        assert runtime.plugin_runtime.core_runtime is runtime.core_runtime
        assert runtime.plugin_runtime.repository is runtime.repository
        assert runtime.plugin_runtime.assets is runtime.assets
        assert runtime.job_runtime.plugin_runtime is runtime.plugin_runtime
        assert runtime.job_runtime.repository is runtime.repository
        assert runtime.job_runtime.assets is runtime.assets
        assert (
            runtime.job_runtime.execution_authority
            is runtime.plugin_runtime.execution_authority
            is runtime.m4_adapters.execution
        )
        assert runtime.job_runtime.composition.repository is runtime.repository
        assert runtime.job_runtime.composition.assets is runtime.assets
        assert runtime.model_configuration.repository is runtime.repository
        assert (
            runtime.model_configuration.configuration.repository
            is runtime.repository
        )
        assert runtime.model_configuration.planning.repository is runtime.repository
        assert runtime.plan_authority is runtime.model_configuration.planning
        assert runtime.private_generation_facade.repository is runtime.repository
        assert runtime.private_generation_facade.assets is runtime.assets
        assert runtime.private_generation_facade.job_runtime is runtime.job_runtime
        assert runtime.pump_thread is None
        assert not runtime.started
    finally:
        runtime.close()


def test_default_private_generation_is_typed_unavailable_without_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from application import paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "unavailable")
    runtime = runtime_module.build_webui_runtime()
    try:
        runtime.repository.create_workspace(Workspace("ws-1", "Novel"))
        runtime.repository.create_document(
            Document("chapter-1", "ws-1", "Chapter", "core.chapter")
        )
        revision = runtime.repository.publish_revision(
            document_id="chapter-1",
            content="Opening",
            expected_revision_id=None,
            created_by="test",
            revision_id="revision-1",
        )
        before = _table_counts(runtime)
        status, body = runtime.private_generation_facade.start(
            "ws-1",
            "chapter-1",
            {
                "schema": "webui-chapter-generation-command/v1",
                "operation_key": "unavailable-op",
                "requested_mode": "plugin",
                "expected_base": {
                    "revision_id": revision.revision_id,
                    "content_hash": revision.content_hash,
                },
                "outline": "Continue",
            },
        )

        assert status == 409
        assert body["schema"] == "webui-chapter-generation-unavailable/v1"
        assert body["reason_code"] == "verified_plugin_binding_unavailable"
        assert _table_counts(runtime) == before
        assert runtime.job_runtime.attempt_registry.tracked() == ()
        assert runtime.pump_thread is None
    finally:
        runtime.close()


def test_concurrent_startup_owns_one_pump_and_shutdown_order_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from application import paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "lifecycle")
    runtime = runtime_module.build_webui_runtime(pump_interval_seconds=0.001)
    entered_tick = threading.Event()
    release_tick = threading.Event()
    original_tick = runtime.job_runtime.supervisor.tick

    def blocked_tick() -> None:
        entered_tick.set()
        assert release_tick.wait(timeout=5)
        original_tick()

    monkeypatch.setattr(runtime.job_runtime.supervisor, "tick", blocked_tick)

    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(lambda _index: runtime.startup(), range(8)))

    pump_thread = runtime.pump_thread
    assert pump_thread is not None
    assert pump_thread.name == "PlotPilot-ProductionJobPump"
    assert pump_thread.is_alive()
    assert entered_tick.wait(timeout=2)

    order: list[str] = []
    original_ingress_close = runtime.job_runtime.ingress.close_and_drain
    original_pump_close = runtime.job_runtime.pump.close
    original_supervisor_close = runtime.job_runtime.supervisor.shutdown
    original_repository_close = runtime.repository.close

    def close_ingress() -> None:
        order.append("ingress")
        original_ingress_close()

    def close_pump() -> None:
        order.append("pump")
        original_pump_close()

    def close_supervisor() -> None:
        order.append("supervisor")
        original_supervisor_close()

    def close_repository() -> None:
        order.append("core")
        original_repository_close()

    monkeypatch.setattr(runtime.job_runtime.ingress, "close_and_drain", close_ingress)
    monkeypatch.setattr(runtime.job_runtime.pump, "close", close_pump)
    monkeypatch.setattr(runtime.job_runtime.supervisor, "shutdown", close_supervisor)
    monkeypatch.setattr(runtime.repository, "close", close_repository)

    with ThreadPoolExecutor(max_workers=1) as pool:
        closing = pool.submit(runtime.shutdown)
        assert not closing.done()
        release_tick.set()
        closing.result(timeout=5)

    assert order == ["ingress", "pump", "supervisor", "core"]
    assert not pump_thread.is_alive()
    assert not runtime.started
    assert runtime.closed
    runtime.shutdown()
    runtime.close()
    assert order == ["ingress", "pump", "supervisor", "core"]


def test_job_shutdown_failure_stops_pump_and_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from application import paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "job-shutdown-retry")
    runtime = runtime_module.build_webui_runtime(pump_interval_seconds=0.001)
    supervisor_shutdown = runtime.job_runtime.supervisor.shutdown
    repository_close = runtime.repository.close
    supervisor_calls = 0
    core_close_calls = 0

    def fail_supervisor_once() -> None:
        nonlocal supervisor_calls
        supervisor_calls += 1
        if supervisor_calls == 1:
            raise RuntimeError("injected supervisor shutdown failure")
        supervisor_shutdown()

    def record_core_close() -> None:
        nonlocal core_close_calls
        core_close_calls += 1
        repository_close()

    monkeypatch.setattr(runtime.job_runtime.supervisor, "shutdown", fail_supervisor_once)
    monkeypatch.setattr(runtime.repository, "close", record_core_close)
    runtime.startup()
    pump_thread = runtime.pump_thread
    assert pump_thread is not None and pump_thread.is_alive()
    assert id(runtime.plugin_runtime) in job_runtime_module._LOCAL_RUNTIMES

    try:
        with pytest.raises(RuntimeError, match="injected supervisor shutdown failure"):
            runtime.shutdown()

        assert not pump_thread.is_alive()
        assert not runtime.started
        assert not runtime.job_runtime._shutdown_complete
        assert id(runtime.plugin_runtime) in job_runtime_module._LOCAL_RUNTIMES
        assert not runtime.core_runtime.closed
        assert not runtime.closed
        assert core_close_calls == 0
        with runtime.repository.read_connection() as connection:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(RuntimeError, match="closed"):
            runtime.startup()

        runtime.shutdown()

        assert runtime.job_runtime._shutdown_complete
        assert id(runtime.plugin_runtime) not in job_runtime_module._LOCAL_RUNTIMES
        assert runtime.core_runtime.closed
        assert runtime.closed
        assert supervisor_calls == 2
        assert core_close_calls == 1
        runtime.shutdown()
        runtime.close()
        assert supervisor_calls == 2
        assert core_close_calls == 1
    finally:
        if not runtime.job_runtime._shutdown_complete:
            runtime.job_runtime.shutdown()
        if not runtime.core_runtime.closed:
            runtime.core_runtime.close()


def test_concurrent_shutdown_waiters_retry_after_owner_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from application import paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "concurrent-shutdown-retry")
    runtime = runtime_module.build_webui_runtime()
    supervisor_shutdown = runtime.job_runtime.supervisor.shutdown
    repository_close = runtime.repository.close
    first_attempt_entered = threading.Event()
    release_first_attempt = threading.Event()
    attempt_lock = threading.Lock()
    waiter_started = (threading.Event(), threading.Event())
    supervisor_calls = 0
    core_close_calls = 0

    def fail_first_supervisor_attempt() -> None:
        nonlocal supervisor_calls
        with attempt_lock:
            supervisor_calls += 1
            attempt = supervisor_calls
        if attempt == 1:
            first_attempt_entered.set()
            assert release_first_attempt.wait(timeout=5)
            raise RuntimeError("injected concurrent shutdown failure")
        supervisor_shutdown()

    def record_core_close() -> None:
        nonlocal core_close_calls
        core_close_calls += 1
        repository_close()

    def shutdown_waiter(started: threading.Event) -> None:
        started.set()
        runtime.shutdown()

    monkeypatch.setattr(
        runtime.job_runtime.supervisor,
        "shutdown",
        fail_first_supervisor_attempt,
    )
    monkeypatch.setattr(runtime.repository, "close", record_core_close)

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(runtime.shutdown)
            assert first_attempt_entered.wait(timeout=2)
            second = pool.submit(shutdown_waiter, waiter_started[0])
            third = pool.submit(shutdown_waiter, waiter_started[1])
            assert all(event.wait(timeout=2) for event in waiter_started)
            assert not second.done() and not third.done()
            release_first_attempt.set()

            with pytest.raises(RuntimeError, match="injected concurrent shutdown failure"):
                first.result(timeout=5)
            second.result(timeout=5)
            third.result(timeout=5)

        assert supervisor_calls == 2
        assert core_close_calls == 1
        assert runtime.job_runtime._shutdown_complete
        assert id(runtime.plugin_runtime) not in job_runtime_module._LOCAL_RUNTIMES
        assert runtime.core_runtime.closed
        assert runtime.closed
    finally:
        release_first_attempt.set()
        if not runtime.job_runtime._shutdown_complete:
            runtime.job_runtime.shutdown()
        if not runtime.core_runtime.closed:
            runtime.core_runtime.close()


def test_core_close_failure_retries_without_reopening_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from application import paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "core-close-retry")
    runtime = runtime_module.build_webui_runtime()
    job_shutdown = runtime.job_runtime.shutdown
    repository_close = runtime.repository.close
    job_shutdown_calls = 0
    core_close_calls = 0

    def record_job_shutdown() -> None:
        nonlocal job_shutdown_calls
        job_shutdown_calls += 1
        job_shutdown()

    def fail_core_once() -> None:
        nonlocal core_close_calls
        core_close_calls += 1
        if core_close_calls == 1:
            raise RuntimeError("injected Core close failure")
        repository_close()

    monkeypatch.setattr(runtime.job_runtime, "shutdown", record_job_shutdown)
    monkeypatch.setattr(runtime.repository, "close", fail_core_once)

    try:
        with pytest.raises(RuntimeError, match="injected Core close failure"):
            runtime.shutdown()

        assert job_shutdown_calls == 1
        assert runtime.job_runtime._shutdown_complete
        assert id(runtime.plugin_runtime) not in job_runtime_module._LOCAL_RUNTIMES
        assert not runtime.core_runtime.closed
        assert not runtime.closed
        with runtime.repository.read_connection() as connection:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(RuntimeError, match="closed"):
            runtime.startup()

        runtime.shutdown()

        assert job_shutdown_calls == 1
        assert core_close_calls == 2
        assert runtime.core_runtime.closed
        assert runtime.closed
        runtime.shutdown()
        runtime.close()
        assert job_shutdown_calls == 1
        assert core_close_calls == 2
    finally:
        if not runtime.job_runtime._shutdown_complete:
            runtime.job_runtime.shutdown()
        if not runtime.core_runtime.closed:
            runtime.core_runtime.close()


def test_partial_build_and_mount_failures_release_all_owners(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from application import paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "build-failure")
    captured: dict[str, object] = {}
    original_core_builder = runtime_module.build_webui_core_runtime
    original_job_builder = runtime_module.build_production_job_runtime

    def capture_core():
        core = original_core_builder()
        captured["core"] = core
        return core

    def capture_job(plugin_runtime):
        job = original_job_builder(plugin_runtime)
        captured["job"] = job
        return job

    def fail_private_router(_facade):
        raise RuntimeError("injected private router failure")

    monkeypatch.setattr(runtime_module, "build_webui_core_runtime", capture_core)
    monkeypatch.setattr(runtime_module, "build_production_job_runtime", capture_job)
    monkeypatch.setattr(
        runtime_module,
        "create_private_generation_router",
        fail_private_router,
    )

    with pytest.raises(RuntimeError, match="injected private router failure"):
        runtime_module.build_webui_runtime()

    failed_core = captured["core"]
    failed_job = captured["job"]
    assert failed_core.closed
    assert failed_job._shutdown_complete
    assert id(failed_job.plugin_runtime) not in job_runtime_module._LOCAL_RUNTIMES

    monkeypatch.undo()
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "mount-failure")
    runtime = runtime_module.build_webui_runtime()
    app = FastAPI()
    original_routes = tuple(app.router.routes)
    original_include = app.include_router
    include_calls = 0

    def fail_second_include(router, *args, **kwargs):
        nonlocal include_calls
        include_calls += 1
        if include_calls == 2:
            raise RuntimeError("injected mount failure")
        return original_include(router, *args, **kwargs)

    monkeypatch.setattr(runtime_module, "build_webui_runtime", lambda: runtime)
    monkeypatch.setattr(app, "include_router", fail_second_include)

    with pytest.raises(RuntimeError, match="injected mount failure"):
        runtime_module.mount_webui_runtime(app)

    assert tuple(app.router.routes) == original_routes
    assert runtime.closed
    assert id(runtime.plugin_runtime) not in job_runtime_module._LOCAL_RUNTIMES


def test_lifespan_start_failure_rolls_back_runtime_before_legacy_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from application import paths
    from interfaces import main

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "start-failure")
    events: list[str] = []

    class RecordingLifecycle:
        def startup(self, _registered_route_count: int) -> None:
            events.append("legacy.startup")

        def shutdown(self) -> None:
            events.append("legacy.shutdown")

    monkeypatch.setattr(main, "_get_lifecycle", lambda: RecordingLifecycle())
    app = main.create_app(_settings(tmp_path, "frontend-start-failure"))
    runtime = app.state.webui_runtime
    original_shutdown = runtime.shutdown

    def fail_startup() -> None:
        events.append("runtime.startup")
        raise RuntimeError("injected pump start failure")

    def record_shutdown() -> None:
        events.append("runtime.shutdown")
        original_shutdown()

    monkeypatch.setattr(runtime, "startup", fail_startup)
    monkeypatch.setattr(runtime, "shutdown", record_shutdown)

    with (
        pytest.raises(RuntimeError, match="injected pump start failure"),
        TestClient(app),
    ):
        pass

    assert events == [
        "legacy.startup",
        "runtime.startup",
        "runtime.shutdown",
        "legacy.shutdown",
    ]
    assert runtime.closed
    assert id(runtime.plugin_runtime) not in job_runtime_module._LOCAL_RUNTIMES
