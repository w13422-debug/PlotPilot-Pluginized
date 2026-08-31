"""Closed Prompt/Skill execute RPC v2 contract.

The module is the SDK-side semantic gate for the additive Prompt/Skill RPC.
It deliberately owns no Core state and grants no plugin publication authority.
The wire method uses the v1 JSON-RPC framing and error contract, while its
method-specific request/result schemas are additive v2 resources.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping
from importlib.resources import files as resource_files
from typing import Any, Literal, TypedDict

from jsonschema import Draft202012Validator

from .canonical import canonical_bytes, hash_jcs
from .errors import ContractError, ContractValidationError, ErrorCode
from .rpc import ERROR_CODES


METHOD = "prompt.skill.execute/v2"
REQUEST_SCHEMA = "prompt-skill-execute-request/v2"
RESULT_SCHEMA = "prompt-skill-execute-result/v2"
SUCCESS_SCHEMA = "rpc-method-success/v2"
ERROR_SCHEMA = "rpc-error-v1"
AUTHORITY = "core"
RESULT_CONTRACT = "artifact-bundle/v1"
PROTOCOL_VERSION = "1"
META_CONTEXT = "attempt"
MAX_SAFE_INTEGER = 9_007_199_254_740_991

PROMPT_SKILL_EXECUTE_METHOD_V2 = METHOD
PROMPT_SKILL_REQUEST_SCHEMA_V2 = REQUEST_SCHEMA
PROMPT_SKILL_RESULT_SCHEMA_V2 = RESULT_SCHEMA
PROMPT_SKILL_SUCCESS_SCHEMA_V2 = SUCCESS_SCHEMA
PROMPT_SKILL_RESULT_CONTRACT_V1 = RESULT_CONTRACT

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_RPC_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_UTC = re.compile(
    r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$"
)

_BOUND_FIELDS = (
    "authority",
    "capability_id",
    "result_contract",
    "operation_key",
    "workspace_id",
    "plugin_id",
    "plugin_release_id",
    "plugin_package_hash",
    "skill_releases",
    "run_snapshot_id",
    "run_snapshot_asset_id",
    "run_snapshot_hash",
    "generation_id",
    "job_id",
    "step_id",
    "attempt_id",
    "lease_epoch",
    "chain_id",
    "chain_asset_id",
    "chain_content_hash",
    "input_asset_id",
    "input_content_hash",
    "parameters_asset_id",
    "parameters_content_hash",
    "model_profile_revision_id",
    "anchor",
)

_AUTHORITY_FIELDS = (
    "workspace_id",
    "generation_id",
    "plugin_id",
    "plugin_release_id",
    "job_id",
    "step_id",
    "attempt_id",
    "lease_epoch",
    "operation_key",
)


class SkillReleaseBinding(TypedDict):
    skill_id: str
    release_id: str
    package_hash: str
    parameters_asset_id: str | None
    parameters_content_hash: str | None


class AssetIdentity(TypedDict):
    asset_id: str
    content_hash: str


class ModelReceiptIdentity(TypedDict):
    receipt_id: str
    asset_id: str
    content_hash: str
    receipt_hash: str
    operation_key: str
    workspace_id: str
    plugin_id: str
    plugin_release_id: str
    plugin_package_hash: str
    generation_id: str
    job_id: str
    step_id: str
    attempt_id: str
    lease_epoch: int
    chain_id: str
    chain_asset_id: str
    chain_content_hash: str
    run_snapshot_id: str
    run_snapshot_asset_id: str
    run_snapshot_hash: str
    model_profile_revision_id: str | None
    input_asset_id: str
    input_content_hash: str


class ResultBundleIdentity(TypedDict):
    contract_id: Literal["artifact-bundle/v1"]
    bundle_id: str
    asset_id: str
    content_hash: str
    result_item_id: str
    workspace_id: str
    plugin_id: str
    plugin_release_id: str
    plugin_package_hash: str
    generation_id: str
    job_id: str
    step_id: str
    attempt_id: str
    lease_epoch: int
    chain_id: str
    chain_asset_id: str
    chain_content_hash: str
    run_snapshot_id: str
    run_snapshot_asset_id: str
    run_snapshot_hash: str
    model_profile_revision_id: str | None
    input_asset_id: str
    input_content_hash: str
    operation_key: str


class AuthoritativeAttemptContext(TypedDict):
    workspace_id: str
    generation_id: str
    plugin_id: str
    plugin_release_id: str
    job_id: str
    step_id: str
    attempt_id: str
    lease_epoch: int
    operation_key: str


class PromptSkillExecuteRequest(TypedDict):
    jsonrpc: Literal["2.0"]
    id: str
    method: Literal["prompt.skill.execute/v2"]
    meta: dict[str, Any]
    params: dict[str, Any]


class PromptSkillExecuteResult(TypedDict):
    schema: Literal["prompt-skill-execute-result/v2"]
    authority: Literal["core"]
    capability_id: Literal["prompt.skill.execute/v2"]
    result_contract: Literal["artifact-bundle/v1"]
    operation_key: str
    workspace_id: str
    plugin_id: str
    plugin_release_id: str
    plugin_package_hash: str
    skill_releases: list[SkillReleaseBinding]
    run_snapshot_id: str
    run_snapshot_asset_id: str
    run_snapshot_hash: str
    generation_id: str
    job_id: str
    step_id: str
    attempt_id: str
    lease_epoch: int
    chain_id: str
    chain_asset_id: str
    chain_content_hash: str
    input_asset_id: str
    input_content_hash: str
    parameters_asset_id: str | None
    parameters_content_hash: str | None
    model_profile_revision_id: str | None
    anchor: dict[str, Any]
    status: Literal["succeeded", "failed", "cancelled", "uncertain"]
    output: AssetIdentity | None
    model_receipt: ModelReceiptIdentity | None
    result_bundle: ResultBundleIdentity | None
    idempotent: bool
    warnings: list[dict[str, Any]]


def _fail(message: str, *, path: str | None = None) -> None:
    raise ContractValidationError(message, path=path)


def _schema_bytes(name: str) -> bytes:
    try:
        return resource_files("plotpilot_plugin_sdk").joinpath("resources", name).read_bytes()
    except (FileNotFoundError, OSError, ModuleNotFoundError) as exc:
        raise ContractValidationError(f"missing packaged Prompt/Skill contract resource: {name}") from exc


def _schema(name: str) -> dict[str, Any]:
    try:
        value = json.loads(_schema_bytes(name).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"invalid packaged Prompt/Skill contract resource: {name}") from exc
    if not isinstance(value, dict):
        raise ContractValidationError(f"packaged schema is not an object: {name}")
    return value


def _schema_path(path: tuple[Any, ...]) -> str:
    return "$" if not path else "/" + "/".join(str(item) for item in path)


def _validate_schema(name: str, value: Any) -> None:
    validator = Draft202012Validator(_schema(name))
    errors = sorted(validator.iter_errors(value), key=lambda error: (tuple(error.absolute_path), error.message))
    if errors:
        error = errors[0]
        _fail(f"{name}: {error.message}", path=_schema_path(tuple(error.absolute_path)))


def _normalize_text(value: str, path: str) -> str:
    """Reject lone UTF-16 surrogates and normalize valid escaped pairs."""

    output: list[str] = []
    index = 0
    while index < len(value):
        code = ord(value[index])
        if 0xD800 <= code <= 0xDBFF:
            if index + 1 >= len(value) or not 0xDC00 <= ord(value[index + 1]) <= 0xDFFF:
                _fail("string contains a lone UTF-16 surrogate", path=path)
            output.append(chr(0x10000 + ((code - 0xD800) << 10) + (ord(value[index + 1]) - 0xDC00)))
            index += 2
            continue
        if 0xDC00 <= code <= 0xDFFF:
            _fail("string contains a lone UTF-16 surrogate", path=path)
        output.append(value[index])
        index += 1
    result = "".join(output)
    try:
        result.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        _fail("string is not a Unicode scalar sequence", path=path)
        raise AssertionError from exc
    return result


def _normalize_json(value: Any, path: str = "$") -> Any:
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > MAX_SAFE_INTEGER:
            _fail("integer is outside the IEEE-754 safe range", path=path)
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or abs(value) > MAX_SAFE_INTEGER:
            _fail("number is not an IEEE-754 safe integer", path=path)
        return int(value)
    if isinstance(value, str):
        return _normalize_text(value, path)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_child in value.items():
            if not isinstance(raw_key, str):
                _fail("JSON object keys must be strings", path=path)
            key = _normalize_text(raw_key, f"{path}.<key>")
            if key in result:
                _fail("normalized JSON object keys collide", path=path)
            result[key] = _normalize_json(raw_child, f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_normalize_json(child, f"{path}[{index}]") for index, child in enumerate(value)]
    _fail("value is not a JSON value", path=path)
    raise AssertionError


def _copy_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    normalized = _normalize_json(value, label)
    if not isinstance(normalized, dict):
        _fail(f"{label} must be an object")
    return copy.deepcopy(normalized)


def _id(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail(f"{field} must be a v2 identity", path=field)
    return value


def _hash(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        _fail(f"{field} must be a lowercase SHA-256", path=field)
    return value


def _safe_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > MAX_SAFE_INTEGER:
        _fail(f"{field} must be an IEEE-754 safe integer >= {minimum}", path=field)
    return value


def _exact_pair(left: Any, right: Any, label: str) -> None:
    if (left is None) != (right is None):
        _fail(f"{label} must be all-null or all-present")


def _validate_secrets(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        _fail("secrets must be an array or null", path="secrets")
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            _fail("secret must be an object", path=f"secrets[{index}]")
        secret_id = _id(item["secret_id"], f"secrets[{index}].secret_id")
        if secret_id in seen:
            _fail("secret IDs must be unique", path="secrets")
        seen.add(secret_id)
        if not isinstance(item["value"], str):
            _fail("secret value must be a string", path=f"secrets[{index}].value")
        _normalize_text(item["value"], f"secrets[{index}].value")


def _validate_skill_releases(value: Mapping[str, Any]) -> None:
    releases = value["skill_releases"]
    if not isinstance(releases, list) or not releases:
        _fail("skill_releases must be a non-empty array", path="skill_releases")
    skill_ids = [item["skill_id"] for item in releases]
    if len(skill_ids) != len(set(skill_ids)):
        _fail("skill_releases Skill identities must be unique", path="skill_releases")
    for index, release in enumerate(releases):
        _id(release["skill_id"], f"skill_releases[{index}].skill_id")
        _hash(release["release_id"], f"skill_releases[{index}].release_id")
        _hash(release["package_hash"], f"skill_releases[{index}].package_hash")
        _id(release["parameters_asset_id"], f"skill_releases[{index}].parameters_asset_id", nullable=True)
        _hash(release["parameters_content_hash"], f"skill_releases[{index}].parameters_content_hash", nullable=True)
        _exact_pair(release["parameters_asset_id"], release["parameters_content_hash"], f"skill_releases[{index}] parameters identity")


def _validate_anchor(value: Mapping[str, Any]) -> None:
    bundle_id = value["result_bundle_id"]
    item_id = value["result_item_id"]
    stream_id = value["stream_id"]
    prefix_hash = value["acked_prefix_hash"]
    _exact_pair(bundle_id, item_id, "Bundle anchor")
    _exact_pair(stream_id, prefix_hash, "stream anchor")
    if bundle_id is not None and stream_id is not None:
        _fail("an execution anchor cannot be both Bundle-backed and stream-backed", path="anchor")
    _id(bundle_id, "anchor.result_bundle_id", nullable=True)
    _id(item_id, "anchor.result_item_id", nullable=True)
    _id(stream_id, "anchor.stream_id", nullable=True)
    _hash(prefix_hash, "anchor.acked_prefix_hash", nullable=True)


def _validate_common(value: Mapping[str, Any], *, label: str) -> None:
    if value["authority"] != AUTHORITY:
        _fail(f"{label} is not Core-owned", path="authority")
    if value["capability_id"] != METHOD:
        _fail(f"{label} capability is not the frozen v2 method", path="capability_id")
    if value["result_contract"] != RESULT_CONTRACT:
        _fail(f"{label} result contract is not the frozen Core contract", path="result_contract")
    for field in (
        "operation_key", "workspace_id", "plugin_id", "run_snapshot_id", "run_snapshot_asset_id",
        "generation_id", "job_id", "step_id", "attempt_id", "chain_id", "chain_asset_id", "input_asset_id",
    ):
        _id(value[field], field)
    for field in (
        "plugin_release_id", "plugin_package_hash", "run_snapshot_hash", "chain_content_hash", "input_content_hash",
    ):
        _hash(value[field], field)
    _id(value["model_profile_revision_id"], "model_profile_revision_id", nullable=True)
    _safe_int(value["lease_epoch"], "lease_epoch", minimum=1)
    _id(value["parameters_asset_id"], "parameters_asset_id", nullable=True)
    _hash(value["parameters_content_hash"], "parameters_content_hash", nullable=True)
    _exact_pair(value["parameters_asset_id"], value["parameters_content_hash"], "parameters identity")
    _validate_skill_releases(value)
    _validate_anchor(value["anchor"])


def _validate_meta(meta: Mapping[str, Any], params: Mapping[str, Any]) -> None:
    if not isinstance(meta, Mapping):
        _fail("request meta must be an object", path="meta")
    if meta["protocol_version"] != PROTOCOL_VERSION:
        _fail("request meta protocol version is not v1", path="meta.protocol_version")
    if meta["context"] != META_CONTEXT:
        _fail("Prompt Skill execute requires attempt meta", path="meta.context")
    _id(meta["generation_id"], "meta.generation_id")
    _hash(meta["plugin_release_id"], "meta.plugin_release_id")
    if not isinstance(meta["deadline_at"], str) or _UTC.fullmatch(meta["deadline_at"]) is None:
        _fail("meta.deadline_at must use the frozen UTC format", path="meta.deadline_at")
    _id(meta["operation_id"], "meta.operation_id")
    for field in ("job_id", "step_id", "attempt_id"):
        _id(meta[field], f"meta.{field}")
    _safe_int(meta["lease_epoch"], "meta.lease_epoch", minimum=1)
    for field in ("generation_id", "plugin_release_id", "job_id", "step_id", "attempt_id", "lease_epoch"):
        if meta[field] != params[field]:
            _fail(f"request meta {field} does not match execute identity", path=f"meta.{field}")
    if meta["operation_id"] != params["operation_key"]:
        _fail("request meta operation_id must equal operation_key", path="meta.operation_id")


def _validate_model_receipt(receipt: Mapping[str, Any]) -> None:
    for field in ("receipt_id", "asset_id", "operation_key", "workspace_id", "plugin_id", "generation_id", "job_id", "step_id", "attempt_id", "chain_id", "chain_asset_id", "run_snapshot_id", "run_snapshot_asset_id"):
        _id(receipt[field], f"model_receipt.{field}")
    for field in ("content_hash", "receipt_hash", "plugin_release_id", "plugin_package_hash", "chain_content_hash", "run_snapshot_hash", "input_content_hash"):
        _hash(receipt[field], f"model_receipt.{field}")
    _safe_int(receipt["lease_epoch"], "model_receipt.lease_epoch", minimum=1)
    _id(receipt["model_profile_revision_id"], "model_receipt.model_profile_revision_id", nullable=True)
    _id(receipt["input_asset_id"], "model_receipt.input_asset_id")


def _validate_bundle(bundle: Mapping[str, Any]) -> None:
    if bundle["contract_id"] != RESULT_CONTRACT:
        _fail("result Bundle contract is not the frozen Core contract", path="result_bundle.contract_id")
    for field in ("bundle_id", "asset_id", "result_item_id", "workspace_id", "plugin_id", "generation_id", "job_id", "step_id", "attempt_id", "chain_id", "chain_asset_id", "run_snapshot_id", "run_snapshot_asset_id", "input_asset_id", "operation_key"):
        _id(bundle[field], f"result_bundle.{field}")
    for field in ("content_hash", "plugin_release_id", "plugin_package_hash", "chain_content_hash", "run_snapshot_hash", "input_content_hash"):
        _hash(bundle[field], f"result_bundle.{field}")
    _id(bundle["model_profile_revision_id"], "result_bundle.model_profile_revision_id", nullable=True)
    _safe_int(bundle["lease_epoch"], "result_bundle.lease_epoch", minimum=1)


def _validate_output(output: Mapping[str, Any] | None) -> None:
    if output is None:
        return
    _id(output["asset_id"], "output.asset_id")
    _hash(output["content_hash"], "output.content_hash")


def _identity_bindings(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "operation_key": value["operation_key"],
        "workspace_id": value["workspace_id"],
        "plugin_id": value["plugin_id"],
        "plugin_release_id": value["plugin_release_id"],
        "plugin_package_hash": value["plugin_package_hash"],
        "generation_id": value["generation_id"],
        "job_id": value["job_id"],
        "step_id": value["step_id"],
        "attempt_id": value["attempt_id"],
        "lease_epoch": value["lease_epoch"],
        "chain_id": value["chain_id"],
        "chain_asset_id": value["chain_asset_id"],
        "chain_content_hash": value["chain_content_hash"],
        "run_snapshot_id": value["run_snapshot_id"],
        "run_snapshot_asset_id": value["run_snapshot_asset_id"],
        "run_snapshot_hash": value["run_snapshot_hash"],
        "model_profile_revision_id": value["model_profile_revision_id"],
        "input_asset_id": value["input_asset_id"],
        "input_content_hash": value["input_content_hash"],
    }


def _bind_identity(child: Mapping[str, Any], expected: Mapping[str, Any], label: str) -> None:
    for field, expected_value in expected.items():
        if child[field] != expected_value:
            _fail(f"{label} {field} does not match the execute identity", path=f"{label}.{field}")


def _validate_result_semantics(value: Mapping[str, Any]) -> None:
    _validate_common(value, label="Prompt Skill result")
    status = value["status"]
    receipt = value["model_receipt"]
    bundle = value["result_bundle"]
    output = value["output"]
    expected = _identity_bindings(value)
    if receipt is not None:
        _validate_model_receipt(receipt)
        _bind_identity(receipt, expected, "ModelReceipt")
    if bundle is not None:
        _validate_bundle(bundle)
        _bind_identity(bundle, expected, "result Bundle")
    _validate_output(output)
    if status == "succeeded":
        if receipt is None or bundle is None or output is None:
            _fail("succeeded execution requires output, ModelReceipt and result Bundle", path="status")
        anchor = value["anchor"]
        if anchor["result_bundle_id"] != bundle["bundle_id"] or anchor["result_item_id"] != bundle["result_item_id"]:
            _fail("result Bundle identity does not match the execution anchor", path="result_bundle")
        if anchor["stream_id"] is not None or anchor["acked_prefix_hash"] is not None:
            _fail("Bundle-backed result cannot carry a stream anchor", path="anchor")
    else:
        if receipt is not None or bundle is not None or output is not None:
            _fail(f"{status} execution cannot expose success artifacts", path="status")
        if any(value["anchor"][field] is not None for field in ("result_bundle_id", "result_item_id", "stream_id", "acked_prefix_hash")):
            _fail(f"{status} execution cannot expose an output anchor", path="anchor")
    if type(value["idempotent"]) is not bool:
        _fail("idempotent must be boolean", path="idempotent")
    for index, warning in enumerate(value["warnings"]):
        _id(warning["code"], f"warnings[{index}].code")
        if not isinstance(warning["message"], str):
            _fail(f"warnings[{index}].message must be a string", path=f"warnings[{index}].message")
        _normalize_text(warning["message"], f"warnings[{index}].message")


def parse_prompt_skill_execute_request_v2(value: Mapping[str, Any]) -> PromptSkillExecuteRequest:
    """Validate and copy one complete JSON-RPC execute request."""

    request = _copy_object(value, "Prompt Skill request")
    _validate_schema("prompt-skill-execute-request-v2.schema.json", request)
    if request["jsonrpc"] != "2.0" or request["method"] != METHOD or _RPC_ID.fullmatch(request["id"]) is None:
        _fail("Prompt Skill request method/version/id is not the frozen v2 envelope")
    params = request["params"]
    _validate_common(params, label="Prompt Skill request")
    _validate_secrets(params["secrets"])
    _validate_meta(request["meta"], params)
    return request  # type: ignore[return-value]


def parse_prompt_skill_execute_result_v2(value: Mapping[str, Any]) -> PromptSkillExecuteResult:
    """Validate and copy one Core-owned execute result payload."""

    result = _copy_object(value, "Prompt Skill result")
    _validate_schema("prompt-skill-execute-result-v2.schema.json", result)
    if result["schema"] != RESULT_SCHEMA:
        _fail("Prompt Skill result schema discriminator is not v2", path="schema")
    _validate_result_semantics(result)
    return result  # type: ignore[return-value]


def parse_rpc_error_v2(value: Mapping[str, Any], *, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate the method's error member using the unchanged v1 error schema."""

    error = _copy_object(value, "RPC error response")
    _validate_schema("rpc-error-v1.schema.json", error)
    if error["jsonrpc"] != "2.0":
        _fail("RPC error response must use JSON-RPC 2.0", path="jsonrpc")
    _normalize_text(error["error"]["message"], "error.message")
    code = error["error"]["code"]
    if type(code) is not int or code not in ERROR_CODES:
        _fail("RPC error code is not registered in rpc-method-matrix/v1", path="error.code")
    if error["id"] is not None and _RPC_ID.fullmatch(error["id"]) is None:
        _fail("RPC error response id is not a UUID", path="id")
    if request is not None:
        parsed_request = parse_prompt_skill_execute_request_v2(request)
        if error["id"] != parsed_request["id"]:
            _fail("RPC error response id does not match its request", path="id")
    return error


def validate_rpc_success_v2(value: Mapping[str, Any], *, request: Mapping[str, Any] | None = None) -> PromptSkillExecuteResult:
    """Validate a closed success envelope and its method-specific result."""

    response = _copy_object(value, "RPC success response")
    _validate_schema("rpc-method-success-v2.schema.json", response)
    if response["jsonrpc"] != "2.0" or _RPC_ID.fullmatch(response["id"]) is None:
        _fail("RPC success response must use JSON-RPC 2.0", path="id")
    if request is not None:
        parsed_request = parse_prompt_skill_execute_request_v2(request)
        if response["id"] != parsed_request["id"]:
            _fail("RPC response id does not match its request", path="id")
    parsed_result = parse_prompt_skill_execute_result_v2(response["result"])
    if request is not None:
        validate_prompt_skill_execute_v2(parsed_request, parsed_result)
    return parsed_result


def parse_prompt_skill_rpc_envelope_v2(value: Mapping[str, Any], *, request: Mapping[str, Any] | None = None) -> Any:
    """Dispatch the logical exactly-one-of request/success/error envelope."""

    envelope = _copy_object(value, "Prompt Skill RPC envelope")
    kinds = [key for key in ("method", "result", "error") if key in envelope]
    if len(kinds) != 1:
        _fail("Prompt Skill RPC envelope must contain exactly one request/success/error member")
    if kinds[0] == "method":
        return parse_prompt_skill_execute_request_v2(envelope)
    if kinds[0] == "result":
        return validate_rpc_success_v2(envelope, request=request)
    return parse_rpc_error_v2(envelope, request=request)


def _binding_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    return {field: copy.deepcopy(value[field]) for field in _BOUND_FIELDS}


def _authority_projection(params: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(context, Mapping):
        raise ContractError(ErrorCode.STALE_LEASE, "a complete authoritative Attempt context is required")
    keys = set(context)
    required = set(_AUTHORITY_FIELDS)
    missing = sorted(required - keys)
    extra = sorted(keys - required)
    if missing or extra:
        detail = f"missing={missing}" if missing else f"additional={extra}"
        raise ContractError(ErrorCode.STALE_LEASE, f"authoritative Attempt context must be exact ({detail})")
    values = {field: context[field] for field in _AUTHORITY_FIELDS}
    for field in ("workspace_id", "generation_id", "plugin_id", "job_id", "step_id", "attempt_id", "operation_key"):
        _id(values[field], f"authoritative_context.{field}")
    _hash(values["plugin_release_id"], "authoritative_context.plugin_release_id")
    _safe_int(values["lease_epoch"], "authoritative_context.lease_epoch", minimum=1)
    for field in _AUTHORITY_FIELDS:
        if values[field] != params[field]:
            raise ContractError(ErrorCode.STALE_LEASE, f"authoritative Attempt {field} does not match the request", path=field)
    return values


def _require_authority(
    request: Mapping[str, Any],
    *,
    authoritative_context: Mapping[str, Any],
) -> bytes:
    return canonical_bytes(_authority_projection(request["params"], authoritative_context))


def validate_prompt_skill_execute_v2(
    request: Mapping[str, Any],
    result_or_response: Mapping[str, Any],
    *,
    authoritative_context: Mapping[str, Any] | None = None,
) -> PromptSkillExecuteResult:
    """Bind one request to its result without introducing plugin authority."""

    parsed_request = parse_prompt_skill_execute_request_v2(request)
    candidate = _copy_object(result_or_response, "Prompt Skill result or response")
    if "error" in candidate:
        parse_rpc_error_v2(candidate)
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Prompt Skill execute returned an RPC error")
    if "result" in candidate and candidate.get("jsonrpc") == "2.0":
        parsed_result = validate_rpc_success_v2(candidate, request=parsed_request)
    else:
        parsed_result = parse_prompt_skill_execute_result_v2(candidate)
    params = parsed_request["params"]
    if canonical_bytes(_binding_projection(params)) != canonical_bytes(_binding_projection(parsed_result)):
        _fail("Prompt Skill result identity projection does not match its request")
    if authoritative_context is not None:
        _require_authority(parsed_request, authoritative_context=authoritative_context)
    return parsed_result


def build_prompt_skill_meta_v2(params: Mapping[str, Any], *, deadline_at: str) -> dict[str, Any]:
    """Build the attempt-scoped v1 meta bound to a Prompt/Skill request."""

    return {
        "protocol_version": PROTOCOL_VERSION,
        "generation_id": params["generation_id"],
        "plugin_release_id": params["plugin_release_id"],
        "deadline_at": deadline_at,
        "context": META_CONTEXT,
        "operation_id": params["operation_key"],
        "job_id": params["job_id"],
        "step_id": params["step_id"],
        "attempt_id": params["attempt_id"],
        "lease_epoch": params["lease_epoch"],
    }


def build_prompt_skill_execute_request_v2(
    params: Mapping[str, Any],
    *,
    request_id: str,
    meta: Mapping[str, Any] | None = None,
) -> PromptSkillExecuteRequest:
    """Build a request only after validating the complete closed params object."""

    params_copy = _normalize_json(dict(params))
    if not isinstance(params_copy, dict):
        _fail("Prompt Skill params must be an object")
    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": METHOD,
        "meta": dict(meta) if meta is not None else build_prompt_skill_meta_v2(params_copy, deadline_at="2026-08-31T00:00:00Z"),
        "params": params_copy,
    }
    return parse_prompt_skill_execute_request_v2(request)


def build_rpc_success_v2(request: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Build a success envelope after exact request/result binding."""

    parsed_request = parse_prompt_skill_execute_request_v2(request)
    parsed_result = validate_prompt_skill_execute_v2(parsed_request, result)
    response = {"jsonrpc": "2.0", "id": parsed_request["id"], "result": parsed_result}
    validate_rpc_success_v2(response, request=parsed_request)
    return response


def build_rpc_error_v2(
    request_id: str | None,
    *,
    code: int,
    message: str,
    retryable: bool,
    error_id: str = "prompt-skill-error-1",
    details_asset_id: str | None = None,
) -> dict[str, Any]:
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message, "data": {"error_id": error_id, "retryable": retryable, "details_asset_id": details_asset_id}},
    }
    return parse_rpc_error_v2(response)


def prompt_skill_operation_key(value: Mapping[str, Any]) -> str:
    """Return the deterministic fingerprint used by the idempotency ledger."""

    request = parse_prompt_skill_execute_request_v2(value)
    return hash_jcs("prompt-skill-execute/v2", request["params"])


class PromptSkillOperationLedgerV2:
    """In-memory replay guard with mandatory authoritative Attempt fencing."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], tuple[bytes, bytes, bytes, dict[str, Any]]] = {}

    def record(
        self,
        request: Mapping[str, Any],
        result_or_response: Mapping[str, Any],
        *,
        authoritative_context: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        parsed_request = parse_prompt_skill_execute_request_v2(request)
        authority_bytes = _require_authority(parsed_request, authoritative_context=authoritative_context)
        parsed_result = validate_prompt_skill_execute_v2(
            parsed_request,
            result_or_response,
            authoritative_context=authoritative_context,
        )
        key = (parsed_request["params"]["workspace_id"], parsed_request["params"]["operation_key"])
        request_bytes = canonical_bytes(parsed_request)
        result_bytes = canonical_bytes(parsed_result)
        previous = self._entries.get(key)
        if previous is not None:
            if previous[2] != authority_bytes:
                raise ContractError(ErrorCode.STALE_LEASE, "Prompt Skill operation authority context changed")
            if previous[0] != request_bytes or previous[1] != result_bytes:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "Prompt Skill operation_key was reused with a different exchange")
            return copy.deepcopy(previous[3]), True
        self._entries[key] = (request_bytes, result_bytes, authority_bytes, copy.deepcopy(parsed_result))
        return copy.deepcopy(parsed_result), False

    def replay(
        self,
        request: Mapping[str, Any],
        *,
        authoritative_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        parsed_request = parse_prompt_skill_execute_request_v2(request)
        authority_bytes = _require_authority(parsed_request, authoritative_context=authoritative_context)
        key = (parsed_request["params"]["workspace_id"], parsed_request["params"]["operation_key"])
        previous = self._entries.get(key)
        if previous is None or previous[0] != canonical_bytes(parsed_request):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Prompt Skill operation has no exact replay")
        if previous[2] != authority_bytes:
            raise ContractError(ErrorCode.STALE_LEASE, "Prompt Skill replay authority context is stale")
        return copy.deepcopy(previous[3])

    lookup = replay


# Friendly names used by downstream contract tests and adapters.
parse_request_v2 = parse_prompt_skill_execute_request_v2
parse_result_v2 = parse_prompt_skill_execute_result_v2
parse_success_v2 = validate_rpc_success_v2
parse_rpc_method_success_v2 = validate_rpc_success_v2
parse_rpc_success_v2 = validate_rpc_success_v2
parse_error_v2 = parse_rpc_error_v2
parse_envelope_v2 = parse_prompt_skill_rpc_envelope_v2
validate_request_v2 = parse_prompt_skill_execute_request_v2
validate_result_v2 = parse_prompt_skill_execute_result_v2
validate_rpc_method_success_v2 = validate_rpc_success_v2
validate_execute_v2 = validate_prompt_skill_execute_v2
validate_exchange_v2 = validate_prompt_skill_execute_v2


__all__ = [
    "AUTHORITY",
    "AuthoritativeAttemptContext",
    "ERROR_SCHEMA",
    "MAX_SAFE_INTEGER",
    "METHOD",
    "META_CONTEXT",
    "PROMPT_SKILL_EXECUTE_METHOD_V2",
    "PROMPT_SKILL_REQUEST_SCHEMA_V2",
    "PROMPT_SKILL_RESULT_CONTRACT_V1",
    "PROMPT_SKILL_RESULT_SCHEMA_V2",
    "PROMPT_SKILL_SUCCESS_SCHEMA_V2",
    "PROTOCOL_VERSION",
    "REQUEST_SCHEMA",
    "RESULT_CONTRACT",
    "RESULT_SCHEMA",
    "SUCCESS_SCHEMA",
    "AssetIdentity",
    "ModelReceiptIdentity",
    "PromptSkillExecuteRequest",
    "PromptSkillExecuteResult",
    "PromptSkillOperationLedgerV2",
    "ResultBundleIdentity",
    "SkillReleaseBinding",
    "build_prompt_skill_execute_request_v2",
    "build_prompt_skill_meta_v2",
    "build_rpc_error_v2",
    "build_rpc_success_v2",
    "parse_envelope_v2",
    "parse_error_v2",
    "parse_prompt_skill_execute_request_v2",
    "parse_prompt_skill_execute_result_v2",
    "parse_prompt_skill_rpc_envelope_v2",
    "parse_request_v2",
    "parse_result_v2",
    "parse_rpc_method_success_v2",
    "parse_rpc_success_v2",
    "parse_success_v2",
    "prompt_skill_operation_key",
    "validate_execute_v2",
    "validate_exchange_v2",
    "validate_prompt_skill_execute_v2",
    "validate_request_v2",
    "validate_result_v2",
    "validate_rpc_method_success_v2",
    "validate_rpc_success_v2",
]
