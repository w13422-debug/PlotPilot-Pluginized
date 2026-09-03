"""FastAPI mounting helper for only the frozen v2 Core authority surface."""
from __future__ import annotations

from typing import Any

from .candidate_publication import CoreCandidatePublicationAdapter


def build_core_router(adapter: CoreCandidatePublicationAdapter) -> Any:
    """Return the six approved Core v2 routes and nothing from v1."""
    from fastapi import APIRouter, Body, Query
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/api/v2/core", tags=["core-v2"])

    def response(result: tuple[int, dict[str, Any]]) -> JSONResponse:
        status, payload = result
        return JSONResponse(status_code=status, content=payload)

    @router.get("/workspaces/{workspace_id}/candidates")
    def list_candidates(
        workspace_id: str,
        schema: str = Query("candidate-list-query/v2"),
        cursor: str | None = Query(None),
        limit: int = Query(50),
    ) -> JSONResponse:
        return response(
            adapter.handle(
                "candidate.list",
                {
                    "schema": schema,
                    "workspace_id": workspace_id,
                    "cursor": cursor,
                    "limit": limit,
                },
                path_identity={"workspace_id": workspace_id},
            )
        )

    @router.get("/workspaces/{workspace_id}/candidates/{candidate_id}")
    def get_candidate(
        workspace_id: str,
        candidate_id: str,
        schema: str = Query("candidate-get-query/v2"),
    ) -> JSONResponse:
        return response(
            adapter.handle(
                "candidate.get",
                {
                    "schema": schema,
                    "workspace_id": workspace_id,
                    "candidate_id": candidate_id,
                },
                path_identity={
                    "workspace_id": workspace_id,
                    "candidate_id": candidate_id,
                },
            )
        )

    @router.post("/workspaces/{workspace_id}/candidates/{candidate_id}/review")
    def review_candidate(
        workspace_id: str,
        candidate_id: str,
        command: dict[str, Any] = Body(...),  # noqa: B008
    ) -> JSONResponse:
        return response(
            adapter.handle(
                "candidate.review",
                command,
                path_identity={
                    "workspace_id": workspace_id,
                    "candidate_id": candidate_id,
                },
            )
        )

    @router.get("/workspaces/{workspace_id}/candidates/{candidate_id}/preview")
    def preview_candidate(
        workspace_id: str,
        candidate_id: str,
        schema: str = Query("candidate-preview-query/v2"),
        offset: int = Query(0),
        length: int = Query(65536),
    ) -> JSONResponse:
        return response(
            adapter.handle(
                "candidate.preview",
                {
                    "schema": schema,
                    "workspace_id": workspace_id,
                    "candidate_id": candidate_id,
                    "offset": offset,
                    "length": length,
                },
                path_identity={
                    "workspace_id": workspace_id,
                    "candidate_id": candidate_id,
                },
            )
        )

    @router.post("/workspaces/{workspace_id}/publications:accept")
    def accept_publication(
        workspace_id: str, command: dict[str, Any] = Body(...)  # noqa: B008
    ) -> JSONResponse:
        return response(
            adapter.handle(
                "publication.accept",
                command,
                path_identity={"workspace_id": workspace_id},
            )
        )

    @router.get("/workspaces/{workspace_id}/story-state/projection-input")
    def story_state_projection_input(
        workspace_id: str,
        entity_kind: str = Query(...),
        entity_id: str = Query(...),
        include_assets: bool = Query(True),
        schema: str = Query("core-authority-query/v2"),
    ) -> JSONResponse:
        return response(
            adapter.handle(
                "story-state.projection-input",
                {
                    "schema": schema,
                    "workspace_id": workspace_id,
                    "entity_kind": entity_kind,
                    "entity_id": entity_id,
                    "include_assets": include_assets,
                },
                path_identity={"workspace_id": workspace_id},
            )
        )

    return router


create_core_router = build_core_router

__all__ = ["build_core_router", "create_core_router"]
