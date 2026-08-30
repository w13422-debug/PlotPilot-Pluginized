"""Validation-only DTOs and semantic guards for the additive M4/M5 v2 API.

The module intentionally has no database handle and no mutation method.  It
is the SDK-side ingress used by P0 fixtures and later Core adapters.  Shape
validation is delegated to the existing :func:`assert_valid` implementation;
the small checks below cover bindings that JSON Schema cannot express.
"""
from __future__ import annotations

import copy
import base64
import binascii
import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, TypedDict

from .canonical import hash_jcs
from .errors import ContractValidationError
from .verifier import assert_valid


ROOT = Path(__file__).resolve().parents[2]
V2_SCHEMA_DIR = ROOT / "contracts" / "json-schema"
V2_CURSOR_RE = re.compile(r"^(candidate|job|core)/[A-Za-z0-9][A-Za-z0-9._:/-]*$")


class V2Target(TypedDict):
    workspace_id: str
    entity_kind: Literal["document", "node_structure", "relation_set"]
    entity_id: str


class V2WriteSetEntry(V2Target):
    revision_id: str
    content_hash: str


class CandidateV2(TypedDict):
    schema: Literal["candidate/v2"]
    candidate_id: str
    workspace_id: str
    item_kind: Literal["document", "node_structure", "relation_set", "incomplete_stream"]
    target: V2Target
    mutation: dict[str, str]
    payload_asset_id: str
    base: dict[str, str]
    write_set: list[V2WriteSetEntry]
    parent_candidate_ids: list[str]
    source_refs: list[dict[str, Any]]
    status: Literal["complete", "partial", "failed", "skipped"]
    publication_eligibility: Literal["eligible", "review_only", "none"]
    created_at: str
    source_job_id: str | None


class PublicationCommandV2(TypedDict):
    schema: Literal["publication-command/v2"]
    publication_operation_key: str
    workspace_id: str
    candidate_id: str
    accepted_by: str


class PublicationResultV2(TypedDict):
    schema: Literal["publication-result/v2"]
    publication_id: str
    publication_operation_key: str
    candidate_id: str
    workspace_id: str
    entity_kind: Literal["document", "node_structure", "relation_set"]
    entity_id: str
    revision_id: str
    revision_number: int
    content_hash: str
    provenance_receipt_id: str
    idempotent: bool


class JobSnapshotV2(TypedDict):
    job_id: str
    workspace_id: str
    state: str
    job_revision: int
    writer_epoch: int
    current_attempt_id: str | None
    candidate_ids: list[str]
    checkpoint_id: str | None
    stream_high_waters: list[dict[str, Any]]
    job_event_high_water: int
    core_event_high_water: int
    created_at: str
    updated_at: str
    snapshot_hash: str


class PluginLifecycleCommandV2(TypedDict):
    schema: Literal["plugin-lifecycle-command/v2"]
    operation_key: str
    plugin_id: str
    action: Literal["install", "upgrade", "retire", "rollback"]
    release_id: str | None
    package_hash: str | None
    expected_generation_id: str | None
    target_generation_id: str | None


_SCHEMA_FOR_DISCRIMINATOR = {
    "candidate/v2": "candidate-query-result-v2",
    "candidate-list-query/v2": "candidate-query-result-v2",
    "candidate-list-result/v2": "candidate-query-result-v2",
    "candidate-get-query/v2": "candidate-query-result-v2",
    "candidate-get-result/v2": "candidate-query-result-v2",
    "candidate-preview-query/v2": "candidate-query-result-v2",
    "candidate-preview-result/v2": "candidate-query-result-v2",
    "core-authority-query/v2": "core-authority-command-query-v2",
    "core-authority-result/v2": "core-authority-command-query-v2",
    "publication-command/v2": "core-authority-command-query-v2",
    "publication-result/v2": "core-authority-command-query-v2",
    "core-http-error/v2": "core-authority-command-query-v2",
    "candidate-review-query/v2": "candidate-review-v2",
    "candidate-review-command/v2": "candidate-review-v2",
    "candidate-review-result/v2": "candidate-review-v2",
    "story-state-projection-input/v2": "story-state-projection-input-v2",
    "job-list-query/v2": "job-http-command-query-v2",
    "job-list-result/v2": "job-http-command-query-v2",
    "job-snapshot-query/v2": "job-http-command-query-v2",
    "job-snapshot-result/v2": "job-http-command-query-v2",
    "job-start-command/v2": "job-http-command-query-v2",
    "job-control-command/v2": "job-http-command-query-v2",
    "job-command-result/v2": "job-http-command-query-v2",
    "job-event-page-query/v2": "job-http-command-query-v2",
    "job-event-page-result/v2": "job-http-command-query-v2",
    "job-sse-recovery-query/v2": "job-http-command-query-v2",
    "job-sse-recovery-result/v2": "job-http-command-query-v2",
    "job-http-error/v2": "job-http-command-query-v2",
    "plugin-discovery-query/v2": "plugin-api-command-query-v2",
    "plugin-discovery-result/v2": "plugin-api-command-query-v2",
    "plugin-lifecycle-command/v2": "plugin-api-command-query-v2",
    "plugin-lifecycle-result/v2": "plugin-api-command-query-v2",
    "plugin-http-error/v2": "plugin-api-command-query-v2",
}


def _fail(message: str, *, path: str | None = None) -> None:
    raise ContractValidationError(f"v2 {message}", path=path)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _copy(value: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(value))


def _schema_for(value: Mapping[str, Any]) -> str:
    schema = value.get("schema")
    if not isinstance(schema, str) or schema not in _SCHEMA_FOR_DISCRIMINATOR:
        _fail(f"unsupported schema discriminator: {schema!r}")
    return _SCHEMA_FOR_DISCRIMINATOR[schema]


def parse_v2(value: Mapping[str, Any], *, contract_id: str | None = None) -> dict[str, Any]:
    """Validate a v2 discriminated value and return an independent copy."""

    record = _mapping(value, "wire value")
    assert_valid(contract_id or _schema_for(record), record)
    return _copy(record)


def parse_candidate_v2(value: Mapping[str, Any], *, expected_workspace_id: str | None = None, parent_records: Mapping[str, Mapping[str, Any]] | None = None) -> CandidateV2:
    parsed = parse_v2(value, contract_id="candidate-query-result-v2")
    if parsed.get("schema") != "candidate/v2":
        _fail("candidate parser received a non-record variant")
    validate_candidate_v2(parsed, expected_workspace_id=expected_workspace_id, parent_records=parent_records, _parsed=True)
    return parsed  # type: ignore[return-value]


def parse_candidate_query_result_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_v2(value, contract_id="candidate-query-result-v2")
    schema = parsed.get("schema")
    if schema == "candidate/v2":
        validate_candidate_v2(parsed)
    if schema == "candidate-get-result/v2":
        candidate = parsed["candidate"]
        validate_candidate_v2(candidate, expected_workspace_id=parsed["workspace_id"])
    if schema == "candidate-list-result/v2":
        _cursor(parsed["next_cursor"], "candidate")
        for candidate in parsed["items"]:
            validate_candidate_v2(candidate, expected_workspace_id=parsed["workspace_id"])
    if schema == "candidate-list-query/v2":
        _cursor(parsed["cursor"], "candidate")
    if schema == "candidate-preview-result/v2":
        _validate_candidate_preview(parsed)
    return parsed


def _validate_candidate_preview(value: Mapping[str, Any]) -> None:
    offset = value["offset"]
    length = value["length"]
    total_length = value["total_length"]
    next_offset = value["next_offset"]
    end = offset + length
    if end > total_length:
        _fail("candidate preview exceeds payload length")
    try:
        decoded = base64.b64decode(value["base64_chunk"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ContractValidationError("v2 candidate preview base64 is invalid") from exc
    if base64.b64encode(decoded).decode("ascii") != value["base64_chunk"]:
        _fail("candidate preview base64 is not canonical")
    if len(decoded) != length:
        _fail("candidate preview decoded length does not match length")
    if hashlib.sha256(decoded).hexdigest() != value["content_hash"]:
        _fail("candidate preview content_hash does not match returned bytes")
    if next_offset is None:
        if end != total_length:
            _fail("candidate preview ends before total payload length")
    elif next_offset != end or next_offset > total_length:
        _fail("candidate preview next_offset is not contiguous")
    if offset == 0 and length == total_length and value["content_hash"] != value["payload_hash"]:
        _fail("complete candidate preview does not match payload_hash")


def validate_candidate_v2(
    value: Mapping[str, Any],
    *,
    expected_workspace_id: str | None = None,
    parent_records: Mapping[str, Mapping[str, Any]] | None = None,
    _parsed: bool = False,
) -> None:
    candidate = _copy(value) if _parsed else parse_v2(value, contract_id="candidate-query-result-v2")
    if candidate.get("schema") != "candidate/v2":
        _fail("candidate semantic validation requires candidate/v2")
    workspace_id = candidate["workspace_id"]
    if expected_workspace_id is not None and workspace_id != expected_workspace_id:
        _fail("candidate crosses the expected Workspace", path="/workspace_id")
    target = candidate["target"]
    if target["workspace_id"] != workspace_id:
        _fail("Candidate target crosses Workspace", path="/target/workspace_id")
    write_set = candidate["write_set"]
    if any(item["workspace_id"] != workspace_id for item in write_set):
        _fail("Candidate write_set crosses Workspace", path="/write_set")
    target_key = (target["entity_kind"], target["entity_id"])
    target_entries = [item for item in write_set if (item["entity_kind"], item["entity_id"]) == target_key]
    if len(target_entries) != 1:
        _fail("Candidate target must occur exactly once in write_set", path="/write_set")
    base = candidate["base"]
    if (target_entries[0]["revision_id"], target_entries[0]["content_hash"]) != (base["revision_id"], base["content_hash"]):
        _fail("Candidate base must bind the target write_set entry", path="/base")

    expected_target_kind = {
        "document": "document",
        "node_structure": "node_structure",
        "relation_set": "relation_set",
        "incomplete_stream": "document",
    }[candidate["item_kind"]]
    if target["entity_kind"] != expected_target_kind:
        _fail("Candidate item_kind does not match target entity_kind", path="/target/entity_kind")
    expected_modes = {
        "document": {"replace", "text_patch", "append_text"},
        "node_structure": {"structure_patch"},
        "relation_set": {"relation_patch"},
        "incomplete_stream": {"replace"},
    }
    if candidate["mutation"]["mode"] not in expected_modes[candidate["item_kind"]]:
        _fail("Candidate mutation mode does not match item_kind", path="/mutation/mode")
    if candidate["item_kind"] == "incomplete_stream":
        if candidate["status"] != "partial" or candidate["publication_eligibility"] != "eligible" or candidate["source_job_id"] is None:
            _fail("incomplete_stream Candidate must be a Core-generated eligible partial")
        if candidate["mutation"]["payload_schema"] != "core/document-text/v2":
            _fail("incomplete_stream Candidate has an unsupported payload schema")
    elif candidate["status"] == "partial" and candidate["publication_eligibility"] == "eligible":
        _fail("normal partial Candidate cannot be publishable")
    if candidate["status"] in {"failed", "skipped"} and candidate["publication_eligibility"] != "none":
        _fail("failed/skipped Candidate cannot be eligible")

    if parent_records is not None:
        _assert_parent_closure(candidate, parent_records)


def _assert_parent_closure(candidate: Mapping[str, Any], parent_records: Mapping[str, Mapping[str, Any]]) -> None:
    parent_ids = list(candidate["parent_candidate_ids"])
    for parent_id in parent_ids:
        parent = parent_records.get(parent_id)
        if parent is None:
            _fail(f"Candidate parent is missing: {parent_id}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(candidate_id: str) -> None:
        if candidate_id in visiting:
            _fail("Candidate parent graph contains a cycle")
        if candidate_id in visited:
            return
        visiting.add(candidate_id)
        node = candidate if candidate_id == candidate.get("candidate_id") else parent_records.get(candidate_id)
        if node is None:
            _fail(f"Candidate parent closure is incomplete: {candidate_id}")
        if node.get("status") in {"rejected", "deleted", "expired"}:
            _fail(f"Candidate parent is not live: {candidate_id}")
        for parent_id in node.get("parent_candidate_ids", []):
            visit(parent_id)
        visiting.remove(candidate_id)
        visited.add(candidate_id)

    visit(candidate["candidate_id"])


def parse_core_authority_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    return parse_v2(value, contract_id="core-authority-command-query-v2")


def parse_candidate_review_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    return parse_v2(value, contract_id="candidate-review-v2")


def parse_story_state_projection_input_v2(value: Mapping[str, Any], *, expected_workspace_id: str | None = None) -> dict[str, Any]:
    parsed = parse_v2(value, contract_id="story-state-projection-input-v2")
    validate_story_state_projection_v2(parsed, expected_workspace_id=expected_workspace_id, _parsed=True)
    return parsed


def validate_story_state_projection_v2(value: Mapping[str, Any], *, expected_workspace_id: str | None = None, _parsed: bool = False) -> None:
    projection = _copy(value) if _parsed else parse_v2(value, contract_id="story-state-projection-input-v2")
    workspace_id = projection["workspace_id"]
    if expected_workspace_id is not None and workspace_id != expected_workspace_id:
        _fail("Story-State projection crosses Workspace")
    if projection["publication"]["candidate_id"] != projection["candidate"]["candidate_id"]:
        _fail("projection Publication and Candidate are not bound", path="/publication/candidate_id")
    if projection["publication"]["revision_id"] != projection["current_revision"]["revision_id"]:
        _fail("projection Publication is not the current Revision", path="/publication/revision_id")
    if projection["publication"]["revision_number"] != projection["current_revision"]["revision_number"] or projection["publication"]["content_hash"] != projection["current_revision"]["content_hash"]:
        _fail("projection Publication does not match the current Revision")
    target = projection["candidate"]["target"]
    if target["workspace_id"] != workspace_id:
        _fail("projection Candidate target crosses Workspace")
    if projection["candidate"]["candidate_id"] != projection["publication"]["candidate_id"]:
        _fail("projection Candidate identity is not bound")
    assets = {item["asset_id"]: item["sha256"] for item in projection["assets"]}
    if assets.get(projection["candidate"]["payload_asset_id"]) != projection["candidate"]["payload_hash"]:
        _fail("projection Candidate payload is absent from Asset closure")
    if assets.get(projection["current_revision"]["content_asset_id"]) != projection["current_revision"]["content_hash"]:
        _fail("projection Revision content is absent from Asset closure")
    provenance = projection["provenance"]
    receipts = {item["receipt_id"]: item for item in projection["receipt_closure"]}
    root = receipts.get(provenance["receipt_id"])
    if root is None or root["receipt_hash"] != provenance["receipt_hash"]:
        _fail("projection provenance root is absent from receipt closure")
    if root["parent_receipt_ids"] != provenance["parent_receipt_ids"]:
        _fail("projection provenance parent binding is inconsistent")
    _assert_receipt_closure(provenance["receipt_id"], receipts)


def _assert_receipt_closure(root_id: str, receipts: Mapping[str, Mapping[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(receipt_id: str) -> None:
        if receipt_id in visiting:
            _fail("provenance receipt closure contains a cycle")
        if receipt_id in visited:
            return
        receipt = receipts.get(receipt_id)
        if receipt is None:
            _fail(f"provenance receipt parent is missing: {receipt_id}")
        visiting.add(receipt_id)
        for parent_id in receipt["parent_receipt_ids"]:
            visit(parent_id)
        visiting.remove(receipt_id)
        visited.add(receipt_id)

    visit(root_id)


def _cursor(value: str | None, expected_domain: str, *, job_id: str | None = None) -> tuple[str, str | None, int | None]:
    if value is None:
        return expected_domain, None, None
    if not isinstance(value, str) or not V2_CURSOR_RE.fullmatch(value):
        _fail("cursor is malformed")
    parts = value.split("/")
    domain = parts[0]
    if domain != expected_domain:
        _fail(f"cursor domain mismatch: expected {expected_domain}, got {domain}")
    if domain == "job":
        if len(parts) != 3 or job_id is not None and parts[1] != job_id or not parts[2].isdigit():
            _fail("job cursor is not bound to the Job")
        return domain, parts[1], int(parts[2])
    if domain == "core":
        if len(parts) != 2 or not parts[1].isdigit():
            _fail("core cursor is malformed")
        return domain, None, int(parts[1])
    return domain, None, None


def parse_job_http_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_v2(value, contract_id="job-http-command-query-v2")
    schema = parsed.get("schema")
    if schema == "job-list-result/v2":
        _validate_job_list_result(parsed)
    elif schema == "job-list-query/v2":
        _cursor(parsed["cursor"], "job")
    elif schema == "job-snapshot-result/v2":
        _validate_job_snapshot_result(parsed)
    elif schema == "job-command-result/v2":
        _validate_job_command_result(parsed)
    elif schema == "job-event-page-result/v2":
        parse_job_event_page_v2(parsed)
    elif schema == "job-event-page-query/v2":
        _validate_job_event_page_query(parsed)
    elif schema == "job-sse-recovery-result/v2":
        parse_job_sse_recovery_v2(parsed)
    elif schema == "job-sse-recovery-query/v2":
        _validate_job_sse_recovery_query(parsed)
    return parsed


def parse_job_snapshot_v2(value: Mapping[str, Any]) -> JobSnapshotV2:
    snapshot = _mapping(value, "Job snapshot")
    wrapper = {"schema": "job-snapshot-result/v2", "workspace_id": snapshot.get("workspace_id"), "job_id": snapshot.get("job_id"), "snapshot": snapshot, "cursor": f"job/{snapshot.get('job_id', '')}/{snapshot.get('job_event_high_water', 0)}"}
    assert_valid("job-http-command-query-v2", wrapper)
    _validate_job_snapshot(snapshot)
    return copy.deepcopy(dict(snapshot))  # type: ignore[return-value]


def _validate_job_snapshot(snapshot: Mapping[str, Any]) -> None:
    if snapshot["job_event_high_water"] < 0 or snapshot["core_event_high_water"] < 0:
        _fail("Job event high-water cannot be negative")
    for stream in snapshot["stream_high_waters"]:
        if stream["acked_bytes"] < 0 or stream["acked_prefix_seq"] < 0:
            _fail("stream high-water cannot be negative")
        if stream["target"]["workspace_id"] != snapshot["workspace_id"]:
            _fail("stream target crosses Job Workspace")
    expected_hash = hash_jcs("job-snapshot/v2", {key: value for key, value in snapshot.items() if key != "snapshot_hash"})
    if snapshot["snapshot_hash"] != expected_hash:
        _fail("Job snapshot hash mismatch")


def _validate_job_snapshot_result(value: Mapping[str, Any]) -> None:
    snapshot = value["snapshot"]
    if value["workspace_id"] != snapshot["workspace_id"] or value["job_id"] != snapshot["job_id"]:
        _fail("Job snapshot result is not bound to its outer Workspace and Job")
    _cursor(value["cursor"], "job", job_id=value["job_id"])
    _validate_job_snapshot(snapshot)


def _validate_job_list_result(value: Mapping[str, Any]) -> None:
    _cursor(value["next_cursor"], "job")
    for snapshot in value["items"]:
        if snapshot["workspace_id"] != value["workspace_id"]:
            _fail("Job list item crosses its Workspace")
        _validate_job_snapshot(snapshot)


def _validate_job_command_result(value: Mapping[str, Any]) -> None:
    _cursor(value["snapshot_cursor"], "job", job_id=value["job_id"])


def _validate_job_event_page_query(value: Mapping[str, Any]) -> None:
    _, cursor_job_id, cursor_seq = _cursor(value["after_cursor"], "job", job_id=value["job_id"])
    if cursor_seq is None:
        if value["after_job_event_seq"] != 0:
            _fail("Job event query without a cursor must start at sequence zero")
    elif cursor_seq != value["after_job_event_seq"]:
        _fail("Job event query cursor and sequence are not paired")
    if cursor_job_id is not None and cursor_job_id != value["job_id"]:
        _fail("Job event query cursor is not bound to the Job")


def _validate_job_sse_recovery_query(value: Mapping[str, Any]) -> None:
    _, cursor_job_id, cursor_seq = _cursor(value["last_event_id"], "job", job_id=value["job_id"])
    if cursor_seq is None or cursor_seq != value["after_seq"]:
        _fail("SSE after_seq and last_event_id are not paired")
    if cursor_job_id != value["job_id"]:
        _fail("SSE last_event_id is not bound to the Job")


def _validate_job_event(value: Mapping[str, Any], *, job_id: str) -> None:
    if value["job_id"] != job_id:
        _fail("Job event crosses Job identity")
    if (value["payload_asset_id"] is None) != (value["payload_hash"] is None):
        _fail("Job event payload Asset ID and hash must be paired")


def parse_job_event_page_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_v2(value, contract_id="job-http-command-query-v2")
    if parsed.get("schema") != "job-event-page-result/v2":
        _fail("Job event parser received a non-page variant")
    _, _, next_seq = _cursor(parsed["next_cursor"], "job", job_id=parsed["job_id"])
    if next_seq is None or next_seq > parsed["high_water_seq"]:
        _fail("Job event next cursor is ahead of its high-water")
    events = parsed["events"]
    if events and events[-1]["job_event_seq"] > parsed["high_water_seq"]:
        _fail("Job event page exceeds its high-water")
    if any(left["job_event_seq"] >= right["job_event_seq"] for left, right in zip(events, events[1:])):
        _fail("Job events are not strictly ordered")
    for event in events:
        _validate_job_event(event, job_id=parsed["job_id"])
    return parsed


def parse_job_sse_recovery_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_v2(value, contract_id="job-http-command-query-v2")
    if parsed.get("schema") != "job-sse-recovery-result/v2":
        _fail("SSE parser received a non-recovery variant")
    _, _, snapshot_seq = _cursor(parsed["snapshot_cursor"], "job", job_id=parsed["job_id"])
    requested = parsed["requested_after_seq"]
    floor = parsed["replay_floor_seq"]
    durable = parsed["durable_high_water_seq"]
    if snapshot_seq is None or snapshot_seq > durable:
        _fail("SSE snapshot cursor is ahead of the durable Job high-water")
    if requested > durable:
        _fail("SSE cursor is ahead of the durable Job high-water")
    if parsed["gap"]:
        if not parsed["snapshot_required"] or parsed["snapshot"] is None or floor <= requested:
            _fail("SSE replay gap requires snapshot recovery")
        if parsed["snapshot"]["workspace_id"] != parsed["workspace_id"] or parsed["snapshot"]["job_id"] != parsed["job_id"]:
            _fail("SSE recovery snapshot is not bound to its outer Workspace and Job")
        parse_job_snapshot_v2(parsed["snapshot"])
        if snapshot_seq != parsed["snapshot"]["job_event_high_water"]:
            _fail("SSE snapshot cursor is not bound to the recovered snapshot")
    else:
        if parsed["snapshot_required"] or parsed["snapshot"] is not None or floor > requested + 1:
            _fail("SSE replay response has an inconsistent gap marker")
    for left, right in zip(parsed["tail"], parsed["tail"][1:]):
        if left["job_event_seq"] >= right["job_event_seq"]:
            _fail("SSE tail events are not strictly ordered")
    for event in parsed["tail"]:
        _validate_job_event(event, job_id=parsed["job_id"])
        if event["job_event_seq"] <= requested or event["job_event_seq"] > durable:
            _fail("SSE tail is outside the requested Job cursor range")
    return parsed


def parse_plugin_api_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_v2(value, contract_id="plugin-api-command-query-v2")
    if parsed.get("schema") == "plugin-discovery-query/v2":
        _cursor(parsed["cursor"], "core")
    elif parsed.get("schema") == "plugin-discovery-result/v2":
        _cursor(parsed["next_cursor"], "core")
    elif parsed.get("schema") == "plugin-lifecycle-command/v2":
        # Keep direct SDK parsing and HTTP ingress on the same semantic guard.
        # JSON Schema can express the nullable fields, but not the action
        # specific release/target-generation requirements.
        validate_plugin_lifecycle_v2(parsed)
    return parsed


def parse_plugin_record_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    record = _mapping(value, "Plugin record")
    wrapper = {"schema": "plugin-discovery-result/v2", "items": [record], "next_cursor": None, "cursor_domain": "core", "total": 1}
    assert_valid("plugin-api-command-query-v2", wrapper)
    return copy.deepcopy(dict(record))


def validate_plugin_lifecycle_v2(value: Mapping[str, Any], *, current_generation_id: str | None = None, active_job: bool = False) -> None:
    command = parse_v2(value, contract_id="plugin-api-command-query-v2")
    if command.get("schema") != "plugin-lifecycle-command/v2":
        _fail("plugin lifecycle validation requires a command")
    if current_generation_id is not None and command["expected_generation_id"] != current_generation_id:
        _fail("plugin generation CAS is stale")
    if active_job and command["action"] == "retire":
        _fail("active Job pins the plugin release")
    if command["action"] in {"install", "upgrade", "rollback"} and command["target_generation_id"] is None:
        _fail("plugin lifecycle command has no target generation")
    if command["action"] in {"install", "upgrade"} and (command["release_id"] is None or command["package_hash"] is None):
        _fail("plugin lifecycle install/upgrade must bind release and package")


def validate_publication_v2(
    command: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any] | None = None,
    expected_workspace_id: str | None = None,
) -> None:
    parsed_command = parse_v2(command, contract_id="core-authority-command-query-v2")
    parsed_result = parse_v2(result, contract_id="core-authority-command-query-v2")
    if parsed_command.get("schema") != "publication-command/v2" or parsed_result.get("schema") != "publication-result/v2":
        _fail("Publication binding requires command and result variants")
    if parsed_command["publication_operation_key"] != parsed_result["publication_operation_key"] or parsed_command["candidate_id"] != parsed_result["candidate_id"] or parsed_command["workspace_id"] != parsed_result["workspace_id"]:
        _fail("Publication result does not match its command")
    if expected_workspace_id is not None and parsed_command["workspace_id"] != expected_workspace_id:
        _fail("Publication crosses Workspace")
    if candidate is not None:
        parsed_candidate = parse_candidate_v2(candidate, expected_workspace_id=parsed_command["workspace_id"])
        if parsed_candidate["candidate_id"] != parsed_result["candidate_id"]:
            _fail("Publication candidate binding mismatch")
        if parsed_candidate["publication_eligibility"] != "eligible" or parsed_candidate["status"] in {"failed", "skipped"}:
            _fail("Candidate is not publishable")
        if parsed_candidate["status"] == "partial" and parsed_candidate["item_kind"] != "incomplete_stream":
            _fail("normal partial Candidate is review-only")
        if parsed_result["entity_kind"] != parsed_candidate["target"]["entity_kind"] or parsed_result["entity_id"] != parsed_candidate["target"]["entity_id"]:
            _fail("Publication result is not bound to Candidate target")
        if parsed_result["content_hash"] != parsed_candidate["mutation"]["payload_hash"]:
            _fail("Publication result is not bound to Candidate payload")


def parse_publication_command_v2(value: Mapping[str, Any]) -> PublicationCommandV2:
    parsed = parse_v2(value, contract_id="core-authority-command-query-v2")
    if parsed.get("schema") != "publication-command/v2":
        _fail("Publication command parser received a non-command variant")
    return parsed  # type: ignore[return-value]


def parse_publication_result_v2(value: Mapping[str, Any]) -> PublicationResultV2:
    parsed = parse_v2(value, contract_id="core-authority-command-query-v2")
    if parsed.get("schema") != "publication-result/v2":
        _fail("Publication result parser received a non-result variant")
    return parsed  # type: ignore[return-value]


def parse_publication_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_core_authority_v2(value)
    if parsed.get("schema") not in {"publication-command/v2", "publication-result/v2"}:
        _fail("Publication parser received a non-Publication variant")
    return parsed


# Friendly aliases used by downstream adapters while retaining one verifier.
parse_candidate = parse_candidate_v2
parse_publication = parse_publication_v2
parse_story_state_projection = parse_story_state_projection_input_v2
parse_job_snapshot = parse_job_snapshot_v2
parse_job_sse_recovery = parse_job_sse_recovery_v2
parse_plugin_lifecycle = validate_plugin_lifecycle_v2
parse_core_authority = parse_core_authority_v2
parse_candidate_review = parse_candidate_review_v2
parse_job_http = parse_job_http_v2
parse_plugin_api = parse_plugin_api_v2
parse_story_state_projection_input = parse_story_state_projection_input_v2


__all__ = [
    "CandidateV2",
    "JobSnapshotV2",
    "PluginLifecycleCommandV2",
    "PublicationCommandV2",
    "PublicationResultV2",
    "V2Target",
    "V2WriteSetEntry",
    "parse_v2",
    "parse_candidate_v2",
    "parse_candidate_query_result_v2",
    "validate_candidate_v2",
    "parse_core_authority_v2",
    "parse_publication_command_v2",
    "parse_publication_result_v2",
    "parse_publication_v2",
    "parse_candidate_review_v2",
    "parse_story_state_projection_input_v2",
    "validate_story_state_projection_v2",
    "parse_job_http_v2",
    "parse_job_snapshot_v2",
    "parse_job_event_page_v2",
    "parse_job_sse_recovery_v2",
    "parse_plugin_api_v2",
    "parse_plugin_record_v2",
    "validate_plugin_lifecycle_v2",
    "validate_publication_v2",
    "parse_candidate",
    "parse_publication",
    "parse_story_state_projection",
    "parse_job_snapshot",
    "parse_job_sse_recovery",
    "parse_plugin_lifecycle",
    "parse_core_authority",
    "parse_candidate_review",
    "parse_job_http",
    "parse_plugin_api",
    "parse_story_state_projection_input",
]
