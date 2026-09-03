"""Unmounted lifecycle router. P0 remains the only production app composer."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .models import LifecycleFault, parse_run_snapshot_worker
from .ports import PluginLifecycleFacade

PLUGIN_API_PREFIX = "/api/v2/plugins"
ROUTE_ALLOWLIST = (
    {
        "method": "POST",
        "path": "/api/v2/plugins/lifecycle/workers/request",
        "route_id": "plugin_worker_request",
    },
)


def error_response(fault: LifecycleFault) -> JSONResponse:
    return JSONResponse(status_code=fault.status_code, content=fault.payload())


def _read(call: Callable[[], dict[str, object]]) -> JSONResponse:
    try:
        return JSONResponse(content=call())
    except LifecycleFault as fault:
        return error_response(fault)


def create_lifecycle_router(facade: PluginLifecycleFacade) -> APIRouter:
    """Build only the frozen route slice; this function does not mount an app."""

    router = APIRouter(prefix=PLUGIN_API_PREFIX, tags=["plugins"])

    @router.post("/lifecycle/workers/request", name="plugin_worker_request")
    def request_worker(payload: dict[str, Any]) -> JSONResponse:
        return _read(lambda: facade.request_worker(parse_run_snapshot_worker(payload)))

    return router


def create_plugin_router(facade: PluginLifecycleFacade) -> APIRouter:
    """Compatibility spelling for P0 composition; it creates no v1 route."""

    return create_lifecycle_router(facade)


def route_inventory(router: APIRouter) -> tuple[dict[str, str], ...]:
    inventory: list[dict[str, str]] = []
    for route in router.routes:
        path = getattr(route, "path", None)
        name = getattr(route, "name", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or not isinstance(name, str) or methods is None:
            continue
        for method in sorted(methods):
            inventory.append({"method": method, "path": path, "route_id": name})
    return tuple(sorted(inventory, key=lambda item: (item["path"], item["method"])))


__all__ = [
    "PLUGIN_API_PREFIX",
    "ROUTE_ALLOWLIST",
    "create_lifecycle_router",
    "create_plugin_router",
    "error_response",
    "route_inventory",
]
