"""Strict Python implementation of the PlotPilot v1 contract surface.

The module deliberately keeps JSON-Schema validation and the cross-object
rules in one place. The schemas reject shape drift; the semantic checks below
reject identity, ordering, fencing and hash drift before a Core mutation can
be considered durable.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator, ValidationError

from .canonical import hash_jcs, normalize_snapshot, parse_json_bytes, sha256_hex
from .errors import ContractError, ContractValidationError, ErrorCode
from .package import (
    build_files_sha256,
    normalize_relative_path,
    package_hash,
    release_id,
    skill_package_hash,
    skill_release_id,
    unicode_nfc_casefold as _frozen_unicode_nfc_casefold,
)
from .rpc import HOST_METHODS, METHOD_MATRIX, WORKER_METHODS


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "contracts" / "json-schema"

EXPECTED_WORKER_METHODS = (
    "runtime.handshake",
    "runtime.health",
    "runtime.heartbeat",
    "capability.describe",
    "settings.validate",
    "migration.plan",
    "migration.apply",
    "migration.verify",
    "job.start",
    "job.resume",
    "job.pause",
    "job.cancel",
    "runtime.shutdown",
)
EXPECTED_HOST_METHODS = (
    "host.asset.read/v1",
    "host.asset.create/v1",
    "host.asset.upload.status/v1",
    "host.model.invoke/v1",
    "host.capability.invoke/v1",
    "host.capability.poll/v1",
    "host.capability.cancel/v1",
    "host.candidate.stage/v1",
    "host.checkpoint.commit/v1",
    "host.stream.commit/v1",
    "host.job.event/v1",
    "host.job.await_user/v1",
    "host.job.complete/v1",
    "host.log/v1",
    "host.migration.lease.renew/v1",
    "host.migration.lease.release/v1",
)
EXPECTED_ERROR_CODES = {
    1001: "incompatible_generation",
    1002: "stale_lease",
    1003: "cancelled",
    1004: "deadline_exceeded",
    1005: "asset_error",
    1006: "settings_invalid",
    1007: "migration_failed",
    1008: "duplicate_request",
    1009: "uncertain_external_effect",
    1010: "invalid_transition",
    1011: "result_contract_mismatch",
    1012: "data_interpreter_unavailable",
    1013: "release_retiring",
    1014: "checkpoint_invalid",
}

SCHEMA_NAME_ALIASES = {
    p.stem.removesuffix(".schema").replace("-v1", "/v1", 1): p.name
    for p in SCHEMA_DIR.glob("*.schema.json")
}
SCHEMA_NAME_ALIASES.update(
    {
        "plotpilot-plugin/v1": "plugin-manifest-v1.schema.json",
        "plotpilot-skill/v1": "skill-manifest-v1.schema.json",
        "rpc-envelope/v1": "rpc-envelope-v1.schema.json",
        "backup-bundle/v1": "backup-bundle-v1.schema.json",
    }
)


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    message: str
    validator: str | None = None


def _schema_path(contract_id: str) -> Path:
    filename = SCHEMA_NAME_ALIASES.get(contract_id)
    if filename is None:
        filename = contract_id.replace("/", "-") + ".schema.json"
    path = SCHEMA_DIR / filename
    if not path.exists():
        raise ContractValidationError(f"unknown contract schema: {contract_id}")
    return path


def _load_schema(contract_id: str) -> dict[str, Any]:
    return json.loads(_schema_path(contract_id).read_text(encoding="utf-8"))


def _path(error: ValidationError) -> str:
    return "/" + "/".join(str(part) for part in error.absolute_path)


def schema_errors(contract_id: str, value: Any) -> list[ValidationIssue]:
    validator = Draft202012Validator(_load_schema(contract_id))
    errors = sorted(validator.iter_errors(value), key=lambda e: (tuple(e.absolute_path), e.message))
    return [ValidationIssue(_path(error), error.message, error.validator) for error in errors]


def validate_contract(contract_id: str, value: Any) -> list[ValidationIssue]:
    return schema_errors(contract_id, value)


def assert_valid(contract_id: str, value: Any) -> None:
    issues = validate_contract(contract_id, value)
    if issues:
        first = issues[0]
        raise ContractValidationError(
            f"{contract_id}: {first.message}",
            path=first.path,
            details=[issue.__dict__ for issue in issues],
        )


def _assert_hash(actual: str, expected: str, label: str) -> None:
    if actual != expected:
        raise ContractValidationError(f"{label} mismatch: expected {expected}, got {actual}")


def _utf8_sort(values: Iterable[str]) -> list[str]:
    return sorted(values, key=lambda value: value.encode("utf-8"))


def unicode_nfc_casefold(value: str) -> str:
    """Return the cross-runtime path identity from the frozen v1 table."""
    if not isinstance(value, str):
        raise ContractValidationError("casefold identity requires a string")
    return _frozen_unicode_nfc_casefold(value)


def _casefold_unique(values: Iterable[str], message: str) -> None:
    _assert_unique((unicode_nfc_casefold(value) for value in values), message)


def _key_tuple(value: Mapping[str, Any], fields: Sequence[str]) -> tuple[str, ...]:
    return tuple("" if value.get(field) is None else str(value.get(field)) for field in fields)


def _assert_unique(values: Iterable[Any], message: str) -> None:
    values_list = list(values)
    try:
        unique = len(values_list) == len(set(values_list))
    except TypeError as exc:
        raise ContractValidationError(message) from exc
    if not unique:
        raise ContractValidationError(message)


def hash_without_field(value: Mapping[str, Any], field: str, prefix: str) -> str:
    unsigned = {key: copy.deepcopy(item) for key, item in value.items() if key != field}
    return hash_jcs(prefix, unsigned)


def request_key_bytes(snapshot: Mapping[str, Any], *, parameters_asset_sha256: str | None = None) -> bytes:
    scope = snapshot["scope"]
    params_hash = parameters_asset_sha256
    if params_hash is None:
        asset_map = {item["asset_id"]: item["sha256"] for item in snapshot.get("asset_hashes", [])}
        params_id = snapshot.get("parameters_asset_id")
        params_hash = asset_map.get(params_id, "-") if params_id else "-"
    revisions = sorted(snapshot.get("input_revisions", []), key=lambda item: (item["document_id"], item["revision_id"]))
    revision_line = ",".join(f"{item['document_id']}={item['revision_id']}={item['content_hash']}" for item in revisions)
    lines = [
        "request-key/v1",
        snapshot["workspace_id"],
        scope["operation"],
        scope["document_id"] or "null",
        scope["node_id"] or "null",
        revision_line,
        snapshot["plan_revision_id"],
        params_hash,
        snapshot["run_intent_id"],
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def request_key(snapshot: Mapping[str, Any], *, parameters_asset_sha256: str | None = None) -> str:
    return sha256_hex(request_key_bytes(snapshot, parameters_asset_sha256=parameters_asset_sha256))


def normalize_snapshot_for_hash(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return normalize_snapshot(dict(snapshot))


def snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    return hash_jcs(
        "run-snapshot/v1",
        normalize_snapshot_for_hash({key: value for key, value in snapshot.items() if key != "snapshot_hash"}),
    )


def verify_manifest(manifest: Mapping[str, Any]) -> None:
    assert_valid("plugin-manifest/v1", manifest)
    capability_ids = [item["capability_id"] for item in manifest["capabilities"]]
    _assert_unique(capability_ids, "manifest capability IDs must be unique")
    for capability in manifest["capabilities"]:
        _assert_unique(capability["operations"], "manifest capability operations must be unique")
    compatibility = manifest["compatibility"]
    if compatibility.get("core_api") != ">=1.0 <2.0" or compatibility.get("plugin_rpc") != "1" or compatibility.get("ui_host") != "1":
        raise ContractValidationError("manifest compatibility is outside the v1 matrix")
    if manifest["kind"] == "data" and manifest["needs"]:
        raise ContractValidationError("data plugin needs must be empty")
    if manifest["kind"] == "data" and any(key in manifest for key in ("backend", "storage", "ui")):
        raise ContractValidationError("data plugin cannot declare backend/storage/UI fields")
    ui = manifest.get("ui")
    if ui is not None:
        _assert_unique(
            ((item["contribution_id"], item["slot"], item["capability_id"]) for item in ui["contributions"]),
            "UI contribution triples must be unique",
        )
        capability_set = set(capability_ids)
        for contribution in ui["contributions"]:
            if contribution["capability_id"] not in capability_set:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "UI contribution references an unknown capability")


def verify_data_bundle(bundle: Mapping[str, Any]) -> None:
    assert_valid("plugin-data-bundle/v1", bundle)
    _assert_hash(hash_without_field(bundle, "bundle_hash", "plugin-data-bundle/v1"), bundle["bundle_hash"], "bundle_hash")
    normalized_paths = [normalize_relative_path(item["path"]) for item in bundle["files"]]
    if normalized_paths != _utf8_sort(normalized_paths):
        raise ContractValidationError("data bundle files must be sorted by normalized UTF-8 path")
    _casefold_unique(normalized_paths, "data bundle paths must be NFC/casefold-unique")
    _assert_unique((item["asset_id"] for item in bundle["files"]), "data bundle asset IDs must be unique")
    if normalize_relative_path(bundle["root_path"]) not in normalized_paths:
        raise ContractValidationError("data bundle root_path is absent from files")


def verify_snapshot(snapshot: Mapping[str, Any]) -> None:
    assert_valid("run-snapshot/v1", snapshot)
    normalized = normalize_snapshot_for_hash(snapshot)
    _assert_hash(request_key(normalized), snapshot["request_key"], "request_key")
    _assert_hash(snapshot_hash(snapshot), snapshot["snapshot_hash"], "snapshot_hash")
    asset_ids = [entry["asset_id"] for entry in snapshot["asset_hashes"]]
    _assert_unique(asset_ids, "asset_hashes contains duplicate asset IDs")
    assets = {entry["asset_id"]: entry["sha256"] for entry in snapshot["asset_hashes"]}
    if snapshot["parameters_asset_id"] is not None and snapshot["parameters_asset_id"] not in assets:
        raise ContractValidationError("parameters_asset_id is absent from asset_hashes")
    for field, fields in (
        ("input_revisions", ("document_id", "revision_id")),
        ("plugin_releases", ("plugin_id",)),
        ("plugin_settings_revisions", ("plugin_id", "scope", "scope_id")),
        ("asset_hashes", ("asset_id",)),
        ("data_bindings", ("order",)),
        ("skill_releases", ("order",)),
    ):
        _assert_unique((_key_tuple(entry, fields) for entry in snapshot[field]), f"{field} contains duplicate identity")
    for field in ("data_bindings", "skill_releases"):
        orders = [entry["order"] for entry in snapshot[field]]
        if orders != sorted(orders):
            raise ContractValidationError(f"{field} order is not ascending")
    for binding in snapshot["data_bindings"]:
        if assets.get(binding["bundle_asset_id"]) != binding["bundle_hash"]:
            raise ContractValidationError("data binding bundle hash is not backed by asset_hashes")
    for skill in snapshot["skill_releases"]:
        parameter_id = skill["parameters_asset_id"]
        if parameter_id is not None and parameter_id not in assets:
            raise ContractValidationError("Skill parameter asset is absent from asset_hashes")


def _verify_candidate_item(item: Mapping[str, Any], *, snapshot_workspace_id: str | None) -> None:
    target_value = item["target"]
    if snapshot_workspace_id is not None and target_value["workspace_id"] != snapshot_workspace_id:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate target is outside the snapshot workspace")
    target_key = (target_value["workspace_id"], target_value["entity_kind"], target_value["entity_id"])
    write_keys: list[tuple[str, str, str]] = []
    target_write_entry: Mapping[str, Any] | None = None
    for entry in item["write_set"]:
        if snapshot_workspace_id is not None and entry["workspace_id"] != snapshot_workspace_id:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate write_set crosses the snapshot workspace")
        entry_key = (entry["workspace_id"], entry["entity_kind"], entry["entity_id"])
        if entry_key in write_keys:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate write_set contains duplicate target identity")
        write_keys.append(entry_key)
        if entry_key == target_key:
            target_write_entry = entry
    if target_write_entry is None:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate target is not present in write_set")
    if (
        target_write_entry["revision_id"] != item["base"]["revision_id"]
        or target_write_entry["content_hash"] != item["base"]["content_hash"]
    ):
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate base does not match target write_set entry")
    if item["item_kind"] == "incomplete_stream":
        if item["status"] != "partial" or target_value["entity_kind"] != "document" or item["mutation"]["mode"] != "replace":
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "incomplete_stream must be a Core-owned partial document replacement")
        if item["mutation"]["payload_schema"] != "core/document-text/v1":
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "incomplete_stream must use the Core document-text schema")
        return
    allowed = {
        "document": {"item_kind": {"document"}, "mode": {"replace", "text_patch", "append_text"}},
        "node_structure": {"item_kind": {"node_structure"}, "mode": {"structure_patch"}},
        "relation_set": {"item_kind": {"relation_set"}, "mode": {"relation_patch"}},
    }
    expected = allowed[target_value["entity_kind"]]
    if item["item_kind"] not in expected["item_kind"] or item["mutation"]["mode"] not in expected["mode"]:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate item kind/mutation does not match target")


def verify_parent_graph(items: Iterable[Mapping[str, Any]], *, known_parent_ids: set[str] | None = None) -> None:
    graph = {item["item_id"]: set(item["parent_candidate_ids"]) for item in items}
    known = set(graph) | (known_parent_ids or set())
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate parent cycle detected")
        if node in visited or node not in graph:
            return
        visiting.add(node)
        for parent in graph[node]:
            if parent not in known:
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "candidate parent does not exist")
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for item_id in graph:
        visit(item_id)


def _verify_chain_ref(
    ref: Mapping[str, Any],
    *,
    bundle_id: str | None = None,
    item_ids: set[str] | None = None,
    allow_stream: bool = True,
    allow_bundleless: bool = False,
) -> None:
    if (ref.get("asset_id") is None) != (ref.get("asset_hash") is None):
        raise ContractValidationError("Skill chain asset ID/hash must be all-null or all-present")
    bundle_pair = ref["result_bundle_id"] is not None and ref["result_item_id"] is not None
    stream_pair = ref["stream_id"] is not None and ref["acked_prefix_hash"] is not None
    if (ref["result_bundle_id"] is None) != (ref["result_item_id"] is None):
        raise ContractValidationError("bundle anchor must be all-null or all-present")
    if (ref["stream_id"] is None) != (ref["acked_prefix_hash"] is None):
        raise ContractValidationError("stream anchor must be all-null or all-present")
    if bundle_pair and stream_pair:
        raise ContractValidationError("Skill chain reference must have exactly one anchor profile")
    if not bundle_pair and not stream_pair and not allow_bundleless:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "bundleless Skill reference is not allowed here")
    if stream_pair and not allow_stream:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "result Bundle refs must be bundle-backed")
    if bundle_pair:
        if bundle_id is not None and ref["result_bundle_id"] != bundle_id:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Skill reference points at another Bundle")
        if item_ids is not None and ref["result_item_id"] not in item_ids:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Skill reference points at an unknown item")


def verify_result_bundle(
    bundle: Mapping[str, Any],
    *,
    snapshot_workspace_id: str | None = None,
    snapshot_hash_value: str | None = None,
    known_parent_ids: set[str] | None = None,
) -> None:
    assert_valid("result-bundle/v1", bundle)
    profile = {
        "candidate-batch/v1": ("candidate_batch", "candidate-item/v1"),
        "artifact-bundle/v1": ("artifact", "artifact-item/v1"),
        "diagnostic-bundle/v1": ("diagnostic", "diagnostic-item/v1"),
    }
    expected_type, expected_item_schema = profile[bundle["contract_id"]]
    if bundle["bundle_type"] != expected_type:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "result contract and bundle type do not match")
    item_id_values = [item["item_id"] for item in bundle["items"]]
    # Check the sequence before materializing a set; doing it after set()
    # silently erased duplicate IDs in the old verifier.
    _assert_unique(item_id_values, "result bundle item IDs must be unique")
    item_ids = set(item_id_values)
    incomplete_targets: set[tuple[str, str, str]] = set()
    for item in bundle["items"]:
        if item["schema"] != expected_item_schema:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "result bundle item profile does not match contract")
        if expected_item_schema == "candidate-item/v1":
            _verify_candidate_item(item, snapshot_workspace_id=snapshot_workspace_id)
            if item["item_kind"] == "incomplete_stream":
                target = item["target"]
                target_key = (target["workspace_id"], target["entity_kind"], target["entity_id"])
                if target_key in incomplete_targets:
                    raise ContractError(ErrorCode.INVALID_TRANSITION, "only one incomplete stream Candidate is allowed per target")
                incomplete_targets.add(target_key)
    if expected_item_schema == "candidate-item/v1":
        verify_parent_graph(bundle["items"], known_parent_ids=known_parent_ids)
    if snapshot_hash_value is not None and bundle["input_snapshot_hash"] != snapshot_hash_value:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "bundle input snapshot does not match current snapshot")
    for ref in bundle["skill_chain_result_refs"]:
        _verify_chain_ref(ref, bundle_id=bundle["bundle_id"], item_ids=item_ids, allow_stream=False)
    statuses = [item["status"] for item in bundle["items"]]
    if bundle["partial"] and not any(status in {"partial", "failed", "skipped"} for status in statuses):
        raise ContractValidationError("partial result bundle must expose a non-complete item")
    if not bundle["partial"] and any(status != "complete" for status in statuses):
        raise ContractValidationError("complete result bundle cannot contain partial/failed/skipped items")


def verify_package_manifest(files: Mapping[str, bytes], manifest_bytes: bytes) -> None:
    if manifest_bytes.startswith(b"\xef\xbb\xbf"):
        raise ContractValidationError("files.sha256 must not contain a UTF-8 BOM")
    try:
        text = manifest_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractValidationError("files.sha256 must be UTF-8") from exc
    if not text or "\r" in text or not text.endswith("\n"):
        raise ContractValidationError("files.sha256 must use LF and end with a newline")
    parsed: dict[str, str] = {}
    parsed_paths: list[str] = []
    for line in text.splitlines(keepends=True):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)\n", line)
        if match is None:
            raise ContractValidationError("files.sha256 line must be '<64hex><two spaces><path>\\n'")
        digest, raw_path = match.groups()
        path = normalize_relative_path(raw_path)
        if path in parsed or unicode_nfc_casefold(path) in {unicode_nfc_casefold(item) for item in parsed}:
            raise ContractValidationError("files.sha256 contains a duplicate path")
        parsed[path] = digest
        parsed_paths.append(path)
    normalized = {normalize_relative_path(path): sha256_hex(content) for path, content in files.items()}
    expected = {path: normalized[path] for path in _utf8_sort(normalized)}
    if parsed_paths != list(expected):
        raise ContractValidationError("files.sha256 paths must be sorted by normalized UTF-8 path")
    if parsed != expected:
        raise ContractValidationError("files.sha256 does not exactly describe package files")


def verify_package_files(files: Mapping[str, bytes], expected_files_sha256: bytes | None = None) -> str:
    calculated = build_files_sha256(files)
    if expected_files_sha256 is not None:
        verify_package_manifest(files, expected_files_sha256)
        if calculated != expected_files_sha256:
            raise ContractValidationError("files.sha256 bytes mismatch")
    return package_hash(files)


def verify_package_identity(
    files: Mapping[str, bytes],
    plugin_id: str,
    version: str,
    expected_package_hash: str,
    expected_release_id: str,
    *,
    expected_files_sha256: bytes | None = None,
) -> None:
    digest = verify_package_files(files, expected_files_sha256)
    _assert_hash(digest, expected_package_hash, "package_hash")
    _assert_hash(release_id(plugin_id, version, digest), expected_release_id, "release_id")


def verify_skill_identity(
    files: Mapping[str, bytes],
    skill_id: str,
    version: str,
    expected_package_hash: str,
    expected_release_id: str,
    *,
    expected_files_sha256: bytes | None = None,
) -> None:
    calculated_manifest = build_files_sha256(files)
    digest = skill_package_hash(files)
    if expected_files_sha256 is not None:
        verify_package_manifest(files, expected_files_sha256)
        if calculated_manifest != expected_files_sha256:
            raise ContractValidationError("Skill files.sha256 bytes mismatch")
    _assert_hash(digest, expected_package_hash, "skill_package_hash")
    _assert_hash(skill_release_id(skill_id, version, digest), expected_release_id, "skill_release_id")


def verify_self_hash(contract_id: str, value: Mapping[str, Any], field: str, prefix: str) -> None:
    assert_valid(contract_id, value)
    _assert_hash(hash_without_field(value, field, prefix), value[field], field)


def verify_backup(backup: Mapping[str, Any]) -> None:
    assert_valid("backup-bundle/v1", backup)
    _assert_hash(hash_without_field(backup, "bundle_hash", "plotpilot-backup/v1"), backup["bundle_hash"], "bundle_hash")
    paths = [normalize_relative_path(entry["path"]) for entry in backup["files"]]
    if paths != _utf8_sort(paths):
        raise ContractValidationError("backup files must be sorted by normalized UTF-8 path")
    _casefold_unique(paths, "backup files must be NFC/casefold-unique")
    roles = {entry["role"] for entry in backup["files"]}
    if backup["mode"] == "workspace" and roles & {"plugin_db", "package"}:
        raise ContractValidationError("workspace backup cannot contain plugin DB/package")
    if backup["mode"] == "data" and "package" in roles:
        raise ContractValidationError("data backup cannot contain packages")
    if not all(backup["verification"].values()):
        raise ContractValidationError("backup verification flags must all be true for a publishable bundle")


def verify_restore_report(report: Mapping[str, Any]) -> None:
    assert_valid("restore-report/v1", report)
    if report["source_root_id"] == report["target_root_id"]:
        raise ContractValidationError("restore staging must use a distinct target root")
    if report["state"] == "restore_ready" and report["completed_at"] is None:
        raise ContractValidationError("restore_ready requires completed_at")


def verify_plan(plan: Mapping[str, Any]) -> None:
    assert_valid("plugin-plan/v1", plan)
    for field, id_field in (("bindings", "binding_id"), ("data_bindings", "data_binding_id")):
        _assert_unique((item[id_field] for item in plan[field]), f"{field} IDs must be unique")
        orders = [item["order"] for item in plan[field]]
        if len(orders) != len(set(orders)) or orders != sorted(orders):
            raise ContractValidationError(f"{field} order must be unique and ascending")
    synthesizer = plan["synthesizer"]
    if plan["result_mode"] == "synthesize":
        if synthesizer is None:
            raise ContractValidationError("synthesize plan requires a synthesizer")
        match = [item for item in plan["bindings"] if item["binding_id"] == synthesizer["binding_id"]]
        if len(match) != 1 or not match[0]["enabled"]:
            raise ContractValidationError("synthesizer must resolve to an enabled binding")
        if any(match[0][field] != synthesizer[field] for field in ("capability_id", "plugin_id", "release_requirement")):
            raise ContractValidationError("synthesizer identity does not match its binding")
    elif synthesizer is not None:
        raise ContractValidationError("non-synthesize plan must not declare a synthesizer")


def verify_core_snapshot(snapshot: Mapping[str, Any]) -> None:
    assert_valid("core-snapshot/v1", snapshot)
    if snapshot["coverage_complete"] is not True:
        raise ContractValidationError("core snapshot must be coverage-complete")
    event_types = snapshot["subscription_scope"]["event_types"]
    if event_types != sorted(set(event_types)):
        raise ContractValidationError("core snapshot event_types must be a sorted set")
    keys = [_key_tuple(item, ("aggregate_type", "aggregate_id")) for item in snapshot["covered_aggregates"]]
    if keys != sorted(set(keys)):
        raise ContractValidationError("covered aggregates must be sorted and unique")
    _assert_hash(hash_without_field(snapshot, "snapshot_hash", "core-snapshot/v1"), snapshot["snapshot_hash"], "snapshot_hash")


def verify_job_snapshot(snapshot: Mapping[str, Any]) -> None:
    """Verify the self-hash and the monotonic identity fields of a Job view."""
    assert_valid("job-snapshot/v1", snapshot)
    _assert_unique((step["step_id"] for step in snapshot["steps"]), "job snapshot step IDs must be unique")
    _assert_unique((attempt["attempt_id"] for attempt in snapshot["attempts"]), "job snapshot attempt IDs must be unique")
    _assert_hash(hash_without_field(snapshot, "snapshot_hash", "job-snapshot/v1"), snapshot["snapshot_hash"], "snapshot_hash")


def verify_settings_validation_receipt(receipt: Mapping[str, Any]) -> None:
    """Verify the Core-materialized settings validation receipt digest."""
    assert_valid("settings-validation-receipt/v1", receipt)
    _assert_hash(hash_without_field(receipt, "receipt_hash", "settings-validation-receipt/v1"), receipt["receipt_hash"], "receipt_hash")


def verify_capability_descriptor(
    descriptor: Mapping[str, Any],
    *,
    expected_capability_id: str | None = None,
    allowed_capability_ids: set[str] | None = None,
    expected_provider: Mapping[str, Any] | None = None,
) -> None:
    """Validate a descriptor and bind it to the capability being described."""
    assert_valid("capability-provider/v1", descriptor)
    if expected_capability_id is not None and descriptor["capability_id"] != expected_capability_id:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "capability descriptor ID does not match the request")
    if allowed_capability_ids is not None and descriptor["capability_id"] not in allowed_capability_ids:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "capability descriptor references an unknown capability")
    if expected_provider is not None and dict(descriptor["provider"]) != dict(expected_provider):
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "capability descriptor provider does not match the installed release")
    _assert_unique(descriptor["supports"], "capability descriptor supports must be unique")
    _assert_unique(descriptor["accepted_data_formats"], "capability descriptor data formats must be unique")


def verify_provenance_receipt(receipt: Mapping[str, Any]) -> None:
    """Verify provenance's nullable Bundle pair, chain refs and self-hash."""
    assert_valid("provenance-receipt/v1", receipt)
    if (receipt["bundle_id"] is None) != (receipt["bundle_hash"] is None):
        raise ContractValidationError("provenance Bundle ID/hash must be all-null or all-present")
    _assert_unique((item["item_id"] for item in receipt["staged_items"]), "provenance staged item IDs must be unique")
    for ref in receipt["skill_chain_result_refs"]:
        _verify_chain_ref(ref, bundle_id=receipt["bundle_id"], allow_bundleless=True)
    _assert_hash(hash_without_field(receipt, "receipt_hash", "provenance-receipt/v1"), receipt["receipt_hash"], "receipt_hash")


def verify_sse_recovery(value: Mapping[str, Any]) -> None:
    assert_valid("sse-recovery/v1", value)
    if value["gap"] != value["snapshot_required"]:
        raise ContractValidationError("SSE snapshot_required must equal gap")
    if value["stream_kind"] == "core_event" and value["aggregate_id"] is not None:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "core event recovery cannot carry an aggregate job ID")
    if value["requested_after_seq"] > value["durable_high_water_seq"]:
        raise ContractError(ErrorCode.INVALID_TRANSITION, "SSE cursor is ahead of the durable high-water mark")
    present = [value[field] is not None for field in ("snapshot_schema", "snapshot_revision", "snapshot_asset_id", "snapshot_hash")]
    if any(present) != value["gap"] or (present and len(set(present)) != 1):
        raise ContractValidationError("SSE snapshot fields must be all-null or all-present with a gap")
    if value["gap"] and value["snapshot_schema"] != ("core-snapshot/v1" if value["stream_kind"] == "core_event" else "job-snapshot/v1"):
        raise ContractValidationError("SSE snapshot schema does not match stream kind")


def verify_checkpoint(checkpoint: Mapping[str, Any], *, expected_snapshot_hash: str | None = None, previous_seq: int | None = None) -> None:
    assert_valid("checkpoint/v1", checkpoint)
    if expected_snapshot_hash is not None and checkpoint["run_snapshot_hash"] != expected_snapshot_hash:
        raise ContractError(ErrorCode.CHECKPOINT_INVALID, "checkpoint belongs to another RunSnapshot")
    if previous_seq is not None and checkpoint["checkpoint_seq"] <= previous_seq:
        raise ContractError(ErrorCode.CHECKPOINT_INVALID, "checkpoint sequence moved backwards")
    _assert_hash(hash_without_field(checkpoint, "checkpoint_hash", "checkpoint/v1"), checkpoint["checkpoint_hash"], "checkpoint_hash")


def verify_stream_prefix(prefix: Mapping[str, Any], *, previous: Mapping[str, Any] | None = None, content: bytes | None = None) -> None:
    assert_valid("stream-prefix/v1", prefix)
    if previous is not None:
        if prefix["stream_id"] != previous["stream_id"] or prefix["attempt_id"] != previous["attempt_id"] or prefix["lease_epoch"] != previous["lease_epoch"]:
            raise ContractError(ErrorCode.STALE_LEASE, "stream prefix binding changed")
        if prefix["prefix_seq"] <= previous["prefix_seq"] or prefix["byte_length"] < previous["byte_length"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "stream prefix moved backwards")
    if content is not None:
        if len(content) != prefix["byte_length"] or sha256_hex(content) != prefix["prefix_hash"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "stream prefix asset hash/length mismatch")


def verify_skill_receipt(receipt: Mapping[str, Any]) -> None:
    assert_valid("skill-run-receipt/v1", receipt)
    _verify_chain_ref(
        {
            "result_bundle_id": receipt["result_bundle_id"],
            "result_item_id": receipt["result_item_id"],
            "stream_id": receipt["stream_id"],
            "acked_prefix_hash": receipt["acked_prefix_hash"],
        },
        allow_bundleless=True,
    )
    if receipt["result_bundle_id"] is None and receipt["stream_id"] is None and receipt["step_state"] not in {"failed", "skipped"}:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "only a failed/skipped Skill receipt may be bundleless")
    if not receipt["frozen"]:
        raise ContractValidationError("Skill receipt must be frozen before chain aggregation")
    if receipt["step_state"] == "skipped" and (receipt["participated"] or receipt["verified_patch"]):
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "skipped Skill step cannot be participated or verified")
    if receipt["model_claimed"] and receipt["claim_evidence_asset_id"] is None:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "model_claimed requires claim evidence")
    if receipt["verified_patch"] and not any(patch["verified"] for patch in receipt["patches"]):
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "verified_patch requires a verified patch")
    for patch in receipt["patches"]:
        if patch["end_codepoint"] < patch["start_codepoint"]:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Skill patch range is inverted")
    if (receipt["output_asset_id"] is None) != (receipt["output_hash"] is None):
        raise ContractValidationError("Skill output Asset ID/hash must be all-null or all-present")
    _assert_hash(hash_without_field(receipt, "receipt_hash", "skill-run-receipt/v1"), receipt["receipt_hash"], "receipt_hash")


def verify_skill_chain(chain: Mapping[str, Any], receipts: Iterable[Mapping[str, Any]]) -> None:
    assert_valid("skill-chain-result/v1", chain)
    receipt_list = sorted(receipts, key=lambda item: item["chain_index"])
    _assert_unique((receipt["receipt_id"] for receipt in receipt_list), "Skill chain receipt IDs must be unique")
    _assert_unique((receipt["chain_index"] for receipt in receipt_list), "Skill chain receipt indexes must be unique")
    if len(receipt_list) != len(chain["receipt_ids"]) or [r["receipt_id"] for r in receipt_list] != chain["receipt_ids"]:
        raise ContractValidationError("Skill chain receipt IDs/indexes are not continuous")
    chain_ref = {
        "result_bundle_id": chain["result_bundle_id"],
        "result_item_id": chain["result_item_id"],
        "stream_id": chain["stream_id"],
        "acked_prefix_hash": chain["acked_prefix_hash"],
    }
    _verify_chain_ref(chain_ref, allow_bundleless=True)
    if chain["result_bundle_id"] is None and chain["stream_id"] is None and chain["chain_status"] not in {"failed", "cancelled"}:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "only a failed/cancelled Skill chain may be bundleless")
    for index, receipt in enumerate(receipt_list):
        verify_skill_receipt(receipt)
        if receipt["chain_index"] != index or receipt["chain_id"] != chain["chain_id"] or receipt["run_snapshot_hash"] != chain["run_snapshot_hash"]:
            raise ContractValidationError("Skill chain receipt index/identity mismatch")
        receipt_ref = {key: receipt[key] for key in ("result_bundle_id", "result_item_id", "stream_id", "acked_prefix_hash")}
        if receipt_ref != chain_ref:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "Skill receipt anchor does not match its chain")
    if chain["receipt_hashes"] != [r["receipt_hash"] for r in receipt_list]:
        raise ContractValidationError("Skill chain receipt hashes do not match")
    if (chain["final_output_asset_id"] is None) != (chain["final_output_hash"] is None):
        raise ContractValidationError("final output asset/hash must be all-null or all-present")
    final_hash = chain["final_output_hash"] or "-"
    expected = sha256_hex(b"skill-chain/v1\n" + b"\n".join(r["receipt_hash"].encode("ascii") for r in receipt_list) + f"\n{final_hash}\n".encode("ascii"))
    _assert_hash(expected, chain["chain_hash"], "chain_hash")


def verify_compatibility(value: Mapping[str, Any]) -> None:
    if dict(value) != {"core_api": ">=1.0 <2.0", "plugin_rpc": "1", "ui_host": "1", "python": "3.12.*"}:
        raise ContractValidationError("compatibility does not match the v1 canonical matrix")
    assert_valid("compatibility-v1", value)


def _assert_exact_fields(value: Mapping[str, Any], fields: Iterable[str], label: str) -> None:
    expected = set(fields)
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ContractValidationError(f"{label} fields are not bound to the method schema: missing={missing}, extra={extra}")


def validate_rpc_request(request: Mapping[str, Any], *, expected_lease_epoch: int | None = None) -> None:
    is_heartbeat = request.get("method") == "runtime.heartbeat" and "id" not in request
    assert_valid("rpc-notification/v1" if is_heartbeat else "rpc-request/v1", request)
    if is_heartbeat:
        if "id" in request:
            raise ContractError(ErrorCode.INVALID_TRANSITION, "heartbeat must not carry an id")
        heartbeat_definition = METHOD_MATRIX["methods"]["runtime.heartbeat"]
        _assert_exact_fields(request["params"], heartbeat_definition["params"]["fields"], "runtime.heartbeat params")
        if expected_lease_epoch is not None and request["meta"]["lease_epoch"] != expected_lease_epoch:
            raise ContractError(ErrorCode.STALE_LEASE, "heartbeat lease epoch is stale")
        return
    method = request["method"]
    definition = METHOD_MATRIX["methods"].get(method)
    if definition is None or method == "runtime.heartbeat":
        raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown or notification-only RPC method")
    context = request["meta"]["context"]
    if context not in definition["meta_profile"].split("/"):
        raise ContractError(ErrorCode.INVALID_TRANSITION, f"{method} cannot use {context} meta profile")
    _assert_exact_fields(request["params"], definition["params"]["fields"], f"{method} params")
    if context in {"install", "attempt"} and expected_lease_epoch is not None:
        actual = request["meta"]["install_lease_epoch"] if context == "install" else request["meta"]["lease_epoch"]
        if actual != expected_lease_epoch:
            raise ContractError(ErrorCode.STALE_LEASE, "RPC lease epoch is stale")
    operation_methods = {
        "host.asset.create/v1",
        "host.model.invoke/v1",
        "host.capability.invoke/v1",
        "host.capability.cancel/v1",
        "host.candidate.stage/v1",
        "host.checkpoint.commit/v1",
        "host.stream.commit/v1",
        "host.job.event/v1",
        "host.job.await_user/v1",
        "host.job.complete/v1",
        "host.migration.lease.renew/v1",
        "host.migration.lease.release/v1",
    }
    if method in operation_methods and "operation_key" not in request["params"]:
        raise ContractValidationError(f"{method} requires operation_key")
    if method == "runtime.health":
        params = request["params"]
        lease_fields = ("db_lease_id", "db_lease_epoch", "owner_instance_id")
        if context == "install" and not all(params[key] is not None for key in lease_fields):
            raise ContractError(ErrorCode.STALE_LEASE, "install health requires all shadow DB lease fields")
        if context == "control" and any(params[key] is not None for key in lease_fields):
            raise ContractError(ErrorCode.STALE_LEASE, "control health cannot carry shadow DB lease fields")


def validate_rpc_result(
    method: str,
    result: Mapping[str, Any],
    *,
    request: Mapping[str, Any] | None = None,
) -> None:
    """Validate a result against the exact method branch, not just any RPC result.

    The generated success schema is a closed union.  Its result branches do
    not carry the request method, so the matrix field binding below is the
    second-stage check that prevents a valid result for another method from
    being accepted here.
    """
    definition = METHOD_MATRIX["methods"].get(method)
    if definition is None or method == "runtime.heartbeat":
        raise ContractError(ErrorCode.INVALID_TRANSITION, "unknown or notification-only RPC method")
    if request is not None and request.get("method") != method:
        raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "RPC result method does not match its request")
    _assert_exact_fields(result, definition["result"]["fields"], f"{method} result")
    response = {
        "jsonrpc": "2.0",
        "id": "123e4567-e89b-12d3-a456-426614174000",
        "result": dict(result),
    }
    assert_valid("rpc-success/v1", response)

    params = request.get("params", {}) if request is not None else {}
    if method == "capability.describe":
        verify_capability_descriptor(result["descriptor"], expected_capability_id=params.get("capability_id"))
    elif method == "runtime.handshake":
        if result["plugin_protocol"] != "1":
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "handshake attempted a protocol downgrade or upgrade")
        if request is not None and result["release_id"] != request["meta"]["plugin_release_id"]:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "handshake release does not match the request meta")
    elif method == "host.capability.invoke/v1":
        expected_contract = params.get("expected_result_contract")
        if expected_contract is not None and result["child_result_contract"] != expected_contract:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "child result contract does not match the binding request")
    elif method == "job.pause" and result["accepted"] and result["checkpoint_asset_id"] is None:
        raise ContractError(ErrorCode.CHECKPOINT_INVALID, "accepted job.pause must return a checkpoint Asset")


def validate_rpc_response(
    response: Mapping[str, Any],
    method: str | None = None,
    *,
    request: Mapping[str, Any] | None = None,
) -> None:
    if request is not None:
        request_method = request.get("method")
        if method is None:
            method = request_method
        elif method != request_method:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "RPC response method does not match its request")
    if "error" in response:
        assert_valid("rpc-error-v1", response)
        if response["error"]["code"] not in EXPECTED_ERROR_CODES:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "RPC error code is not in the v1 registry")
    else:
        assert_valid("rpc-success-v1", response)
        if method is not None:
            validate_rpc_result(method, response["result"], request=request)


def verify_contract_inventory() -> None:
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
    if tuple(METHOD_MATRIX["worker_methods"]) != EXPECTED_WORKER_METHODS or tuple(WORKER_METHODS) != EXPECTED_WORKER_METHODS:
        raise ContractValidationError("RPC worker method set/order differs from §20.1")
    if tuple(METHOD_MATRIX["host_methods"]) != EXPECTED_HOST_METHODS or tuple(HOST_METHODS) != EXPECTED_HOST_METHODS:
        raise ContractValidationError("RPC host method set/order differs from §20.2")
    if set(METHOD_MATRIX["methods"]) != set(EXPECTED_WORKER_METHODS + EXPECTED_HOST_METHODS):
        raise ContractValidationError("RPC matrix method set differs from §20.4")
    if {int(key): value for key, value in METHOD_MATRIX["error_codes"].items()} != EXPECTED_ERROR_CODES:
        raise ContractValidationError("RPC error code registry differs from §20.5")


def verify_history_bytes(raw: bytes, expected: bytes) -> None:
    if raw != expected:
        raise ContractValidationError("v1 history reader changed raw Asset bytes")


def load_strict_json(path: Path) -> Any:
    return parse_json_bytes(path.read_bytes())
