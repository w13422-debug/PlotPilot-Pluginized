"""Pure-Python HTTP/SSE contract adapter for the additive M4/M5 surface.

This is a deterministic fixture/ingress helper, not an HTTP client.  It
reuses the v2 schema and semantic verifiers from :mod:`core_api_v2` and keeps
operation-key replay in memory so tests can exercise ACK-loss semantics
without granting a plugin a Core or network handle.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .canonical import canonical_bytes
from .errors import ContractValidationError
from .core_api_v2 import (
    _cursor,
    parse_candidate_query_result_v2,
    parse_core_authority_v2,
    parse_candidate_review_v2,
    parse_job_event_page_v2,
    parse_job_http_v2,
    parse_job_sse_recovery_v2,
    parse_plugin_api_v2,
    parse_story_state_projection_input_v2,
    parse_v2,
    validate_plugin_lifecycle_v2,
    validate_publication_v2,
)
from .macro_planning_v2 import (
    parse_macro_planning,
    validate_model_profile_revision_exchange,
    validate_project_planning_start_exchange,
    validate_secret_put_exchange,
    validate_workspace_plan_selection,
)


CORE_API_MATRIX_V2 = __import__("json").loads(
    (__import__("pathlib").Path(__file__).resolve().parents[2] / "contracts" / "json-schema" / "core-api-method-matrix.v2.json").read_text(encoding="utf-8")
)
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_ROUTES = {route["route_id"]: route for route in CORE_API_MATRIX_V2["routes"]}
_MACRO_ROUTE_IDS = frozenset(
    {
        "model-secret.put",
        "model-profile.revise",
        "workspace-plan.select",
        "project-planning.get",
        "project-planning.start",
    }
)


def _fail(message: str) -> None:
    raise ContractValidationError(f"v2 HTTP {message}")


def route_for(route_id: str) -> dict[str, Any]:
    route = _ROUTES.get(route_id)
    if route is None:
        _fail(f"unknown route: {route_id}")
    return copy.deepcopy(route)


def _parse_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    schema = value.get("schema")
    if schema in {
        "model-secret-put-command/v2",
        "model-secret-put-result/v2",
        "model-profile-revise-command/v2",
        "model-profile-revise-result/v2",
        "workspace-plan-selection-command/v2",
        "workspace-plan-selection-result/v2",
        "model-secret-http-error/v2",
        "model-profile-http-error/v2",
        "workspace-planning-http-error/v2",
        "project-planning-query/v2",
        "project-planning-availability-result/v2",
        "project-planning-start-command/v2",
        "project-planning-start-result/v2",
    }:
        return parse_macro_planning(value)
    if schema == "story-state-projection-input/v2":
        return parse_story_state_projection_input_v2(value)
    if schema == "candidate-list-query/v2" or schema == "candidate-list-result/v2" or schema == "candidate-get-query/v2" or schema == "candidate-get-result/v2" or schema == "candidate-preview-query/v2" or schema == "candidate-preview-result/v2" or schema == "candidate/v2":
        return parse_candidate_query_result_v2(value)
    if schema in {"candidate-review-query/v2", "candidate-review-command/v2", "candidate-review-result/v2"}:
        return parse_candidate_review_v2(value)
    if schema in {"core-authority-query/v2", "core-authority-result/v2", "publication-command/v2", "publication-result/v2", "core-http-error/v2"}:
        return parse_core_authority_v2(value)
    if schema in {"job-event-page-result/v2"}:
        return parse_job_event_page_v2(value)
    if schema == "job-sse-recovery-result/v2":
        return parse_job_sse_recovery_v2(value)
    if schema == "job-snapshot-result/v2":
        parsed = parse_job_http_v2(value)
        # The generic parser validates the closed union.  The nested snapshot
        # is additionally checked by the recovery-aware parser when present.
        from .core_api_v2 import parse_job_snapshot_v2

        parse_job_snapshot_v2(parsed["snapshot"])
        return parsed
    if schema == "job-snapshot-query/v2":
        return parse_job_http_v2(value)
    if schema in {"job-list-query/v2", "job-list-result/v2", "job-start-command/v2", "job-control-command/v2", "job-command-result/v2", "job-event-page-query/v2", "job-sse-recovery-query/v2", "job-http-error/v2"}:
        return parse_job_http_v2(value)
    if schema in {"plugin-discovery-query/v2", "plugin-discovery-result/v2", "plugin-lifecycle-command/v2", "plugin-lifecycle-result/v2", "plugin-http-error/v2"}:
        return parse_plugin_api_v2(value)
    _fail(f"unsupported payload schema: {schema!r}")


def _operation_key(value: Mapping[str, Any]) -> str | None:
    for key in ("operation_key", "publication_operation_key"):
        candidate = value.get(key)
        if candidate is not None:
            return candidate if isinstance(candidate, str) else None
    return None


def _route_error_schema(route: Mapping[str, Any]) -> str:
    declared = route.get("error_schema")
    if isinstance(declared, str):
        return declared
    route_id = str(route["route_id"])
    if route_id.startswith("job."):
        return "job-http-error/v2"
    if route_id.startswith("plugin."):
        return "plugin-http-error/v2"
    return "core-http-error/v2"


def _validate_route_variant(route: Mapping[str, Any], parsed: Mapping[str, Any]) -> None:
    route_id = str(route["route_id"])
    if route_id.startswith("plugin.") and parsed.get("schema") in {"plugin-lifecycle-command/v2", "plugin-lifecycle-result/v2"}:
        expected_action = route_id.removeprefix("plugin.")
        if parsed.get("action") != expected_action:
            _fail(f"plugin lifecycle action is not bound to {route_id}")
    if route_id.startswith("job.") and parsed.get("schema") in {"job-control-command/v2", "job-command-result/v2"}:
        expected_command = route_id.removeprefix("job.")
        if parsed.get("command") != expected_command:
            _fail(f"Job command is not bound to {route_id}")


def _validate_plugin_discovery_cursor(parsed: Mapping[str, Any]) -> None:
    if parsed.get("schema") == "plugin-discovery-query/v2":
        _cursor(parsed["cursor"], "core")
    elif parsed.get("schema") == "plugin-discovery-result/v2":
        _cursor(parsed["next_cursor"], "core")


def _trusted_path_params(
    route_id: str,
    route: Mapping[str, Any],
    path_params: Mapping[str, str] | None,
) -> dict[str, str] | None:
    required = route_id in _MACRO_ROUTE_IDS
    if path_params is None:
        if required:
            _fail(f"{route_id} requires trusted parsed path_params")
        return None
    if not isinstance(path_params, Mapping):
        _fail("path_params must be an object")
    expected_names = list(route.get("path_identity", []))
    if set(path_params) != set(expected_names) or len(path_params) != len(expected_names):
        _fail(f"path_params are not the exact identity set for {route_id}")
    trusted: dict[str, str] = {}
    for name in expected_names:
        value = path_params.get(name)
        if not isinstance(value, str) or not value:
            _fail(f"path_params {name} must be a non-empty string")
        trusted[name] = value
    return trusted


def _bind_trusted_path(
    route_id: str,
    parsed: Mapping[str, Any],
    path_params: Mapping[str, str] | None,
    *,
    direction: str,
) -> None:
    if path_params is None:
        return
    for name, trusted_value in path_params.items():
        represented = _response_bound_id(parsed, name)
        if represented != trusted_value:
            _fail(f"{route_id} {direction} {name} does not match trusted path_params")


def _request_identity_bytes(
    request: Mapping[str, Any],
    path_params: Mapping[str, str] | None,
) -> bytes:
    if path_params is None:
        return canonical_bytes(request)
    return canonical_bytes({"path_params": dict(path_params), "request": dict(request)})


def parse_http_request(
    route_id: str,
    request: Mapping[str, Any],
    *,
    path_params: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    route = _ROUTES.get(route_id)
    if route is None:
        _fail(f"unknown route: {route_id}")
    if not isinstance(request, Mapping):
        _fail("request must be an object")
    if request.get("schema") != route["request_schema"]:
        _fail(f"request schema is not bound to {route_id}")
    trusted_path = _trusted_path_params(route_id, route, path_params)
    for placeholder in _PLACEHOLDER.findall(route["path_template"]):
        value = request.get(placeholder)
        if not isinstance(value, str) or not value:
            _fail(f"path identity {placeholder} is absent from {route_id}")
    if route.get("operation_key_required") and _operation_key(request) is None:
        _fail(f"{route_id} requires an operation key")
    parsed = _parse_payload(request)
    _bind_trusted_path(route_id, parsed, trusted_path, direction="request")
    _validate_route_variant(route, parsed)
    _validate_plugin_discovery_cursor(parsed)
    if parsed.get("schema") == "plugin-lifecycle-command/v2":
        validate_plugin_lifecycle_v2(parsed)
    cursor_domain = route.get("cursor_domain")
    if cursor_domain:
        cursor_fields = ("cursor", "after_cursor", "last_event_id")
        for field_name in cursor_fields:
            if field_name in parsed and parsed[field_name] is not None:
                job_id = parsed.get("job_id") if cursor_domain == "job" else None
                _cursor(parsed[field_name], cursor_domain, job_id=job_id)
        if parsed.get("requested_cursor_domain") is not None and parsed["requested_cursor_domain"] != cursor_domain:
            _fail("request cursor domain is not bound to the route")
    return parsed


def parse_http_response(
    route_id: str,
    status: int,
    response: Mapping[str, Any],
    *,
    path_params: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    route = _ROUTES.get(route_id)
    if route is None:
        _fail(f"unknown route: {route_id}")
    if not isinstance(status, int) or isinstance(status, bool):
        _fail("status must be an integer")
    trusted_path = _trusted_path_params(route_id, route, path_params)
    parsed = _parse_payload(response)
    _bind_trusted_path(route_id, parsed, trusted_path, direction="response")
    _validate_route_variant(route, parsed)
    _validate_plugin_discovery_cursor(parsed)
    if status in route["success_statuses"]:
        if parsed.get("schema") != route["result_schema"]:
            _fail(f"success response is not bound to {route_id}")
        return parsed
    failures = {int(item["status"]): set(item["error_codes"]) for item in route.get("failure_statuses", [])}
    allowed = failures.get(status)
    if allowed is None or parsed.get("schema") != _route_error_schema(route):
        _fail(f"status {status} is not declared by {route_id}")
    if parsed.get("error_code") not in allowed:
        _fail(f"error code is not allowed for {route_id}/{status}")
    return parsed


def _response_bound_id(response: Mapping[str, Any], field_name: str) -> Any:
    """Return the identity represented by a route response.

    Candidate GET is the only v2 response whose Candidate identity is nested;
    keeping this mapping explicit prevents lineage/source references from being
    mistaken for the request's primary identity.
    """

    if field_name in response:
        return response[field_name]
    if field_name == "candidate_id" and isinstance(response.get("candidate"), Mapping):
        return response["candidate"].get("candidate_id")
    return None


def _validate_exchange_binding(
    route_id: str,
    route: Mapping[str, Any],
    request: Mapping[str, Any],
    status: int,
    response: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any] | None = None,
) -> None:
    """Bind one parsed HTTP request to its parsed response.

    Schema validity is necessary but not sufficient at this boundary: a
    closed, independently-valid response can still belong to another Job,
    Candidate, Plugin or operation.  The route matrix is the source of truth
    for which identities must be paired here.
    """

    if "workspace_id" in request and "workspace_id" in response and response["workspace_id"] != request["workspace_id"]:
        _fail(f"{route_id} response crosses Workspace identity")

    for field_name in ("candidate_id", "job_id", "plugin_id", "profile_id", "secret_id"):
        request_value = request.get(field_name)
        if request_value is None:
            continue
        response_value = _response_bound_id(response, field_name)
        if response_value is not None and response_value != request_value:
            _fail(f"{route_id} response is not bound to request {field_name}")
        if route_id in {"candidate.get", "candidate.preview", "candidate.review", "publication.accept"} and response_value != request_value:
            _fail(f"{route_id} response is missing request {field_name} binding")
        if route_id.startswith("job.") and response_value != request_value:
            _fail(f"{route_id} response is missing request Job binding")
        if route_id.startswith("plugin.") and response_value != request_value:
            _fail(f"{route_id} response is missing request Plugin binding")

    for field_name in ("action", "command"):
        if field_name in request and field_name in response and response[field_name] != request[field_name]:
            _fail(f"{route_id} response {field_name} does not match request")

    if route.get("operation_key_required") and status in route["success_statuses"]:
        request_key = _operation_key(request)
        response_key = _operation_key(response)
        if request_key is None or response_key != request_key:
            _fail(f"{route_id} response operation key does not match request")
    elif route.get("operation_key_required"):
        request_key = _operation_key(request)
        response_key = _operation_key(response)
        if response_key is not None and response_key != request_key:
            _fail(f"{route_id} error operation key does not match request")

    if route_id.startswith("plugin.") and response.get("schema") == "plugin-lifecycle-result/v2":
        target_generation = request.get("target_generation_id")
        if target_generation is not None and response.get("generation_id") != target_generation:
            _fail(f"{route_id} response generation does not match requested target")

    if route_id == "publication.accept" and status in route["success_statuses"]:
        validate_publication_v2(
            request,
            response,
            candidate=candidate,
            expected_workspace_id=request.get("workspace_id"),
        )

    if route_id == "model-secret.put":
        validate_secret_put_exchange(request, response)
    elif route_id == "model-profile.revise" and status in route["success_statuses"]:
        validate_model_profile_revision_exchange(request, response)
    elif route_id == "workspace-plan.select" and status in route["success_statuses"]:
        validate_workspace_plan_selection(request, response)
    elif route_id == "project-planning.start" and status in route["success_statuses"]:
        validate_project_planning_start_exchange(request, response)


def validate_http_exchange(
    route_id: str,
    request: Mapping[str, Any],
    status: int,
    response: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any] | None = None,
    path_params: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and bind a complete v2 HTTP exchange."""

    parsed_request = parse_http_request(route_id, request, path_params=path_params)
    parsed_response = parse_http_response(route_id, status, response, path_params=path_params)
    route = _ROUTES[route_id]
    _validate_exchange_binding(route_id, route, parsed_request, status, parsed_response, candidate=candidate)
    return parsed_request, parsed_response


def validate_cursor(value: str | None, domain: str, *, job_id: str | None = None) -> None:
    _cursor(value, domain, job_id=job_id)


@dataclass
class OperationKeyLedgerV2:
    """Small deterministic replay ledger for one HTTP fixture boundary."""

    _entries: dict[tuple[str, str], tuple[bytes, int, dict[str, Any]]] = field(default_factory=dict)

    def record(
        self,
        route_id: str,
        request: Mapping[str, Any],
        status: int,
        response: Mapping[str, Any],
        *,
        candidate: Mapping[str, Any] | None = None,
        path_params: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any], bool]:
        parsed_request, parsed_response = validate_http_exchange(
            route_id,
            request,
            status,
            response,
            candidate=candidate,
            path_params=path_params,
        )
        operation_key = _operation_key(parsed_request)
        if operation_key is None:
            _fail("operation replay requires an operation key")
        identity = (route_id, operation_key)
        payload = _request_identity_bytes(parsed_request, path_params)
        existing = self._entries.get(identity)
        if existing is not None:
            if existing[0] != payload:
                _fail("same operation key was reused with a different payload")
            if existing[1] != status or canonical_bytes(existing[2]) != canonical_bytes(parsed_response):
                _fail("same operation key was reused with a different response")
            return existing[1], copy.deepcopy(existing[2]), True
        self._entries[identity] = (payload, status, copy.deepcopy(parsed_response))
        return status, copy.deepcopy(parsed_response), False

    def replay(
        self,
        route_id: str,
        request: Mapping[str, Any],
        *,
        path_params: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any], bool]:
        parsed_request = parse_http_request(route_id, request, path_params=path_params)
        operation_key = _operation_key(parsed_request)
        if operation_key is None or (route_id, operation_key) not in self._entries:
            _fail("operation key has no recorded response")
        payload = _request_identity_bytes(parsed_request, path_params)
        expected, status, response = self._entries[(route_id, operation_key)]
        if expected != payload:
            _fail("same operation key was reused with a different payload")
        return status, copy.deepcopy(response), True


@dataclass
class HttpSSEFixtureV2:
    """Frozen route fixture with independent request/response validation."""

    exchanges: dict[str, list[tuple[bytes, int, dict[str, Any]]]] = field(default_factory=dict)

    def add(
        self,
        route_id: str,
        request: Mapping[str, Any],
        status: int,
        response: Mapping[str, Any],
        *,
        candidate: Mapping[str, Any] | None = None,
        path_params: Mapping[str, str] | None = None,
    ) -> None:
        parsed_request, parsed_response = validate_http_exchange(
            route_id,
            request,
            status,
            response,
            candidate=candidate,
            path_params=path_params,
        )
        request_bytes = _request_identity_bytes(parsed_request, path_params)
        entries = self.exchanges.setdefault(route_id, [])
        if any(item[0] == request_bytes for item in entries):
            _fail(f"duplicate frozen request: {route_id}")
        entries.append((request_bytes, status, parsed_response))

    def request(
        self,
        route_id: str,
        request: Mapping[str, Any],
        *,
        path_params: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        parsed = parse_http_request(route_id, request, path_params=path_params)
        request_bytes = _request_identity_bytes(parsed, path_params)
        for expected, status, response in self.exchanges.get(route_id, []):
            if expected == request_bytes:
                return status, copy.deepcopy(response)
        _fail(f"fixture has no response for {route_id}")


# The shorter name is convenient for consumers and keeps the v2 module
# compatible with the existing v1 CoreHttpContractFixture naming convention.
CoreHttpContractFixtureV2 = HttpSSEFixtureV2


__all__ = [
    "CORE_API_MATRIX_V2",
    "CoreHttpContractFixtureV2",
    "HttpSSEFixtureV2",
    "OperationKeyLedgerV2",
    "route_for",
    "parse_http_request",
    "parse_http_response",
    "validate_http_exchange",
    "validate_cursor",
]
