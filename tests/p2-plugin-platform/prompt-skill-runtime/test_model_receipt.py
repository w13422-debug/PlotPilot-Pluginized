from __future__ import annotations

import copy
import json

import pytest
from plotpilot_plugin_sdk import ContractError, hash_jcs, sha256_hex
from plotpilot_prompt_skill_runtime import (
    AssetRef,
    FrozenModelInvocation,
    decode_model_receipt,
    verify_model_receipt_asset,
)


def complete_receipt() -> dict:
    value = {
        "schema": "model-receipt/v1",
        "receipt_id": "model-receipt-1",
        "invocation_id": "invocation-1",
        "invocation_key": "invocation-key-1",
        "state": "receipted",
        "request_hash": "1" * 64,
        "response_hash": "2" * 64,
        "profile_revision_id": "profile-revision-1",
        "provider_plugin_id": "com.plotpilot.provider.test",
        "provider_release_id": "3" * 64,
        "endpoint": "https://provider.invalid/v1",
        "model": "test-model",
        "lifecycle": ["prepared", "sent", "receipted"],
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "cost": "0.0025",
        "retry_count": 1,
        "stream_termination": "stop",
        "error": None,
        "input_context": {
            "job_id": "job-1",
            "step_id": "step-1",
            "skill_id": "skill-1",
            "input_asset_id": "asset-input-1",
            "output_asset_id": "asset-output-1",
        },
        "profile_revision": {"revision_id": "profile-revision-1", "temperature": 0.2},
        "metadata": {"region": "lab", "attempt": 2},
        "response_asset_id": "asset-response-1",
        "recovered": False,
        "uncertain": False,
        "receipt_hash": "",
    }
    value["receipt_hash"] = hash_jcs(
        "model-receipt/v1", {key: item for key, item in value.items() if key != "receipt_hash"}
    )
    return value


def binding(receipt: dict) -> FrozenModelInvocation:
    return FrozenModelInvocation(
        invocation_id=receipt["invocation_id"],
        invocation_key=receipt["invocation_key"],
        request_hash=receipt["request_hash"],
        response_asset_id=receipt["response_asset_id"],
        response_hash=receipt["response_hash"],
        profile_revision_id=receipt["profile_revision_id"],
        provider_plugin_id=receipt["provider_plugin_id"],
        provider_release_id=receipt["provider_release_id"],
        input_context=receipt["input_context"],
        endpoint=receipt["endpoint"],
        model=receipt["model"],
        profile_revision=receipt["profile_revision"],
        max_retries=2,
        terminal_receipt_id=receipt["receipt_id"],
        terminal_receipt_hash=receipt["receipt_hash"],
    )


def receipt_asset(receipt: dict) -> AssetRef:
    content = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return AssetRef("asset-model-receipt-1", content, sha256_hex(content))


def test_complete_receipt_is_closed_immutable_and_bound() -> None:
    raw = complete_receipt()
    decoded = decode_model_receipt(raw)
    raw["metadata"]["region"] = "mutated"
    assert decoded["metadata"]["region"] == "lab"
    proof = verify_model_receipt_asset(receipt_asset(complete_receipt()), invocation=binding(complete_receipt()))
    assert proof.receipt.receipt_id == "model-receipt-1"


@pytest.mark.parametrize("field", list(complete_receipt()))
def test_every_receipt_member_is_required(field: str) -> None:
    value = complete_receipt()
    value.pop(field)
    with pytest.raises(ContractError):
        decode_model_receipt(value)


def test_extra_receipt_member_is_rejected_even_when_rehashed() -> None:
    value = complete_receipt()
    value["extra"] = "not-authority"
    value["receipt_hash"] = hash_jcs(
        "model-receipt/v1", {key: item for key, item in value.items() if key != "receipt_hash"}
    )
    with pytest.raises(ContractError):
        decode_model_receipt(value)


WRONG_TYPES = {
    "schema": 1,
    "receipt_id": None,
    "invocation_id": [],
    "invocation_key": {},
    "state": "complete",
    "request_hash": "A" * 64,
    "response_hash": 2,
    "profile_revision_id": [],
    "provider_plugin_id": 1,
    "provider_release_id": "short",
    "endpoint": [],
    "model": {},
    "lifecycle": ("prepared",),
    "prompt_tokens": True,
    "completion_tokens": 1.5,
    "total_tokens": -1,
    "cost": True,
    "retry_count": False,
    "stream_termination": [],
    "error": {},
    "input_context": [],
    "profile_revision": None,
    "metadata": [],
    "response_asset_id": 9,
    "recovered": 0,
    "uncertain": 1,
    "receipt_hash": "F" * 64,
}


@pytest.mark.parametrize(("field", "wrong"), WRONG_TYPES.items())
def test_every_receipt_member_has_closed_type_and_nullability(field: str, wrong: object) -> None:
    value = complete_receipt()
    value[field] = wrong
    if field != "receipt_hash":
        value["receipt_hash"] = hash_jcs(
            "model-receipt/v1", {key: item for key, item in value.items() if key != "receipt_hash"}
        )
    with pytest.raises((ContractError, ValueError, TypeError)):
        decode_model_receipt(value)


REHASH_DRIFT = {
    "profile_revision_id": "profile-revision-2",
    "provider_plugin_id": "com.plotpilot.provider.other",
    "provider_release_id": "4" * 64,
    "endpoint": "https://provider.invalid/v2",
    "model": "other-model",
    "lifecycle": ["prepared", "receipted"],
    "prompt_tokens": 12,
    "completion_tokens": 8,
    "total_tokens": 20,
    "cost": "0.0030",
    "retry_count": 2,
    "stream_termination": "length",
    "input_context": {"job_id": "job-other"},
    "profile_revision": {"revision_id": "profile-revision-2"},
    "metadata": {"region": "other"},
}


@pytest.mark.parametrize(("field", "replacement"), REHASH_DRIFT.items())
def test_rehashed_omitted_field_drift_cannot_escape_frozen_terminal_binding(
    field: str, replacement: object
) -> None:
    original = complete_receipt()
    drifted = copy.deepcopy(original)
    drifted[field] = replacement
    drifted["receipt_hash"] = hash_jcs(
        "model-receipt/v1", {key: item for key, item in drifted.items() if key != "receipt_hash"}
    )
    decode_model_receipt(drifted)  # Structurally valid but not the frozen terminal receipt.
    with pytest.raises(ContractError):
        verify_model_receipt_asset(receipt_asset(drifted), invocation=binding(original))


def test_failed_uncertain_and_retry_drift_cannot_create_attribution() -> None:
    for state, uncertain in (("failed", False), ("cancelled", False), ("uncertain", True)):
        value = complete_receipt()
        value["state"] = state
        value["uncertain"] = uncertain
        value["receipt_hash"] = hash_jcs(
            "model-receipt/v1", {key: item for key, item in value.items() if key != "receipt_hash"}
        )
        with pytest.raises(ContractError):
            verify_model_receipt_asset(
                receipt_asset(value), invocation=binding(value), require_receipted=True
            )
    value = complete_receipt()
    value["retry_count"] = 3
    value["receipt_hash"] = hash_jcs(
        "model-receipt/v1", {key: item for key, item in value.items() if key != "receipt_hash"}
    )
    with pytest.raises(ContractError):
        verify_model_receipt_asset(receipt_asset(value), invocation=binding(value))
