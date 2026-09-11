"""Validation-only contracts for host configuration and Project planning.

The module deliberately owns no HTTP client, runtime handler, persistence or
secret resolver.  JSON Schema closes every wire object; the checks here bind
hashes, CAS facts, trusted identities and value-aware secret redaction that a
shape-only validator cannot express.
"""
from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from .canonical import hash_jcs
from .errors import ContractValidationError
from .verifier import assert_valid


MODEL_CONFIG_CONTRACT = "model-config-command-query-v2"
MODEL_PROFILE_CONTRACT = "model-profile-revision-v1"
MODEL_HTTP_ERROR_CONTRACT = "model-planning-http-error-v2"
PROJECT_PLANNING_CONTRACT = "project-planning-command-query-v2"
PLANNER_RUNTIME_INPUT_CONTRACT = "project-planner-runtime-input-v2"
PLANNER_MODEL_OUTPUT_CONTRACT = "project-planner-model-output-v1"

MODEL_PROFILE_SCHEMA = "model-profile-revision/v1"
PLANNER_RUNTIME_INPUT_SCHEMA = "project-planner-runtime-input/v2"
PLANNER_MODEL_OUTPUT_SCHEMA = "project-planner-model-output/v1"
JSON_MAX_SAFE_INTEGER = 9_007_199_254_740_991
WIRE_WHITESPACE_CODEPOINTS = tuple(
    [*range(0x0009, 0x000E)]
    + [*range(0x001C, 0x0021)]
    + [
        0x0085,
        0x00A0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    ]
)
_WIRE_WHITESPACE_CHARACTERS = frozenset(chr(codepoint) for codepoint in WIRE_WHITESPACE_CODEPOINTS)

HOST_SECRET_REF_RE = re.compile(
    r"^secret://[A-Za-z0-9](?:[A-Za-z0-9._~-]{0,127})"
    r"(?:/[A-Za-z0-9](?:[A-Za-z0-9._~-]{0,127}))*$"
)
HOST_SECRET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")

_SCHEMA_TO_CONTRACT = {
    "model-secret-put-command/v2": MODEL_CONFIG_CONTRACT,
    "model-secret-put-result/v2": MODEL_CONFIG_CONTRACT,
    "model-profile-revise-command/v2": MODEL_CONFIG_CONTRACT,
    "model-profile-revise-result/v2": MODEL_CONFIG_CONTRACT,
    "workspace-plan-selection-command/v2": MODEL_CONFIG_CONTRACT,
    "workspace-plan-selection-result/v2": MODEL_CONFIG_CONTRACT,
    MODEL_PROFILE_SCHEMA: MODEL_PROFILE_CONTRACT,
    "model-secret-http-error/v2": MODEL_HTTP_ERROR_CONTRACT,
    "model-profile-http-error/v2": MODEL_HTTP_ERROR_CONTRACT,
    "workspace-planning-http-error/v2": MODEL_HTTP_ERROR_CONTRACT,
    "project-planning-query/v2": PROJECT_PLANNING_CONTRACT,
    "project-planning-availability-result/v2": PROJECT_PLANNING_CONTRACT,
    "project-planning-start-command/v2": PROJECT_PLANNING_CONTRACT,
    "project-planning-start-result/v2": PROJECT_PLANNING_CONTRACT,
    PLANNER_RUNTIME_INPUT_SCHEMA: PLANNER_RUNTIME_INPUT_CONTRACT,
    PLANNER_MODEL_OUTPUT_SCHEMA: PLANNER_MODEL_OUTPUT_CONTRACT,
}

_MODEL_CONFIG_SCHEMAS = frozenset(
    schema for schema, contract in _SCHEMA_TO_CONTRACT.items() if contract == MODEL_CONFIG_CONTRACT
)
_PROJECT_PLANNING_SCHEMAS = frozenset(
    schema for schema, contract in _SCHEMA_TO_CONTRACT.items() if contract == PROJECT_PLANNING_CONTRACT
)
_ERROR_SCHEMAS = frozenset(
    schema for schema, contract in _SCHEMA_TO_CONTRACT.items() if contract == MODEL_HTTP_ERROR_CONTRACT
)
_READY_REASON = "ready"
_MISSING = object()
_MODEL_OPTION_INTEGER_BOUNDS = (
    ("max_output_tokens", 1, 10_000_000),
    ("timeout_seconds", 1, 86_400),
    ("max_retries", 0, 16),
)


def _fail(message: str, *, path: str | None = None) -> None:
    raise ContractValidationError(f"macro planning {message}", path=path)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _copy(value: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(value))


def _schema(value: Mapping[str, Any]) -> str:
    discriminator = value.get("schema")
    if not isinstance(discriminator, str) or discriminator not in _SCHEMA_TO_CONTRACT:
        _fail(f"unsupported schema discriminator: {discriminator!r}")
    return discriminator


def is_wire_whitespace_character(value: Any) -> bool:
    """Return whether *value* is one exact frozen wire-whitespace code point."""

    return isinstance(value, str) and len(value) == 1 and value in _WIRE_WHITESPACE_CHARACTERS


def contains_wire_whitespace(value: Any) -> bool:
    """Use the frozen wire set rather than Python's locale/version semantics."""

    return isinstance(value, str) and any(character in _WIRE_WHITESPACE_CHARACTERS for character in value)


def has_non_wire_whitespace_character(value: Any) -> bool:
    """Return whether a string contains at least one non-wire-whitespace code point."""

    return isinstance(value, str) and any(character not in _WIRE_WHITESPACE_CHARACTERS for character in value)


def _normalize_json_integer(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    path: str,
) -> int:
    """Return one Draft 2020-12 mathematical integer as a Python ``int``.

    JSON Schema intentionally treats integral numeric representations such as
    ``1``, ``1.0`` and ``1e0`` as the same integer.  Python's native type
    identity is therefore not a wire-level authority: booleans, non-finite
    floats, non-integral floats and values outside the field bounds fail, while
    an integral ``int`` or ``float`` is copied into its canonical ``int`` form.
    """

    if isinstance(value, bool):
        normalized: int | None = None
    elif isinstance(value, int):
        normalized = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        normalized = int(value)
    else:
        normalized = None
    if normalized is None or normalized < minimum or normalized > maximum:
        _fail(
            f"mathematical JSON integer must be in [{minimum}, {maximum}]",
            path=path,
        )
    return normalized


def _normalize_provider_integer_fields(
    value: Mapping[str, Any],
    *,
    path: str,
) -> dict[str, Any]:
    provider = _copy(_mapping(value, "provider"))
    options_path = f"{path}/options"
    options = _copy(_mapping(provider.get("options"), "provider options"))
    for field, minimum, maximum in _MODEL_OPTION_INTEGER_BOUNDS:
        options[field] = _normalize_json_integer(
            options.get(field),
            minimum=minimum,
            maximum=maximum,
            path=f"{options_path}/{field}",
        )
    provider["options"] = options
    return provider


def _normalize_macro_planning_integers(value: Mapping[str, Any]) -> dict[str, Any]:
    """Defensively copy one P0A value and normalize every integer field."""

    normalized = _copy(_mapping(value, "wire value"))
    discriminator = _schema(normalized)
    if discriminator == MODEL_PROFILE_SCHEMA:
        normalized["revision_number"] = _normalize_json_integer(
            normalized.get("revision_number"),
            minimum=1,
            maximum=JSON_MAX_SAFE_INTEGER,
            path="/revision_number",
        )
        normalized["provider"] = _normalize_provider_integer_fields(
            _mapping(normalized.get("provider"), "provider"),
            path="/provider",
        )
    elif discriminator == "model-profile-revise-command/v2":
        normalized["provider"] = _normalize_provider_integer_fields(
            _mapping(normalized.get("provider"), "provider"),
            path="/provider",
        )
    elif discriminator == "model-profile-revise-result/v2":
        normalized["revision"] = _normalize_macro_planning_integers(
            _mapping(normalized.get("revision"), "revision")
        )
    elif discriminator == "workspace-plan-selection-command/v2":
        normalized["expected_workspace_revision"] = _normalize_json_integer(
            normalized.get("expected_workspace_revision"),
            minimum=0,
            maximum=JSON_MAX_SAFE_INTEGER,
            path="/expected_workspace_revision",
        )
    elif discriminator == "workspace-plan-selection-result/v2":
        normalized["workspace_revision"] = _normalize_json_integer(
            normalized.get("workspace_revision"),
            minimum=1,
            maximum=JSON_MAX_SAFE_INTEGER,
            path="/workspace_revision",
        )
    elif discriminator == "project-planning-start-result/v2":
        normalized["writer_epoch"] = _normalize_json_integer(
            normalized.get("writer_epoch"),
            minimum=1,
            maximum=JSON_MAX_SAFE_INTEGER,
            path="/writer_epoch",
        )
    elif discriminator == PLANNER_RUNTIME_INPUT_SCHEMA:
        normalized["writer_epoch"] = _normalize_json_integer(
            normalized.get("writer_epoch"),
            minimum=1,
            maximum=JSON_MAX_SAFE_INTEGER,
            path="/writer_epoch",
        )
    return normalized


def _validate_secret_ref(value: Any) -> str:
    if not isinstance(value, str) or HOST_SECRET_REF_RE.fullmatch(value) is None:
        _fail("api_key_ref must be an exact secret:// opaque reference", path="/api_key_ref")
    return value


def _validate_endpoint(value: Any) -> None:
    if not isinstance(value, str) or contains_wire_whitespace(value):
        _fail("endpoint must be a whitespace-free URL", path="/provider/endpoint")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ContractValidationError("macro planning endpoint is malformed", path="/provider/endpoint") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        _fail("endpoint must be an http(s) origin/path without userinfo, query or fragment", path="/provider/endpoint")


def _validate_model_name(value: Any) -> None:
    if (
        not isinstance(value, str)
        or not value
        or is_wire_whitespace_character(value[0])
        or is_wire_whitespace_character(value[-1])
    ):
        _fail("model_name must be non-empty without wire-whitespace boundaries", path="/provider/model_name")


def _validate_nonblank_wire_text(value: Any, *, path: str) -> None:
    if not has_non_wire_whitespace_character(value):
        _fail("text must contain a non-wire-whitespace code point", path=path)


def _validate_provider_profile(provider: Mapping[str, Any]) -> None:
    _validate_endpoint(provider["endpoint"])
    _validate_model_name(provider["model_name"])
    _validate_secret_ref(provider["api_key_ref"])


def parse_macro_planning(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate any P0A discriminator and return a defensive deep copy."""

    record = _mapping(value, "wire value")
    discriminator = _schema(record)
    assert_valid(_SCHEMA_TO_CONTRACT[discriminator], record)
    parsed = _normalize_macro_planning_integers(record)
    if discriminator == MODEL_PROFILE_SCHEMA:
        _validate_model_profile_revision(parsed)
    elif discriminator in _MODEL_CONFIG_SCHEMAS:
        _validate_model_config_variant(parsed)
    elif discriminator in _ERROR_SCHEMAS:
        _validate_http_error(parsed)
    elif discriminator in _PROJECT_PLANNING_SCHEMAS:
        _validate_project_planning_variant(parsed)
    elif discriminator == PLANNER_RUNTIME_INPUT_SCHEMA:
        _validate_planner_runtime_input(parsed)
    elif discriminator == PLANNER_MODEL_OUTPUT_SCHEMA:
        _validate_planner_model_output(parsed)
    return parsed


def _model_profile_revision_hash_normalized(record: Mapping[str, Any]) -> str:
    payload = {
        "profile_id": record["profile_id"],
        "revision_id": record["revision_id"],
        "revision_number": record["revision_number"],
        "parent_revision_id": record["parent_revision_id"],
        "provider": copy.deepcopy(record["provider"]),
        "created_at": record["created_at"],
    }
    return hash_jcs(MODEL_PROFILE_SCHEMA, payload)


def model_profile_revision_hash(value: Mapping[str, Any]) -> str:
    """Hash a normalized defensive copy of the immutable profile revision."""

    record = _normalize_macro_planning_integers(
        _mapping(value, "model profile revision")
    )
    if record["schema"] != MODEL_PROFILE_SCHEMA:
        _fail("profile hash received a different contract")
    return _model_profile_revision_hash_normalized(record)


def _validate_model_profile_revision(value: Mapping[str, Any]) -> None:
    _validate_provider_profile(_mapping(value["provider"], "provider"))
    revision_number = value["revision_number"]
    parent_revision_id = value["parent_revision_id"]
    if (revision_number == 1) is not (parent_revision_id is None):
        _fail("revision 1 must have no parent and later revisions must name one", path="/parent_revision_id")
    if _model_profile_revision_hash_normalized(value) != value["revision_hash"]:
        _fail("revision_hash does not bind the immutable profile revision", path="/revision_hash")


def parse_model_profile_revision_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_macro_planning(value)
    if parsed.get("schema") != MODEL_PROFILE_SCHEMA:
        _fail("model profile parser received a different contract")
    return parsed


def _validate_plan_pair(value: Mapping[str, Any], id_field: str, hash_field: str) -> None:
    if (value[id_field] is None) != (value[hash_field] is None):
        _fail(f"{id_field} and {hash_field} must be null or present together")


def _validate_model_config_variant(value: Mapping[str, Any]) -> None:
    discriminator = value["schema"]
    if discriminator == "model-secret-put-command/v2":
        if HOST_SECRET_ID_RE.fullmatch(value["secret_id"]) is None:
            _fail("secret_id is not a Host opaque identifier", path="/secret_id")
    elif discriminator == "model-secret-put-result/v2":
        expected = f"secret://{value['secret_id']}"
        if _validate_secret_ref(value["api_key_ref"]) != expected:
            _fail("secret result reference is not bound to secret_id", path="/api_key_ref")
    elif discriminator == "model-profile-revise-command/v2":
        _validate_provider_profile(_mapping(value["provider"], "provider"))
    elif discriminator == "model-profile-revise-result/v2":
        revision = parse_model_profile_revision_v1(value["revision"])
        if revision["profile_id"] != value["profile_id"]:
            _fail("profile result crosses profile identity", path="/revision/profile_id")
    elif discriminator == "workspace-plan-selection-command/v2":
        _validate_plan_pair(
            value,
            "expected_current_plan_revision_id",
            "expected_current_plan_revision_hash",
        )
    elif discriminator == "workspace-plan-selection-result/v2":
        _validate_plan_pair(value, "previous_plan_revision_id", "previous_plan_revision_hash")


def _validate_http_error(value: Mapping[str, Any]) -> None:
    _validate_nonblank_wire_text(value["message"], path="/message")


def parse_model_config_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_macro_planning(value)
    if parsed.get("schema") not in _MODEL_CONFIG_SCHEMAS:
        _fail("model configuration parser received a different contract")
    return parsed


def parse_model_planning_http_error_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_macro_planning(value)
    if parsed.get("schema") not in _ERROR_SCHEMAS:
        _fail("HTTP error parser received a different contract")
    return parsed


def _secret_leak_path(value: Any, raw_value: str, path: str = "$") -> str | None:
    if isinstance(value, str):
        return path if raw_value in value else None
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and raw_value in key:
                return f"{path}.<key>"
            found = _secret_leak_path(child, raw_value, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            found = _secret_leak_path(child, raw_value, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def validate_secret_put_exchange(
    command: Mapping[str, Any],
    response: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    parsed_command = parse_model_config_v2(command)
    if parsed_command["schema"] != "model-secret-put-command/v2":
        _fail("secret exchange requires a PUT command")
    raw_value = parsed_command["value"]
    leak_path = _secret_leak_path(response, raw_value)
    if leak_path is not None:
        _fail(f"secret PUT output contains the raw value at {leak_path}")
    parsed_response = parse_macro_planning(response)
    if parsed_response["schema"] == "model-secret-put-result/v2":
        if (
            parsed_response["operation_key"] != parsed_command["operation_key"]
            or parsed_response["secret_id"] != parsed_command["secret_id"]
        ):
            _fail("secret PUT result is not bound to its command")
    elif parsed_response["schema"] == "model-secret-http-error/v2":
        if parsed_response["secret_id"] != parsed_command["secret_id"]:
            _fail("secret PUT error is not bound to its command")
        if parsed_response["operation_key"] not in {None, parsed_command["operation_key"]}:
            _fail("secret PUT error operation key mismatch")
    else:
        _fail("secret PUT response uses the wrong contract")
    return parsed_command, parsed_response


def validate_model_profile_revision_exchange(
    command: Mapping[str, Any],
    result: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    parsed_command = parse_model_config_v2(command)
    parsed_result = parse_model_config_v2(result)
    if (
        parsed_command["schema"] != "model-profile-revise-command/v2"
        or parsed_result["schema"] != "model-profile-revise-result/v2"
    ):
        _fail("profile revision exchange uses the wrong variants")
    revision = parsed_result["revision"]
    if (
        parsed_result["operation_key"] != parsed_command["operation_key"]
        or parsed_result["profile_id"] != parsed_command["profile_id"]
        or revision["profile_id"] != parsed_command["profile_id"]
        or revision["parent_revision_id"] != parsed_command["expected_parent_revision_id"]
        or revision["provider"] != parsed_command["provider"]
    ):
        _fail("profile revision result is not bound to its append-only command")
    return parsed_command, parsed_result


def validate_workspace_plan_selection(
    command: Mapping[str, Any],
    result: Mapping[str, Any] | None = None,
    *,
    expected_workspace_revision: int | None = None,
    expected_current_plan_revision_id: str | None | object = _MISSING,
    expected_current_plan_revision_hash: str | None | object = _MISSING,
    active_generation_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    parsed_command = parse_model_config_v2(command)
    if parsed_command["schema"] != "workspace-plan-selection-command/v2":
        _fail("Plan selection requires the explicit command variant")
    if expected_workspace_revision is not None and parsed_command["expected_workspace_revision"] != expected_workspace_revision:
        _fail("workspace Plan selection uses a stale Workspace CAS")
    if expected_current_plan_revision_id is not _MISSING and parsed_command["expected_current_plan_revision_id"] != expected_current_plan_revision_id:
        _fail("workspace Plan selection uses a stale current Plan revision")
    if expected_current_plan_revision_hash is not _MISSING and parsed_command["expected_current_plan_revision_hash"] != expected_current_plan_revision_hash:
        _fail("workspace Plan selection uses a stale current Plan hash")
    if active_generation_id is not None and parsed_command["expected_active_generation_id"] != active_generation_id:
        _fail("workspace Plan selection crosses the active Generation")
    if result is None:
        return parsed_command, None
    parsed_result = parse_model_config_v2(result)
    if parsed_result["schema"] != "workspace-plan-selection-result/v2":
        _fail("Plan selection result uses the wrong variant")
    bindings = (
        ("operation_key", "operation_key"),
        ("workspace_id", "workspace_id"),
        ("expected_current_plan_revision_id", "previous_plan_revision_id"),
        ("expected_current_plan_revision_hash", "previous_plan_revision_hash"),
        ("selection_mode", "selection_mode"),
        ("plan_revision_id", "plan_revision_id"),
        ("plan_revision_hash", "plan_revision_hash"),
        ("model_profile_revision_id", "model_profile_revision_id"),
        ("model_profile_revision_hash", "model_profile_revision_hash"),
        ("expected_active_generation_id", "active_generation_id"),
    )
    for command_field, result_field in bindings:
        if parsed_command[command_field] != parsed_result[result_field]:
            _fail(f"Plan selection result does not bind {command_field}")
    if not parsed_result["idempotent"] and parsed_result["workspace_revision"] != parsed_command["expected_workspace_revision"] + 1:
        _fail("non-replayed Plan selection did not advance the Workspace revision exactly once")
    if parsed_result["idempotent"] and parsed_result["workspace_revision"] < parsed_command["expected_workspace_revision"]:
        _fail("replayed Plan selection regressed the Workspace revision")
    return parsed_command, parsed_result


def _validate_project_planning_variant(value: Mapping[str, Any]) -> None:
    if value["schema"] == "project-planning-availability-result/v2":
        ready = value["reason"] == _READY_REASON
        if value["available"] is not ready:
            _fail("planning availability must fail closed unless reason is ready")


def parse_project_planning_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_macro_planning(value)
    if parsed.get("schema") not in _PROJECT_PLANNING_SCHEMAS:
        _fail("project planning parser received a different contract")
    return parsed


def validate_project_planning_start(
    command: Mapping[str, Any],
    *,
    expected_project_brief: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    parsed = parse_project_planning_v2(command)
    if parsed["schema"] != "project-planning-start-command/v2":
        _fail("planning start requires the start command variant")
    if expected_project_brief is not None:
        authority = _mapping(expected_project_brief, "expected Project Brief authority")
        expected = {
            "document_id": parsed["project_brief_document_id"],
            **parsed["expected_project_brief"],
        }
        if dict(authority) != expected:
            _fail("planning start uses a stale Project Brief CAS")
    return parsed


def validate_project_planning_start_exchange(
    command: Mapping[str, Any],
    result: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    parsed_command = validate_project_planning_start(command)
    parsed_result = parse_project_planning_v2(result)
    if parsed_result["schema"] != "project-planning-start-result/v2":
        _fail("planning start result uses the wrong variant")
    if (
        parsed_result["operation_key"] != parsed_command["operation_key"]
        or parsed_result["workspace_id"] != parsed_command["workspace_id"]
    ):
        _fail("planning start result is not bound to request identity")
    return parsed_command, parsed_result


def _planner_runtime_input_hash_normalized(record: Mapping[str, Any]) -> str:
    unsigned = copy.deepcopy(dict(record))
    unsigned.pop("input_hash", None)
    return hash_jcs(PLANNER_RUNTIME_INPUT_SCHEMA, unsigned)


def planner_runtime_input_hash(value: Mapping[str, Any]) -> str:
    """Hash a normalized defensive copy of one Planner runtime input."""

    record = _normalize_macro_planning_integers(
        _mapping(value, "Planner runtime input")
    )
    if record["schema"] != PLANNER_RUNTIME_INPUT_SCHEMA:
        _fail("Planner input hash received a different contract")
    return _planner_runtime_input_hash_normalized(record)


def _validate_planner_runtime_input(value: Mapping[str, Any]) -> None:
    workspace_id = value["workspace_id"]
    references = [value["project_brief"], *value["targets"].values()]
    if any(reference["workspace_id"] != workspace_id for reference in references):
        _fail("Planner input contains a cross-Workspace document reference")
    target_ids = [reference["document_id"] for reference in value["targets"].values()]
    if len(target_ids) != len(set(target_ids)):
        _fail("Planner output targets must be three independent Documents")
    if value["project_brief"]["document_id"] in set(target_ids):
        _fail("Project Brief cannot also be a Planner output target")
    if _planner_runtime_input_hash_normalized(value) != value["input_hash"]:
        _fail("Planner input_hash does not bind all host-frozen inputs", path="/input_hash")


def parse_project_planner_runtime_input_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_macro_planning(value)
    if parsed.get("schema") != PLANNER_RUNTIME_INPUT_SCHEMA:
        _fail("Planner input parser received a legacy or unrelated contract")
    return parsed


def _validate_planner_model_output(value: Mapping[str, Any]) -> None:
    for role in ("setting", "bible", "outline"):
        _validate_nonblank_wire_text(value[role], path=f"/{role}")


def parse_project_planner_model_output_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_macro_planning(value)
    if parsed.get("schema") != PLANNER_MODEL_OUTPUT_SCHEMA:
        _fail("Planner output parser received a different contract")
    return parsed


__all__ = [
    "HOST_SECRET_REF_RE",
    "JSON_MAX_SAFE_INTEGER",
    "MODEL_CONFIG_CONTRACT",
    "MODEL_HTTP_ERROR_CONTRACT",
    "MODEL_PROFILE_CONTRACT",
    "MODEL_PROFILE_SCHEMA",
    "PLANNER_MODEL_OUTPUT_CONTRACT",
    "PLANNER_MODEL_OUTPUT_SCHEMA",
    "PLANNER_RUNTIME_INPUT_CONTRACT",
    "PLANNER_RUNTIME_INPUT_SCHEMA",
    "PROJECT_PLANNING_CONTRACT",
    "WIRE_WHITESPACE_CODEPOINTS",
    "contains_wire_whitespace",
    "has_non_wire_whitespace_character",
    "is_wire_whitespace_character",
    "model_profile_revision_hash",
    "parse_macro_planning",
    "parse_model_config_v2",
    "parse_model_planning_http_error_v2",
    "parse_model_profile_revision_v1",
    "parse_project_planner_model_output_v1",
    "parse_project_planner_runtime_input_v2",
    "parse_project_planning_v2",
    "planner_runtime_input_hash",
    "validate_model_profile_revision_exchange",
    "validate_project_planning_start",
    "validate_project_planning_start_exchange",
    "validate_secret_put_exchange",
    "validate_workspace_plan_selection",
]
