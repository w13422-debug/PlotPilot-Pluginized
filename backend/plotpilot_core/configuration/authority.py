"""Durable local value and P0A model-profile authority.

Raw configuration values cross exactly one persistence boundary: the ``value``
column of ``p1_local_secret_value``.  Replay fingerprints and responses are
redacted before they reach the operation ledger.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from backend.plotpilot_plugin_sdk import (
    canonical_bytes,
    model_profile_revision_hash,
    parse_model_config_v2,
    parse_model_profile_revision_v1,
    validate_model_profile_revision_exchange,
    validate_secret_put_exchange,
)
from backend.plotpilot_plugin_sdk.macro_planning_v2 import HOST_SECRET_REF_RE

from ..domain.entities import utc_now
from ..repositories.authority import CoreAuthorityRepository

MODEL_SECRET_PUT_ROUTE = "model-secret.put"
MODEL_PROFILE_REVISE_ROUTE = "model-profile.revise"

FIXED_ERROR_MESSAGES = MappingProxyType({
    "malformed_request": "Request is malformed.",
    "unknown_reference": "Referenced authority record was not found.",
    "cross_workspace": "Referenced authority record belongs to another Workspace.",
    "stale_cas": "Authority compare-and-swap is stale.",
    "duplicate_operation": "Operation key was reused with different input.",
    "invalid_secret_reference": "Secret reference is not a local opaque reference.",
    "secret_value_rejected": "Secret value was rejected.",
    "generation_conflict": "Active plugin Generation does not match.",
    "planning_unavailable": "Project planning is unavailable.",
})


class ConfigurationAuthorityError(RuntimeError):
    """A closed, fixed-message configuration authority failure."""

    def __init__(self, error_code: str, status: int) -> None:
        self.error_code = error_code
        self.status = status
        super().__init__(FIXED_ERROR_MESSAGES[error_code])


class MalformedConfigurationRequestError(ConfigurationAuthorityError):
    def __init__(self) -> None:
        super().__init__("malformed_request", 400)


class UnknownConfigurationReferenceError(ConfigurationAuthorityError):
    def __init__(self) -> None:
        super().__init__("unknown_reference", 404)


class CrossWorkspaceConfigurationError(ConfigurationAuthorityError):
    def __init__(self) -> None:
        super().__init__("cross_workspace", 404)


class StaleConfigurationCasError(ConfigurationAuthorityError):
    def __init__(self) -> None:
        super().__init__("stale_cas", 409)


class DuplicateConfigurationOperationError(ConfigurationAuthorityError):
    def __init__(self) -> None:
        super().__init__("duplicate_operation", 409)


class InvalidLocalSecretReferenceError(ConfigurationAuthorityError):
    def __init__(self) -> None:
        super().__init__("invalid_secret_reference", 400)


class SecretValueRejectedError(ConfigurationAuthorityError):
    def __init__(self) -> None:
        super().__init__("secret_value_rejected", 400)


class GenerationConfigurationConflictError(ConfigurationAuthorityError):
    def __init__(self) -> None:
        super().__init__("generation_conflict", 409)


class PlanningUnavailableError(ConfigurationAuthorityError):
    def __init__(self) -> None:
        super().__init__("planning_unavailable", 400)


@dataclass(frozen=True, slots=True)
class OperationReplay:
    status: int
    response: dict[str, Any]


def canonical_json(value: Mapping[str, Any]) -> str:
    return canonical_bytes(dict(value)).decode("utf-8")


def request_fingerprint(
    route_id: str,
    path_params: Mapping[str, str],
    request: Mapping[str, Any],
) -> str:
    envelope = {
        "route_id": route_id,
        "path_params": dict(path_params),
        "request": copy.deepcopy(dict(request)),
    }
    return hashlib.sha256(canonical_bytes(envelope)).hexdigest()


def replay_operation(
    connection: sqlite3.Connection,
    *,
    route_id: str,
    operation_key: str,
    fingerprint: str,
) -> OperationReplay | None:
    row = connection.execute(
        "SELECT request_fingerprint,success_status,response_json "
        "FROM p1_configuration_operation WHERE route_id=? AND operation_key=?",
        (route_id, operation_key),
    ).fetchone()
    if row is None:
        return None
    if row["request_fingerprint"] != fingerprint:
        raise DuplicateConfigurationOperationError()
    try:
        response = json.loads(row["response_json"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("stored configuration response is invalid") from exc
    if not isinstance(response, dict) or response.get("idempotent") is not False:
        raise RuntimeError("stored configuration response is invalid")
    replayed = copy.deepcopy(response)
    replayed["idempotent"] = True
    return OperationReplay(int(row["success_status"]), replayed)


def record_operation(
    connection: sqlite3.Connection,
    *,
    route_id: str,
    operation_key: str,
    fingerprint: str,
    value_hash: str | None,
    status: int,
    response: Mapping[str, Any],
    created_at: str,
) -> None:
    connection.execute(
        "INSERT INTO p1_configuration_operation("
        "route_id,operation_key,request_fingerprint,value_hash,success_status,"
        "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (
            route_id,
            operation_key,
            fingerprint,
            value_hash,
            status,
            canonical_json(response),
            created_at,
        ),
    )


def _parse_model_command(
    command: Mapping[str, Any], expected_schema: str
) -> dict[str, Any]:
    try:
        parsed = parse_model_config_v2(command)
    except Exception:
        raise MalformedConfigurationRequestError() from None
    if parsed.get("schema") != expected_schema:
        raise MalformedConfigurationRequestError()
    return parsed


def _local_secret_id(api_key_ref: str) -> str:
    if not isinstance(api_key_ref, str) or HOST_SECRET_REF_RE.fullmatch(api_key_ref) is None:
        raise InvalidLocalSecretReferenceError()
    secret_id = api_key_ref.removeprefix("secret://")
    if not secret_id or "/" in secret_id:
        raise InvalidLocalSecretReferenceError()
    return secret_id


class ModelConfigurationAuthority:
    """Single-database writer for local values and immutable P0A profiles."""

    def __init__(
        self,
        repository: CoreAuthorityRepository,
        *,
        clock: Callable[[], str] = utc_now,
        revision_id_factory: Callable[[], str] | None = None,
    ) -> None:
        repository.ensure_model_configuration_schema()
        self.repository = repository
        self._clock = clock
        self._revision_id_factory = revision_id_factory or (
            lambda: f"model-profile-revision-{uuid.uuid4().hex}"
        )

    def put_secret(self, command: Mapping[str, Any]) -> dict[str, Any]:
        parsed = _parse_model_command(command, "model-secret-put-command/v2")
        raw_value = parsed["value"]
        value_hash = hashlib.sha256(raw_value.encode("utf-8")).hexdigest()
        redacted_request = {
            "schema": parsed["schema"],
            "operation_key": parsed["operation_key"],
            "secret_id": parsed["secret_id"],
            "value_hash": value_hash,
        }
        fingerprint = request_fingerprint(
            MODEL_SECRET_PUT_ROUTE,
            {"secret_id": parsed["secret_id"]},
            redacted_request,
        )
        now = self._clock()
        with self.repository.transaction() as connection:
            replay = replay_operation(
                connection,
                route_id=MODEL_SECRET_PUT_ROUTE,
                operation_key=parsed["operation_key"],
                fingerprint=fingerprint,
            )
            if replay is not None:
                try:
                    _, response = validate_secret_put_exchange(
                        parsed, replay.response
                    )
                except Exception:
                    raise RuntimeError("stored secret PUT response is invalid") from None
                return response

            existing = connection.execute(
                "SELECT revision,created_at FROM p1_local_secret_value "
                "WHERE secret_id=?",
                (parsed["secret_id"],),
            ).fetchone()
            created = existing is None
            if created:
                connection.execute(
                    "INSERT INTO p1_local_secret_value("
                    "secret_id,value,value_hash,revision,created_at,updated_at) "
                    "VALUES(?,?,?,1,?,?)",
                    (parsed["secret_id"], raw_value, value_hash, now, now),
                )
            else:
                connection.execute(
                    "UPDATE p1_local_secret_value SET value=?,value_hash=?,"
                    "revision=revision+1,updated_at=? WHERE secret_id=?",
                    (raw_value, value_hash, now, parsed["secret_id"]),
                )

            response = {
                "schema": "model-secret-put-result/v2",
                "operation_key": parsed["operation_key"],
                "secret_id": parsed["secret_id"],
                "api_key_ref": f"secret://{parsed['secret_id']}",
                "created": created,
                "idempotent": False,
            }
            try:
                _, response = validate_secret_put_exchange(parsed, response)
            except Exception:
                raise RuntimeError(
                    "server-generated secret PUT response is invalid"
                ) from None
            record_operation(
                connection,
                route_id=MODEL_SECRET_PUT_ROUTE,
                operation_key=parsed["operation_key"],
                fingerprint=fingerprint,
                value_hash=value_hash,
                status=201 if created else 200,
                response=response,
                created_at=now,
            )
            return copy.deepcopy(response)

    def resolve_secret_value(self, api_key_ref: str) -> str:
        """Resolve one local opaque reference for a future internal Provider seam."""

        secret_id = _local_secret_id(api_key_ref)
        with self.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT value FROM p1_local_secret_value WHERE secret_id=?",
                (secret_id,),
            ).fetchone()
        if row is None:
            raise UnknownConfigurationReferenceError()
        return str(row["value"])

    def revise_profile(self, command: Mapping[str, Any]) -> dict[str, Any]:
        parsed = _parse_model_command(command, "model-profile-revise-command/v2")
        fingerprint = request_fingerprint(
            MODEL_PROFILE_REVISE_ROUTE,
            {"profile_id": parsed["profile_id"]},
            parsed,
        )
        now = self._clock()
        with self.repository.transaction() as connection:
            replay = replay_operation(
                connection,
                route_id=MODEL_PROFILE_REVISE_ROUTE,
                operation_key=parsed["operation_key"],
                fingerprint=fingerprint,
            )
            if replay is not None:
                if replay.status != 201:
                    raise RuntimeError("stored profile response status is invalid")
                try:
                    _, response = validate_model_profile_revision_exchange(
                        parsed, replay.response
                    )
                except Exception:
                    raise RuntimeError("stored profile response is invalid") from None
                return response

            secret_id = _local_secret_id(parsed["provider"]["api_key_ref"])
            secret = connection.execute(
                "SELECT 1 FROM p1_local_secret_value WHERE secret_id=?",
                (secret_id,),
            ).fetchone()
            if secret is None:
                raise UnknownConfigurationReferenceError()

            tip = connection.execute(
                "SELECT revision_id,revision_number FROM p1_model_profile_revision "
                "WHERE profile_id=? ORDER BY revision_number DESC LIMIT 1",
                (parsed["profile_id"],),
            ).fetchone()
            expected_parent = parsed["expected_parent_revision_id"]
            if expected_parent is not None:
                parent = connection.execute(
                    "SELECT profile_id FROM p1_model_profile_revision "
                    "WHERE revision_id=?",
                    (expected_parent,),
                ).fetchone()
                if parent is None or parent["profile_id"] != parsed["profile_id"]:
                    raise UnknownConfigurationReferenceError()
            actual_parent = None if tip is None else str(tip["revision_id"])
            if actual_parent != expected_parent:
                raise StaleConfigurationCasError()

            revision_number = 1 if tip is None else int(tip["revision_number"]) + 1
            if revision_number > 9_007_199_254_740_991:
                raise StaleConfigurationCasError()
            revision = {
                "schema": "model-profile-revision/v1",
                "profile_id": parsed["profile_id"],
                "revision_id": self._revision_id_factory(),
                "revision_number": revision_number,
                "parent_revision_id": actual_parent,
                "provider": copy.deepcopy(parsed["provider"]),
                "created_at": now,
            }
            revision["revision_hash"] = model_profile_revision_hash(revision)
            try:
                revision = parse_model_profile_revision_v1(revision)
            except Exception:
                raise RuntimeError("server-generated profile revision is invalid") from None
            connection.execute(
                "INSERT INTO p1_model_profile_revision("
                "revision_id,profile_id,revision_number,parent_revision_id,secret_id,"
                "payload_json,revision_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    revision["revision_id"],
                    revision["profile_id"],
                    revision["revision_number"],
                    revision["parent_revision_id"],
                    secret_id,
                    canonical_json(revision),
                    revision["revision_hash"],
                    revision["created_at"],
                ),
            )
            response = {
                "schema": "model-profile-revise-result/v2",
                "operation_key": parsed["operation_key"],
                "profile_id": parsed["profile_id"],
                "revision": revision,
                "idempotent": False,
            }
            try:
                _, response = validate_model_profile_revision_exchange(
                    parsed, response
                )
            except Exception:
                raise RuntimeError("server-generated profile response is invalid") from None
            record_operation(
                connection,
                route_id=MODEL_PROFILE_REVISE_ROUTE,
                operation_key=parsed["operation_key"],
                fingerprint=fingerprint,
                value_hash=None,
                status=201,
                response=response,
                created_at=now,
            )
            return copy.deepcopy(response)

    def get_profile_revision(self, revision_id: str) -> dict[str, Any]:
        with self.repository.read_connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM p1_model_profile_revision "
                "WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
        if row is None:
            raise UnknownConfigurationReferenceError()
        try:
            payload = json.loads(row["payload_json"])
            return parse_model_profile_revision_v1(payload)
        except Exception:
            raise RuntimeError("stored profile revision is invalid") from None


# Descriptive compatibility name for callers that treat the whole slice as
# local configuration rather than only model configuration.
LocalConfigurationAuthority = ModelConfigurationAuthority


__all__ = [
    "ConfigurationAuthorityError",
    "CrossWorkspaceConfigurationError",
    "DuplicateConfigurationOperationError",
    "FIXED_ERROR_MESSAGES",
    "GenerationConfigurationConflictError",
    "InvalidLocalSecretReferenceError",
    "LocalConfigurationAuthority",
    "MalformedConfigurationRequestError",
    "ModelConfigurationAuthority",
    "PlanningUnavailableError",
    "SecretValueRejectedError",
    "StaleConfigurationCasError",
    "UnknownConfigurationReferenceError",
]
