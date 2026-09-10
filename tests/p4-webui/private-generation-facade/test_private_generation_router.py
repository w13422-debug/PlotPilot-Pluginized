"""P12 proof for the private, unmounted two-route router."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.plotpilot_core.api.v1.webui import (
    PRIVATE_GENERATION_ROUTE_ALLOWLIST,
    create_private_generation_router,
    route_inventory,
)

ROOT = Path(__file__).resolve().parents[3]


def test_p12_actual_router_has_exactly_two_private_routes(facade_stack):
    router = create_private_generation_router(facade_stack.facade)

    assert route_inventory(router) == PRIVATE_GENERATION_ROUTE_ALLOWLIST
    assert len(router.routes) == 2
    assert all(route.include_in_schema is False for route in router.routes)
    assert {method for route in router.routes for method in route.methods} == {
        "GET",
        "POST",
    }


def test_p12_router_is_not_mounted_by_the_current_application(facade_stack):
    create_private_generation_router(facade_stack.facade)
    production_root = (ROOT / "interfaces" / "main.py").read_text(encoding="utf-8")

    assert "create_private_generation_router" not in production_root
    assert "/api/v1/webui/workspaces/{workspace_id}/chapters/" not in production_root


def test_private_http_routes_preserve_facade_status_and_closed_body(facade_stack):
    facade_stack.bind()
    app = FastAPI()
    app.include_router(create_private_generation_router(facade_stack.facade))
    path = "/api/v1/webui/workspaces/ws-1/chapters/chapter-1/generation"

    with TestClient(app) as client:
        status = client.get(path)
        started = client.post(path, json=facade_stack.command())

    assert status.status_code == 200
    assert status.json()["schema"] == "webui-chapter-generation-status/v1"
    assert started.status_code == 201
    assert started.json()["schema"] == "webui-chapter-generation-result/v1"
