"""Thin Core-only adapter for the frozen v2 Candidate/Publication routes."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.plotpilot_plugin_sdk.m4_m5_http_v2 import (
    parse_http_request,
    parse_http_response,
    validate_http_exchange,
)

from ....candidates.application import (
    CandidateApplication,
    CandidateApplicationError,
    CandidateConflictError,
    CandidateCrossWorkspaceError,
    CandidateNotFoundError,
    CandidateNotPublishableError,
)
from ....publication.application import (
    CrossWorkspaceError,
    IncompletePublicationError,
    OperationKeyReuseError,
    PublicationApplication,
    StaleCasError,
    UnknownReferenceError,
)


@dataclass(slots=True)
class CoreCandidatePublicationAdapter:
    """One dispatcher whose route IDs are constrained by the frozen matrix."""

    candidates: CandidateApplication
    publication: PublicationApplication

    def handle(
        self,
        route_id: str,
        request: Mapping[str, Any],
        *,
        path_identity: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        try:
            parsed = parse_http_request(route_id, request)
            if path_identity is not None:
                for name, value in path_identity.items():
                    if parsed.get(name) != value:
                        raise ValueError(f"path identity {name} does not match body")
        except Exception as exc:  # noqa: BLE001
            return self._error(route_id, 400, "malformed_request", str(exc), request)

        try:
            if route_id == "candidate.list":
                response = self.candidates.list_candidates(parsed)
            elif route_id == "candidate.get":
                response = self.candidates.get_candidate(parsed)
            elif route_id == "candidate.preview":
                response = self.candidates.preview_candidate(parsed)
            elif route_id == "candidate.review":
                response = self.candidates.review(parsed)
            elif route_id == "publication.accept":
                response = self.publication.accept_v2(parsed)
            elif route_id == "story-state.projection-input":
                response = self.candidates.story_state_projection_input(parsed)
            else:
                return self._error(
                    route_id,
                    400,
                    "malformed_request",
                    "route is not part of the Core adapter",
                    parsed,
                )
        except Exception as exc:  # noqa: BLE001
            return self._exception_error(route_id, exc, parsed)

        candidate = None
        if route_id == "publication.accept":
            try:
                candidate = self.candidates.get_candidate(
                    {
                        "schema": "candidate-get-query/v2",
                        "workspace_id": parsed["workspace_id"],
                        "candidate_id": parsed["candidate_id"],
                    }
                )["candidate"]
            except Exception:  # noqa: BLE001
                # PublicationApplication already proved the Result; this extra
                # exchange guard must not turn a committed result into a
                # synthetic adapter failure if a reader observes corruption.
                candidate = None
        try:
            validate_http_exchange(
                route_id, parsed, 200, response, candidate=candidate
            )
        except Exception as exc:  # noqa: BLE001
            return self._error(
                route_id,
                400,
                "malformed_request",
                f"Core adapter result failed contract binding: {exc}",
                parsed,
            )
        return 200, response

    dispatch = handle

    @staticmethod
    def _operation_key(value: Mapping[str, Any]) -> str | None:
        for name in ("operation_key", "publication_operation_key"):
            key = value.get(name)
            if isinstance(key, str):
                return key
        return None

    def _exception_error(
        self, route_id: str, exc: Exception, request: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if isinstance(exc, (CandidateNotFoundError, UnknownReferenceError)):
            return self._error(route_id, 404, "unknown_reference", str(exc), request)
        if isinstance(exc, (CandidateCrossWorkspaceError, CrossWorkspaceError)):
            return self._error(route_id, 404, "cross_workspace", str(exc), request)
        if isinstance(
            exc,
            (CandidateConflictError, OperationKeyReuseError),
        ):
            return self._error(
                route_id, 409, "duplicate_operation", str(exc), request
            )
        if isinstance(exc, StaleCasError):
            return self._error(route_id, 409, "stale_cas", str(exc), request)
        if isinstance(
            exc,
            (CandidateNotPublishableError, IncompletePublicationError),
        ):
            return self._error(
                route_id, 400, "candidate_not_publishable", str(exc), request
            )
        if isinstance(exc, CandidateApplicationError):
            return self._error(route_id, 400, "malformed_request", str(exc), request)
        return self._error(route_id, 400, "malformed_request", str(exc), request)

    def _error(
        self,
        route_id: str,
        status: int,
        code: str,
        message: str,
        request: Mapping[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        result = {
            "schema": "core-http-error/v2",
            "error_code": code,
            "message": message or code,
            "retryable": False,
            "operation_key": self._operation_key(request),
        }
        try:
            return status, parse_http_response(route_id, status, result)
        except Exception:  # noqa: BLE001
            # Every approved Core route admits malformed_request/400.  It is
            # the fail-closed fallback when an internal exception cannot be
            # represented by that route's narrower error registry.
            fallback = {
                **result,
                "error_code": "malformed_request",
            }
            return 400, parse_http_response(route_id, 400, fallback)


CoreAuthorityV2Adapter = CoreCandidatePublicationAdapter

__all__ = ["CoreAuthorityV2Adapter", "CoreCandidatePublicationAdapter"]
