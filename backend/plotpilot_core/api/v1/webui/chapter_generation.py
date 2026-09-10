"""FastAPI router for the unmounted private chapter-generation facade."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ....webui import PrivateChapterGenerationFacade

PRIVATE_GENERATION_PREFIX = "/api/v1/webui"
PRIVATE_GENERATION_ROUTE_ALLOWLIST = (
    {
        "method": "GET",
        "path": (
            "/api/v1/webui/workspaces/{workspace_id}/chapters/"
            "{chapter_document_id}/generation"
        ),
        "route_id": "webui.chapter-generation.get",
    },
    {
        "method": "POST",
        "path": (
            "/api/v1/webui/workspaces/{workspace_id}/chapters/"
            "{chapter_document_id}/generation"
        ),
        "route_id": "webui.chapter-generation.start",
    },
)


def create_private_generation_router(
    facade: PrivateChapterGenerationFacade,
) -> APIRouter:
    if not isinstance(facade, PrivateChapterGenerationFacade):
        raise TypeError("facade must be PrivateChapterGenerationFacade")
    router = APIRouter(prefix=PRIVATE_GENERATION_PREFIX, tags=["webui-private"])

    def response(result: tuple[int, Mapping[str, Any]]) -> JSONResponse:
        status, payload = result
        return JSONResponse(status_code=status, content=dict(payload))

    path = "/workspaces/{workspace_id}/chapters/{chapter_document_id}/generation"

    @router.get(
        path,
        name="webui.chapter-generation.get",
        include_in_schema=False,
    )
    def get_generation(workspace_id: str, chapter_document_id: str) -> JSONResponse:
        return response(facade.get(workspace_id, chapter_document_id))

    @router.post(
        path,
        name="webui.chapter-generation.start",
        include_in_schema=False,
    )
    def start_generation(
        workspace_id: str,
        chapter_document_id: str,
        command: dict[str, Any] = Body(...),  # noqa: B008
    ) -> JSONResponse:
        return response(facade.start(workspace_id, chapter_document_id, command))

    if route_inventory(router) != PRIVATE_GENERATION_ROUTE_ALLOWLIST:
        raise RuntimeError("private generation route inventory drifted")
    return router


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
    return tuple(sorted(inventory, key=lambda item: item["method"]))


__all__ = [
    "PRIVATE_GENERATION_PREFIX",
    "PRIVATE_GENERATION_ROUTE_ALLOWLIST",
    "create_private_generation_router",
    "route_inventory",
]
