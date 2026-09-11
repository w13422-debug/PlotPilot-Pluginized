"""Additive Core-to-Provider RPC overlay for model invocation.

This module validates only the Provider transport overlay and its bindings.  A
canonical model receipt remains an opaque authority produced and decoded by
the existing Provider/Prompt-Skill implementation.  Callers either pass that
already-authoritatively-decoded view or a callback returning the accepted
receipt/Asset proof; this module never implements a second full receipt
decoder.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Callable, Mapping
from importlib.resources import files as resource_files
from typing import Any, Literal, TypedDict

from jsonschema import Draft202012Validator

from .canonical import canonical_bytes, hash_jcs
from .errors import ContractError, ContractValidationError, ErrorCode
from .prompt_skill_rpc_v2 import MAX_SAFE_INTEGER
from .rpc import ERROR_CODES


METHOD = "model.provider.invoke/v1"
METHOD_MATRIX_SCHEMA = "model-provider-rpc-method-matrix/v2"
REQUEST_SCHEMA = "model-provider-invoke-request/v2"
RESULT_SCHEMA = "model-provider-invoke-result/v2"
SUCCESS_SCHEMA = "model-provider-invoke-success/v2"
ERROR_SCHEMA = "rpc-error/v1"
AUTHORITY = "core"
DIRECTION = "host-to-provider"
PROTOCOL_VERSION = "1"
META_CONTEXT = "attempt"
TERMINAL_STATES = ("receipted", "failed", "cancelled", "uncertain")

MODEL_PROVIDER_INVOKE_METHOD_V1 = METHOD
MODEL_PROVIDER_RPC_METHOD_MATRIX_SCHEMA_V2 = METHOD_MATRIX_SCHEMA
MODEL_PROVIDER_REQUEST_SCHEMA_V2 = REQUEST_SCHEMA
MODEL_PROVIDER_RESULT_SCHEMA_V2 = RESULT_SCHEMA
MODEL_PROVIDER_SUCCESS_SCHEMA_V2 = SUCCESS_SCHEMA

PLANNER_CONTEXT_FIELDS = (
    "operation_key",
    "workspace_id",
    "plugin_id",
    "plugin_release_id",
    "plugin_package_hash",
    "generation_id",
    "job_id",
    "step_id",
    "attempt_id",
    "lease_epoch",
    "chain_id",
    "chain_asset_id",
    "chain_content_hash",
    "run_snapshot_id",
    "run_snapshot_asset_id",
    "run_snapshot_hash",
    "model_profile_revision_id",
    "input_asset_id",
    "input_content_hash",
)
MODEL_RECEIPT_ANCHOR_FIELDS = (
    "receipt_id",
    "asset_id",
    "content_hash",
    "receipt_hash",
    *PLANNER_CONTEXT_FIELDS,
)
HASH_DOMAINS = (
    "model_request_asset.content_hash",
    "model_receipt_anchor.content_hash",
    "provider_transport_request_hash",
    "provider_transport_response_hash",
    "response_asset.content_hash",
    "model_receipt_anchor.receipt_hash",
)

_MATRIX_RESOURCE = "model-provider-rpc-method-matrix.v2.json"
_REQUEST_RESOURCE = "model-provider-invoke-request-v2.schema.json"
_RESULT_RESOURCE = "model-provider-invoke-result-v2.schema.json"
_SUCCESS_RESOURCE = "model-provider-invoke-success-v2.schema.json"
_ERROR_RESOURCE = "rpc-error-v1.schema.json"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_RPC_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


class AssetIdentity(TypedDict):
    asset_id: str
    content_hash: str


class PlannerContext(TypedDict):
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


class ModelReceiptAnchor(PlannerContext):
    receipt_id: str
    asset_id: str
    content_hash: str
    receipt_hash: str


class ModelProviderInvokeRequest(TypedDict):
    jsonrpc: Literal["2.0"]
    id: str
    method: Literal["model.provider.invoke/v1"]
    meta: dict[str, Any]
    params: dict[str, Any]


class ModelProviderInvokeResult(TypedDict):
    schema: Literal["model-provider-invoke-result/v2"]
    model_receipt_anchor: ModelReceiptAnchor
    model_request_asset: AssetIdentity
    provider_transport_request_hash: str
    response_asset: AssetIdentity | None
    provider_transport_response_hash: str | None
    provider_terminal_state: Literal[
        "receipted", "failed", "cancelled", "uncertain"
    ]


ReceiptVerifier = Callable[[Mapping[str, Any]], Any]
AssetVerifier = Callable[[Mapping[str, Any], str], Any]


def _fail(message: str, *, path: str | None = None) -> None:
    raise ContractValidationError(message, path=path)


def _resource_bytes(name: str) -> bytes:
    try:
        return resource_files("plotpilot_plugin_sdk").joinpath("resources", name).read_bytes()
    except (FileNotFoundError, OSError, ModuleNotFoundError) as exc:
        raise ContractValidationError(
            f"missing packaged Model Provider contract resource: {name}"
        ) from exc


def _resource_object(name: str) -> dict[str, Any]:
    try:
        value = json.loads(_resource_bytes(name).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            f"invalid packaged Model Provider contract resource: {name}"
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError(f"packaged resource is not an object: {name}")
    return value


def _validate_matrix(value: Mapping[str, Any]) -> None:
    expected_root = {
        "schema",
        "protocol",
        "authority",
        "direction",
        "plugin_authority_allowed",
        "reserved_method_ids",
        "envelope",
        "methods",
    }
    if set(value) != expected_root:
        _fail("Model Provider reserved-method registry shape drift")
    if (
        value["schema"] != METHOD_MATRIX_SCHEMA
        or value["authority"] != AUTHORITY
        or value["direction"] != DIRECTION
        or value["plugin_authority_allowed"] is not False
        or value["reserved_method_ids"] != [METHOD]
        or value["envelope"]
        != {"exactly_one": True, "members": ["request", "success", "error"]}
    ):
        _fail("Model Provider reserved-method registry authority drift")
    if value["protocol"] != {
        "jsonrpc": "2.0",
        "version": PROTOCOL_VERSION,
        "framing": "content-length-crlf",
        "encoding": "utf-8",
        "batch": False,
    }:
        _fail("Model Provider protocol registry drift")
    methods = value["methods"]
    if not isinstance(methods, list) or len(methods) != 1:
        _fail("Model Provider method inventory drift")
    method = methods[0]
    expected_method = {
        "method": METHOD,
        "reserved": True,
        "authority": AUTHORITY,
        "direction": DIRECTION,
        "endpoint": "provider",
        "protocol_version": PROTOCOL_VERSION,
        "framing": "content-length-crlf",
        "meta_profile": META_CONTEXT,
        "lease_fenced": True,
        "operation_key_required": True,
        "request_schema": REQUEST_SCHEMA,
        "result_schema": RESULT_SCHEMA,
        "success_schema": SUCCESS_SCHEMA,
        "error_schema": ERROR_SCHEMA,
        "error_code_registry": "rpc-method-matrix/v1#error_codes",
        "terminal_states": list(TERMINAL_STATES),
        "terminal_states_use_jsonrpc_success": True,
        "rpc_error_meaning": "no-verifiable-terminal-receipt",
    }
    if not isinstance(method, Mapping) or dict(method) != expected_method:
        _fail("Model Provider method registry drift")


_METHOD_MATRIX = _resource_object(_MATRIX_RESOURCE)
_validate_matrix(_METHOD_MATRIX)
RESERVED_METHOD_IDS = frozenset(_METHOD_MATRIX["reserved_method_ids"])
MODEL_PROVIDER_RESERVED_METHOD_IDS = RESERVED_METHOD_IDS


def get_model_provider_rpc_method_matrix_v2() -> dict[str, Any]:
    """Return a defensive copy of the separate Provider method registry."""

    return copy.deepcopy(_METHOD_MATRIX)


def get_reserved_model_provider_method_ids() -> frozenset[str]:
    return frozenset(RESERVED_METHOD_IDS)


def _schema_path(path: tuple[Any, ...]) -> str:
    return "$" if not path else "/" + "/".join(str(item) for item in path)


def _validate_schema(name: str, value: Any) -> None:
    validator = Draft202012Validator(_resource_object(name))
    errors = sorted(
        validator.iter_errors(value),
        key=lambda error: (tuple(error.absolute_path), error.message),
    )
    if errors:
        first = errors[0]
        _fail(
            f"{name}: {first.message}",
            path=_schema_path(tuple(first.absolute_path)),
        )


def _normalize_text(value: str, path: str) -> str:
    """Normalize valid escaped surrogate pairs and reject lone surrogates."""

    output: list[str] = []
    index = 0
    while index < len(value):
        code = ord(value[index])
        if 0xD800 <= code <= 0xDBFF:
            if index + 1 >= len(value) or not 0xDC00 <= ord(value[index + 1]) <= 0xDFFF:
                _fail("string contains a lone UTF-16 surrogate", path=path)
            output.append(
                chr(
                    0x10000
                    + ((code - 0xD800) << 10)
                    + (ord(value[index + 1]) - 0xDC00)
                )
            )
            index += 2
            continue
        if 0xDC00 <= code <= 0xDFFF:
            _fail("string contains a lone UTF-16 surrogate", path=path)
        output.append(value[index])
        index += 1
    result = "".join(output)
    try:
        result.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:  # pragma: no cover - defensive parity
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
        if (
            not math.isfinite(value)
            or not value.is_integer()
            or abs(value) > MAX_SAFE_INTEGER
        ):
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
        return [
            _normalize_json(child, f"{path}[{index}]")
            for index, child in enumerate(value)
        ]
    _fail("value is not a JSON value", path=path)
    raise AssertionError


def _copy_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    normalized = _normalize_json(value, label)
    if not isinstance(normalized, dict):  # pragma: no cover - Mapping above
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


def _safe_positive(value: Any, field: str) -> int:
    if type(value) is not int or value < 1 or value > MAX_SAFE_INTEGER:
        _fail(
            f"{field} must be an IEEE-754 safe positive integer",
            path=field,
        )
    return value


def _validate_planner_context(value: Mapping[str, Any], *, label: str) -> None:
    for field in (
        "operation_key",
        "workspace_id",
        "plugin_id",
        "generation_id",
        "job_id",
        "step_id",
        "attempt_id",
        "chain_id",
        "chain_asset_id",
        "run_snapshot_id",
        "run_snapshot_asset_id",
        "input_asset_id",
    ):
        _id(value[field], f"{label}.{field}")
    for field in (
        "plugin_release_id",
        "plugin_package_hash",
        "chain_content_hash",
        "run_snapshot_hash",
        "input_content_hash",
    ):
        _hash(value[field], f"{label}.{field}")
    _id(
        value["model_profile_revision_id"],
        f"{label}.model_profile_revision_id",
        nullable=True,
    )
    _safe_positive(value["lease_epoch"], f"{label}.lease_epoch")


def _validate_asset(value: Mapping[str, Any], label: str) -> None:
    _id(value["asset_id"], f"{label}.asset_id")
    _hash(value["content_hash"], f"{label}.content_hash")


def _validate_meta(meta: Mapping[str, Any], context: Mapping[str, Any]) -> None:
    if meta["protocol_version"] != PROTOCOL_VERSION or meta["context"] != META_CONTEXT:
        _fail("Model Provider request meta profile drift", path="meta")
    for field in ("operation_id", "generation_id", "job_id", "step_id", "attempt_id"):
        _id(meta[field], f"meta.{field}")
    _safe_positive(meta["lease_epoch"], "meta.lease_epoch")
    bindings = {
        "operation_id": "operation_key",
        "generation_id": "generation_id",
        "job_id": "job_id",
        "step_id": "step_id",
        "attempt_id": "attempt_id",
        "lease_epoch": "lease_epoch",
    }
    for meta_field, context_field in bindings.items():
        if meta[meta_field] != context[context_field]:
            _fail(
                f"request meta {meta_field} does not match planner_context.{context_field}",
                path=f"meta.{meta_field}",
            )


def parse_model_provider_invoke_request_v2(
    value: Mapping[str, Any],
) -> ModelProviderInvokeRequest:
    """Validate and defensively copy one complete Provider request envelope."""

    request = _copy_object(value, "Model Provider request")
    _validate_schema(_REQUEST_RESOURCE, request)
    if (
        request["jsonrpc"] != "2.0"
        or request["method"] != METHOD
        or _RPC_ID.fullmatch(request["id"]) is None
    ):
        _fail("Model Provider request method/version/id drift")
    context = request["params"]["planner_context"]
    _validate_planner_context(context, label="planner_context")
    _validate_asset(request["params"]["model_request_asset"], "model_request_asset")
    _validate_meta(request["meta"], context)
    return request  # type: ignore[return-value]


def parse_model_provider_invoke_result_v2(
    value: Mapping[str, Any],
) -> ModelProviderInvokeResult:
    """Validate the closed overlay payload without decoding its receipt Asset."""

    result = _copy_object(value, "Model Provider result")
    _validate_schema(_RESULT_RESOURCE, result)
    if result["schema"] != RESULT_SCHEMA:
        _fail("Model Provider result discriminator drift", path="schema")
    anchor = result["model_receipt_anchor"]
    _validate_planner_context(anchor, label="model_receipt_anchor")
    for field in ("receipt_id", "asset_id"):
        _id(anchor[field], f"model_receipt_anchor.{field}")
    for field in ("content_hash", "receipt_hash"):
        _hash(anchor[field], f"model_receipt_anchor.{field}")
    _validate_asset(result["model_request_asset"], "model_request_asset")
    _hash(result["provider_transport_request_hash"], "provider_transport_request_hash")
    _hash(
        result["provider_transport_response_hash"],
        "provider_transport_response_hash",
        nullable=True,
    )
    if result["response_asset"] is not None:
        _validate_asset(result["response_asset"], "response_asset")
    if result["provider_terminal_state"] not in TERMINAL_STATES:
        _fail("Provider terminal state drift", path="provider_terminal_state")
    return result  # type: ignore[return-value]


def parse_model_provider_rpc_error_v2(
    value: Mapping[str, Any],
    *,
    request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the unchanged v1 RPC error member and optional request ID."""

    error = _copy_object(value, "Model Provider RPC error")
    _validate_schema(_ERROR_RESOURCE, error)
    if error["jsonrpc"] != "2.0":
        _fail("RPC error response must use JSON-RPC 2.0", path="jsonrpc")
    code = error["error"]["code"]
    if type(code) is not int or code not in ERROR_CODES:
        _fail("RPC error code is not registered in rpc-method-matrix/v1", path="error.code")
    _normalize_text(error["error"]["message"], "error.message")
    if error["id"] is not None and _RPC_ID.fullmatch(error["id"]) is None:
        _fail("RPC error response id is not a UUID", path="id")
    if request is not None:
        parsed_request = parse_model_provider_invoke_request_v2(request)
        if error["id"] is None or error["id"] != parsed_request["id"]:
            _fail("RPC error response id does not match its parsed request", path="id")
    return error


def _receipt_from_evidence(
    anchor: Mapping[str, Any],
    *,
    canonical_receipt: Mapping[str, Any] | Any | None,
    receipt_verifier: ReceiptVerifier | None,
) -> Mapping[str, Any]:
    if canonical_receipt is not None and receipt_verifier is not None:
        _fail("canonical_receipt and receipt_verifier are mutually exclusive")
    evidence: Any
    if receipt_verifier is not None:
        evidence = receipt_verifier(copy.deepcopy(dict(anchor)))
    else:
        evidence = canonical_receipt
    if evidence is None:
        _fail(
            "an accepted canonical receipt view or receipt verifier is required"
        )

    receipt = getattr(evidence, "receipt", evidence)
    if not isinstance(receipt, Mapping):
        _fail("canonical receipt evidence did not return a decoded receipt view")

    # An AttributionProof-like value binds the authoritative Asset identity as
    # well as the decoded receipt.  A direct decoded view is trusted only under
    # the caller's explicit already-authoritative contract.
    if evidence is not receipt:
        evidence_asset_id = getattr(evidence, "asset_id", None)
        evidence_asset_hash = getattr(evidence, "asset_hash", None)
        if evidence_asset_id != anchor["asset_id"]:
            _fail(
                "canonical receipt Asset ID does not match its overlay anchor",
                path="model_receipt_anchor.asset_id",
            )
        if evidence_asset_hash != anchor["content_hash"]:
            _fail(
                "canonical receipt Asset hash does not match its overlay anchor",
                path="model_receipt_anchor.content_hash",
            )
    return receipt


def _bind_canonical_receipt(
    result: Mapping[str, Any],
    *,
    canonical_receipt: Mapping[str, Any] | Any | None,
    receipt_verifier: ReceiptVerifier | None,
) -> None:
    anchor = result["model_receipt_anchor"]
    receipt = _receipt_from_evidence(
        anchor,
        canonical_receipt=canonical_receipt,
        receipt_verifier=receipt_verifier,
    )
    mirrors = {
        "receipt_id": anchor["receipt_id"],
        "receipt_hash": anchor["receipt_hash"],
        "state": result["provider_terminal_state"],
        "request_hash": result["provider_transport_request_hash"],
        "response_hash": result["provider_transport_response_hash"],
        "profile_revision_id": anchor["model_profile_revision_id"],
    }
    try:
        for receipt_field, expected in mirrors.items():
            if receipt[receipt_field] != expected:
                _fail(
                    f"canonical receipt {receipt_field} does not match the Provider overlay",
                    path=receipt_field,
                )
        receipt_response_asset_id = receipt["response_asset_id"]
    except KeyError as exc:
        _fail(
            "accepted canonical receipt view lacks a required mirror field",
            path=str(exc),
        )
        raise AssertionError from exc

    response_asset = result["response_asset"]
    if receipt_response_asset_id is None:
        if response_asset is not None:
            _fail(
                "response_asset must be null when the canonical receipt has no response Asset",
                path="response_asset",
            )
    elif response_asset is None or response_asset["asset_id"] != receipt_response_asset_id:
        _fail(
            "response_asset ID does not mirror the canonical receipt",
            path="response_asset",
        )


def _bind_request_result(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    context = request["params"]["planner_context"]
    anchor = result["model_receipt_anchor"]
    for field in PLANNER_CONTEXT_FIELDS:
        if context[field] != anchor[field]:
            _fail(
                f"model_receipt_anchor.{field} does not match planner_context",
                path=f"model_receipt_anchor.{field}",
            )
    if canonical_bytes(request["params"]["model_request_asset"]) != canonical_bytes(
        result["model_request_asset"]
    ):
        _fail("result model_request_asset does not match its request")


def validate_model_provider_invoke_result_v2(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    canonical_receipt: Mapping[str, Any] | Any | None = None,
    receipt_verifier: ReceiptVerifier | None = None,
    asset_verifier: AssetVerifier | None = None,
) -> ModelProviderInvokeResult:
    """Bind one result to its request and accepted canonical receipt authority."""

    parsed_request = parse_model_provider_invoke_request_v2(request)
    parsed_result = parse_model_provider_invoke_result_v2(result)
    _bind_request_result(parsed_request, parsed_result)
    if asset_verifier is not None:
        request_asset_verdict = asset_verifier(
            copy.deepcopy(parsed_result["model_request_asset"]),
            "model_request_asset",
        )
        if request_asset_verdict is False:
            _fail("model_request_asset verifier rejected the Host Asset identity")
        if parsed_result["response_asset"] is not None:
            response_asset_verdict = asset_verifier(
                copy.deepcopy(parsed_result["response_asset"]),
                "response_asset",
            )
            if response_asset_verdict is False:
                _fail("response_asset verifier rejected the Host Asset identity")
    _bind_canonical_receipt(
        parsed_result,
        canonical_receipt=canonical_receipt,
        receipt_verifier=receipt_verifier,
    )
    return parsed_result


def validate_model_provider_rpc_success_v2(
    value: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    canonical_receipt: Mapping[str, Any] | Any | None = None,
    receipt_verifier: ReceiptVerifier | None = None,
    asset_verifier: AssetVerifier | None = None,
) -> ModelProviderInvokeResult:
    """Validate a success envelope, exact ID and terminal receipt binding."""

    parsed_request = parse_model_provider_invoke_request_v2(request)
    response = _copy_object(value, "Model Provider RPC success")
    _validate_schema(_SUCCESS_RESOURCE, response)
    if response["jsonrpc"] != "2.0" or response["id"] != parsed_request["id"]:
        _fail("RPC success response id does not match its parsed request", path="id")
    return validate_model_provider_invoke_result_v2(
        parsed_request,
        response["result"],
        canonical_receipt=canonical_receipt,
        receipt_verifier=receipt_verifier,
        asset_verifier=asset_verifier,
    )


def validate_model_provider_rpc_response_v2(
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    canonical_receipt: Mapping[str, Any] | Any | None = None,
    receipt_verifier: ReceiptVerifier | None = None,
    asset_verifier: AssetVerifier | None = None,
) -> ModelProviderInvokeResult | dict[str, Any]:
    """Validate exactly one success/error response for a parsed request."""

    parsed_request = parse_model_provider_invoke_request_v2(request)
    envelope = _copy_object(response, "Model Provider RPC response")
    kinds = [key for key in ("result", "error") if key in envelope]
    if len(kinds) != 1 or "method" in envelope:
        _fail("Model Provider RPC response must contain exactly one success/error member")
    if kinds[0] == "error":
        if canonical_receipt is not None or receipt_verifier is not None:
            _fail("RPC error cannot be paired with terminal receipt evidence")
        return parse_model_provider_rpc_error_v2(envelope, request=parsed_request)
    return validate_model_provider_rpc_success_v2(
        envelope,
        request=parsed_request,
        canonical_receipt=canonical_receipt,
        receipt_verifier=receipt_verifier,
        asset_verifier=asset_verifier,
    )


def parse_model_provider_rpc_envelope_v2(
    value: Mapping[str, Any],
    *,
    request: Mapping[str, Any] | None = None,
    canonical_receipt: Mapping[str, Any] | Any | None = None,
    receipt_verifier: ReceiptVerifier | None = None,
    asset_verifier: AssetVerifier | None = None,
) -> Any:
    """Dispatch a logical exactly-one-of request/success/error envelope."""

    envelope = _copy_object(value, "Model Provider RPC envelope")
    kinds = [key for key in ("method", "result", "error") if key in envelope]
    if len(kinds) != 1:
        _fail("Model Provider RPC envelope must contain exactly one request/success/error member")
    if kinds[0] == "method":
        if request is not None:
            _fail("a request envelope cannot be parsed as a response")
        return parse_model_provider_invoke_request_v2(envelope)
    if request is None:
        _fail("a parsed request is required for every Provider response envelope")
    return validate_model_provider_rpc_response_v2(
        request,
        envelope,
        canonical_receipt=canonical_receipt,
        receipt_verifier=receipt_verifier,
        asset_verifier=asset_verifier,
    )


def build_model_provider_meta_v2(
    planner_context: Mapping[str, Any],
) -> dict[str, Any]:
    context = _copy_object(planner_context, "planner_context")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "context": META_CONTEXT,
        "operation_id": context["operation_key"],
        "generation_id": context["generation_id"],
        "job_id": context["job_id"],
        "step_id": context["step_id"],
        "attempt_id": context["attempt_id"],
        "lease_epoch": context["lease_epoch"],
    }


def build_model_provider_invoke_request_v2(
    planner_context: Mapping[str, Any],
    model_request_asset: Mapping[str, Any],
    *,
    request_id: str,
    meta: Mapping[str, Any] | None = None,
) -> ModelProviderInvokeRequest:
    context = _copy_object(planner_context, "planner_context")
    asset = _copy_object(model_request_asset, "model_request_asset")
    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": METHOD,
        "meta": (
            _copy_object(meta, "Model Provider meta")
            if meta is not None
            else build_model_provider_meta_v2(context)
        ),
        "params": {
            "schema": REQUEST_SCHEMA,
            "planner_context": context,
            "model_request_asset": asset,
        },
    }
    return parse_model_provider_invoke_request_v2(request)


def build_model_provider_invoke_success_v2(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    canonical_receipt: Mapping[str, Any] | Any | None = None,
    receipt_verifier: ReceiptVerifier | None = None,
    asset_verifier: AssetVerifier | None = None,
) -> dict[str, Any]:
    parsed_request = parse_model_provider_invoke_request_v2(request)
    response = {
        "jsonrpc": "2.0",
        "id": parsed_request["id"],
        "result": _copy_object(result, "Model Provider result"),
    }
    parsed_result = validate_model_provider_rpc_success_v2(
        response,
        request=parsed_request,
        canonical_receipt=canonical_receipt,
        receipt_verifier=receipt_verifier,
        asset_verifier=asset_verifier,
    )
    response["result"] = parsed_result
    return response


def build_model_provider_rpc_error_v2(
    request_id: str | None,
    *,
    code: int,
    message: str,
    retryable: bool,
    error_id: str = "model-provider-error-1",
    details_asset_id: str | None = None,
) -> dict[str, Any]:
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
            "data": {
                "error_id": error_id,
                "retryable": retryable,
                "details_asset_id": details_asset_id,
            },
        },
    }
    return parse_model_provider_rpc_error_v2(response)


def model_provider_operation_digest_v2(value: Mapping[str, Any]) -> str:
    request = parse_model_provider_invoke_request_v2(value)
    return hash_jcs("model-provider-invoke/v2", request["params"])


def validate_model_provider_invoke_v2(
    request: Mapping[str, Any],
    result_or_success: Mapping[str, Any],
    *,
    canonical_receipt: Mapping[str, Any] | Any | None = None,
    receipt_verifier: ReceiptVerifier | None = None,
    asset_verifier: AssetVerifier | None = None,
) -> ModelProviderInvokeResult:
    """Bind either a result payload or its closed success envelope."""

    candidate = _copy_object(result_or_success, "Model Provider result or success")
    if "result" in candidate:
        return validate_model_provider_rpc_success_v2(
            candidate,
            request=request,
            canonical_receipt=canonical_receipt,
            receipt_verifier=receipt_verifier,
            asset_verifier=asset_verifier,
        )
    return validate_model_provider_invoke_result_v2(
        request,
        candidate,
        canonical_receipt=canonical_receipt,
        receipt_verifier=receipt_verifier,
        asset_verifier=asset_verifier,
    )


class ModelProviderOperationLedgerV2:
    """In-memory exact-replay proof; it owns no persistence or dispatch."""

    def __init__(self) -> None:
        self._entries: dict[
            tuple[str, str], tuple[bytes, bytes, dict[str, Any]]
        ] = {}

    def record(
        self,
        request: Mapping[str, Any],
        result_or_success: Mapping[str, Any],
        *,
        canonical_receipt: Mapping[str, Any] | Any | None = None,
        receipt_verifier: ReceiptVerifier | None = None,
        asset_verifier: AssetVerifier | None = None,
    ) -> tuple[dict[str, Any], bool]:
        parsed_request = parse_model_provider_invoke_request_v2(request)
        candidate = _copy_object(result_or_success, "Model Provider result or success")
        if "result" in candidate:
            parsed_result = validate_model_provider_rpc_success_v2(
                candidate,
                request=parsed_request,
                canonical_receipt=canonical_receipt,
                receipt_verifier=receipt_verifier,
                asset_verifier=asset_verifier,
            )
        else:
            parsed_result = validate_model_provider_invoke_result_v2(
                parsed_request,
                candidate,
                canonical_receipt=canonical_receipt,
                receipt_verifier=receipt_verifier,
                asset_verifier=asset_verifier,
            )
        context = parsed_request["params"]["planner_context"]
        key = (context["workspace_id"], context["operation_key"])
        request_bytes = canonical_bytes(parsed_request)
        result_bytes = canonical_bytes(parsed_result)
        previous = self._entries.get(key)
        if previous is not None:
            if previous[0] != request_bytes or previous[1] != result_bytes:
                raise ContractError(
                    ErrorCode.DUPLICATE_REQUEST,
                    "Model Provider operation_key was reused with a different exchange",
                )
            return copy.deepcopy(previous[2]), True
        self._entries[key] = (
            request_bytes,
            result_bytes,
            copy.deepcopy(parsed_result),
        )
        return copy.deepcopy(parsed_result), False

    def replay(self, request: Mapping[str, Any]) -> dict[str, Any]:
        parsed_request = parse_model_provider_invoke_request_v2(request)
        context = parsed_request["params"]["planner_context"]
        key = (context["workspace_id"], context["operation_key"])
        previous = self._entries.get(key)
        if previous is None or previous[0] != canonical_bytes(parsed_request):
            raise ContractError(
                ErrorCode.RESULT_CONTRACT_MISMATCH,
                "Model Provider operation has no exact replay",
            )
        return copy.deepcopy(previous[2])

    lookup = replay


# Downstream-friendly aliases remain scoped to this module; generic rpc.py is
# intentionally unchanged and cannot dispatch the reserved method.
parse_request_v2 = parse_model_provider_invoke_request_v2
parse_result_v2 = parse_model_provider_invoke_result_v2
parse_success_v2 = validate_model_provider_rpc_success_v2
parse_model_provider_invoke_success_v2 = validate_model_provider_rpc_success_v2
parse_rpc_success_v2 = validate_model_provider_rpc_success_v2
parse_error_v2 = parse_model_provider_rpc_error_v2
parse_rpc_error_v2 = parse_model_provider_rpc_error_v2
parse_envelope_v2 = parse_model_provider_rpc_envelope_v2
validate_request_v2 = parse_model_provider_invoke_request_v2
validate_result_v2 = parse_model_provider_invoke_result_v2
validate_exchange_v2 = validate_model_provider_invoke_v2
validate_response_v2 = validate_model_provider_rpc_response_v2
model_provider_operation_key = model_provider_operation_digest_v2
model_provider_operation_key_v2 = model_provider_operation_digest_v2
build_rpc_success_v2 = build_model_provider_invoke_success_v2
build_rpc_error_v2 = build_model_provider_rpc_error_v2


__all__ = [
    "AUTHORITY",
    "DIRECTION",
    "ERROR_SCHEMA",
    "HASH_DOMAINS",
    "MAX_SAFE_INTEGER",
    "METHOD",
    "METHOD_MATRIX_SCHEMA",
    "META_CONTEXT",
    "MODEL_PROVIDER_INVOKE_METHOD_V1",
    "MODEL_PROVIDER_REQUEST_SCHEMA_V2",
    "MODEL_PROVIDER_RESERVED_METHOD_IDS",
    "MODEL_PROVIDER_RESULT_SCHEMA_V2",
    "MODEL_PROVIDER_RPC_METHOD_MATRIX_SCHEMA_V2",
    "MODEL_PROVIDER_SUCCESS_SCHEMA_V2",
    "MODEL_RECEIPT_ANCHOR_FIELDS",
    "PLANNER_CONTEXT_FIELDS",
    "PROTOCOL_VERSION",
    "REQUEST_SCHEMA",
    "RESERVED_METHOD_IDS",
    "RESULT_SCHEMA",
    "SUCCESS_SCHEMA",
    "TERMINAL_STATES",
    "AssetIdentity",
    "AssetVerifier",
    "ModelProviderInvokeRequest",
    "ModelProviderInvokeResult",
    "ModelProviderOperationLedgerV2",
    "ModelReceiptAnchor",
    "PlannerContext",
    "ReceiptVerifier",
    "build_model_provider_invoke_request_v2",
    "build_model_provider_invoke_success_v2",
    "build_model_provider_meta_v2",
    "build_model_provider_rpc_error_v2",
    "build_rpc_error_v2",
    "build_rpc_success_v2",
    "get_model_provider_rpc_method_matrix_v2",
    "get_reserved_model_provider_method_ids",
    "model_provider_operation_digest_v2",
    "model_provider_operation_key",
    "model_provider_operation_key_v2",
    "parse_envelope_v2",
    "parse_error_v2",
    "parse_model_provider_invoke_request_v2",
    "parse_model_provider_invoke_result_v2",
    "parse_model_provider_invoke_success_v2",
    "parse_model_provider_rpc_envelope_v2",
    "parse_model_provider_rpc_error_v2",
    "parse_request_v2",
    "parse_rpc_error_v2",
    "parse_rpc_success_v2",
    "parse_result_v2",
    "parse_success_v2",
    "validate_exchange_v2",
    "validate_model_provider_invoke_result_v2",
    "validate_model_provider_invoke_v2",
    "validate_model_provider_rpc_response_v2",
    "validate_model_provider_rpc_success_v2",
    "validate_request_v2",
    "validate_response_v2",
    "validate_result_v2",
]
