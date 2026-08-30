"""Closed decoding and exact invocation binding for Provider ModelReceipts."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from plotpilot_plugin_sdk import ContractError, hash_jcs, parse_json_bytes, sha256_hex

from .immutability import freeze_json, thaw_json

_HASH = re.compile(r"^[0-9a-f]{64}$")
_STATES = frozenset({"receipted", "failed", "cancelled", "uncertain"})
_FIELDS = (
    "schema",
    "receipt_id",
    "invocation_id",
    "invocation_key",
    "state",
    "request_hash",
    "response_hash",
    "profile_revision_id",
    "provider_plugin_id",
    "provider_release_id",
    "endpoint",
    "model",
    "lifecycle",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cost",
    "retry_count",
    "stream_termination",
    "error",
    "input_context",
    "profile_revision",
    "metadata",
    "response_asset_id",
    "recovered",
    "uncertain",
    "receipt_hash",
)
_FIELD_SET = frozenset(_FIELDS)
_NULLABLE_TEXT = (
    "profile_revision_id",
    "provider_plugin_id",
    "provider_release_id",
    "endpoint",
    "model",
    "stream_termination",
    "error",
    "response_asset_id",
)
_NULLABLE_INTS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _invalid(message: str, *, path: str | None = None) -> ContractError:
    return ContractError(1011, message, path=path)


def _text(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise _invalid(f"{field} must be a non-empty string" + (" or null" if nullable else ""), path=field)
    return value


def _hash(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise _invalid(f"{field} must be lowercase SHA-256" + (" or null" if nullable else ""), path=field)
    return value


def _nullable_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise _invalid(f"{field} must be a non-negative integer or null", path=field)
    return value


def _json_object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(f"{field} must be an object", path=field)
    try:
        frozen = freeze_json(value, path=field)
    except (TypeError, ValueError) as exc:
        raise _invalid(str(exc), path=field) from exc
    return frozen


@dataclass(frozen=True, slots=True)
class ValidatedModelReceipt(Mapping[str, Any]):
    """Immutable result of decoding the complete accepted model-receipt/v1."""

    _value: Mapping[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self._value[key]

    def __iter__(self) -> Iterator[str]:
        return iter(_FIELDS)

    def __len__(self) -> int:
        return len(_FIELDS)

    def as_dict(self) -> dict[str, Any]:
        return thaw_json(self._value)

    @property
    def receipt_hash(self) -> str:
        return self._value["receipt_hash"]

    @property
    def receipt_id(self) -> str:
        return self._value["receipt_id"]


def decode_model_receipt(value: Mapping[str, Any]) -> ValidatedModelReceipt:
    """Decode a closed ModelReceipt projection and verify its self-hash.

    Missing *and* extra members are rejected.  Nullable fields must still be
    present explicitly, which prevents a partial receipt from acquiring
    attribution merely by recomputing a hash over an incomplete mapping.
    """

    if not isinstance(value, Mapping):
        raise _invalid("model receipt must be an object")
    keys = frozenset(value)
    if keys != _FIELD_SET:
        missing = sorted(_FIELD_SET - keys)
        extra = sorted(keys - _FIELD_SET)
        raise _invalid(f"model receipt shape mismatch; missing={missing}, extra={extra}")
    if value["schema"] != "model-receipt/v1":
        raise _invalid("model receipt schema must be model-receipt/v1", path="schema")
    for field in ("receipt_id", "invocation_id", "invocation_key"):
        _text(value[field], field)
    if not isinstance(value["state"], str) or value["state"] not in _STATES:
        raise _invalid("model receipt state is invalid", path="state")
    _hash(value["request_hash"], "request_hash")
    _hash(value["response_hash"], "response_hash", nullable=True)
    _hash(value["provider_release_id"], "provider_release_id", nullable=True)
    for field in _NULLABLE_TEXT:
        if field != "provider_release_id":
            _text(value[field], field, nullable=True)
    lifecycle = value["lifecycle"]
    if not isinstance(lifecycle, list) or any(not isinstance(item, str) or not item for item in lifecycle):
        raise _invalid("lifecycle must be an array of non-empty strings", path="lifecycle")
    for field in _NULLABLE_INTS:
        _nullable_int(value[field], field)
    cost = value["cost"]
    if cost is not None:
        if type(cost) not in {int, float, str}:
            raise _invalid("cost must be an integer, number, string or null", path="cost")
        if isinstance(cost, float) and not math.isfinite(cost):
            raise _invalid("cost must be finite", path="cost")
        if isinstance(cost, str) and not cost:
            raise _invalid("cost string must not be empty", path="cost")
    if type(value["retry_count"]) is not int or value["retry_count"] < 0:
        raise _invalid("retry_count must be a non-negative integer", path="retry_count")
    for field in ("input_context", "profile_revision", "metadata"):
        _json_object(value[field], field)
    for field in ("recovered", "uncertain"):
        if type(value[field]) is not bool:
            raise _invalid(f"{field} must be boolean", path=field)
    if value["state"] == "uncertain" and value["uncertain"] is not True:
        raise _invalid("uncertain state requires uncertain=true", path="uncertain")
    _hash(value["receipt_hash"], "receipt_hash")
    without_hash = {field: value[field] for field in _FIELDS if field != "receipt_hash"}
    expected = hash_jcs("model-receipt/v1", without_hash)
    if value["receipt_hash"] != expected:
        raise _invalid("receipt_hash does not match the complete immutable receipt", path="receipt_hash")
    try:
        frozen = freeze_json({field: value[field] for field in _FIELDS})
    except (TypeError, ValueError) as exc:
        raise _invalid(str(exc)) from exc
    return ValidatedModelReceipt(frozen)


@dataclass(frozen=True, slots=True)
class FrozenModelInvocation:
    """The exact provider invocation expected for one frozen Skill step."""

    invocation_id: str
    invocation_key: str
    request_hash: str
    response_asset_id: str | None
    response_hash: str | None
    profile_revision_id: str | None
    provider_plugin_id: str | None
    provider_release_id: str | None
    input_context: Mapping[str, Any]
    endpoint: str | None = None
    model: str | None = None
    profile_revision: Mapping[str, Any] | None = None
    max_retries: int | None = None
    terminal_receipt_id: str | None = None
    terminal_receipt_hash: str | None = None

    def __post_init__(self) -> None:
        for field in ("invocation_id", "invocation_key"):
            _text(getattr(self, field), field)
        _hash(self.request_hash, "request_hash")
        _hash(self.response_hash, "response_hash", nullable=True)
        _hash(self.provider_release_id, "provider_release_id", nullable=True)
        for field in ("response_asset_id", "profile_revision_id", "provider_plugin_id", "endpoint", "model"):
            _text(getattr(self, field), field, nullable=True)
        _text(self.terminal_receipt_id, "terminal_receipt_id")
        _hash(self.terminal_receipt_hash, "terminal_receipt_hash")
        if self.max_retries is not None and (type(self.max_retries) is not int or self.max_retries < 0):
            raise _invalid("max_retries must be a non-negative integer or null")
        if (self.response_asset_id is None) != (self.response_hash is None):
            raise _invalid("frozen invocation output Asset ID/hash must be all-null or all-present")
        object.__setattr__(self, "input_context", _json_object(self.input_context, "input_context"))
        object.__setattr__(
            self,
            "profile_revision",
            None if self.profile_revision is None else _json_object(self.profile_revision, "profile_revision"),
        )


@dataclass(frozen=True, slots=True)
class AttributionProof:
    """Opaque-to-chain proof returned only after Asset and invocation checks."""

    asset_id: str
    asset_hash: str
    receipt: ValidatedModelReceipt
    invocation: FrozenModelInvocation


def verify_model_receipt_asset(
    asset: Any,
    *,
    invocation: FrozenModelInvocation,
    expected_context: Mapping[str, Any] | None = None,
    require_receipted: bool = True,
) -> AttributionProof:
    """Verify authoritative Asset bytes, closed receipt and exact invocation."""

    try:
        asset_id = asset.asset_id
        content = bytes(asset.content)
        asset_hash = asset.sha256
    except (AttributeError, TypeError, ValueError) as exc:
        raise _invalid("model receipt evidence must be an authoritative AssetRef") from exc
    _text(asset_id, "model_receipt_asset_id")
    _hash(asset_hash, "model_receipt_asset_hash")
    if sha256_hex(content) != asset_hash:
        raise _invalid("model receipt Asset content/hash mismatch", path="model_receipt_asset_hash")
    try:
        decoded = parse_json_bytes(content)
    except (UnicodeDecodeError, json.JSONDecodeError, ContractError, ValueError, TypeError) as exc:
        raise _invalid("model receipt Asset must contain UTF-8 JSON") from exc
    receipt = decode_model_receipt(decoded)
    return validate_model_receipt_binding(
        receipt,
        invocation=invocation,
        expected_context=expected_context,
        asset_id=asset_id,
        asset_hash=asset_hash,
        require_receipted=require_receipted,
    )


def validate_model_receipt_binding(
    receipt: Mapping[str, Any] | ValidatedModelReceipt,
    *,
    invocation: FrozenModelInvocation,
    expected_context: Mapping[str, Any] | None = None,
    asset_id: str | None = None,
    asset_hash: str | None = None,
    require_receipted: bool = True,
) -> AttributionProof:
    """Validate a decoded receipt against a frozen invocation.

    ``expected_context`` is an optional second binding used by the runtime to
    pin Job/step/Skill/input/output context.  Keeping this helper independent
    of Asset storage makes the same closed gate available to adapters and
    durable readers without introducing another receipt authority.
    """

    if isinstance(receipt, ValidatedModelReceipt):
        decoded = receipt
    else:
        decoded = decode_model_receipt(receipt)
    expected_pairs = {
        "invocation_id": invocation.invocation_id,
        "invocation_key": invocation.invocation_key,
        "request_hash": invocation.request_hash,
        "response_asset_id": invocation.response_asset_id,
        "response_hash": invocation.response_hash,
        "profile_revision_id": invocation.profile_revision_id,
        "provider_plugin_id": invocation.provider_plugin_id,
        "provider_release_id": invocation.provider_release_id,
        "endpoint": invocation.endpoint,
        "model": invocation.model,
    }
    for field, expected in expected_pairs.items():
        if decoded[field] != expected:
            raise _invalid(f"model receipt {field} does not match the frozen invocation", path=field)
    if thaw_json(decoded["input_context"]) != thaw_json(invocation.input_context):
        raise _invalid("model receipt input_context does not exactly match the frozen Skill invocation", path="input_context")
    if expected_context is not None and thaw_json(decoded["input_context"]) != thaw_json(expected_context):
        raise _invalid("model receipt input_context does not match the frozen Job/Skill context", path="input_context")
    if invocation.profile_revision is not None and thaw_json(decoded["profile_revision"]) != thaw_json(invocation.profile_revision):
        raise _invalid("model receipt profile_revision does not match the frozen invocation", path="profile_revision")
    if invocation.max_retries is not None and decoded["retry_count"] > invocation.max_retries:
        raise _invalid("model receipt retry_count exceeds the frozen invocation", path="retry_count")
    if (
        decoded["receipt_id"] != invocation.terminal_receipt_id
        or decoded["receipt_hash"] != invocation.terminal_receipt_hash
    ):
        raise _invalid("model receipt terminal identity/hash drift", path="receipt_hash")
    if require_receipted and (decoded["state"] != "receipted" or decoded["uncertain"]):
        raise _invalid("Skill attribution requires a certain receipted ModelReceipt", path="state")
    return AttributionProof(asset_id or "", asset_hash or "", decoded, invocation)
