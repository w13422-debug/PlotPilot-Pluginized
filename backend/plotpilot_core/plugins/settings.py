"""Immutable Settings revision and validation-receipt domain rules."""
from __future__ import annotations

import copy
from typing import Any, Mapping

from plotpilot_plugin_sdk.errors import ContractError, ErrorCode
from plotpilot_plugin_sdk.verifier import assert_valid, verify_settings_validation_receipt


def accept_draft(revision: Mapping[str, Any], *, parent: Mapping[str, Any] | None) -> dict[str, Any]:
    value = copy.deepcopy(dict(revision))
    assert_valid("settings-revision/v1", value)
    expected_parent = None if parent is None else parent["settings_revision_id"]
    if value["parent_revision_id"] != expected_parent:
        raise ContractError(ErrorCode.SETTINGS_INVALID, "settings parent revision does not match current revision")
    if parent is not None and value["plugin_id"] != parent["plugin_id"]:
        raise ContractError(ErrorCode.SETTINGS_INVALID, "settings history cannot cross plugin identity")
    if value["state"] != "draft" or value["validation_receipt_id"] is not None or value["validation_receipt_hash"] is not None:
        raise ContractError(ErrorCode.SETTINGS_INVALID, "new settings revision must begin as an unvalidated draft")
    return value


def apply_validation(revision: Mapping[str, Any], receipt: Mapping[str, Any]) -> dict[str, Any]:
    draft = copy.deepcopy(dict(revision))
    assert_valid("settings-revision/v1", draft)
    verify_settings_validation_receipt(receipt)
    bindings = {
        "settings_revision_id": "settings_revision_id",
        "plugin_id": "plugin_id",
        "plugin_release_id": "plugin_release_id",
        "schema_hash": "schema_hash",
        "payload_hash": "payload_hash",
    }
    if any(draft[left] != receipt[right] for left, right in bindings.items()):
        raise ContractError(ErrorCode.SETTINGS_INVALID, "validation receipt is not bound to this settings revision")
    if draft["state"] != "draft":
        raise ContractError(ErrorCode.INVALID_TRANSITION, "only a draft settings revision can be validated")
    draft["validation_receipt_id"] = receipt["receipt_id"]
    draft["validation_receipt_hash"] = receipt["receipt_hash"]
    draft["state"] = "validated" if receipt["valid"] else "invalid"
    assert_valid("settings-revision/v1", draft)
    return draft


def require_activatable(revision: Mapping[str, Any], *, release_id: str, schema_hash: str) -> None:
    assert_valid("settings-revision/v1", revision)
    if revision["state"] != "validated" or revision["plugin_release_id"] != release_id or revision["schema_hash"] != schema_hash:
        raise ContractError(ErrorCode.SETTINGS_INVALID, "settings revision is not validated for the selected release/schema")
