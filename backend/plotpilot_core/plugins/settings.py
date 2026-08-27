"""Immutable Settings revision and validation-receipt domain rules."""
from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, MutableMapping, MutableSequence
from typing import Any

from plotpilot_plugin_sdk.canonical import parse_json_bytes
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode
from plotpilot_plugin_sdk.verifier import (
    assert_valid,
    verify_settings_validation_receipt,
)


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
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "validation receipt is not bound to this settings revision")
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


def _tokens(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration pointer must be RFC 6901 absolute")
    result: list[str] = []
    for raw in pointer[1:].split("/"):
        token = ""
        index = 0
        while index < len(raw):
            if raw[index] != "~":
                token += raw[index]
                index += 1
                continue
            if index + 1 >= len(raw) or raw[index + 1] not in "01":
                raise ContractError(ErrorCode.MIGRATION_FAILED, "invalid RFC 6901 escape")
            token += "~" if raw[index + 1] == "0" else "/"
            index += 2
        result.append(token)
    return result


def _parent(root: Any, pointer: str) -> tuple[Any, str]:
    tokens = _tokens(pointer)
    if not tokens:
        raise ContractError(ErrorCode.MIGRATION_FAILED, "root replacement is not supported by settings migration v1")
    current = root
    for token in tokens[:-1]:
        if isinstance(current, MutableMapping):
            if token not in current:
                raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration source path is absent")
            current = current[token]
        elif isinstance(current, MutableSequence):
            try:
                position = int(token)
                current = current[position]
            except (ValueError, IndexError) as exc:
                raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration array path is invalid") from exc
        else:
            raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration traverses a scalar")
    return current, tokens[-1]


def _read(root: Any, pointer: str) -> Any:
    parent, token = _parent(root, pointer)
    if isinstance(parent, MutableMapping):
        if token not in parent:
            raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration source path is absent")
        return copy.deepcopy(parent[token])
    if isinstance(parent, MutableSequence):
        try:
            return copy.deepcopy(parent[int(token)])
        except (ValueError, IndexError) as exc:
            raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration array source is invalid") from exc
    raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration source parent is scalar")


def _exists(root: Any, pointer: str) -> bool:
    try:
        _read(root, pointer)
    except ContractError:
        return False
    return True


def _remove(root: Any, pointer: str) -> Any:
    parent, token = _parent(root, pointer)
    if isinstance(parent, MutableMapping):
        if token not in parent:
            raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration remove source is absent")
        return parent.pop(token)
    if isinstance(parent, MutableSequence):
        try:
            return parent.pop(int(token))
        except (ValueError, IndexError) as exc:
            raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration array remove is invalid") from exc
    raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration remove parent is scalar")


def _write_nonreplace(root: Any, pointer: str, value: Any) -> None:
    parent, token = _parent(root, pointer)
    if isinstance(parent, MutableMapping):
        if token in parent:
            raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration destination already exists")
        parent[token] = copy.deepcopy(value)
        return
    if isinstance(parent, MutableSequence):
        if token == "-":
            parent.append(copy.deepcopy(value))
            return
        try:
            position = int(token)
        except ValueError as exc:
            raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration array destination is invalid") from exc
        if position < 0 or position > len(parent):
            raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration array destination is out of range")
        parent.insert(position, copy.deepcopy(value))
        return
    raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration destination parent is scalar")


def migrate_settings_payload(
    payload: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    current_schema_hash: str,
    target_schema_hash: str,
    read_value_asset: Callable[[str], Any],
) -> dict[str, Any]:
    """Apply the closed data-only settings migration language.

    The caller persists the returned value as a new immutable revision and
    then runs the target release validator.  This function never changes the
    input object or silently supplies defaults outside an explicit manifest.
    """
    value = copy.deepcopy(dict(payload))
    migration = copy.deepcopy(dict(manifest))
    assert_valid("settings-migration-manifest/v1", migration)
    if migration["from_schema_hash"] != current_schema_hash or migration["to_schema_hash"] != target_schema_hash:
        raise ContractError(ErrorCode.MIGRATION_FAILED, "settings migration schema binding mismatch")
    for step in migration["steps"]:
        operation = step["operation"]
        source = step["from_pointer"]
        target = step["to_pointer"]
        asset_id = step["value_asset_id"]
        if operation in {"rename", "copy"}:
            if source is None or target is None or asset_id is not None:
                raise ContractError(ErrorCode.MIGRATION_FAILED, f"{operation} migration step has invalid fields")
            if source == target:
                raise ContractError(ErrorCode.MIGRATION_FAILED, f"{operation} source and destination must differ")
            item = _read(value, source)
            if operation == "rename":
                _remove(value, source)
            _write_nonreplace(value, target, item)
        elif operation == "remove":
            if source is None or target is not None or asset_id is not None:
                raise ContractError(ErrorCode.MIGRATION_FAILED, "remove migration step has invalid fields")
            _remove(value, source)
        elif operation == "set_default":
            if source is not None or target is None or asset_id is None:
                raise ContractError(ErrorCode.MIGRATION_FAILED, "set_default migration step has invalid fields")
            if not _exists(value, target):
                asset = read_value_asset(asset_id)
                if isinstance(asset, (bytes, bytearray)):
                    asset = parse_json_bytes(bytes(asset))
                _write_nonreplace(value, target, asset)
        else:  # schema validation should make this unreachable
            raise ContractError(ErrorCode.MIGRATION_FAILED, "unknown settings migration operation")
    return value
