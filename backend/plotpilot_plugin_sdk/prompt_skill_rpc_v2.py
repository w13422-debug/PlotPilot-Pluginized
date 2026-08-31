"""Closed Prompt/Skill execute RPC v2 contract.

This module is a validation-only SDK surface.  It authenticates the identity
projection carried by a Core-owned execute exchange, but it does not create a
Skill, publish a Candidate, open a database, or provide a plugin-local
authority.  The v2 wire method is deliberately separate from the frozen v1
RPC matrix.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, TypedDict

from jsonschema import Draft202012Validator

from .canonical import canonical_bytes, hash_jcs
from .errors import ContractError, ContractValidationError, ErrorCode


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "contracts" / "json-schema"
REQUEST_SCHEMA_PATH = SCHEMA_DIR / "prompt-skill-execute-request-v2.schema.json"
RESULT_SCHEMA_PATH = SCHEMA_DIR / "prompt-skill-execute-result-v2.schema.json"
SUCCESS_SCHEMA_PATH = SCHEMA_DIR / "rpc-method-success-v2.schema.json"

METHOD = "prompt.skill.execute/v2"
REQUEST_SCHEMA = "prompt-skill-execute-request/v2"
RESULT_SCHEMA = "prompt-skill-execute-result/v2"
SUCCESS_SCHEMA = "rpc-method-success/v2"
AUTHORITY = "core"
RESULT_CONTRACT = "artifact-bundle/v1"
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
    "input_asset_id",
    "input_content_hash",
    "parameters_asset_id",
    "parameters_content_hash",
    "model_profile_revision_id",
    "anchor",
)


class SkillReleaseBinding(TypedDict):
    order: int
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
    run_snapshot_hash: str
    operation_key: str


class PromptSkillExecuteRequest(TypedDict):
    jsonrpc: Literal["2.0"]
    id: str
    method: Literal["prompt.skill.execute/v2"]
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


def _schema_path(path: tuple[Any, ...]) -> str:
    if not path:
        return "$"
    return "/" + "/".join(str(item) for item in path)


def _validate_schema(path: Path, value: Any) -> None:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"cannot load Prompt/Skill v2 schema: {path.name}") from exc
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(value), key=lambda error: (tuple(error.absolute_path), error.message))
    if errors:
        error = errors[0]
        _fail(f"{path.stem}: {error.message}", path=_schema_path(tuple(error.absolute_path)))


def _copy_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return copy.deepcopy(dict(value))


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


def _exact_pair(left: Any, right: Any, label: str) -> None:
    if (left is None) != (right is None):
        _fail(f"{label} must be all-null or all-present")


def _validate_skill_releases(value: Mapping[str, Any]) -> None:
    releases = value["skill_releases"]
    orders = [item["order"] for item in releases]
    skill_ids = [item["skill_id"] for item in releases]
    if len(orders) != len(set(orders)):
        _fail("skill_releases order values must be unique", path="skill_releases")
    if len(skill_ids) != len(set(skill_ids)):
        _fail("skill_releases Skill identities must be unique", path="skill_releases")
    if orders != sorted(orders):
        _fail("skill_releases must be in stable order", path="skill_releases")
    for index, release in enumerate(releases):
        _id(release["skill_id"], f"skill_releases[{index}].skill_id")
        _hash(release["release_id"], f"skill_releases[{index}].release_id")
        _hash(release["package_hash"], f"skill_releases[{index}].package_hash")
        _id(release["parameters_asset_id"], f"skill_releases[{index}].parameters_asset_id", nullable=True)
        _hash(release["parameters_content_hash"], f"skill_releases[{index}].parameters_content_hash", nullable=True)
        _exact_pair(
            release["parameters_asset_id"],
            release["parameters_content_hash"],
            f"skill_releases[{index}] parameters identity",
        )


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
        "operation_key",
        "workspace_id",
        "plugin_id",
        "run_snapshot_id",
        "run_snapshot_asset_id",
        "generation_id",
        "job_id",
        "step_id",
        "attempt_id",
        "chain_id",
        "input_asset_id",
    ):
        _id(value[field], field)
    for field in (
        "plugin_release_id",
        "plugin_package_hash",
        "run_snapshot_hash",
        "input_content_hash",
    ):
        _hash(value[field], field)
    _id(value["model_profile_revision_id"], "model_profile_revision_id", nullable=True)
    if type(value["lease_epoch"]) is not int or value["lease_epoch"] < 1:
        _fail("lease_epoch must be a positive integer", path="lease_epoch")
    _id(value["parameters_asset_id"], "parameters_asset_id", nullable=True)
    _hash(value["parameters_content_hash"], "parameters_content_hash", nullable=True)
    _exact_pair(value["parameters_asset_id"], value["parameters_content_hash"], "parameters identity")
    _validate_skill_releases(value)
    _validate_anchor(value["anchor"])


def _validate_model_receipt(value: Mapping[str, Any]) -> None:
    for field in ("receipt_id", "asset_id"):
        _id(value[field], f"model_receipt.{field}")
    for field in ("content_hash", "receipt_hash"):
        _hash(value[field], f"model_receipt.{field}")
    _id(value["operation_key"], "model_receipt.operation_key")
    _hash(value["run_snapshot_hash"], "model_receipt.run_snapshot_hash")
    _id(value["model_profile_revision_id"], "model_receipt.model_profile_revision_id", nullable=True)
    _id(value["input_asset_id"], "model_receipt.input_asset_id")
    _hash(value["input_content_hash"], "model_receipt.input_content_hash")


def _validate_bundle(value: Mapping[str, Any]) -> None:
    if value["contract_id"] != RESULT_CONTRACT:
        _fail("result Bundle contract is not the frozen Core contract", path="result_bundle.contract_id")
    for field in ("bundle_id", "asset_id", "result_item_id", "workspace_id", "operation_key"):
        _id(value[field], f"result_bundle.{field}")
    _hash(value["content_hash"], "result_bundle.content_hash")
    _hash(value["run_snapshot_hash"], "result_bundle.run_snapshot_hash")


def _validate_output(value: Mapping[str, Any] | None) -> None:
    if value is None:
        return
    _id(value["asset_id"], "output.asset_id")
    _hash(value["content_hash"], "output.content_hash")


def _validate_result_semantics(value: Mapping[str, Any]) -> None:
    _validate_common(value, label="Prompt Skill result")
    status = value["status"]
    model_receipt = value["model_receipt"]
    result_bundle = value["result_bundle"]
    output = value["output"]
    if model_receipt is not None:
        _validate_model_receipt(model_receipt)
        receipt_bindings = {
            "operation_key": value["operation_key"],
            "run_snapshot_hash": value["run_snapshot_hash"],
            "model_profile_revision_id": value["model_profile_revision_id"],
            "input_asset_id": value["input_asset_id"],
            "input_content_hash": value["input_content_hash"],
        }
        for field, expected in receipt_bindings.items():
            if model_receipt[field] != expected:
                _fail(f"ModelReceipt {field} does not match the execute identity", path=f"model_receipt.{field}")
    if result_bundle is not None:
        _validate_bundle(result_bundle)
        bundle_bindings = {
            "operation_key": value["operation_key"],
            "run_snapshot_hash": value["run_snapshot_hash"],
            "workspace_id": value["workspace_id"],
        }
        for field, expected in bundle_bindings.items():
            if result_bundle[field] != expected:
                _fail(f"result Bundle {field} does not match the execute identity", path=f"result_bundle.{field}")
    _validate_output(output)
    if status == "succeeded":
        if model_receipt is None:
            _fail("succeeded Prompt Skill execution requires a ModelReceipt", path="model_receipt")
        if result_bundle is None:
            _fail("succeeded Prompt Skill execution requires a result Bundle", path="result_bundle")
        anchor = value["anchor"]
        if anchor["result_bundle_id"] != result_bundle["bundle_id"] or anchor["result_item_id"] != result_bundle["result_item_id"]:
            _fail("result Bundle identity does not match the frozen execution anchor", path="result_bundle")
        if anchor["stream_id"] is not None or anchor["acked_prefix_hash"] is not None:
            _fail("Bundle-backed result cannot carry a stream anchor", path="anchor")
    elif status == "uncertain":
        if model_receipt is not None or result_bundle is not None or output is not None:
            _fail("uncertain execution cannot expose unproven receipt, Bundle, or output", path="status")
    else:
        if result_bundle is not None or output is not None:
            _fail("non-success execution cannot expose a result Bundle or output", path="status")
    if type(value["idempotent"]) is not bool:
        _fail("idempotent must be boolean", path="idempotent")
    for index, warning in enumerate(value["warnings"]):
        if not isinstance(warning["code"], str) or not warning["code"]:
            _fail(f"warnings[{index}].code must be non-empty", path=f"warnings[{index}].code")
        if not isinstance(warning["message"], str):
            _fail(f"warnings[{index}].message must be a string", path=f"warnings[{index}].message")


def parse_prompt_skill_execute_request_v2(value: Mapping[str, Any]) -> PromptSkillExecuteRequest:
    """Validate and copy one complete JSON-RPC execute request."""

    request = _copy_object(value, "Prompt Skill request")
    _validate_schema(REQUEST_SCHEMA_PATH, request)
    if request["jsonrpc"] != "2.0" or request["method"] != METHOD:
        _fail("Prompt Skill request method/version is not the frozen v2 method")
    if _RPC_ID.fullmatch(request["id"]) is None:
        _fail("RPC request id is not a UUID", path="id")
    _validate_common(request["params"], label="Prompt Skill request")
    return request  # type: ignore[return-value]


def parse_prompt_skill_execute_result_v2(value: Mapping[str, Any]) -> PromptSkillExecuteResult:
    """Validate and copy one Core-owned execute result payload."""

    result = _copy_object(value, "Prompt Skill result")
    _validate_schema(RESULT_SCHEMA_PATH, result)
    if result["schema"] != RESULT_SCHEMA:
        _fail("Prompt Skill result schema discriminator is not v2", path="schema")
    _validate_result_semantics(result)
    return result  # type: ignore[return-value]


def validate_rpc_success_v2(
    value: Mapping[str, Any],
    *,
    request: Mapping[str, Any] | None = None,
) -> PromptSkillExecuteResult:
    """Validate a closed success envelope and its method-specific result."""

    response = _copy_object(value, "RPC success response")
    _validate_schema(SUCCESS_SCHEMA_PATH, response)
    if response["jsonrpc"] != "2.0":
        _fail("RPC success response must use JSON-RPC 2.0", path="jsonrpc")
    if request is not None:
        parsed_request = parse_prompt_skill_execute_request_v2(request)
        if response["id"] != parsed_request["id"]:
            _fail("RPC response id does not match its request", path="id")
    return parse_prompt_skill_execute_result_v2(response["result"])


def _binding_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    return {field: copy.deepcopy(value[field]) for field in _BOUND_FIELDS}


def validate_prompt_skill_execute_v2(
    request: Mapping[str, Any],
    result_or_response: Mapping[str, Any],
    *,
    expected_lease_epoch: int | None = None,
) -> PromptSkillExecuteResult:
    """Bind one request to its result without introducing plugin authority."""

    parsed_request = parse_prompt_skill_execute_request_v2(request)
    if "result" in result_or_response and result_or_response.get("jsonrpc") == "2.0":
        parsed_result = validate_rpc_success_v2(result_or_response, request=parsed_request)
    else:
        parsed_result = parse_prompt_skill_execute_result_v2(result_or_response)
    params = parsed_request["params"]
    if canonical_bytes(_binding_projection(params)) != canonical_bytes(_binding_projection(parsed_result)):
        _fail("Prompt Skill result identity projection does not match its request")
    if params["operation_key"] != parsed_result["operation_key"]:
        _fail("Prompt Skill operation_key is not stable across the exchange", path="operation_key")
    if expected_lease_epoch is not None:
        if type(expected_lease_epoch) is not int or expected_lease_epoch < 1:
            _fail("expected_lease_epoch must be a positive integer")
        if params["lease_epoch"] != expected_lease_epoch or parsed_result["lease_epoch"] != expected_lease_epoch:
            raise ContractError(ErrorCode.STALE_LEASE, "Prompt Skill execute lease epoch is stale", path="lease_epoch")
    return parsed_result


def build_prompt_skill_execute_request_v2(
    params: Mapping[str, Any],
    *,
    request_id: str,
) -> PromptSkillExecuteRequest:
    """Build a request only after validating the complete closed params object."""

    request = {"jsonrpc": "2.0", "id": request_id, "method": METHOD, "params": dict(params)}
    return parse_prompt_skill_execute_request_v2(request)


def build_rpc_success_v2(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a success envelope after exact request/result binding."""

    parsed_request = parse_prompt_skill_execute_request_v2(request)
    parsed_result = validate_prompt_skill_execute_v2(parsed_request, result)
    response = {"jsonrpc": "2.0", "id": parsed_request["id"], "result": parsed_result}
    validate_rpc_success_v2(response, request=parsed_request)
    return response


def prompt_skill_operation_key(value: Mapping[str, Any]) -> str:
    """Return the deterministic fingerprint used by an idempotency ledger."""

    request = parse_prompt_skill_execute_request_v2(value)
    return hash_jcs("prompt-skill-execute/v2", request["params"])


class PromptSkillOperationLedgerV2:
    """Small in-memory replay guard for tests and adapters.

    It stores no business state and has no publish/DB operation.  A reused
    operation key is accepted only when both the complete request and the
    complete Core result are byte-identical.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], tuple[bytes, bytes, dict[str, Any]]] = {}

    def record(
        self,
        request: Mapping[str, Any],
        result_or_response: Mapping[str, Any],
        *,
        expected_lease_epoch: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        parsed_request = parse_prompt_skill_execute_request_v2(request)
        parsed_result = validate_prompt_skill_execute_v2(
            parsed_request,
            result_or_response,
            expected_lease_epoch=expected_lease_epoch,
        )
        key = (parsed_request["params"]["workspace_id"], parsed_request["params"]["operation_key"])
        request_bytes = canonical_bytes(parsed_request)
        result_bytes = canonical_bytes(parsed_result)
        previous = self._entries.get(key)
        if previous is not None:
            if previous[0] != request_bytes or previous[1] != result_bytes:
                raise ContractError(ErrorCode.DUPLICATE_REQUEST, "Prompt Skill operation_key was reused with a different exchange")
            return copy.deepcopy(previous[2]), True
        self._entries[key] = (request_bytes, result_bytes, copy.deepcopy(parsed_result))
        return copy.deepcopy(parsed_result), False

    def replay(self, request: Mapping[str, Any]) -> dict[str, Any]:
        parsed_request = parse_prompt_skill_execute_request_v2(request)
        key = (parsed_request["params"]["workspace_id"], parsed_request["params"]["operation_key"])
        previous = self._entries.get(key)
        if previous is None or previous[0] != canonical_bytes(parsed_request):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Prompt Skill operation has no exact replay")
        return copy.deepcopy(previous[2])


# Friendly names used by downstream contract tests and adapters.
parse_request_v2 = parse_prompt_skill_execute_request_v2
parse_result_v2 = parse_prompt_skill_execute_result_v2
parse_success_v2 = validate_rpc_success_v2
parse_rpc_method_success_v2 = validate_rpc_success_v2
parse_rpc_success_v2 = validate_rpc_success_v2
validate_request_v2 = parse_prompt_skill_execute_request_v2
validate_result_v2 = parse_prompt_skill_execute_result_v2
validate_rpc_method_success_v2 = validate_rpc_success_v2
validate_execute_v2 = validate_prompt_skill_execute_v2
validate_exchange_v2 = validate_prompt_skill_execute_v2


__all__ = [
    "AUTHORITY",
    "METHOD",
    "PROMPT_SKILL_EXECUTE_METHOD_V2",
    "PROMPT_SKILL_REQUEST_SCHEMA_V2",
    "PROMPT_SKILL_RESULT_CONTRACT_V1",
    "PROMPT_SKILL_RESULT_SCHEMA_V2",
    "PROMPT_SKILL_SUCCESS_SCHEMA_V2",
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
    "build_rpc_success_v2",
    "parse_prompt_skill_execute_request_v2",
    "parse_prompt_skill_execute_result_v2",
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
