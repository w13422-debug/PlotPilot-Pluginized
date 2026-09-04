"""FastAPI mounting helper for the frozen v2 Job surface."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

JOB_API_PREFIX = "/api/v2/jobs"
ROUTE_ALLOWLIST = (
    {"method": "GET", "path": "/api/v2/jobs/{workspace_id}", "route_id": "job.list"},
    {
        "method": "GET",
        "path": "/api/v2/jobs/{workspace_id}/{job_id}",
        "route_id": "job.get",
    },
    {
        "method": "POST",
        "path": "/api/v2/jobs/{workspace_id}/start",
        "route_id": "job.start",
    },
    {
        "method": "POST",
        "path": "/api/v2/jobs/{workspace_id}/{job_id}/pause",
        "route_id": "job.pause",
    },
    {
        "method": "POST",
        "path": "/api/v2/jobs/{workspace_id}/{job_id}/resume",
        "route_id": "job.resume",
    },
    {
        "method": "POST",
        "path": "/api/v2/jobs/{workspace_id}/{job_id}/cancel",
        "route_id": "job.cancel",
    },
    {
        "method": "GET",
        "path": "/api/v2/jobs/{workspace_id}/{job_id}/events",
        "route_id": "job.events",
    },
    {
        "method": "GET",
        "path": "/api/v2/jobs/{workspace_id}/{job_id}/events/stream",
        "route_id": "job.sse-recovery",
    },
)


class JobHttpAdapter(Protocol):
    def handle(
        self,
        route_id: str,
        request: Mapping[str, Any],
        *,
        path_identity: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]: ...


def build_job_router(adapter: JobHttpAdapter) -> Any:
    """Return exactly the eight approved Job v2 routes and no v1 route."""

    from fastapi import APIRouter, Body, Query
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix=JOB_API_PREFIX, tags=["jobs-v2"])

    def response(result: tuple[int, dict[str, Any]]) -> JSONResponse:
        status, payload = result
        return JSONResponse(status_code=status, content=payload)

    @router.get("/{workspace_id}", name="job.list")
    def list_jobs(
        workspace_id: str,
        schema: str = Query("job-list-query/v2"),
        cursor: str | None = Query(None),
        limit: int = Query(50),
        state: str | None = Query(None),
    ) -> JSONResponse:
        return response(
            adapter.handle(
                "job.list",
                {
                    "schema": schema,
                    "workspace_id": workspace_id,
                    "cursor": cursor,
                    "limit": limit,
                    "state": state,
                },
                path_identity={"workspace_id": workspace_id},
            )
        )

    @router.get("/{workspace_id}/{job_id}", name="job.get")
    def get_job(
        workspace_id: str,
        job_id: str,
        schema: str = Query("job-snapshot-query/v2"),
    ) -> JSONResponse:
        return response(
            adapter.handle(
                "job.get",
                {"schema": schema, "workspace_id": workspace_id, "job_id": job_id},
                path_identity={"workspace_id": workspace_id, "job_id": job_id},
            )
        )

    @router.post("/{workspace_id}/start", name="job.start")
    def start_job(
        workspace_id: str,
        command: dict[str, Any] = Body(...),  # noqa: B008
    ) -> JSONResponse:
        return response(
            adapter.handle(
                "job.start",
                command,
                path_identity={"workspace_id": workspace_id},
            )
        )

    def control(
        route_id: str,
        workspace_id: str,
        job_id: str,
        command: dict[str, Any],
    ) -> JSONResponse:
        return response(
            adapter.handle(
                route_id,
                command,
                path_identity={"workspace_id": workspace_id, "job_id": job_id},
            )
        )

    @router.post("/{workspace_id}/{job_id}/pause", name="job.pause")
    def pause_job(
        workspace_id: str,
        job_id: str,
        command: dict[str, Any] = Body(...),  # noqa: B008
    ) -> JSONResponse:
        return control("job.pause", workspace_id, job_id, command)

    @router.post("/{workspace_id}/{job_id}/resume", name="job.resume")
    def resume_job(
        workspace_id: str,
        job_id: str,
        command: dict[str, Any] = Body(...),  # noqa: B008
    ) -> JSONResponse:
        return control("job.resume", workspace_id, job_id, command)

    @router.post("/{workspace_id}/{job_id}/cancel", name="job.cancel")
    def cancel_job(
        workspace_id: str,
        job_id: str,
        command: dict[str, Any] = Body(...),  # noqa: B008
    ) -> JSONResponse:
        return control("job.cancel", workspace_id, job_id, command)

    @router.get("/{workspace_id}/{job_id}/events", name="job.events")
    def list_job_events(
        workspace_id: str,
        job_id: str,
        schema: str = Query("job-event-page-query/v2"),
        after_cursor: str | None = Query(None),
        after_job_event_seq: int = Query(0),
        limit: int = Query(50),
    ) -> JSONResponse:
        cursor = after_cursor or f"job/{job_id}/{after_job_event_seq}"
        return response(
            adapter.handle(
                "job.events",
                {
                    "schema": schema,
                    "workspace_id": workspace_id,
                    "job_id": job_id,
                    "after_cursor": cursor,
                    "after_job_event_seq": after_job_event_seq,
                    "limit": limit,
                },
                path_identity={"workspace_id": workspace_id, "job_id": job_id},
            )
        )

    @router.get("/{workspace_id}/{job_id}/events/stream", name="job.sse-recovery")
    def recover_job_events(
        workspace_id: str,
        job_id: str,
        schema: str = Query("job-sse-recovery-query/v2"),
        after_seq: int = Query(0),
        last_event_id: str | None = Query(None),
        requested_cursor_domain: str = Query("job"),
    ) -> JSONResponse:
        cursor = last_event_id or f"job/{job_id}/{after_seq}"
        return response(
            adapter.handle(
                "job.sse-recovery",
                {
                    "schema": schema,
                    "workspace_id": workspace_id,
                    "job_id": job_id,
                    "after_seq": after_seq,
                    "last_event_id": cursor,
                    "requested_cursor_domain": requested_cursor_domain,
                },
                path_identity={"workspace_id": workspace_id, "job_id": job_id},
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


create_job_router = build_job_router

__all__ = [
    "JOB_API_PREFIX",
    "ROUTE_ALLOWLIST",
    "JobHttpAdapter",
    "build_job_router",
    "create_job_router",
    "route_inventory",
]
