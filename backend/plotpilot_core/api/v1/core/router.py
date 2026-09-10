"""Unmounted FastAPI router for the frozen Core v1 HTTP matrix."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote_to_bytes

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.routing import Match
from starlette.types import Scope

from backend.plotpilot_plugin_sdk.canonical import parse_json_bytes

from .adapter import (
    AUTHORITY_ROUTE_ALLOWLIST,
    AUTHORITY_ROUTE_SPECS,
    CORE_ROUTE_SPECS,
    ROUTE_ALLOWLIST,
    ROUTE_BY_ID,
    CoreHttpAdapter,
    QueryDecodeError,
    decode_query_request,
)

_CORE_PREFIX = "/api/v1/core"
_HEX_BYTES = frozenset(b"0123456789abcdefABCDEF")


def _router_path(path_template: str) -> str:
    """Return the frozen relative path without changing its public template."""

    if not path_template.startswith(_CORE_PREFIX):
        raise RuntimeError(f"Core v1 matrix route is outside {_CORE_PREFIX}: {path_template}")
    return path_template[len(_CORE_PREFIX) :] or "/"


def _decode_slash_identity(raw_identity: bytes) -> str | None:
    if b"/" in raw_identity or b"%2f" not in raw_identity.lower():
        return None
    index = 0
    while index < len(raw_identity):
        if raw_identity[index] != ord("%"):
            index += 1
            continue
        if (
            index + 2 >= len(raw_identity)
            or raw_identity[index + 1] not in _HEX_BYTES
            or raw_identity[index + 2] not in _HEX_BYTES
        ):
            return None
        index += 3
    try:
        decoded = unquote_to_bytes(raw_identity).decode("utf-8")
    except UnicodeDecodeError:
        return None
    return decoded if "/" in decoded else None


class _SlashIdentityRoute(APIRoute):
    """Recover encoded slash-bearing IDs without consuming structural slashes."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        match, child_scope = super().matches(scope)
        if match is not Match.NONE or scope.get("type") != "http":
            return match, child_scope
        spec = ROUTE_BY_ID.get(self.name)
        if spec is None or len(spec.path_identity) != 1:
            return match, child_scope

        raw_path = scope.get("raw_path")
        if not isinstance(raw_path, bytes):
            return match, child_scope
        raw_root = str(scope.get("root_path", "")).encode("utf-8")
        if raw_root and raw_path.startswith(raw_root + _CORE_PREFIX.encode("ascii")):
            raw_path = raw_path[len(raw_root) :]

        identity_name = spec.path_identity[0]
        marker = "{" + identity_name + "}"
        prefix, suffix = spec.path_template.split(marker)
        raw_prefix = prefix.encode("ascii")
        raw_suffix = suffix.encode("ascii")
        if not raw_path.startswith(raw_prefix) or (
            raw_suffix and not raw_path.endswith(raw_suffix)
        ):
            return match, child_scope
        identity_end = len(raw_path) - len(raw_suffix) if raw_suffix else len(raw_path)
        raw_identity = raw_path[len(raw_prefix) : identity_end]
        identity = _decode_slash_identity(raw_identity)
        if identity is None:
            return match, child_scope

        path_params = dict(scope.get("path_params", {}))
        path_params[identity_name] = identity
        child_scope = {
            "endpoint": self.endpoint,
            "path_params": path_params,
            "route": self,
        }
        if self.methods and scope.get("method") not in self.methods:
            return Match.PARTIAL, child_scope
        return Match.FULL, child_scope


def _response(result: tuple[int, Mapping[str, Any]]) -> JSONResponse:
    status, payload = result
    return JSONResponse(status_code=status, content=dict(payload))


async def _decode_body(request: Request) -> tuple[bool, Mapping[str, Any] | None]:
    """Return ``(is_malformed_json, object_body)`` without FastAPI body coercion."""

    raw = await request.body()
    try:
        parsed = parse_json_bytes(raw)
    except ValueError:
        return True, None
    if not isinstance(parsed, Mapping):
        return False, None
    return False, dict(parsed)


def _endpoint(adapter: CoreHttpAdapter, route_id: str):
    spec = ROUTE_BY_ID[route_id]

    async def endpoint(request: Request) -> JSONResponse:
        path_identity = dict(request.path_params)
        if spec.method == "GET":
            raw = await request.body()
            if raw:
                malformed, _ = await _decode_body(request)
                if malformed:
                    return _response(adapter.request_error("malformed_json"))
                return _response(adapter.request_error("invalid_request"))
            try:
                body = decode_query_request(
                    route_id,
                    request.query_params.multi_items(),
                    path_identity=path_identity,
                )
            except QueryDecodeError:
                return _response(adapter.request_error("invalid_query"))
        else:
            if request.query_params.multi_items():
                return _response(adapter.request_error("invalid_query"))
            malformed, body = await _decode_body(request)
            if malformed:
                return _response(adapter.request_error("malformed_json"))
            if body is None:
                return _response(adapter.request_error("invalid_request"))
        return _response(adapter.handle(route_id, body, path_identity=path_identity))

    endpoint.__name__ = "core_v1_" + route_id.replace(".", "_")
    return endpoint


def create_core_router(adapter: CoreHttpAdapter) -> APIRouter:
    """Build exactly the unmounted 26-route Core v1 matrix and nothing else."""

    router = APIRouter(
        prefix=_CORE_PREFIX,
        tags=["core-v1"],
        route_class=_SlashIdentityRoute,
    )
    # Matrix order deliberately keeps each parent identity before its child
    # route.  The custom matcher accepts only encoded slashes in the former,
    # leaving literal structural separators for the latter.
    for spec in CORE_ROUTE_SPECS:
        router.add_api_route(
            _router_path(spec.path_template),
            _endpoint(adapter, spec.route_id),
            methods=[spec.method],
            name=spec.route_id,
            status_code=spec.success_status,
            response_model=None,
            include_in_schema=False,
        )
    inventory = route_inventory(router)
    if inventory != ROUTE_ALLOWLIST:
        raise RuntimeError("actual Core v1 APIRoute inventory drifted from the frozen matrix")
    if authority_route_inventory(router) != AUTHORITY_ROUTE_ALLOWLIST:
        raise RuntimeError("actual Core v1 authority APIRoute inventory drifted")
    return router


build_core_router = create_core_router


def route_inventory(router: APIRouter) -> tuple[dict[str, Any], ...]:
    """Derive matrix-order inventory from actual FastAPI routes, not documents."""

    actual: dict[str, dict[str, Any]] = {}
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if route.name not in ROUTE_BY_ID:
            if path == _CORE_PREFIX or path.startswith(f"{_CORE_PREFIX}/"):
                raise RuntimeError(f"Core v1 APIRoute is outside the frozen allowlist: {path}")
            continue
        spec = ROUTE_BY_ID[route.name]
        methods = set(route.methods or ())
        if methods != {spec.method}:
            raise RuntimeError(f"Core v1 APIRoute method drifted: {spec.route_id}")
        if path != spec.path_template:
            raise RuntimeError(f"Core v1 APIRoute path drifted: {spec.route_id}")
        if route.status_code != spec.success_status:
            raise RuntimeError(f"Core v1 APIRoute success status drifted: {spec.route_id}")
        if route.name in actual:
            raise RuntimeError(f"Core v1 APIRoute duplicated: {spec.route_id}")
        actual[route.name] = {
            "route_id": route.name,
            "method": spec.method,
            "path": path,
            "success_status": route.status_code,
        }
    if set(actual) != set(ROUTE_BY_ID):
        missing = sorted(set(ROUTE_BY_ID) - set(actual))
        extra = sorted(set(actual) - set(ROUTE_BY_ID))
        raise RuntimeError(f"Core v1 APIRoute allowlist mismatch; missing={missing}, extra={extra}")
    return tuple(actual[spec.route_id] for spec in CORE_ROUTE_SPECS)


def authority_route_inventory(router: APIRouter) -> tuple[dict[str, Any], ...]:
    """Return the 23 authority entity routes as an actual-router subset."""

    full = {item["route_id"]: item for item in route_inventory(router)}
    return tuple(full[spec.route_id] for spec in AUTHORITY_ROUTE_SPECS)


__all__ = [
    "authority_route_inventory",
    "build_core_router",
    "create_core_router",
    "route_inventory",
]
