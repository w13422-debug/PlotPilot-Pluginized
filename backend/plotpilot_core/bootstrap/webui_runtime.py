"""Single production authority graph for the browser WebUI entrypoint."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, FastAPI

from backend.plotpilot_core.api.v1.core import (
    ROUTE_ALLOWLIST as CORE_V1_ROUTE_ALLOWLIST,
)
from backend.plotpilot_core.api.v1.core import create_core_router
from backend.plotpilot_core.api.v1.core import route_inventory as core_v1_inventory
from backend.plotpilot_core.api.v1.webui import (
    PRIVATE_GENERATION_ROUTE_ALLOWLIST,
    create_private_generation_router,
)
from backend.plotpilot_core.api.v1.webui import (
    route_inventory as private_generation_inventory,
)
from backend.plotpilot_core.api.v2.configuration.router import (
    ROUTE_ALLOWLIST as CONFIGURATION_V2_ROUTE_ALLOWLIST,
)
from backend.plotpilot_core.api.v2.configuration.router import (
    route_inventory as configuration_v2_inventory,
)
from backend.plotpilot_core.api.v2.jobs.router import (
    ROUTE_ALLOWLIST as JOBS_V2_ROUTE_ALLOWLIST,
)
from backend.plotpilot_core.api.v2.jobs.router import (
    route_inventory as jobs_v2_inventory,
)
from backend.plotpilot_core.webui import PrivateChapterGenerationFacade

from .m4_authority_adapters import M4AuthorityAdapters, build_m4_authority_adapters
from .model_configuration_adapters import (
    ModelConfigurationAdapters,
    build_model_configuration_adapters,
)
from .production_job_runtime import (
    ProductionJobRuntime,
    build_production_job_runtime,
)
from .production_plugin_runtime import (
    ProductionPluginRuntime,
    build_production_plugin_runtime,
)
from .webui_core_runtime import WebUiCoreRuntime, build_webui_core_runtime

logger = logging.getLogger(__name__)

DEFAULT_JOB_PUMP_INTERVAL_SECONDS = 0.05
WEBUI_RUNTIME_ROUTE_COUNTS = (26, 6, 5, 8, 2)

_CORE_V2_ROUTE_ALLOWLIST = (
    ("GET", "/api/v2/core/workspaces/{workspace_id}/candidates"),
    ("GET", "/api/v2/core/workspaces/{workspace_id}/candidates/{candidate_id}"),
    (
        "POST",
        "/api/v2/core/workspaces/{workspace_id}/candidates/{candidate_id}/review",
    ),
    (
        "GET",
        "/api/v2/core/workspaces/{workspace_id}/candidates/{candidate_id}/preview",
    ),
    ("POST", "/api/v2/core/workspaces/{workspace_id}/publications:accept"),
    (
        "GET",
        "/api/v2/core/workspaces/{workspace_id}/story-state/projection-input",
    ),
)
_PLUGIN_V1_PREFIX = "/api/v1/plugins"
_SPA_FALLBACK_PATH = "/{full_path:path}"


def _iter_routes(router: Any) -> Iterable[Any]:
    for route in router.routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _iter_routes(nested)
        else:
            yield route


def _method_path_inventory(router: APIRouter) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for route in _iter_routes(router):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or methods is None:
            continue
        result.extend((method, path) for method in sorted(methods))
    return tuple(result)


def _assert_router_contracts(
    core_v1_router: APIRouter,
    core_v2_router: APIRouter,
    configuration_v2_router: APIRouter,
    jobs_v2_router: APIRouter,
    private_generation_router: APIRouter,
) -> None:
    if core_v1_inventory(core_v1_router) != CORE_V1_ROUTE_ALLOWLIST:
        raise RuntimeError("Core v1 WebUI route inventory drifted")
    if _method_path_inventory(core_v2_router) != _CORE_V2_ROUTE_ALLOWLIST:
        raise RuntimeError("Core v2 WebUI route inventory drifted")
    expected_configuration = tuple(
        sorted(
            CONFIGURATION_V2_ROUTE_ALLOWLIST,
            key=lambda item: (item["path"], item["method"]),
        )
    )
    if configuration_v2_inventory(configuration_v2_router) != expected_configuration:
        raise RuntimeError("configuration v2 WebUI route inventory drifted")
    expected_jobs = tuple(
        sorted(
            JOBS_V2_ROUTE_ALLOWLIST,
            key=lambda item: (item["path"], item["method"]),
        )
    )
    if jobs_v2_inventory(jobs_v2_router) != expected_jobs:
        raise RuntimeError("Jobs v2 WebUI route inventory drifted")
    if (
        private_generation_inventory(private_generation_router)
        != PRIVATE_GENERATION_ROUTE_ALLOWLIST
    ):
        raise RuntimeError("private WebUI generation route inventory drifted")

    routers = (
        core_v1_router,
        core_v2_router,
        configuration_v2_router,
        jobs_v2_router,
        private_generation_router,
    )
    counts = tuple(len(router.routes) for router in routers)
    if counts != WEBUI_RUNTIME_ROUTE_COUNTS:
        raise RuntimeError(
            f"WebUI production route counts drifted: expected "
            f"{WEBUI_RUNTIME_ROUTE_COUNTS}, got {counts}"
        )
    if any(
        path == _PLUGIN_V1_PREFIX or path.startswith(f"{_PLUGIN_V1_PREFIX}/")
        for router in routers
        for _, path in _method_path_inventory(router)
    ):
        raise RuntimeError("Plugin v1 routes are disabled in this WebUI release")


def _close_partial_runtime(
    core_runtime: WebUiCoreRuntime | None,
    job_runtime: ProductionJobRuntime | None,
) -> None:
    if job_runtime is not None:
        try:
            job_runtime.shutdown()
        except BaseException:
            logger.exception("partial WebUI Job runtime rollback failed")
    if core_runtime is not None:
        try:
            core_runtime.close()
        except BaseException:
            logger.exception("partial WebUI Core runtime rollback failed")


class WebUiRuntime:
    """Own one durable Core/Plugin/Job/M4/private production graph."""

    def __init__(
        self,
        *,
        core_runtime: WebUiCoreRuntime,
        plugin_runtime: ProductionPluginRuntime,
        job_runtime: ProductionJobRuntime,
        m4_adapters: M4AuthorityAdapters,
        model_configuration_adapters: ModelConfigurationAdapters,
        private_generation_facade: PrivateChapterGenerationFacade,
        core_v1_router: APIRouter,
        core_v2_router: APIRouter,
        configuration_v2_router: APIRouter,
        jobs_v2_router: APIRouter,
        private_generation_router: APIRouter,
        pump_interval_seconds: float = DEFAULT_JOB_PUMP_INTERVAL_SECONDS,
    ) -> None:
        if isinstance(pump_interval_seconds, bool) or pump_interval_seconds <= 0:
            raise ValueError("pump_interval_seconds must be positive")
        self.core_runtime = core_runtime
        self.plugin_runtime = plugin_runtime
        self.job_runtime = job_runtime
        self.m4_adapters = m4_adapters
        self.model_configuration_adapters = model_configuration_adapters
        self.private_generation_facade = private_generation_facade
        self.core_v1_router = core_v1_router
        self.core_v2_router = core_v2_router
        self.configuration_v2_router = configuration_v2_router
        self.jobs_v2_router = jobs_v2_router
        self.private_generation_router = private_generation_router
        self.pump_interval_seconds = float(pump_interval_seconds)

        self._state_condition = threading.Condition(threading.RLock())
        self._pump_stop = threading.Event()
        self._pump_thread: threading.Thread | None = None
        self._pump_failure: BaseException | None = None
        self._started = False
        self._shutdown_requested = False
        self._job_shutdown_complete = False
        self._closing = False
        self._closed = False

    # Compatibility projections retained from the accepted Core-only owner.
    @property
    def repository(self):
        return self.core_runtime.repository

    @property
    def assets(self):
        return self.core_runtime.assets

    @property
    def publication_service(self):
        return self.core_runtime.publication_service

    @property
    def authority(self):
        return self.core_runtime.authority

    @property
    def adapter(self):
        return self.core_runtime.adapter

    @property
    def execution_authority(self):
        return self.job_runtime.execution_authority

    @property
    def pump(self):
        return self.job_runtime.pump

    @property
    def private_facade(self) -> PrivateChapterGenerationFacade:
        return self.private_generation_facade

    @property
    def m4(self) -> M4AuthorityAdapters:
        return self.m4_adapters

    @property
    def model_configuration(self) -> ModelConfigurationAdapters:
        return self.model_configuration_adapters

    @property
    def plan_authority(self):
        return self.model_configuration_adapters.planning

    @property
    def started(self) -> bool:
        with self._state_condition:
            return self._started

    @property
    def closed(self) -> bool:
        with self._state_condition:
            return self._closed

    @property
    def pump_thread(self) -> threading.Thread | None:
        with self._state_condition:
            return self._pump_thread

    @property
    def pump_failure(self) -> BaseException | None:
        with self._state_condition:
            return self._pump_failure

    @property
    def routers(
        self,
    ) -> tuple[APIRouter, APIRouter, APIRouter, APIRouter, APIRouter]:
        return (
            self.core_v1_router,
            self.core_v2_router,
            self.configuration_v2_router,
            self.jobs_v2_router,
            self.private_generation_router,
        )

    def _run_pump(self) -> None:
        while not self._pump_stop.wait(self.pump_interval_seconds):
            try:
                self.job_runtime.pump.run_once()
            except Exception as exc:
                if self._pump_stop.is_set():
                    break
                with self._state_condition:
                    self._pump_failure = exc
                logger.exception("WebUI production Job pump tick failed")

    def startup(self) -> None:
        """Start exactly one caller-owned pump thread, including concurrent calls."""

        with self._state_condition:
            if self._closed or self._shutdown_requested:
                raise RuntimeError("WebUI production runtime is closed")
            if self._started:
                return
            self._pump_stop.clear()
            thread = threading.Thread(
                target=self._run_pump,
                name="PlotPilot-ProductionJobPump",
                daemon=True,
            )
            self._pump_thread = thread
            self._started = True
            try:
                thread.start()
            except BaseException:
                self._started = False
                self._pump_thread = None
                self._pump_stop.set()
                raise

    start = startup

    def shutdown(self) -> None:
        """Retry failed cleanup while preserving Job-before-Core ownership order."""

        with self._state_condition:
            self._shutdown_requested = True
            if self._closed:
                return
            while self._closing:
                self._state_condition.wait()
                if self._closed:
                    return
            if self._closed:
                return
            self._closing = True
            self._pump_stop.set()
            pump_thread = self._pump_thread
            close_job = not self._job_shutdown_complete

        first_error: BaseException | None = None
        job_shutdown_complete = not close_job
        if close_job:
            try:
                # Frozen WU2B owns the exact ingress -> pump -> supervisor sequence.
                self.job_runtime.shutdown()
            except BaseException as exc:  # noqa: BLE001 - retry must remain possible
                first_error = exc
            else:
                job_shutdown_complete = True

        try:
            if pump_thread is not None and pump_thread is not threading.current_thread():
                pump_thread.join()
        except BaseException as exc:  # noqa: BLE001 - retry must remain possible
            if first_error is None:
                first_error = exc

        core_closed = False
        if first_error is None:
            try:
                self.core_runtime.close()
            except BaseException as exc:  # noqa: BLE001 - retry must remain possible
                first_error = exc
            else:
                core_closed = True

        with self._state_condition:
            self._started = False
            if job_shutdown_complete:
                self._job_shutdown_complete = True
            if core_closed:
                self._closed = True
            self._closing = False
            self._state_condition.notify_all()

        if first_error is not None:
            raise first_error

    close = shutdown


WebUiProductionRuntime = WebUiRuntime


def build_webui_runtime(
    *,
    pump_interval_seconds: float = DEFAULT_JOB_PUMP_INTERVAL_SECONDS,
) -> WebUiRuntime:
    """Build, but do not mount or start, the single production graph."""

    core_runtime: WebUiCoreRuntime | None = None
    job_runtime: ProductionJobRuntime | None = None
    try:
        core_runtime = build_webui_core_runtime()
        plugin_runtime = build_production_plugin_runtime(core_runtime)
        job_runtime = build_production_job_runtime(plugin_runtime)
        m4_adapters = build_m4_authority_adapters(
            core_runtime.repository,
            core_runtime.assets,
            execution_authority=plugin_runtime.execution_authority,
        )
        model_configuration_adapters = build_model_configuration_adapters(
            core_runtime.repository
        )
        private_facade = PrivateChapterGenerationFacade(
            core_runtime.repository,
            core_runtime.assets,
            job_runtime,
        )
        core_v1_router = create_core_router(core_runtime.adapter)
        core_v2_router = m4_adapters.router()
        configuration_v2_router = model_configuration_adapters.router()
        jobs_v2_router = job_runtime.router
        private_router = create_private_generation_router(private_facade)
        _assert_router_contracts(
            core_v1_router,
            core_v2_router,
            configuration_v2_router,
            jobs_v2_router,
            private_router,
        )
        return WebUiRuntime(
            core_runtime=core_runtime,
            plugin_runtime=plugin_runtime,
            job_runtime=job_runtime,
            m4_adapters=m4_adapters,
            model_configuration_adapters=model_configuration_adapters,
            private_generation_facade=private_facade,
            core_v1_router=core_v1_router,
            core_v2_router=core_v2_router,
            configuration_v2_router=configuration_v2_router,
            jobs_v2_router=jobs_v2_router,
            private_generation_router=private_router,
            pump_interval_seconds=pump_interval_seconds,
        )
    except BaseException:
        _close_partial_runtime(core_runtime, job_runtime)
        raise


def _contains_spa_fallback(router: APIRouter) -> bool:
    return any(
        getattr(route, "path", None) == _SPA_FALLBACK_PATH
        for route in _iter_routes(router)
    )


def mount_webui_runtime(app: FastAPI) -> WebUiRuntime:
    """Atomically mount the 26/6/5/8/2 graph before any SPA catch-all."""

    if getattr(app.state, "webui_runtime", None) is not None:
        raise RuntimeError("WebUI production runtime is already mounted")
    if _contains_spa_fallback(app.router):
        raise RuntimeError("WebUI production routes must be mounted before SPA fallback")

    runtime = build_webui_runtime()
    original_routes = tuple(app.router.routes)
    try:
        for router in runtime.routers:
            app.include_router(router)
        if any(
            path == _PLUGIN_V1_PREFIX or path.startswith(f"{_PLUGIN_V1_PREFIX}/")
            for _, path in _method_path_inventory(app.router)
        ):
            raise RuntimeError("Plugin v1 routes are disabled in this WebUI release")
        app.state.webui_runtime = runtime
        return runtime
    except BaseException:
        app.router.routes[:] = original_routes
        runtime.close()
        raise


__all__ = [
    "DEFAULT_JOB_PUMP_INTERVAL_SECONDS",
    "WEBUI_RUNTIME_ROUTE_COUNTS",
    "WebUiProductionRuntime",
    "WebUiRuntime",
    "build_webui_runtime",
    "mount_webui_runtime",
]
