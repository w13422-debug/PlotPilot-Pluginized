"""Strict three-route Plugin API allowlist and SPA fall-through guard."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.routing import Match
from starlette.types import ASGIApp, Receive, Scope, Send

from .models import (
    PluginApiFault,
    require_semver,
    require_sha256,
    require_v1_id,
)
from .ports import PluginReadFacade

PLUGIN_API_PREFIX = "/api/v1/plugins"
ROUTE_ALLOWLIST = (
    {
        "method": "GET",
        "path": "/api/v1/plugins/releases",
        "route_id": "plugin_release_list",
    },
    {
        "method": "GET",
        "path": "/api/v1/plugins/releases/{plugin_id}/{version}",
        "route_id": "plugin_release_exact",
    },
    {
        "method": "GET",
        "path": "/api/v1/plugins/workers/{worker_id}/status",
        "route_id": "plugin_worker_status",
    },
)


def error_response(fault: PluginApiFault, *, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=fault.status_code,
        content=fault.payload(),
        headers=dict(headers or {}),
    )


def _read(call: Callable[[], dict[str, object]]) -> JSONResponse:
    try:
        return JSONResponse(content=call())
    except PluginApiFault as fault:
        return error_response(fault)


def create_plugin_router(facade: PluginReadFacade) -> APIRouter:
    router = APIRouter(prefix=PLUGIN_API_PREFIX, tags=["plugins"])

    @router.get("/releases", name="plugin_release_list")
    def list_releases() -> JSONResponse:
        return _read(facade.list_releases)

    @router.get(
        "/releases/{plugin_id}/{version}", name="plugin_release_exact"
    )
    def get_release(
        plugin_id: str,
        version: str,
        package_hash: str | None = Query(default=None),
    ) -> JSONResponse:
        def read_release() -> dict[str, object]:
            valid_plugin_id = require_v1_id(plugin_id, field="plugin_id")
            valid_version = require_semver(version)
            valid_hash = (
                None
                if package_hash is None
                else require_sha256(package_hash, field="package_hash")
            )
            return facade.get_release(valid_plugin_id, valid_version, valid_hash)

        return _read(read_release)

    @router.get("/workers/{worker_id}/status", name="plugin_worker_status")
    def get_worker_status(worker_id: str) -> JSONResponse:
        return _read(
            lambda: facade.get_worker_status(
                require_v1_id(worker_id, field="worker_id")
            )
        )

    return router


def route_inventory(router: APIRouter) -> tuple[dict[str, str], ...]:
    """Derive the source inventory from real APIRoute objects, never docs."""
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


class PluginApiBoundaryMiddleware:
    """Keep unknown/wrong-method Plugin API requests out of the legacy SPA."""

    def __init__(self, app: ASGIApp, *, routes: Iterable[Any]) -> None:
        self.app = app
        self.routes = tuple(routes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if path != PLUGIN_API_PREFIX and not path.startswith(f"{PLUGIN_API_PREFIX}/"):
            await self.app(scope, receive, send)
            return
        allowed: set[str] = set()
        for route in self.routes:
            match, _ = route.matches(scope)
            if match is Match.FULL:
                await self.app(scope, receive, send)
                return
            if match is Match.PARTIAL:
                allowed.update(getattr(route, "methods", set()))
        if allowed:
            headers = {"Allow": ", ".join(sorted(allowed))}
            response = error_response(
                PluginApiFault(
                    405,
                    "method_not_allowed",
                    "the requested method is not in the Plugin API allowlist",
                ),
                headers=headers,
            )
        else:
            response = error_response(
                PluginApiFault(
                    404,
                    "route_not_found",
                    "the requested path is not in the Plugin API allowlist",
                )
            )
        await response(scope, receive, send)


__all__ = [
    "PLUGIN_API_PREFIX",
    "ROUTE_ALLOWLIST",
    "PluginApiBoundaryMiddleware",
    "create_plugin_router",
    "error_response",
    "route_inventory",
]
