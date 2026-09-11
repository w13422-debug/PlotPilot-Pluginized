"""Closed HTTP adapter for the five frozen P0A configuration routes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.plotpilot_plugin_sdk.m4_m5_http_v2 import (
    parse_http_request,
    parse_http_response,
    validate_http_exchange,
)
from backend.plotpilot_plugin_sdk.macro_planning_v2 import (
    parse_model_planning_http_error_v2,
)

from ....configuration import (
    ConfigurationAuthorityError,
    MalformedConfigurationRequestError,
    ModelConfigurationAuthority,
    WorkspacePlanAuthority,
)
from ....configuration.authority import FIXED_ERROR_MESSAGES

CONFIGURATION_ROUTE_IDS = frozenset(
    {
        "model-secret.put",
        "model-profile.revise",
        "workspace-plan.select",
        "project-planning.get",
        "project-planning.start",
    }
)
_OPERATION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_COMMON_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SECRET_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


@dataclass(slots=True)
class ConfigurationHttpAdapter:
    configuration: ModelConfigurationAuthority
    planning: WorkspacePlanAuthority

    def handle(
        self,
        route_id: str,
        request: Any,
        *,
        path_params: Mapping[str, str] | None,
    ) -> tuple[int, dict[str, Any]]:
        if route_id not in CONFIGURATION_ROUTE_IDS:
            raise ValueError("route is not part of the configuration adapter")
        parsed: dict[str, Any] | None = None
        try:
            parsed = parse_http_request(
                route_id, request, path_params=path_params
            )
        except Exception:
            return self._error(
                route_id,
                request,
                path_params,
                MalformedConfigurationRequestError(),
                parsed_request=None,
            )

        try:
            if route_id == "model-secret.put":
                result = self.configuration.put_secret(parsed)
                status = 201 if result["created"] else 200
            elif route_id == "model-profile.revise":
                result = self.configuration.revise_profile(parsed)
                status = 201
            elif route_id == "workspace-plan.select":
                result = self.planning.select_workspace_plan(parsed)
                status = 200
            elif route_id == "project-planning.get":
                result = self.planning.planning_availability(parsed)
                status = 200
            else:
                self.planning.start_planning(parsed)
                raise RuntimeError("P1 planning start unexpectedly returned")
        except ConfigurationAuthorityError as exc:
            return self._error(
                route_id,
                parsed,
                path_params,
                exc,
                parsed_request=parsed,
            )
        except Exception:
            # Arbitrary exception text can contain the local raw value.  It is
            # deliberately discarded at this boundary.
            return self._error(
                route_id,
                parsed,
                path_params,
                MalformedConfigurationRequestError(),
                parsed_request=parsed,
            )

        try:
            _, checked = validate_http_exchange(
                route_id,
                parsed,
                status,
                result,
                path_params=path_params,
            )
        except Exception:
            return self._error(
                route_id,
                parsed,
                path_params,
                MalformedConfigurationRequestError(),
                parsed_request=parsed,
            )
        return status, checked

    dispatch = handle

    @staticmethod
    def _safe_operation_key(request: Any) -> str | None:
        if not isinstance(request, Mapping):
            return None
        value = request.get("operation_key")
        return value if isinstance(value, str) and _OPERATION_KEY.fullmatch(value) else None

    @staticmethod
    def _identity_value(
        field: str, request: Any, path_params: Mapping[str, str] | None
    ) -> str:
        value: Any = None
        if isinstance(path_params, Mapping):
            value = path_params.get(field)
        if value is None and isinstance(request, Mapping):
            value = request.get(field)
        pattern = _SECRET_IDENTITY if field == "secret_id" else _COMMON_IDENTITY
        if isinstance(value, str) and pattern.fullmatch(value):
            return value
        return "invalid"

    def _error(
        self,
        route_id: str,
        request: Any,
        path_params: Mapping[str, str] | None,
        error: ConfigurationAuthorityError,
        *,
        parsed_request: Mapping[str, Any] | None,
    ) -> tuple[int, dict[str, Any]]:
        operation_key = self._safe_operation_key(request)
        if route_id == "model-secret.put":
            response = {
                "schema": "model-secret-http-error/v2",
                "secret_id": self._identity_value(
                    "secret_id", request, path_params
                ),
                "error_code": error.error_code,
                "message": FIXED_ERROR_MESSAGES[error.error_code],
                "retryable": False,
                "operation_key": operation_key,
            }
        elif route_id == "model-profile.revise":
            response = {
                "schema": "model-profile-http-error/v2",
                "profile_id": self._identity_value(
                    "profile_id", request, path_params
                ),
                "error_code": error.error_code,
                "message": FIXED_ERROR_MESSAGES[error.error_code],
                "retryable": False,
                "operation_key": operation_key,
            }
        else:
            response = {
                "schema": "workspace-planning-http-error/v2",
                "workspace_id": self._identity_value(
                    "workspace_id", request, path_params
                ),
                "error_code": error.error_code,
                "message": FIXED_ERROR_MESSAGES[error.error_code],
                "retryable": False,
                "operation_key": operation_key,
            }

        # A fully parsed request admits the frozen exchange validator.  For a
        # malformed/missing trusted path, only the closed error object can be
        # validated because there is intentionally no accepted request side.
        try:
            if parsed_request is not None:
                _, checked = validate_http_exchange(
                    route_id,
                    parsed_request,
                    error.status,
                    response,
                    path_params=path_params,
                )
            else:
                checked = parse_model_planning_http_error_v2(response)
                if path_params is not None:
                    checked = parse_http_response(
                        route_id,
                        error.status,
                        checked,
                        path_params=path_params,
                    )
        except Exception:
            # Exception classification must also remain in the route's closed
            # status matrix.  Never expose validator or authority details.
            fallback = {**response, "error_code": "malformed_request"}
            fallback["message"] = FIXED_ERROR_MESSAGES["malformed_request"]
            try:
                checked = parse_model_planning_http_error_v2(fallback)
            except Exception:
                # All synthesized identities above are contract-valid; this is
                # unreachable unless frozen schemas drift underneath P1.
                raise RuntimeError("configuration error schema is unavailable") from None
            return 400, checked
        return error.status, checked


ModelConfigurationHttpAdapter = ConfigurationHttpAdapter


__all__ = [
    "CONFIGURATION_ROUTE_IDS",
    "ConfigurationHttpAdapter",
    "ModelConfigurationHttpAdapter",
]
