"""FastAPI mount for exactly the five P0A model/planning routes."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from .adapter import ConfigurationHttpAdapter

CONFIGURATION_API_PREFIX = "/api/v2/core"
ROUTE_ALLOWLIST = (
    {
        "method": "PUT",
        "path": "/api/v2/core/secrets/{secret_id}",
        "route_id": "model-secret.put",
    },
    {
        "method": "POST",
        "path": "/api/v2/core/model-profiles/{profile_id}/revisions",
        "route_id": "model-profile.revise",
    },
    {
        "method": "POST",
        "path": "/api/v2/core/workspaces/{workspace_id}/plans:select",
        "route_id": "workspace-plan.select",
    },
    {
        "method": "GET",
        "path": "/api/v2/core/workspaces/{workspace_id}/project-planning",
        "route_id": "project-planning.get",
    },
    {
        "method": "POST",
        "path": "/api/v2/core/workspaces/{workspace_id}/project-planning",
        "route_id": "project-planning.start",
    },
)


def build_configuration_router(adapter: ConfigurationHttpAdapter) -> Any:
    from fastapi import APIRouter, Query
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix=CONFIGURATION_API_PREFIX, tags=["configuration-v2"])

    def response(result: tuple[int, dict[str, Any]]) -> JSONResponse:
        status, payload = result
        return JSONResponse(status_code=status, content=payload)

    async def json_body(request: Request) -> Any:
        try:
            return await request.json()
        except Exception:
            return None

    @router.put("/secrets/{secret_id}", name="model-secret.put")
    async def put_secret(
        secret_id: str,
        request: Request,
    ) -> JSONResponse:
        command = await json_body(request)
        return response(
            adapter.handle(
                "model-secret.put",
                command,
                path_params={"secret_id": secret_id},
            )
        )

    @router.post(
        "/model-profiles/{profile_id}/revisions", name="model-profile.revise"
    )
    async def revise_profile(
        profile_id: str,
        request: Request,
    ) -> JSONResponse:
        command = await json_body(request)
        return response(
            adapter.handle(
                "model-profile.revise",
                command,
                path_params={"profile_id": profile_id},
            )
        )

    @router.post(
        "/workspaces/{workspace_id}/plans:select", name="workspace-plan.select"
    )
    async def select_workspace_plan(
        workspace_id: str,
        request: Request,
    ) -> JSONResponse:
        command = await json_body(request)
        return response(
            adapter.handle(
                "workspace-plan.select",
                command,
                path_params={"workspace_id": workspace_id},
            )
        )

    @router.get(
        "/workspaces/{workspace_id}/project-planning",
        name="project-planning.get",
    )
    def get_project_planning(
        workspace_id: str,
        schema: str = Query("project-planning-query/v2"),
    ) -> JSONResponse:
        return response(
            adapter.handle(
                "project-planning.get",
                {"schema": schema, "workspace_id": workspace_id},
                path_params={"workspace_id": workspace_id},
            )
        )

    @router.post(
        "/workspaces/{workspace_id}/project-planning",
        name="project-planning.start",
    )
    async def start_project_planning(
        workspace_id: str,
        request: Request,
    ) -> JSONResponse:
        command = await json_body(request)
        return response(
            adapter.handle(
                "project-planning.start",
                command,
                path_params={"workspace_id": workspace_id},
            )
        )

    return router


def route_inventory(router: Any) -> tuple[dict[str, str], ...]:
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


create_configuration_router = build_configuration_router


__all__ = [
    "CONFIGURATION_API_PREFIX",
    "ROUTE_ALLOWLIST",
    "build_configuration_router",
    "create_configuration_router",
    "route_inventory",
]
