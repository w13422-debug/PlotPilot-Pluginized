"""Generate the checked-in PlotPilot v1 contract schemas.

The generator is deliberately data-only: it contains the field inventory from
formal design v1.2 and emits deterministic Draft 2020-12 JSON.  The generated
files are part of the public contract surface; callers must not edit them by
hand.  ``--check`` is used by the M0 verifier to detect drift.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "contracts" / "json-schema"
PACKAGE_RESOURCE_DIR = ROOT / "backend" / "plotpilot_plugin_sdk" / "resources"
EXTERNAL_AUTHORED_ARTIFACTS = frozenset(
    {
        "prompt-skill-execute-request-v2.schema.json",
        "prompt-skill-execute-result-v2.schema.json",
        "rpc-method-matrix.v2.json",
        "rpc-method-success-v2.schema.json",
    }
)
PACKAGE_RESOURCE_SOURCES = {
    "unicode-casefold-v1.json": ROOT / "contracts" / "unicode-casefold-v1.json",
    "rpc-method-matrix.v1.json": SCHEMA_DIR / "rpc-method-matrix.v1.json",
    "rpc-method-matrix.v2.json": SCHEMA_DIR / "rpc-method-matrix.v2.json",
    "rpc-error-v1.schema.json": SCHEMA_DIR / "rpc-error-v1.schema.json",
    "prompt-skill-execute-request-v2.schema.json": SCHEMA_DIR / "prompt-skill-execute-request-v2.schema.json",
    "prompt-skill-execute-result-v2.schema.json": SCHEMA_DIR / "prompt-skill-execute-result-v2.schema.json",
    "rpc-method-success-v2.schema.json": SCHEMA_DIR / "rpc-method-success-v2.schema.json",
}


def const(value: Any) -> dict[str, Any]:
    return {"const": value}


def enum(*values: Any) -> dict[str, Any]:
    return {"enum": list(values)}


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def array(items: dict[str, Any], *, min_items: int = 0, unique: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "array", "items": items, "minItems": min_items}
    if unique:
        out["uniqueItems"] = True
    return out


def obj(properties: dict[str, dict[str, Any]], required: tuple[str, ...] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        out["required"] = list(required)
    return out


ID = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"}
UUID = {"type": "string", "pattern": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"}
HASH = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
BASE64 = {
    "type": "string",
    "pattern": r"^(?:[A-Za-z0-9+/]{4})*(?:(?:[A-Za-z0-9+/]{2}==)|(?:[A-Za-z0-9+/]{3}=))?$",
}
UTC = {"type": "string", "pattern": r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$"}
SEMVER = {"type": "string", "pattern": r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"}
PATH = {"type": "string", "minLength": 1, "maxLength": 240, "pattern": r"^[^\\\x00]+$"}
STR = {"type": "string"}
NONEMPTY = {"type": "string", "minLength": 1}
BOOL = {"type": "boolean"}
INT = {"type": "integer"}
POS_INT = {"type": "integer", "minimum": 1}
NONNEG_INT = {"type": "integer", "minimum": 0}


CORE_EVENT_TYPES = (
    "workspace.created",
    "revision.published",
    "candidate.staged",
    "candidate.decided",
    "job.state.changed",
    "job.terminal",
    "plugin.generation.changed",
    "plugin.release.retiring",
    "backup.completed",
    "restore.completed",
)
HOST_METHODS = (
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
WORKER_METHODS = (
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
ALL_METHODS = WORKER_METHODS + HOST_METHODS


def diagnostic() -> dict[str, Any]:
    return obj(
        {"code": NONEMPTY, "message": STR, "details_asset_id": nullable(ID)},
        ("code", "message", "details_asset_id"),
    )


def source_ref() -> dict[str, Any]:
    return obj(
        {
            "workspace_id": nullable(ID),
            "source_type": NONEMPTY,
            "source_id": ID,
            "revision_or_hash": NONEMPTY,
        },
        ("workspace_id", "source_type", "source_id", "revision_or_hash"),
    )


def target() -> dict[str, Any]:
    return obj(
        {"workspace_id": ID, "entity_kind": enum("document", "node_structure", "relation_set"), "entity_id": ID},
        ("workspace_id", "entity_kind", "entity_id"),
    )


def candidate_item() -> dict[str, Any]:
    return obj(
        {
            "schema": const("candidate-item/v1"),
            "item_id": ID,
            "item_kind": enum("document", "node_structure", "relation_set", "incomplete_stream"),
            "target": target(),
            "mutation": obj(
                {
                    "mode": enum("replace", "text_patch", "structure_patch", "relation_patch", "append_text"),
                    "payload_schema": ID,
                    "payload_hash": HASH,
                },
                ("mode", "payload_schema", "payload_hash"),
            ),
            "payload_asset_id": ID,
            "base": obj({"revision_id": ID, "content_hash": HASH}, ("revision_id", "content_hash")),
            "write_set": array(
                obj(
                    {
                        "workspace_id": ID,
                        "entity_kind": enum("document", "node_structure", "relation_set"),
                        "entity_id": ID,
                        "revision_id": ID,
                        "content_hash": HASH,
                    },
                    ("workspace_id", "entity_kind", "entity_id", "revision_id", "content_hash"),
                ),
                min_items=1,
            ),
            "parent_candidate_ids": array(ID, unique=True),
            "source_refs": array(source_ref()),
            "status": enum("complete", "partial", "failed", "skipped"),
        },
        (
            "schema",
            "item_id",
            "item_kind",
            "target",
            "mutation",
            "payload_asset_id",
            "base",
            "write_set",
            "parent_candidate_ids",
            "source_refs",
            "status",
        ),
    )


def artifact_item() -> dict[str, Any]:
    return obj(
        {
            "schema": const("artifact-item/v1"),
            "item_id": ID,
            "artifact_kind": ID,
            "payload_asset_id": ID,
            "payload_hash": HASH,
            "mime": NONEMPTY,
            "source_refs": array(source_ref()),
            "status": enum("complete", "partial", "failed", "skipped"),
        },
        ("schema", "item_id", "artifact_kind", "payload_asset_id", "payload_hash", "mime", "source_refs", "status"),
    )


def diagnostic_item() -> dict[str, Any]:
    return obj(
        {
            "schema": const("diagnostic-item/v1"),
            "item_id": ID,
            "severity": enum("info", "warning", "error"),
            "code": NONEMPTY,
            "message": STR,
            "details_asset_id": nullable(ID),
            "details_hash": nullable(HASH),
            "source_refs": array(source_ref()),
            "status": enum("complete", "failed", "skipped"),
        },
        (
            "schema",
            "item_id",
            "severity",
            "code",
            "message",
            "details_asset_id",
            "details_hash",
            "source_refs",
            "status",
        ),
    )


def skill_chain_ref() -> dict[str, Any]:
    return obj(
        {
            "schema": const("skill-chain-ref/v1"),
            "chain_result_id": ID,
            "asset_id": nullable(ID),
            "asset_hash": nullable(HASH),
            "result_bundle_id": nullable(ID),
            "result_item_id": nullable(ID),
            "stream_id": nullable(ID),
            "acked_prefix_hash": nullable(HASH),
        },
        (
            "schema",
            "chain_result_id",
            "asset_id",
            "asset_hash",
            "result_bundle_id",
            "result_item_id",
            "stream_id",
            "acked_prefix_hash",
        ),
    )


def producer() -> dict[str, Any]:
    return obj(
        {
            "plugin_id": ID,
            "release_id": HASH,
            "capability_id": ID,
            "job_id": ID,
            "step_id": ID,
            "attempt_id": ID,
            "lease_epoch": POS_INT,
        },
        ("plugin_id", "release_id", "capability_id", "job_id", "step_id", "attempt_id", "lease_epoch"),
    )


def result_bundle() -> dict[str, Any]:
    item = {"oneOf": [candidate_item(), artifact_item(), diagnostic_item()]}
    return obj(
        {
            "schema": const("result-bundle/v1"),
            "contract_id": enum("candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"),
            "bundle_id": ID,
            "bundle_type": enum("candidate_batch", "artifact", "diagnostic"),
            "producer": producer(),
            "input_snapshot_hash": HASH,
            "items": array(item),
            "warnings": array(
                obj(
                    {"code": NONEMPTY, "message": STR, "details_asset_id": nullable(ID)},
                    ("code", "message", "details_asset_id"),
                )
            ),
            "partial": BOOL,
            "provenance_receipt_id": ID,
            "skill_chain_result_refs": array(skill_chain_ref()),
        },
        (
            "schema",
            "contract_id",
            "bundle_id",
            "bundle_type",
            "producer",
            "input_snapshot_hash",
            "items",
            "warnings",
            "partial",
            "provenance_receipt_id",
            "skill_chain_result_refs",
        ),
    )


def manifest_schema() -> dict[str, Any]:
    compatibility = obj(
        {
            "core_api": {"type": "string", "pattern": r"^>=(0|[1-9][0-9]*)\.(0|[1-9][0-9]*) <(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"},
            "plugin_rpc": {"type": "string", "pattern": r"^(0|[1-9][0-9]*)$"},
            "ui_host": {"type": "string", "pattern": r"^(0|[1-9][0-9]*)$"},
            "python": {"type": "string", "pattern": r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.\*$"},
        },
        ("core_api", "plugin_rpc", "ui_host"),
    )
    capability = obj(
        {
            "capability_id": ID,
            "operations": array(enum("run", "resume", "cancel", "validate"), min_items=1, unique=True),
            "result_contract": enum("candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"),
        },
        ("capability_id", "operations", "result_contract"),
    )
    settings = obj(
        {
            "namespace": {"type": "string", "pattern": r"^plugin\.[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"},
            "schema": PATH,
            "defaults": PATH,
            "migration_manifest": nullable(PATH),
        },
        ("namespace", "schema", "defaults", "migration_manifest"),
    )
    contribution = obj({"contribution_id": ID, "slot": ID, "capability_id": ID}, ("contribution_id", "slot", "capability_id"))
    ui = obj(
        {"entry": PATH, "runtime": const("worker-ui/v1"), "contributions": array(contribution)},
        ("entry", "runtime", "contributions"),
    )
    backend = obj(
        {
            "entrypoint": NONEMPTY,
            "wheel": PATH,
            "requirements_lock": PATH,
            "wheelhouse": PATH,
            "max_concurrency": POS_INT,
        },
        ("entrypoint", "wheel", "requirements_lock", "wheelhouse", "max_concurrency"),
    )
    storage = obj(
        {"schema_version": POS_INT, "migration_policy": const("transactional-shadow"), "migration_manifest": PATH},
        ("schema_version", "migration_policy", "migration_manifest"),
    )
    data = obj({"format": ID, "root": PATH}, ("format", "root"))
    common = {
        "schema": const("plotpilot-plugin/v1"),
        "plugin_id": ID,
        "version": SEMVER,
        "display_name": NONEMPTY,
        "compatibility": compatibility,
        "capabilities": array(capability),
        "settings": nullable(settings),
        "needs": array(enum(*HOST_METHODS), unique=True),
    }
    code = copy.deepcopy(common)
    code.update({"kind": const("code"), "backend": backend, "storage": storage, "ui": nullable(ui), "data": nullable(data)})
    data_plugin = copy.deepcopy(common)
    data_plugin.update({"kind": const("data"), "data": data})
    return {"oneOf": [obj(code, tuple(code)), obj(data_plugin, tuple(data_plugin))], "unevaluatedProperties": False}


def common_manifest_schemas() -> dict[str, dict[str, Any]]:
    capability_provider = obj(
        {
            "schema": const("capability-provider/v1"),
            "capability_id": ID,
            "provider": obj({"plugin_id": ID, "release_id": ID}, ("plugin_id", "release_id")),
            "input_schema": ID,
            "output_schema": ID,
            "result_contract": enum("candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"),
            "supports": array(enum("run", "resume", "cancel", "validate"), unique=True),
            "deterministic": BOOL,
            "accepted_data_formats": array(ID, unique=True),
        },
        (
            "schema",
            "capability_id",
            "provider",
            "input_schema",
            "output_schema",
            "result_contract",
            "supports",
            "deterministic",
            "accepted_data_formats",
        ),
    )
    data_bundle = obj(
        {
            "schema": const("plugin-data-bundle/v1"),
            "bundle_id": ID,
            "data_plugin_id": ID,
            "data_release_id": ID,
            "package_hash": HASH,
            "format_id": ID,
            "root_path": PATH,
            "files": array(
                obj({"path": PATH, "asset_id": ID, "sha256": HASH, "mime": NONEMPTY, "size": NONNEG_INT}, ("path", "asset_id", "sha256", "mime", "size")),
                unique=True,
            ),
            "bundle_hash": HASH,
        },
        ("schema", "bundle_id", "data_plugin_id", "data_release_id", "package_hash", "format_id", "root_path", "files", "bundle_hash"),
    )
    return {"capability-provider-v1": capability_provider, "plugin-data-bundle-v1": data_bundle}


def run_snapshot() -> dict[str, Any]:
    scope = obj({"document_id": nullable(ID), "node_id": nullable(ID), "operation": ID}, ("document_id", "node_id", "operation"))
    input_revision = obj({"document_id": ID, "revision_id": ID, "content_hash": HASH}, ("document_id", "revision_id", "content_hash"))
    plugin_release = obj({"plugin_id": ID, "release_id": ID, "package_hash": HASH, "data_generation_id": nullable(ID)}, ("plugin_id", "release_id", "package_hash", "data_generation_id"))
    setting = obj({"plugin_id": ID, "settings_revision_id": ID, "scope": enum("global", "workspace"), "scope_id": nullable(ID), "schema_hash": HASH, "validated_by_release_id": ID}, ("plugin_id", "settings_revision_id", "scope", "scope_id", "schema_hash", "validated_by_release_id"))
    data_binding = obj({"data_plugin_id": ID, "data_release_id": ID, "format_id": ID, "bundle_asset_id": ID, "bundle_hash": HASH, "interpreter_binding_id": ID, "order": POS_INT}, ("data_plugin_id", "data_release_id", "format_id", "bundle_asset_id", "bundle_hash", "interpreter_binding_id", "order"))
    skill = obj({"skill_id": ID, "release_id": ID, "package_hash": HASH, "parameters_asset_id": nullable(ID), "order": POS_INT}, ("skill_id", "release_id", "package_hash", "parameters_asset_id", "order"))
    asset = obj({"asset_id": ID, "sha256": HASH}, ("asset_id", "sha256"))
    return obj(
        {
            "schema": const("run-snapshot/v1"),
            "snapshot_id": ID,
            "core_contract_version": SEMVER,
            "workspace_id": ID,
            "scope": scope,
            "input_revisions": array(input_revision),
            "plan_revision_id": ID,
            "plugin_releases": array(plugin_release),
            "plugin_settings_revisions": array(setting),
            "data_bindings": array(data_binding),
            "skill_releases": array(skill),
            "model_profile_revision_id": nullable(ID),
            "parameters_asset_id": nullable(ID),
            "asset_hashes": array(asset),
            "request_key": HASH,
            "run_intent_id": ID,
            "created_at": UTC,
            "snapshot_hash": HASH,
        },
        (
            "schema", "snapshot_id", "core_contract_version", "workspace_id", "scope", "input_revisions", "plan_revision_id",
            "plugin_releases", "plugin_settings_revisions", "data_bindings", "skill_releases", "model_profile_revision_id",
            "parameters_asset_id", "asset_hashes", "request_key", "run_intent_id", "created_at", "snapshot_hash",
        ),
    )


def broker_invocation() -> dict[str, Any]:
    return obj(
        {
            "schema": const("broker-invocation/v1"),
            "invocation_id": ID,
            "parent_job_id": ID,
            "parent_step_id": ID,
            "parent_attempt_id": ID,
            "invoke_operation_key": ID,
            "binding_id": ID,
            "input_asset_id": ID,
            "input_hash": HASH,
            "parameters_asset_id": nullable(ID),
            "parameters_hash": nullable(HASH),
            "expected_result_contract": enum("candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"),
            "required": BOOL,
            "propagate_cancel": BOOL,
        },
        (
            "schema", "invocation_id", "parent_job_id", "parent_step_id", "parent_attempt_id", "invoke_operation_key", "binding_id",
            "input_asset_id", "input_hash", "parameters_asset_id", "parameters_hash", "expected_result_contract", "required", "propagate_cancel",
        ),
    )


def provenance_receipt() -> dict[str, Any]:
    return obj(
        {
            "schema": const("provenance-receipt/v1"),
            "receipt_id": ID,
            "plugin_id": ID,
            "release_id": HASH,
            "package_hash": HASH,
            "capability_id": ID,
            "job_id": ID,
            "step_id": ID,
            "attempt_id": ID,
            "lease_epoch": POS_INT,
            "run_snapshot_hash": HASH,
            "bundle_id": nullable(ID),
            "bundle_hash": nullable(HASH),
            "parent_receipt_ids": array(ID, unique=True),
            "model_receipt_ids": array(ID, unique=True),
            "skill_chain_result_refs": array(skill_chain_ref()),
            "staged_items": array(ID, unique=True),
            "created_at": UTC,
            "receipt_hash": HASH,
        },
        (
            "schema", "receipt_id", "plugin_id", "release_id", "package_hash", "capability_id", "job_id", "step_id", "attempt_id",
            "lease_epoch", "run_snapshot_hash", "bundle_id", "bundle_hash", "parent_receipt_ids", "model_receipt_ids",
            "skill_chain_result_refs", "staged_items", "created_at", "receipt_hash",
        ),
    )


def event_schemas() -> dict[str, dict[str, Any]]:
    core_producer = obj({"producer_type": enum("core", "plugin", "user"), "producer_id": ID, "release_id": nullable(HASH)}, ("producer_type", "producer_id", "release_id"))
    core_event = obj(
        {
            "schema": const("core-event/v1"), "event_id": ID, "core_event_seq": POS_INT, "event_type": enum(*CORE_EVENT_TYPES),
            "aggregate_id": ID, "aggregate_revision": POS_INT, "workspace_id": nullable(ID), "occurred_at": UTC, "producer": core_producer,
            "causation_id": nullable(ID), "correlation_id": ID, "payload_asset_id": nullable(ID), "payload_hash": nullable(HASH),
        },
        ("schema", "event_id", "core_event_seq", "event_type", "aggregate_id", "aggregate_revision", "workspace_id", "occurred_at", "producer", "causation_id", "correlation_id", "payload_asset_id", "payload_hash"),
    )
    plugin_event = obj(
        {
            "schema": const("plugin-job-event/v1"), "event_id": ID, "job_id": ID, "step_id": ID, "attempt_id": ID,
            "job_event_seq": POS_INT, "event_type": {"type": "string", "pattern": r"^plugin\.[A-Za-z0-9._:/-]+\.[A-Za-z0-9._:/-]+$"},
            "plugin_id": ID, "release_id": HASH, "local_seq": POS_INT, "payload_asset_id": nullable(ID), "payload_hash": nullable(HASH), "occurred_at": UTC,
        },
        ("schema", "event_id", "job_id", "step_id", "attempt_id", "job_event_seq", "event_type", "plugin_id", "release_id", "local_seq", "payload_asset_id", "payload_hash", "occurred_at"),
    )
    job_page = obj(
        {"schema": const("job-event-page/v1"), "job_id": ID, "after_job_event_seq": NONNEG_INT, "events": array(plugin_event), "next_job_event_seq": NONNEG_INT, "high_water_seq": NONNEG_INT},
        ("schema", "job_id", "after_job_event_seq", "events", "next_job_event_seq", "high_water_seq"),
    )
    stream_binding = obj({"stream_id": ID, "step_id": ID, "output_role": ID, "target": target(), "acked_prefix_seq": NONNEG_INT, "acked_bytes": NONNEG_INT, "acked_prefix_hash": HASH}, ("stream_id", "step_id", "output_role", "target", "acked_prefix_seq", "acked_bytes", "acked_prefix_hash"))
    step = obj({"step_id": ID, "state": ID, "revision": POS_INT}, ("step_id", "state", "revision"))
    attempt = obj({"attempt_id": ID, "state": ID, "lease_epoch": POS_INT}, ("attempt_id", "state", "lease_epoch"))
    job_snapshot = obj(
        {
            "schema": const("job-snapshot/v1"), "job_id": ID, "workspace_id": ID,
            "job_state": enum("queued", "running", "waiting_user", "paused", "cancelling", "succeeded", "partial", "failed", "cancelled", "needs_attention"),
            "job_revision": POS_INT, "steps": array(step), "attempts": array(attempt), "candidate_ids": array(ID, unique=True), "current_checkpoint_id": nullable(ID),
            "stream_high_waters": array(stream_binding), "core_event_high_water": NONNEG_INT, "job_event_high_water": NONNEG_INT, "created_at": UTC, "snapshot_hash": HASH,
        },
        ("schema", "job_id", "workspace_id", "job_state", "job_revision", "steps", "attempts", "candidate_ids", "current_checkpoint_id", "stream_high_waters", "core_event_high_water", "job_event_high_water", "created_at", "snapshot_hash"),
    )
    aggregate = obj({"aggregate_type": ID, "aggregate_id": ID, "aggregate_revision": POS_INT, "state_asset_id": ID, "state_hash": HASH}, ("aggregate_type", "aggregate_id", "aggregate_revision", "state_asset_id", "state_hash"))
    core_snapshot = obj(
        {
            "schema": const("core-snapshot/v1"), "snapshot_id": ID,
            "subscription_scope": obj({"workspace_id": nullable(ID), "event_types": array(enum(*CORE_EVENT_TYPES), unique=True)}, ("workspace_id", "event_types")),
            "core_snapshot_revision": POS_INT, "core_event_high_water": NONNEG_INT, "coverage_complete": const(True), "covered_aggregates": array(aggregate), "created_at": UTC, "snapshot_hash": HASH,
        },
        ("schema", "snapshot_id", "subscription_scope", "core_snapshot_revision", "core_event_high_water", "coverage_complete", "covered_aggregates", "created_at", "snapshot_hash"),
    )
    sse = obj(
        {
            "schema": const("sse-recovery/v1"), "stream_kind": enum("core_event", "job_event"), "aggregate_id": nullable(ID), "requested_after_seq": NONNEG_INT,
            "replay_floor_seq": NONNEG_INT, "durable_high_water_seq": NONNEG_INT, "gap": BOOL, "snapshot_required": BOOL, "snapshot_schema": nullable(ID), "snapshot_revision": nullable(POS_INT), "snapshot_asset_id": nullable(ID), "snapshot_hash": nullable(HASH),
        },
        ("schema", "stream_kind", "aggregate_id", "requested_after_seq", "replay_floor_seq", "durable_high_water_seq", "gap", "snapshot_required", "snapshot_schema", "snapshot_revision", "snapshot_asset_id", "snapshot_hash"),
    )
    checkpoint = obj(
        {
            "schema": const("checkpoint/v1"), "checkpoint_id": ID, "checkpoint_seq": POS_INT, "job_id": ID, "step_id": ID, "source_attempt_id": ID,
            "lease_epoch": POS_INT, "run_snapshot_hash": HASH, "replay_policy": enum("idempotent_auto", "checkpoint_resume", "manual_if_unknown", "never_replay"),
            "completed_units": NONNEG_INT, "total_units": nullable(NONNEG_INT), "unit_set_hash": nullable(HASH), "state_asset_id": nullable(ID), "created_at": UTC, "checkpoint_hash": HASH,
        },
        ("schema", "checkpoint_id", "checkpoint_seq", "job_id", "step_id", "source_attempt_id", "lease_epoch", "run_snapshot_hash", "replay_policy", "completed_units", "total_units", "unit_set_hash", "state_asset_id", "created_at", "checkpoint_hash"),
    )
    stream_prefix = obj(
        {
            "schema": const("stream-prefix/v1"), "stream_id": ID, "job_id": ID, "step_id": ID, "output_role": ID, "target": target(), "attempt_id": ID,
            "lease_epoch": POS_INT, "prefix_seq": POS_INT, "prefix_asset_id": ID, "prefix_hash": HASH, "byte_length": NONNEG_INT, "encoding": const("utf-8"),
        },
        ("schema", "stream_id", "job_id", "step_id", "output_role", "target", "attempt_id", "lease_epoch", "prefix_seq", "prefix_asset_id", "prefix_hash", "byte_length", "encoding"),
    )
    return {
        "core-event-v1": core_event, "plugin-job-event-v1": plugin_event, "job-event-page-v1": job_page, "job-snapshot-v1": job_snapshot,
        "core-snapshot-v1": core_snapshot, "sse-recovery-v1": sse, "checkpoint-v1": checkpoint, "stream-prefix-v1": stream_prefix,
    }


def plan_schemas() -> dict[str, dict[str, Any]]:
    binding = obj(
        {"binding_id": ID, "capability_id": ID, "plugin_id": ID, "release_requirement": SEMVER, "order": POS_INT, "enabled": BOOL, "required": BOOL, "propagate_cancel": BOOL, "parameters_asset_id": nullable(ID)},
        ("binding_id", "capability_id", "plugin_id", "release_requirement", "order", "enabled", "required", "propagate_cancel", "parameters_asset_id"),
    )
    data_binding = obj(
        {"data_binding_id": ID, "data_plugin_id": ID, "release_requirement": SEMVER, "format_id": ID, "interpreter_binding_id": ID, "order": POS_INT, "enabled": BOOL, "parameters_asset_id": nullable(ID)},
        ("data_binding_id", "data_plugin_id", "release_requirement", "format_id", "interpreter_binding_id", "order", "enabled", "parameters_asset_id"),
    )
    synthesizer = nullable(obj({"binding_id": ID, "capability_id": ID, "plugin_id": ID, "release_requirement": SEMVER}, ("binding_id", "capability_id", "plugin_id", "release_requirement")))
    plan = obj(
        {"schema": const("plugin-plan/v1"), "plan_id": ID, "revision": POS_INT, "name": NONEMPTY, "description": STR, "bindings": array(binding), "data_bindings": array(data_binding), "skill_preset_revision_id": nullable(ID), "result_mode": enum("separate", "compare", "synthesize"), "synthesizer": synthesizer, "model_profile_revision_id": nullable(ID), "ui_defaults": array(obj({"slot": ID, "expanded": BOOL}, ("slot", "expanded")))},
        ("schema", "plan_id", "revision", "name", "description", "bindings", "data_bindings", "skill_preset_revision_id", "result_mode", "synthesizer", "model_profile_revision_id", "ui_defaults"),
    )
    member = obj({"plugin_id": ID, "release_id": HASH, "package_hash": HASH, "data_generation_id": nullable(ID), "ui_bundle_hash": nullable(HASH), "global_settings_revision_id": nullable(ID), "settings_schema_hash": nullable(HASH), "data_bundle_asset_id": nullable(ID)}, ("plugin_id", "release_id", "package_hash", "data_generation_id", "ui_bundle_hash", "global_settings_revision_id", "settings_schema_hash", "data_bundle_asset_id"))
    generation = obj({"schema": const("plugin-generation/v1"), "generation_id": ID, "core_api_version": SEMVER, "members": array(member), "created_reason": NONEMPTY, "created_at": UTC, "health_result_asset_id": ID, "parent_generation_id": nullable(ID), "base_generation_id": nullable(ID)}, ("schema", "generation_id", "core_api_version", "members", "created_reason", "created_at", "health_result_asset_id", "parent_generation_id", "base_generation_id"))
    settings_revision = obj({"schema": const("settings-revision/v1"), "settings_revision_id": ID, "plugin_id": ID, "plugin_release_id": HASH, "schema_hash": HASH, "payload_asset_id": ID, "payload_hash": HASH, "parent_revision_id": nullable(ID), "migrated_from_revision_id": nullable(ID), "migration_receipt_hash": nullable(HASH), "validation_receipt_id": nullable(ID), "validation_receipt_hash": nullable(HASH), "state": enum("draft", "validated", "invalid"), "created_at": UTC}, ("schema", "settings_revision_id", "plugin_id", "plugin_release_id", "schema_hash", "payload_asset_id", "payload_hash", "parent_revision_id", "migrated_from_revision_id", "migration_receipt_hash", "validation_receipt_id", "validation_receipt_hash", "state", "created_at"))
    settings_receipt = obj({"schema": const("settings-validation-receipt/v1"), "receipt_id": ID, "plugin_id": ID, "plugin_release_id": HASH, "settings_revision_id": ID, "schema_hash": HASH, "payload_hash": HASH, "valid": BOOL, "details_asset_id": nullable(ID), "created_at": UTC, "receipt_hash": HASH}, ("schema", "receipt_id", "plugin_id", "plugin_release_id", "settings_revision_id", "schema_hash", "payload_hash", "valid", "details_asset_id", "created_at", "receipt_hash"))
    migration_step = obj({"operation": enum("rename", "copy", "remove", "set_default"), "from_pointer": nullable(NONEMPTY), "to_pointer": nullable(NONEMPTY), "value_asset_id": nullable(ID)}, ("operation", "from_pointer", "to_pointer", "value_asset_id"))
    migration_manifest = obj({"schema": const("settings-migration-manifest/v1"), "from_schema_hash": HASH, "to_schema_hash": HASH, "steps": array(migration_step)}, ("schema", "from_schema_hash", "to_schema_hash", "steps"))
    lifecycle = obj({"schema": const("plugin-lifecycle-transition/v1"), "install_operation_id": ID, "base_generation_id": nullable(ID), "base_lkg_generation_id": nullable(ID), "target_generation_id": nullable(ID), "state": enum("selected", "staged", "package_published", "env_prepared", "shadow_prepared", "migrated", "settings_validated", "qualified", "pending_apply", "current_committed", "lkg_pending", "lkg_promoted", "failed", "superseded", "rollback_armed", "rolled_back", "safe_mode"), "package_store_status": enum("absent", "staged", "published", "orphan"), "shadow_data_generation_id": nullable(ID), "target_settings_revision_ids": array(obj({"plugin_id": ID, "settings_revision_id": ID}, ("plugin_id", "settings_revision_id"))), "qualification_id": nullable(ID), "rollback_attempt": enum(0, 1), "rollback_token": nullable(ID), "failure_code": nullable(NONEMPTY), "created_at": UTC, "updated_at": UTC}, ("schema", "install_operation_id", "base_generation_id", "base_lkg_generation_id", "target_generation_id", "state", "package_store_status", "shadow_data_generation_id", "target_settings_revision_ids", "qualification_id", "rollback_attempt", "rollback_token", "failure_code", "created_at", "updated_at"))
    retirement = obj({"schema": const("release-retirement/v1"), "release_id": HASH, "state": enum("installed", "retiring", "retired"), "retire_epoch": POS_INT, "package_present": BOOL, "started_at": nullable(UTC), "completed_at": nullable(UTC)}, ("schema", "release_id", "state", "retire_epoch", "package_present", "started_at", "completed_at"))
    pin = obj({"schema": const("release-pin/v1"), "pin_id": ID, "release_id": HASH, "retire_epoch": POS_INT, "pin_kind": enum("current_generation", "lkg_generation", "pending_generation", "plan_binding", "job", "attempt", "worker", "install", "backup"), "owner_id": ID, "created_at": UTC, "released_at": nullable(UTC)}, ("schema", "pin_id", "release_id", "retire_epoch", "pin_kind", "owner_id", "created_at", "released_at"))
    return {"plugin-plan-v1": plan, "plugin-generation-v1": generation, "settings-revision-v1": settings_revision, "settings-validation-receipt-v1": settings_receipt, "settings-migration-manifest-v1": migration_manifest, "plugin-lifecycle-transition-v1": lifecycle, "release-retirement-v1": retirement, "release-pin-v1": pin}


def core_api_schemas() -> dict[str, dict[str, Any]]:
    """First-publication Core HTTP and scoped post-M0 contract families.

    These families are additive: they do not change the frozen plugin RPC
    method matrix or expose a Publication callable to plugin workers.  The
    HTTP matrix below is consumed by P1/P4; the Export input is a Core-created
    immutable Asset consumed through the already-frozen host.asset.read/v1.
    """

    workspace = obj(
        {
            "schema": const("core-workspace/v1"),
            "workspace_id": ID,
            "workspace_kind": ID,
            "title": NONEMPTY,
            "status": ID,
            "current_plan_revision_id": nullable(ID),
            "created_at": UTC,
            "updated_at": UTC,
            "revision": NONNEG_INT,
        },
        ("schema", "workspace_id", "workspace_kind", "title", "status", "current_plan_revision_id", "created_at", "updated_at", "revision"),
    )
    document_value = obj(
        {
            "schema": const("core-document/v1"),
            "document_id": ID,
            "workspace_id": ID,
            "document_type": ID,
            "title": NONEMPTY,
            "current_revision_id": nullable(ID),
            "created_at": UTC,
            "updated_at": UTC,
            "revision": NONNEG_INT,
        },
        ("schema", "document_id", "workspace_id", "document_type", "title", "current_revision_id", "created_at", "updated_at", "revision"),
    )
    node = obj(
        {
            "schema": const("core-node/v1"),
            "node_id": ID,
            "workspace_id": ID,
            "document_id": nullable(ID),
            "node_type": ID,
            "title": NONEMPTY,
            "parent_node_id": nullable(ID),
            "position": NONNEG_INT,
            "current_revision_id": nullable(ID),
            "created_at": UTC,
            "updated_at": UTC,
            "revision": NONNEG_INT,
        },
        ("schema", "node_id", "workspace_id", "document_id", "node_type", "title", "parent_node_id", "position", "current_revision_id", "created_at", "updated_at", "revision"),
    )
    relation = obj(
        {
            "schema": const("core-relation/v1"),
            "relation_id": ID,
            "workspace_id": ID,
            "relation_type": ID,
            "source_id": ID,
            "target_id": ID,
            "revision_id": nullable(ID),
            "created_at": UTC,
        },
        ("schema", "relation_id", "workspace_id", "relation_type", "source_id", "target_id", "revision_id", "created_at"),
    )
    revision = obj(
        {
            "schema": const("core-revision/v1"),
            "revision_id": ID,
            "workspace_id": ID,
            "document_id": nullable(ID),
            "node_id": nullable(ID),
            "parent_revision_id": nullable(ID),
            "content_hash": HASH,
            "created_by": ID,
            "source_candidate_id": nullable(ID),
            "created_at": UTC,
            "revision_number": POS_INT,
            "payload_schema": nullable(ID),
        },
        ("schema", "revision_id", "workspace_id", "document_id", "node_id", "parent_revision_id", "content_hash", "created_by", "source_candidate_id", "created_at", "revision_number", "payload_schema"),
    )

    def page(schema_name: str, item_ref: str) -> dict[str, Any]:
        return obj(
            {
                "schema": const(schema_name),
                "items": array({"$ref": f"#/$defs/{item_ref}"}),
                "offset": NONNEG_INT,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "total": NONNEG_INT,
                "next_offset": nullable(NONNEG_INT),
            },
            ("schema", "items", "offset", "limit", "total", "next_offset"),
        )

    workspace_query = obj({"schema": const("core-workspace-query/v1"), "workspace_id": nullable(ID), "offset": NONNEG_INT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("schema", "workspace_id", "offset", "limit"))
    workspace_get_query = obj({"schema": const("core-workspace-get-query/v1"), "workspace_id": ID}, ("schema", "workspace_id"))
    document_query = obj({"schema": const("core-document-query/v1"), "workspace_id": ID, "document_id": nullable(ID), "offset": NONNEG_INT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("schema", "workspace_id", "document_id", "offset", "limit"))
    document_get_query = obj({"schema": const("core-document-get-query/v1"), "workspace_id": ID, "document_id": ID}, ("schema", "workspace_id", "document_id"))
    node_query = obj({"schema": const("core-node-query/v1"), "workspace_id": ID, "node_id": nullable(ID), "document_id": nullable(ID), "parent_node_id": nullable(ID), "offset": NONNEG_INT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("schema", "workspace_id", "node_id", "document_id", "parent_node_id", "offset", "limit"))
    node_get_query = obj({"schema": const("core-node-get-query/v1"), "workspace_id": ID, "node_id": ID}, ("schema", "workspace_id", "node_id"))
    relation_query = obj({"schema": const("core-relation-query/v1"), "workspace_id": ID, "relation_id": nullable(ID), "source_id": nullable(ID), "target_id": nullable(ID), "relation_type": nullable(ID), "offset": NONNEG_INT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("schema", "workspace_id", "relation_id", "source_id", "target_id", "relation_type", "offset", "limit"))
    document_revision_query = obj({"schema": const("core-document-revision-query/v1"), "workspace_id": ID, "document_id": ID, "revision_id": nullable(ID), "offset": NONNEG_INT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("schema", "workspace_id", "document_id", "revision_id", "offset", "limit"))
    node_revision_query = obj({"schema": const("core-node-revision-query/v1"), "workspace_id": ID, "node_id": ID, "revision_id": nullable(ID), "offset": NONNEG_INT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("schema", "workspace_id", "node_id", "revision_id", "offset", "limit"))
    revision_get_query = obj({"schema": const("core-revision-get-query/v1"), "workspace_id": ID, "revision_id": ID}, ("schema", "workspace_id", "revision_id"))
    content_query = obj({"schema": const("core-revision-content-query/v1"), "workspace_id": ID, "revision_id": ID, "offset": NONNEG_INT, "length": {"type": "integer", "minimum": 0, "maximum": 65536}}, ("schema", "workspace_id", "revision_id", "offset", "length"))
    content_page = obj({"schema": const("core-revision-content-page/v1"), "revision_id": ID, "offset": NONNEG_INT, "length": NONNEG_INT, "total_length": NONNEG_INT, "text": STR, "next_offset": nullable(NONNEG_INT)}, ("schema", "revision_id", "offset", "length", "total_length", "text", "next_offset"))

    commands = {
        "workspace_create": obj({"schema": const("core-workspace-create-command/v1"), "operation_key": ID, "workspace_id": ID, "workspace_kind": ID, "title": NONEMPTY}, ("schema", "operation_key", "workspace_id", "workspace_kind", "title")),
        "workspace_update": obj({"schema": const("core-workspace-update-command/v1"), "operation_key": ID, "workspace_id": ID, "expected_revision": NONNEG_INT, "title": nullable(NONEMPTY), "status": nullable(ID)}, ("schema", "operation_key", "workspace_id", "expected_revision", "title", "status")),
        "workspace_delete": obj({"schema": const("core-workspace-delete-command/v1"), "operation_key": ID, "workspace_id": ID, "expected_revision": NONNEG_INT}, ("schema", "operation_key", "workspace_id", "expected_revision")),
        "document_create": obj({"schema": const("core-document-create-command/v1"), "operation_key": ID, "document_id": ID, "workspace_id": ID, "document_type": ID, "title": NONEMPTY}, ("schema", "operation_key", "document_id", "workspace_id", "document_type", "title")),
        "document_update": obj({"schema": const("core-document-update-command/v1"), "operation_key": ID, "document_id": ID, "workspace_id": ID, "expected_revision": NONNEG_INT, "title": NONEMPTY}, ("schema", "operation_key", "document_id", "workspace_id", "expected_revision", "title")),
        "node_create": obj({"schema": const("core-node-create-command/v1"), "operation_key": ID, "node_id": ID, "workspace_id": ID, "document_id": nullable(ID), "node_type": ID, "title": NONEMPTY, "parent_node_id": nullable(ID), "position": NONNEG_INT}, ("schema", "operation_key", "node_id", "workspace_id", "document_id", "node_type", "title", "parent_node_id", "position")),
        "node_update": obj({"schema": const("core-node-update-command/v1"), "operation_key": ID, "node_id": ID, "workspace_id": ID, "expected_revision": NONNEG_INT, "title": NONEMPTY, "parent_node_id": nullable(ID), "position": NONNEG_INT}, ("schema", "operation_key", "node_id", "workspace_id", "expected_revision", "title", "parent_node_id", "position")),
        "node_delete": obj({"schema": const("core-node-delete-command/v1"), "operation_key": ID, "node_id": ID, "workspace_id": ID, "expected_revision": NONNEG_INT}, ("schema", "operation_key", "node_id", "workspace_id", "expected_revision")),
        "relation_create": obj({"schema": const("core-relation-create-command/v1"), "operation_key": ID, "relation_id": ID, "workspace_id": ID, "relation_type": ID, "source_id": ID, "target_id": ID, "revision_id": nullable(ID)}, ("schema", "operation_key", "relation_id", "workspace_id", "relation_type", "source_id", "target_id", "revision_id")),
        "relation_delete": obj({"schema": const("core-relation-delete-command/v1"), "operation_key": ID, "relation_id": ID, "workspace_id": ID, "expected_revision_id": nullable(ID)}, ("schema", "operation_key", "relation_id", "workspace_id", "expected_revision_id")),
        "document_revision_create": obj({"schema": const("core-document-revision-create-command/v1"), "operation_key": ID, "revision_id": ID, "workspace_id": ID, "document_id": ID, "base_revision_id": nullable(ID), "content": {"type": "string", "maxLength": 8388608}, "created_by": ID, "source_candidate_id": nullable(ID), "payload_schema": nullable(ID)}, ("schema", "operation_key", "revision_id", "workspace_id", "document_id", "base_revision_id", "content", "created_by", "source_candidate_id", "payload_schema")),
        "node_revision_create": obj({"schema": const("core-node-revision-create-command/v1"), "operation_key": ID, "revision_id": ID, "workspace_id": ID, "node_id": ID, "base_revision_id": nullable(ID), "content": {"type": "string", "maxLength": 8388608}, "created_by": ID, "source_candidate_id": nullable(ID), "payload_schema": nullable(ID)}, ("schema", "operation_key", "revision_id", "workspace_id", "node_id", "base_revision_id", "content", "created_by", "source_candidate_id", "payload_schema")),
    }
    delete_result = obj({"schema": const("core-delete-result/v1"), "operation_key": ID, "workspace_id": ID, "entity_kind": enum("workspace", "node", "relation"), "entity_id": ID, "previous_revision": nullable(NONNEG_INT), "deleted": const(True), "idempotent": BOOL}, ("schema", "operation_key", "workspace_id", "entity_kind", "entity_id", "previous_revision", "deleted", "idempotent"))

    defs: dict[str, Any] = {
        "workspace": workspace,
        "document": document_value,
        "node": node,
        "relation": relation,
        "revision": revision,
        "workspace_page": page("core-workspace-page/v1", "workspace"),
        "document_page": page("core-document-page/v1", "document"),
        "node_page": page("core-node-page/v1", "node"),
        "relation_page": page("core-relation-page/v1", "relation"),
        "revision_page": page("core-revision-page/v1", "revision"),
        "workspace_query": workspace_query,
        "workspace_get_query": workspace_get_query,
        "document_query": document_query,
        "document_get_query": document_get_query,
        "node_query": node_query,
        "node_get_query": node_get_query,
        "relation_query": relation_query,
        "document_revision_query": document_revision_query,
        "node_revision_query": node_revision_query,
        "revision_get_query": revision_get_query,
        "content_query": content_query,
        "content_page": content_page,
        **commands,
        "delete_result": delete_result,
    }
    core_http_error = obj(
        {
            "schema": const("core-http-error/v1"),
            "error_code": enum("unknown_reference", "cross_workspace", "stale_cas", "operation_key_reuse", "incomplete_publication"),
            "message": NONEMPTY,
            "retryable": BOOL,
        },
        ("schema", "error_code", "message", "retryable"),
    )
    defs["http_error"] = core_http_error
    authority = {"oneOf": [{"$ref": f"#/$defs/{name}"} for name in defs], "$defs": defs, "unevaluatedProperties": False}

    revision_ref = obj({"revision_id": ID, "workspace_id": ID, "entity_kind": enum("document", "node_structure", "relation_set"), "entity_id": ID, "content_hash": HASH, "revision_number": POS_INT}, ("revision_id", "workspace_id", "entity_kind", "entity_id", "content_hash", "revision_number"))
    publication_command = obj({"schema": const("publication-command/v1"), "publication_operation_key": ID, "workspace_id": ID, "candidate_id": ID, "accepted_by": ID}, ("schema", "publication_operation_key", "workspace_id", "candidate_id", "accepted_by"))
    publication_result = obj({"schema": const("publication-result/v1"), "publication_id": ID, "candidate_id": ID, "workspace_id": ID, "entity_kind": enum("document", "node_structure", "relation_set"), "entity_id": ID, "resulting_revision": revision_ref, "idempotent": BOOL}, ("schema", "publication_id", "candidate_id", "workspace_id", "entity_kind", "entity_id", "resulting_revision", "idempotent"))
    publication = {"oneOf": [publication_command, publication_result], "unevaluatedProperties": False}

    mime = {"type": "string", "pattern": r"^[^\s/]+/[^\s/]+$"}
    asset_query = obj({"schema": const("asset-query/v1"), "asset_id": ID}, ("schema", "asset_id"))
    asset_range_query = obj({"schema": const("asset-read-range-query/v1"), "asset_id": ID, "offset": NONNEG_INT, "length": {"type": "integer", "minimum": 0, "maximum": 1048576}}, ("schema", "asset_id", "offset", "length"))
    asset_metadata = obj({"schema": const("asset-metadata/v1"), "asset_id": ID, "sha256": HASH, "mime": mime, "size": NONNEG_INT, "logical_role": ID, "provenance": NONEMPTY, "rebuildable": BOOL}, ("schema", "asset_id", "sha256", "mime", "size", "logical_role", "provenance", "rebuildable"))
    asset_range = obj({"schema": const("asset-read-range/v1"), "asset_id": ID, "offset": NONNEG_INT, "length": {"type": "integer", "minimum": 0, "maximum": 1048576}, "total_size": NONNEG_INT, "base64_chunk": {"type": "string", "pattern": r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"}, "next_offset": nullable(NONNEG_INT), "content_hash": HASH}, ("schema", "asset_id", "offset", "length", "total_size", "base64_chunk", "next_offset", "content_hash"))
    asset = {"oneOf": [asset_query, asset_range_query, asset_metadata, asset_range], "unevaluatedProperties": False}

    context_common = {"schema": const("operation-context-identity/v1"), "protocol_version": const("1"), "generation_id": ID, "plugin_release_id": HASH}
    context_identity = {
        "oneOf": [
            obj({**context_common, "context": const("control")}, ("schema", "protocol_version", "context", "generation_id", "plugin_release_id")),
            obj({**context_common, "context": const("install"), "install_operation_id": ID}, ("schema", "protocol_version", "context", "generation_id", "plugin_release_id", "install_operation_id")),
            obj({**context_common, "context": const("attempt"), "job_id": ID, "step_id": ID, "attempt_id": ID}, ("schema", "protocol_version", "context", "generation_id", "plugin_release_id", "job_id", "step_id", "attempt_id")),
        ],
        "unevaluatedProperties": False,
    }

    export_item = obj({"ordinal": NONNEG_INT, "document_id": ID, "document_type": ID, "title": NONEMPTY, "revision_id": ID, "content_asset_id": ID, "content_hash": HASH, "mime": const("text/plain"), "encoding": const("utf-8")}, ("ordinal", "document_id", "document_type", "title", "revision_id", "content_asset_id", "content_hash", "mime", "encoding"))
    ordered = array(export_item, min_items=1)
    ordered["maxItems"] = 10000
    export_current = obj({"schema": const("export-current-revisions/v1"), "workspace_id": ID, "core_snapshot_revision": NONNEG_INT, "ordered_revisions": ordered, "generated_at": UTC}, ("schema", "workspace_id", "core_snapshot_revision", "ordered_revisions", "generated_at"))

    return {
        "core-authority-command-query-v1": authority,
        "publication-command-result-v1": publication,
        "asset-metadata-v1": asset,
        "operation-context-identity-v1": context_identity,
        "export-current-revisions-v1": export_current,
    }


def core_http_request_failure_schemas() -> dict[str, dict[str, Any]]:
    """Additive pre-domain request-failure contracts from ADR-043.

    These schemas intentionally remain separate from ``core-http-error/v1``
    and the frozen Core API method matrix.  They describe failures raised by
    P0 composition before a valid typed request can reach P1 authority.
    """

    error_code = enum(
        "malformed_json",
        "invalid_request",
        "invalid_query",
        "range_out_of_bounds",
    )
    request_error = obj(
        {
            "schema": const("core-http-request-error/v1"),
            "error_code": error_code,
            "message": NONEMPTY,
            "retryable": const(False),
        },
        ("schema", "error_code", "message", "retryable"),
    )
    exact_bindings = [
        {"source": "json_decode", "error_code": "malformed_json", "scope": "all_core_routes"},
        {"source": "closed_request_schema", "error_code": "invalid_request", "scope": "all_core_routes"},
        {"source": "query_decode", "error_code": "invalid_query", "scope": "all_core_routes"},
        {"source": "asset_range_bounds", "error_code": "range_out_of_bounds", "scope": "asset.range"},
    ]
    policy = obj(
        {
            "schema": const("core-http-request-failure-policy/v1"),
            "status": const(400),
            "error_schema": const("core-http-request-error/v1"),
            "retryable": const(False),
            # The policy is data, not a generic tuple registry.  A deep const
            # makes Draft 2020-12 enforce the same exact tuples and order as
            # the specialized Python and TypeScript parsers.
            "bindings": const(exact_bindings),
        },
        ("schema", "status", "error_schema", "retryable", "bindings"),
    )
    return {
        "core-http-request-error-v1": request_error,
        "core-http-request-failure-policy-v1": policy,
    }


def core_api_method_matrix() -> dict[str, Any]:
    """The minimal P1/P4 Core HTTP adapter surface from ADR-041."""

    def route(
        route_id: str,
        method: str,
        path: str,
        request: str,
        result: str,
        statuses: tuple[int, ...] = (200,),
        *,
        path_identity: tuple[str, ...] = (),
        path_entity_kind: str | None = None,
        result_entity_kind: str | None = None,
        failures: tuple[tuple[int, tuple[str, ...]], ...] = ((404, ("unknown_reference",)),),
    ) -> dict[str, Any]:
        return {
            "route_id": route_id,
            "method": method,
            "path_template": path,
            "path_identity": list(path_identity),
            "path_entity_kind": path_entity_kind,
            "result_entity_kind": result_entity_kind,
            "request_schema": request,
            "result_schema": result,
            "success_statuses": list(statuses),
            "error_schema": "core-http-error/v1",
            "failure_statuses": [
                {"status": status, "error_codes": list(codes)} for status, codes in failures
            ],
        }

    query_failures = ((404, ("unknown_reference", "cross_workspace")),)
    mutation_failures = (
        (404, ("unknown_reference", "cross_workspace")),
        (409, ("stale_cas", "operation_key_reuse")),
    )
    publication_failures = (
        (404, ("unknown_reference", "cross_workspace")),
        (409, ("stale_cas", "operation_key_reuse")),
        (422, ("incomplete_publication",)),
    )

    return {
        "schema": "core-api-method-matrix/v1",
        "core_api_compatibility": ">=1.0 <2.0",
        "plugin_rpc_unchanged": True,
        "plugin_ui_bundle_direct_http": False,
        "routes": [
            route("workspace.list", "GET", "/api/v1/core/workspaces", "core-workspace-query/v1", "core-workspace-page/v1", result_entity_kind="workspace", failures=query_failures),
            route("workspace.get", "GET", "/api/v1/core/workspaces/{workspace_id}", "core-workspace-get-query/v1", "core-workspace/v1", path_identity=("workspace_id",), path_entity_kind="workspace", result_entity_kind="workspace", failures=query_failures),
            route("workspace.create", "POST", "/api/v1/core/workspaces", "core-workspace-create-command/v1", "core-workspace/v1", (201,), result_entity_kind="workspace", failures=mutation_failures),
            route("workspace.update", "PATCH", "/api/v1/core/workspaces/{workspace_id}", "core-workspace-update-command/v1", "core-workspace/v1", path_identity=("workspace_id",), path_entity_kind="workspace", result_entity_kind="workspace", failures=mutation_failures),
            route("workspace.delete", "DELETE", "/api/v1/core/workspaces/{workspace_id}", "core-workspace-delete-command/v1", "core-delete-result/v1", path_identity=("workspace_id",), path_entity_kind="workspace", result_entity_kind="workspace", failures=mutation_failures),
            route("document.list", "GET", "/api/v1/core/workspaces/{workspace_id}/documents", "core-document-query/v1", "core-document-page/v1", path_identity=("workspace_id",), path_entity_kind="workspace", result_entity_kind="document", failures=query_failures),
            route("document.get", "GET", "/api/v1/core/documents/{document_id}", "core-document-get-query/v1", "core-document/v1", path_identity=("document_id",), path_entity_kind="document", result_entity_kind="document", failures=query_failures),
            route("document.create", "POST", "/api/v1/core/workspaces/{workspace_id}/documents", "core-document-create-command/v1", "core-document/v1", (201,), path_identity=("workspace_id",), path_entity_kind="workspace", result_entity_kind="document", failures=mutation_failures),
            route("document.update", "PATCH", "/api/v1/core/documents/{document_id}", "core-document-update-command/v1", "core-document/v1", path_identity=("document_id",), path_entity_kind="document", result_entity_kind="document", failures=mutation_failures),
            route("node.list", "GET", "/api/v1/core/workspaces/{workspace_id}/nodes", "core-node-query/v1", "core-node-page/v1", path_identity=("workspace_id",), path_entity_kind="workspace", result_entity_kind="node", failures=query_failures),
            route("node.get", "GET", "/api/v1/core/nodes/{node_id}", "core-node-get-query/v1", "core-node/v1", path_identity=("node_id",), path_entity_kind="node", result_entity_kind="node", failures=query_failures),
            route("node.create", "POST", "/api/v1/core/workspaces/{workspace_id}/nodes", "core-node-create-command/v1", "core-node/v1", (201,), path_identity=("workspace_id",), path_entity_kind="workspace", result_entity_kind="node", failures=mutation_failures),
            route("node.update", "PATCH", "/api/v1/core/nodes/{node_id}", "core-node-update-command/v1", "core-node/v1", path_identity=("node_id",), path_entity_kind="node", result_entity_kind="node", failures=mutation_failures),
            route("node.delete", "DELETE", "/api/v1/core/nodes/{node_id}", "core-node-delete-command/v1", "core-delete-result/v1", path_identity=("node_id",), path_entity_kind="node", result_entity_kind="node", failures=mutation_failures),
            route("relation.list", "GET", "/api/v1/core/workspaces/{workspace_id}/relations", "core-relation-query/v1", "core-relation-page/v1", path_identity=("workspace_id",), path_entity_kind="workspace", result_entity_kind="relation", failures=query_failures),
            route("relation.create", "POST", "/api/v1/core/workspaces/{workspace_id}/relations", "core-relation-create-command/v1", "core-relation/v1", (201,), path_identity=("workspace_id",), path_entity_kind="workspace", result_entity_kind="relation", failures=mutation_failures),
            route("relation.delete", "DELETE", "/api/v1/core/relations/{relation_id}", "core-relation-delete-command/v1", "core-delete-result/v1", path_identity=("relation_id",), path_entity_kind="relation", result_entity_kind="relation", failures=mutation_failures),
            route("document.revision.list", "GET", "/api/v1/core/documents/{document_id}/revisions", "core-document-revision-query/v1", "core-revision-page/v1", path_identity=("document_id",), path_entity_kind="document", result_entity_kind="revision", failures=query_failures),
            route("node.revision.list", "GET", "/api/v1/core/nodes/{node_id}/revisions", "core-node-revision-query/v1", "core-revision-page/v1", path_identity=("node_id",), path_entity_kind="node", result_entity_kind="revision", failures=query_failures),
            route("revision.get", "GET", "/api/v1/core/revisions/{revision_id}", "core-revision-get-query/v1", "core-revision/v1", path_identity=("revision_id",), path_entity_kind="revision", result_entity_kind="revision", failures=query_failures),
            route("revision.content", "GET", "/api/v1/core/revisions/{revision_id}/content", "core-revision-content-query/v1", "core-revision-content-page/v1", path_identity=("revision_id",), path_entity_kind="revision", result_entity_kind="revision", failures=query_failures),
            route("document.revision.create", "POST", "/api/v1/core/documents/{document_id}/revisions", "core-document-revision-create-command/v1", "core-revision/v1", (201,), path_identity=("document_id",), path_entity_kind="document", result_entity_kind="revision", failures=mutation_failures),
            route("node.revision.create", "POST", "/api/v1/core/nodes/{node_id}/revisions", "core-node-revision-create-command/v1", "core-revision/v1", (201,), path_identity=("node_id",), path_entity_kind="node", result_entity_kind="revision", failures=mutation_failures),
            route("publication.accept", "POST", "/api/v1/core/publications/accept", "publication-command/v1", "publication-result/v1", failures=publication_failures),
            route("asset.metadata", "GET", "/api/v1/core/assets/{asset_id}", "asset-query/v1", "asset-metadata/v1", path_identity=("asset_id",), path_entity_kind="asset", result_entity_kind="asset", failures=query_failures),
            route("asset.range", "GET", "/api/v1/core/assets/{asset_id}/range", "asset-read-range-query/v1", "asset-read-range/v1", path_identity=("asset_id",), path_entity_kind="asset", result_entity_kind="asset", failures=query_failures),
        ],
    }


def ui_schemas() -> dict[str, dict[str, Any]]:
    freshness = obj({"generation_id": ID, "plugin_release_id": HASH, "workspace_id": nullable(ID), "workspace_revision_id": nullable(ID), "plan_revision_id": nullable(ID)}, ("generation_id", "plugin_release_id", "workspace_id", "workspace_revision_id", "plan_revision_id"))
    init = obj({"schema": const("plugin-ui-init/v1"), "ui_session_id": ID, "initial_render_seq": NONNEG_INT, "initial_intent_seq": NONNEG_INT, "contribution_config_asset_id": nullable(ID)}, ("schema", "ui_session_id", "initial_render_seq", "initial_intent_seq", "contribution_config_asset_id"))
    event = obj({"schema": const("plugin-ui-event/v1"), "event_id": ID, "event_seq": POS_INT, "render_seq": POS_INT, "action_id": ID, "event_type": enum("change", "click", "select_row", "change_tab", "select_node", "open_core_operation"), "payload_asset_id": nullable(ID), "freshness": freshness}, ("schema", "event_id", "event_seq", "render_seq", "action_id", "event_type", "payload_asset_id", "freshness"))
    dispose = obj({"schema": const("plugin-ui-dispose/v1"), "ui_session_id": ID, "reason": enum("normal_shutdown", "slot_unmounted", "generation_changed", "workspace_changed", "watchdog"), "deadline_at": UTC}, ("schema", "ui_session_id", "reason", "deadline_at"))
    ack = obj({"schema": const("plugin-ui-ack/v1"), "intent_id": ID, "accepted": BOOL, "error_code": nullable(NONEMPTY), "core_event_seq": nullable(NONNEG_INT), "job_id": nullable(ID)}, ("schema", "intent_id", "accepted", "error_code", "core_event_seq", "job_id"))
    error = obj({"schema": const("plugin-ui-error/v1"), "code": NONEMPTY, "message": STR, "details_asset_id": nullable(ID), "retryable": BOOL}, ("schema", "code", "message", "details_asset_id", "retryable"))
    props_by_component = {
        "stack": obj({"direction": enum("horizontal", "vertical"), "row_gap": NONNEG_INT}, ("direction", "row_gap")),
        "text": obj({"text": STR, "tone": NONEMPTY}, ("text", "tone")),
        "input": obj({"label": NONEMPTY, "value": STR, "placeholder": STR, "disabled": BOOL}, ("label", "value", "placeholder", "disabled")),
        "textarea": obj({"label": NONEMPTY, "value": STR, "rows": POS_INT, "disabled": BOOL}, ("label", "value", "rows", "disabled")),
        "select": obj({"label": NONEMPTY, "value": STR, "options_asset_id": ID, "disabled": BOOL}, ("label", "value", "options_asset_id", "disabled")),
        "button": obj({"label": NONEMPTY, "tone": NONEMPTY, "disabled": BOOL}, ("label", "tone", "disabled")),
        "table": obj({"columns_asset_id": ID, "rows_asset_id": ID, "empty_text": STR}, ("columns_asset_id", "rows_asset_id", "empty_text")),
        "tabs": obj({"active_tab": ID, "tabs_asset_id": ID}, ("active_tab", "tabs_asset_id")),
        "diff": obj({"before_asset_id": ID, "after_asset_id": ID, "language": NONEMPTY}, ("before_asset_id", "after_asset_id", "language")),
        "tree": obj({"nodes_asset_id": ID, "selected_id": nullable(ID)}, ("nodes_asset_id", "selected_id")),
        "graph": obj({"graph_asset_id": ID, "layout": NONEMPTY}, ("graph_asset_id", "layout")),
        "progress": obj({"label": NONEMPTY, "completed": NONNEG_INT, "total": nullable(NONNEG_INT), "state": NONEMPTY}, ("label", "completed", "total", "state")),
        "candidate_preview": obj({"candidate_id": ID, "view_mode": NONEMPTY}, ("candidate_id", "view_mode")),
    }
    node_defs: dict[str, Any] = {}
    branches = []
    events = {"stack": (), "text": (), "input": ("change",), "textarea": ("change",), "select": ("change",), "button": ("click",), "table": ("select_row",), "tabs": ("change_tab",), "diff": (), "tree": ("select_node",), "graph": ("select_node",), "progress": (), "candidate_preview": ("open_core_operation",)}
    for component, props in props_by_component.items():
        branch_props = {"component": const(component), "key": ID, "props": props, "children": array({"$ref": "#/$defs/node"}), "event_ids": array(ID, unique=True)}
        branches.append(obj(branch_props, ("component", "key", "props", "children", "event_ids")))
    node_defs["node"] = {"oneOf": branches}
    tree = obj({"schema": const("plugin-ui-tree/v1"), "tree_id": ID, "render_seq": POS_INT, "root": {"$ref": "#/$defs/node"}}, ("schema", "tree_id", "render_seq", "root"))
    tree["$defs"] = node_defs
    intent = obj({"schema": const("plugin-ui-intent/v1"), "intent_id": ID, "intent_seq": POS_INT, "render_seq": POS_INT, "action_id": ID, "event_type": enum("change", "click", "select_row", "change_tab", "select_node", "open_core_operation"), "intent_kind": enum("invoke_capability", "open_core_operation", "request_candidate_preview", "request_job_cancel", "set_view_state"), "capability_id": nullable(ID), "payload_asset_id": nullable(ID), "operation_key": ID, "freshness": freshness}, ("schema", "intent_id", "intent_seq", "render_seq", "action_id", "event_type", "intent_kind", "capability_id", "payload_asset_id", "operation_key", "freshness"))
    common = {"schema": const("plugin-ui-message/v1"), "message_id": ID, "direction": enum("host_to_worker", "worker_to_host"), "message_seq": POS_INT, "message_type": enum("init", "render", "intent", "ack", "error", "dispose"), "worker_instance_id": ID, "plugin_release_id": HASH, "generation_id": ID, "contribution_id": ID, "slot": ID, "workspace_id": nullable(ID), "workspace_revision_id": nullable(ID), "plan_revision_id": nullable(ID)}
    body_branches = [
        ("host_to_worker", "init", init), ("host_to_worker", "intent", event), ("host_to_worker", "ack", ack), ("host_to_worker", "error", error), ("host_to_worker", "dispose", dispose),
        ("worker_to_host", "render", tree), ("worker_to_host", "intent", intent), ("worker_to_host", "error", error),
    ]
    message_branches = []
    for direction, message_type, body in body_branches:
        properties = copy.deepcopy(common)
        properties.update({"direction": const(direction), "message_type": const(message_type), "body": body})
        message_branches.append(obj(properties, tuple(properties)))
    message = {"oneOf": message_branches, "unevaluatedProperties": False}
    # ``tree`` is embedded below ``plugin-ui-message-v1`` for the render
    # branch.  JSON-Schema references in an embedded schema are resolved from
    # the enclosing resource, so the tree's ``#/$defs/node`` must also be
    # available on the message root.  Keep the standalone tree definition as
    # well, because it is independently published and verified.
    message["$defs"] = copy.deepcopy(node_defs)
    return {"plugin-ui-message-v1": message, "plugin-ui-init-v1": init, "plugin-ui-event-v1": event, "plugin-ui-dispose-v1": dispose, "plugin-ui-tree-v1": tree, "plugin-ui-intent-v1": intent, "plugin-ui-ack-v1": ack, "plugin-ui-error-v1": error}


def skill_schemas() -> dict[str, dict[str, Any]]:
    skill_manifest = obj({"schema": const("plotpilot-skill/v1"), "skill_id": ID, "version": SEMVER, "display_name": NONEMPTY, "stage": NONEMPTY, "actions": array(ID, min_items=1, unique=True)}, ("schema", "skill_id", "version", "display_name", "stage", "actions"))
    chain_ref = skill_chain_ref()
    patch = obj({"patch_id": ID, "start_codepoint": NONNEG_INT, "end_codepoint": NONNEG_INT, "replacement_asset_id": ID, "replacement_hash": HASH, "before_hash": HASH, "after_hash": HASH, "verified": BOOL}, ("patch_id", "start_codepoint", "end_codepoint", "replacement_asset_id", "replacement_hash", "before_hash", "after_hash", "verified"))
    receipt = obj({"schema": const("skill-run-receipt/v1"), "receipt_id": ID, "chain_id": ID, "chain_index": NONNEG_INT, "run_snapshot_hash": HASH, "result_bundle_id": nullable(ID), "result_item_id": nullable(ID), "stream_id": nullable(ID), "acked_prefix_hash": nullable(HASH), "skill_id": ID, "release_id": HASH, "package_hash": HASH, "parameters_asset_id": nullable(ID), "input_asset_id": ID, "input_hash": HASH, "output_asset_id": nullable(ID), "output_hash": nullable(HASH), "step_state": enum("executed", "failed", "skipped"), "frozen": BOOL, "participated": BOOL, "model_claimed": BOOL, "verified_patch": BOOL, "claim_evidence_asset_id": nullable(ID), "patches": array(patch), "warnings": array(diagnostic()), "previous_receipt_hash": nullable(HASH), "receipt_hash": HASH}, ("schema", "receipt_id", "chain_id", "chain_index", "run_snapshot_hash", "result_bundle_id", "result_item_id", "stream_id", "acked_prefix_hash", "skill_id", "release_id", "package_hash", "parameters_asset_id", "input_asset_id", "input_hash", "output_asset_id", "output_hash", "step_state", "frozen", "participated", "model_claimed", "verified_patch", "claim_evidence_asset_id", "patches", "warnings", "previous_receipt_hash", "receipt_hash"))
    chain = obj({"schema": const("skill-chain-result/v1"), "chain_id": ID, "run_snapshot_hash": HASH, "result_bundle_id": nullable(ID), "result_item_id": nullable(ID), "stream_id": nullable(ID), "acked_prefix_hash": nullable(HASH), "receipt_ids": array(ID, min_items=1), "receipt_hashes": array(HASH, min_items=1), "chain_status": enum("succeeded", "partial", "failed", "cancelled"), "input_hash": HASH, "final_output_asset_id": nullable(ID), "final_output_hash": nullable(HASH), "chain_hash": HASH}, ("schema", "chain_id", "run_snapshot_hash", "result_bundle_id", "result_item_id", "stream_id", "acked_prefix_hash", "receipt_ids", "receipt_hashes", "chain_status", "input_hash", "final_output_asset_id", "final_output_hash", "chain_hash"))
    return {"skill-manifest-v1": skill_manifest, "skill-chain-ref-v1": chain_ref, "skill-run-receipt-v1": receipt, "skill-chain-result-v1": chain}


def backup_schemas() -> dict[str, dict[str, Any]]:
    file_entry = obj({"path": PATH, "size": NONNEG_INT, "sha256": HASH, "role": enum("core_db", "plugin_db", "asset", "package", "metadata")}, ("path", "size", "sha256", "role"))
    release = obj({"plugin_id": ID, "release_id": HASH, "package_hash": HASH, "package_present": BOOL}, ("plugin_id", "release_id", "package_hash", "package_present"))
    verification = obj({"databases_valid": BOOL, "assets_valid": BOOL, "files_valid": BOOL, "compatible": BOOL, "verified_at": UTC}, ("databases_valid", "assets_valid", "files_valid", "compatible", "verified_at"))
    backup = obj({"schema": const("backup-bundle/v1"), "backup_id": ID, "library_root_id": ID, "backup_epoch": POS_INT, "mode": enum("full", "data", "workspace"), "workspace_ids": array(ID, unique=True), "core_contract_version": SEMVER, "core_snapshot_hash": HASH, "workspace_snapshot_hash": nullable(HASH), "current_generation_id": nullable(ID), "lkg_generation_id": nullable(ID), "asset_closure_root": HASH, "plugin_releases": array(release), "projection_rebuild_required": array(obj({"plugin_id": ID, "release_id": HASH, "reason": const("plugin_projection_rebuild_required")}, ("plugin_id", "release_id", "reason"))), "files": array(file_entry, unique=True), "created_at": UTC, "verification": verification, "bundle_hash": HASH}, ("schema", "backup_id", "library_root_id", "backup_epoch", "mode", "workspace_ids", "core_contract_version", "core_snapshot_hash", "workspace_snapshot_hash", "current_generation_id", "lkg_generation_id", "asset_closure_root", "plugin_releases", "projection_rebuild_required", "files", "created_at", "verification", "bundle_hash"))
    restore = obj({"schema": const("restore-report/v1"), "restore_id": ID, "backup_id": ID, "source_root_id": ID, "target_root_id": ID, "state": enum("staging", "verifying", "restore_ready", "switched", "failed"), "verified_files": array(PATH), "missing_release_ids": array(HASH, unique=True), "projection_rebuild_required": array(obj({"plugin_id": ID, "release_id": HASH, "reason": const("plugin_projection_rebuild_required")}, ("plugin_id", "release_id", "reason"))), "errors": array(diagnostic()), "created_at": UTC, "completed_at": nullable(UTC)}, ("schema", "restore_id", "backup_id", "source_root_id", "target_root_id", "state", "verified_files", "missing_release_ids", "projection_rebuild_required", "errors", "created_at", "completed_at"))
    return {"backup-bundle-v1": backup, "restore-report-v1": restore}


def job_broker_schemas() -> dict[str, dict[str, Any]]:
    child = obj({"child_job_id": ID, "parent_job_id": ID, "parent_step_id": ID, "parent_attempt_id": ID, "invoke_operation_key": ID, "binding_id": ID, "broker_invocation_asset_id": ID, "broker_invocation_hash": HASH, "child_run_snapshot_asset_id": ID, "child_run_snapshot_hash": HASH, "result_contract": enum("candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"), "required": BOOL, "propagate_cancel": BOOL, "state": ID, "result_bundle_asset_id": nullable(ID), "provenance_receipt_id": nullable(ID)}, ("child_job_id", "parent_job_id", "parent_step_id", "parent_attempt_id", "invoke_operation_key", "binding_id", "broker_invocation_asset_id", "broker_invocation_hash", "child_run_snapshot_asset_id", "child_run_snapshot_hash", "result_contract", "required", "propagate_cancel", "state", "result_bundle_asset_id", "provenance_receipt_id"))
    return {"broker-child-record-v1": child}


def rpc_method_matrix(*, include_schemas: bool = False) -> dict[str, dict[str, Any]]:
    s = lambda: STR
    n = lambda: NONNEG_INT
    params: dict[str, tuple[str, dict[str, dict[str, Any]], tuple[str, ...], tuple[str, ...]]] = {}
    def add(name: str, profile: str, fields: dict[str, dict[str, Any]], required: tuple[str, ...], result: dict[str, dict[str, Any]], result_required: tuple[str, ...]) -> None:
        params[name] = (profile, fields, required, result, result_required)
    add("runtime.handshake", "control/install", {"host_protocol": s(), "generation_id": ID, "plugin_release_id": HASH, "data_generation_id": nullable(ID)}, ("host_protocol", "generation_id", "plugin_release_id", "data_generation_id"), {"plugin_protocol": s(), "plugin_id": ID, "release_id": HASH, "capabilities": array(ID), "worker_instance_id": ID}, ("plugin_protocol", "plugin_id", "release_id", "capabilities", "worker_instance_id"))
    add("runtime.health", "control/install", {"probe_id": ID, "db_lease_id": nullable(ID), "db_lease_epoch": nullable(POS_INT), "owner_instance_id": nullable(ID)}, ("probe_id", "db_lease_id", "db_lease_epoch", "owner_instance_id"), {"status": enum("ok", "degraded", "fail"), "details_asset_id": nullable(ID), "checked_at": UTC}, ("status", "details_asset_id", "checked_at"))
    add("runtime.heartbeat", "attempt", {"worker_instance_id": ID, "observed_at": UTC, "local_seq": POS_INT}, ("worker_instance_id", "observed_at", "local_seq"), {}, ())
    add("capability.describe", "control", {"capability_id": ID}, ("capability_id",), {"descriptor": obj({"schema": const("capability-provider/v1"), "capability_id": ID, "provider": obj({"plugin_id": ID, "release_id": HASH}, ("plugin_id", "release_id")), "input_schema": ID, "output_schema": ID, "result_contract": ID, "supports": array(ID), "deterministic": BOOL, "accepted_data_formats": array(ID)}, ("schema", "capability_id", "provider", "input_schema", "output_schema", "result_contract", "supports", "deterministic", "accepted_data_formats"))}, ("descriptor",))
    add("settings.validate", "control/install", {"settings_revision_id": ID, "plugin_release_id": HASH, "schema_hash": HASH, "payload_asset_id": ID}, ("settings_revision_id", "plugin_release_id", "schema_hash", "payload_asset_id"), {"valid": BOOL, "evaluated_payload_hash": HASH, "details_asset_id": nullable(ID), "errors": array(diagnostic())}, ("valid", "evaluated_payload_hash", "details_asset_id", "errors"))
    add("migration.plan", "install", {"from_schema": HASH, "to_schema": HASH, "migration_manifest_hash": HASH}, ("from_schema", "to_schema", "migration_manifest_hash"), {"plan_id": ID, "steps": array(obj({"step_id": ID, "file": PATH, "sha256": HASH, "from_schema": HASH, "to_schema": HASH}, ("step_id", "file", "sha256", "from_schema", "to_schema"))), "backward_compatible": BOOL, "requires_verified_backup": BOOL}, ("plan_id", "steps", "backward_compatible", "requires_verified_backup"))
    add("migration.apply", "install", {"plan_id": ID, "db_lease_id": ID, "db_lease_epoch": POS_INT, "owner_instance_id": ID}, ("plan_id", "db_lease_id", "db_lease_epoch", "owner_instance_id"), {"applied_schema": HASH, "receipt_hash": HASH}, ("applied_schema", "receipt_hash"))
    add("migration.verify", "install", {"db_lease_id": ID, "db_lease_epoch": POS_INT, "owner_instance_id": ID, "expected_schema": HASH}, ("db_lease_id", "db_lease_epoch", "owner_instance_id", "expected_schema"), {"valid": BOOL, "schema_hash": HASH, "errors": array(diagnostic())}, ("valid", "schema_hash", "errors"))
    add("job.start", "attempt", {"capability_id": ID, "run_snapshot_asset_id": ID, "checkpoint_asset_id": nullable(ID), "secrets": nullable(array(obj({"secret_id": ID, "value": {"type": "string", "maxLength": 65536}}, ("secret_id", "value"))))}, ("capability_id", "run_snapshot_asset_id", "checkpoint_asset_id", "secrets"), {"accepted": BOOL, "worker_run_id": ID, "provenance_receipt_id": ID, "output_streams": array(obj({"stream_id": ID, "job_id": ID, "step_id": ID, "output_role": ID, "target": target()}, ("stream_id", "job_id", "step_id", "output_role", "target")))}, ("accepted", "worker_run_id", "provenance_receipt_id", "output_streams"))
    add("job.resume", "attempt", {"capability_id": ID, "run_snapshot_asset_id": ID, "resume_of_attempt_id": ID, "checkpoint_asset_id": nullable(ID), "resume_intent_id": ID, "resume_reason": NONEMPTY, "secrets": nullable(array(obj({"secret_id": ID, "value": {"type": "string", "maxLength": 65536}}, ("secret_id", "value"))))}, ("capability_id", "run_snapshot_asset_id", "resume_of_attempt_id", "checkpoint_asset_id", "resume_intent_id", "resume_reason", "secrets"), {"accepted": BOOL, "worker_run_id": ID, "provenance_receipt_id": ID, "output_streams": array(obj({"stream_id": ID, "job_id": ID, "step_id": ID, "output_role": ID, "target": target()}, ("stream_id", "job_id", "step_id", "output_role", "target")))}, ("accepted", "worker_run_id", "provenance_receipt_id", "output_streams"))
    add("job.pause", "attempt", {"worker_run_id": ID, "reason": NONEMPTY}, ("worker_run_id", "reason"), {"accepted": BOOL, "checkpoint_asset_id": nullable(ID)}, ("accepted", "checkpoint_asset_id"))
    add("job.cancel", "attempt", {"worker_run_id": ID, "reason": NONEMPTY}, ("worker_run_id", "reason"), {"accepted": BOOL, "terminal_known": BOOL, "attempt_state": ID}, ("accepted", "terminal_known", "attempt_state"))
    add("runtime.shutdown", "control/install/attempt", {"reason": NONEMPTY, "deadline_at": UTC}, ("reason", "deadline_at"), {"accepted": BOOL}, ("accepted",))
    add("host.asset.read/v1", "control/install/attempt", {"asset_id": ID, "offset": NONNEG_INT, "length": POS_INT}, ("asset_id", "offset", "length"), {"base64_chunk": STR, "next_offset": nullable(NONNEG_INT), "content_hash": HASH}, ("base64_chunk", "next_offset", "content_hash"))
    add("host.asset.create/v1", "install/attempt", {"operation_key": ID, "upload_id": ID, "offset": NONNEG_INT, "mime": NONEMPTY, "total_size": NONNEG_INT, "expected_hash": HASH, "chunk_hash": HASH, "base64_chunk": STR, "final": BOOL}, ("operation_key", "upload_id", "offset", "mime", "total_size", "expected_hash", "chunk_hash", "base64_chunk", "final"), {"upload_id": ID, "accepted_bytes": NONNEG_INT, "completed": BOOL, "asset_id": nullable(ID)}, ("upload_id", "accepted_bytes", "completed", "asset_id"))
    add("host.asset.upload.status/v1", "install/attempt", {"upload_id": ID, "expected_hash": HASH}, ("upload_id", "expected_hash"), {"accepted_bytes": NONNEG_INT, "completed": BOOL, "asset_id": nullable(ID)}, ("accepted_bytes", "completed", "asset_id"))
    add("host.model.invoke/v1", "attempt", {"operation_key": ID, "invocation_id": ID, "invocation_key": ID, "model_profile_revision_id": ID, "request_asset_id": ID, "replay_policy": enum("idempotent_auto", "manual_if_unknown", "never_replay")}, ("operation_key", "invocation_id", "invocation_key", "model_profile_revision_id", "request_asset_id", "replay_policy"), {"state": enum("received", "failed", "uncertain"), "response_asset_id": nullable(ID), "receipt_id": ID, "uncertainty": nullable(diagnostic())}, ("state", "response_asset_id", "receipt_id", "uncertainty"))
    add("host.capability.invoke/v1", "attempt", {"operation_key": ID, "binding_id": ID, "input_asset_id": ID, "parameters_asset_id": nullable(ID), "expected_result_contract": enum("candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"), "propagate_cancel": BOOL}, ("operation_key", "binding_id", "input_asset_id", "parameters_asset_id", "expected_result_contract", "propagate_cancel"), {"accepted": BOOL, "child_job_id": ID, "child_step_id": ID, "child_run_snapshot_asset_id": ID, "child_run_snapshot_hash": HASH, "child_result_contract": enum("candidate-batch/v1", "artifact-bundle/v1", "diagnostic-bundle/v1"), "child_job_event_seq": NONNEG_INT}, ("accepted", "child_job_id", "child_step_id", "child_run_snapshot_asset_id", "child_run_snapshot_hash", "child_result_contract", "child_job_event_seq"))
    add("host.capability.poll/v1", "attempt", {"child_job_id": ID, "after_job_event_seq": NONNEG_INT}, ("child_job_id", "after_job_event_seq"), {"job_snapshot_asset_id": ID, "job_event_page_asset_id": ID, "next_job_event_seq": NONNEG_INT, "terminal": BOOL, "result_bundle_asset_id": nullable(ID), "provenance_receipt_id": nullable(ID)}, ("job_snapshot_asset_id", "job_event_page_asset_id", "next_job_event_seq", "terminal", "result_bundle_asset_id", "provenance_receipt_id"))
    add("host.capability.cancel/v1", "attempt", {"operation_key": ID, "child_job_id": ID, "reason": NONEMPTY}, ("operation_key", "child_job_id", "reason"), {"accepted": BOOL, "terminal_known": BOOL, "child_state": ID, "child_job_event_seq": NONNEG_INT}, ("accepted", "terminal_known", "child_state", "child_job_event_seq"))
    add("host.candidate.stage/v1", "attempt", {"operation_key": ID, "result_bundle_asset_id": ID, "input_snapshot_hash": HASH}, ("operation_key", "result_bundle_asset_id", "input_snapshot_hash"), {"accepted": BOOL, "staged_items": array(obj({"item_id": ID, "candidate_id": nullable(ID), "stage_status": enum("created", "existing", "failed", "skipped", "rejected"), "publication_eligibility": enum("eligible", "review_only", "none")}, ("item_id", "candidate_id", "stage_status", "publication_eligibility"))), "job_event_seq": NONNEG_INT}, ("accepted", "staged_items", "job_event_seq"))
    add("host.checkpoint.commit/v1", "attempt", {"operation_key": ID, "checkpoint_asset_id": ID}, ("operation_key", "checkpoint_asset_id"), {"accepted": BOOL, "checkpoint_id": ID, "completed_units": NONNEG_INT, "total_units": nullable(NONNEG_INT), "job_event_seq": NONNEG_INT}, ("accepted", "checkpoint_id", "completed_units", "total_units", "job_event_seq"))
    add("host.stream.commit/v1", "attempt", {"operation_key": ID, "stream_prefix_asset_id": ID}, ("operation_key", "stream_prefix_asset_id"), {"accepted": BOOL, "stream_id": ID, "acked_prefix_seq": NONNEG_INT, "acked_bytes": NONNEG_INT, "acked_prefix_hash": HASH, "job_event_seq": NONNEG_INT}, ("accepted", "stream_id", "acked_prefix_seq", "acked_bytes", "acked_prefix_hash", "job_event_seq"))
    add("host.job.event/v1", "attempt", {"operation_key": ID, "event_type": NONEMPTY, "payload_asset_id": nullable(ID), "local_seq": POS_INT}, ("operation_key", "event_type", "payload_asset_id", "local_seq"), {"accepted": BOOL, "job_event_seq": NONNEG_INT}, ("accepted", "job_event_seq"))
    add("host.job.await_user/v1", "attempt", {"operation_key": ID, "worker_run_id": ID, "checkpoint_asset_id": ID, "prompt_asset_id": nullable(ID), "reason": enum("user_input", "external_confirmation")}, ("operation_key", "worker_run_id", "checkpoint_asset_id", "prompt_asset_id", "reason"), {"accepted": BOOL, "attempt_state": const("suspended"), "step_state": const("waiting_user"), "job_state": const("waiting_user"), "job_event_seq": NONNEG_INT}, ("accepted", "attempt_state", "step_state", "job_state", "job_event_seq"))
    add("host.job.complete/v1", "attempt", {"operation_key": ID, "worker_run_id": ID, "outcome": enum("succeeded", "partial", "failed", "cancelled"), "result_bundle_asset_id": nullable(ID), "candidate_stage_operation_key": nullable(ID), "terminal_detail_asset_id": nullable(ID), "local_seq": POS_INT}, ("operation_key", "worker_run_id", "outcome", "result_bundle_asset_id", "candidate_stage_operation_key", "terminal_detail_asset_id", "local_seq"), {"accepted": BOOL, "attempt_state": ID, "step_state": ID, "job_state": ID, "provenance_receipt_id": ID, "job_event_seq": NONNEG_INT, "core_event_high_water": nullable(NONNEG_INT)}, ("accepted", "attempt_state", "step_state", "job_state", "provenance_receipt_id", "job_event_seq", "core_event_high_water"))
    add("host.log/v1", "control/install/attempt", {"level": enum("debug", "info", "warning", "error"), "message": STR, "fields_asset_id": nullable(ID), "local_seq": POS_INT}, ("level", "message", "fields_asset_id", "local_seq"), {"accepted": BOOL, "dropped": BOOL}, ("accepted", "dropped"))
    add("host.migration.lease.renew/v1", "install", {"operation_key": ID, "db_lease_id": ID, "db_lease_epoch": POS_INT, "owner_instance_id": ID, "requested_expires_at": UTC}, ("operation_key", "db_lease_id", "db_lease_epoch", "owner_instance_id", "requested_expires_at"), {"accepted": BOOL, "db_lease_epoch": POS_INT, "expires_at": UTC}, ("accepted", "db_lease_epoch", "expires_at"))
    add("host.migration.lease.release/v1", "install", {"operation_key": ID, "db_lease_id": ID, "db_lease_epoch": POS_INT, "owner_instance_id": ID, "reason": NONEMPTY}, ("operation_key", "db_lease_id", "db_lease_epoch", "owner_instance_id", "reason"), {"accepted": BOOL, "state": enum("released", "expired", "revoked")}, ("accepted", "state"))
    out: dict[str, dict[str, Any]] = {}
    for name, (profile, fields, required, result, result_required) in params.items():
        definition: dict[str, Any] = {
            "meta_profile": profile,
            "params": {"required": list(required), "fields": list(fields)},
            "result": {"required": list(result_required), "fields": list(result)},
        }
        # The public method matrix intentionally contains field names and
        # requiredness only.  Schema generation still needs the normative
        # field shapes from the same source; keep them private to this
        # in-process call so the checked-in matrix does not grow a second,
        # competing type registry.
        if include_schemas:
            definition["_param_schemas"] = copy.deepcopy(fields)
            definition["_result_schemas"] = copy.deepcopy(result)
        out[name] = definition
    return out


def rpc_schemas() -> dict[str, dict[str, Any]]:
    matrix = rpc_method_matrix(include_schemas=True)
    control = obj({"protocol_version": const("1"), "generation_id": ID, "plugin_release_id": HASH, "deadline_at": UTC, "context": const("control"), "operation_id": ID}, ("protocol_version", "generation_id", "plugin_release_id", "deadline_at", "context", "operation_id"))
    install = obj({"protocol_version": const("1"), "generation_id": ID, "plugin_release_id": HASH, "deadline_at": UTC, "context": const("install"), "operation_id": ID, "install_operation_id": ID, "install_lease_epoch": POS_INT}, ("protocol_version", "generation_id", "plugin_release_id", "deadline_at", "context", "operation_id", "install_operation_id", "install_lease_epoch"))
    attempt = obj({"protocol_version": const("1"), "generation_id": ID, "plugin_release_id": HASH, "deadline_at": UTC, "context": const("attempt"), "operation_id": ID, "job_id": ID, "step_id": ID, "attempt_id": ID, "lease_epoch": POS_INT}, ("protocol_version", "generation_id", "plugin_release_id", "deadline_at", "context", "operation_id", "job_id", "step_id", "attempt_id", "lease_epoch"))
    profiles = {"control": control, "install": install, "attempt": attempt}
    def profile_schema(value: str) -> dict[str, Any]:
        return {"anyOf": [profiles[p] for p in value.split("/")]}
    def make_params(data: dict[str, Any]) -> dict[str, Any]:
        return obj(copy.deepcopy(data["_param_schemas"]), tuple(data["params"]["required"]))
    def make_result(data: dict[str, Any]) -> dict[str, Any]:
        return obj(copy.deepcopy(data["_result_schemas"]), tuple(data["result"]["required"]))
    request_branches = []
    success_results = []
    for method, data in matrix.items():
        if method == "runtime.heartbeat":
            continue
        request_branches.append(obj({"jsonrpc": const("2.0"), "id": UUID, "method": const(method), "meta": profile_schema(data["meta_profile"]), "params": make_params(data)}, ("jsonrpc", "id", "method", "meta", "params")))
        success_results.append(make_result(data))
    request = {"oneOf": request_branches, "unevaluatedProperties": False}
    success = obj({"jsonrpc": const("2.0"), "id": UUID, "result": {"oneOf": success_results}}, ("jsonrpc", "id", "result"))
    error_data = obj({"error_id": ID, "retryable": BOOL, "details_asset_id": nullable(ID)}, ("error_id", "retryable", "details_asset_id"))
    error = obj({"jsonrpc": const("2.0"), "id": nullable(UUID), "error": obj({"code": INT, "message": STR, "data": nullable(error_data)}, ("code", "message", "data"))}, ("jsonrpc", "id", "error"))
    heartbeat = obj({"jsonrpc": const("2.0"), "method": const("runtime.heartbeat"), "meta": attempt, "params": make_params(matrix["runtime.heartbeat"])}, ("jsonrpc", "method", "meta", "params"))
    return {"rpc-request-v1": request, "rpc-success-v1": success, "rpc-error-v1": error, "rpc-notification-v1": heartbeat, "rpc-envelope-v1": {"oneOf": [request, success, error, heartbeat], "unevaluatedProperties": False}}


def compatibility_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://plotpilot.local/contracts/compatibility-v1",
        "title": "PlotPilot compatibility/v1",
        **obj({"core_api": {"type": "string", "pattern": r"^>=(0|[1-9][0-9]*)\.(0|[1-9][0-9]*) <(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"}, "plugin_rpc": {"type": "string", "pattern": r"^(0|[1-9][0-9]*)$"}, "ui_host": {"type": "string", "pattern": r"^(0|[1-9][0-9]*)$"}, "python": {"type": "string", "pattern": r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.\*$"}}, ("core_api", "plugin_rpc", "ui_host")),
    }


# ---------------------------------------------------------------------------
# Additive post-M0 public surface (v2)
# ---------------------------------------------------------------------------
#
# These builders intentionally live in the existing generator.  The v2 lane
# is an additive contract, not a second schema-generation system.  Every
# object is closed and every union is guarded by ``unevaluatedProperties`` so
# that a future field or route cannot be accepted accidentally.


V2_CURSOR = {"type": "string", "pattern": r"^(?:candidate|job|core)/[A-Za-z0-9][A-Za-z0-9._:/-]*$"}
V2_ENTITY_KIND = enum("document", "node_structure", "relation_set")
# Failed/skipped values belong to staging/outcome mappings.  They are not
# Candidate records and therefore must not be representable on the public
# Candidate query/review surface.
V2_CANDIDATE_STATUS = enum("complete", "partial")


def v2_union(*branches: dict[str, Any]) -> dict[str, Any]:
    return {"oneOf": list(branches), "unevaluatedProperties": False}


def v2_target() -> dict[str, Any]:
    return obj(
        {"workspace_id": ID, "entity_kind": V2_ENTITY_KIND, "entity_id": ID},
        ("workspace_id", "entity_kind", "entity_id"),
    )


def v2_mutation() -> dict[str, Any]:
    return obj(
        {
            "mode": enum("replace", "text_patch", "structure_patch", "relation_patch", "append_text"),
            "payload_schema": ID,
            "payload_hash": HASH,
        },
        ("mode", "payload_schema", "payload_hash"),
    )


def v2_write_set_entry() -> dict[str, Any]:
    return obj(
        {
            "workspace_id": ID,
            "entity_kind": V2_ENTITY_KIND,
            "entity_id": ID,
            "revision_id": ID,
            "content_hash": HASH,
        },
        ("workspace_id", "entity_kind", "entity_id", "revision_id", "content_hash"),
    )


def v2_source_ref() -> dict[str, Any]:
    return obj(
        {
            "workspace_id": nullable(ID),
            "source_type": enum("publication", "asset", "candidate", "revision", "job", "external"),
            "source_id": ID,
            "revision_or_hash": ID,
        },
        ("workspace_id", "source_type", "source_id", "revision_or_hash"),
    )


def v2_candidate_record() -> dict[str, Any]:
    return obj(
        {
            "schema": const("candidate/v2"),
            "candidate_id": ID,
            "workspace_id": ID,
            "item_kind": enum("document", "node_structure", "relation_set", "incomplete_stream"),
            "target": v2_target(),
            "mutation": v2_mutation(),
            "payload_asset_id": ID,
            "base": obj({"revision_id": ID, "content_hash": HASH}, ("revision_id", "content_hash")),
            "write_set": array(v2_write_set_entry(), min_items=1, unique=True),
            "parent_candidate_ids": array(ID, unique=True),
            "source_refs": array(v2_source_ref(), unique=True),
            "status": V2_CANDIDATE_STATUS,
            "publication_eligibility": enum("eligible", "review_only", "none"),
            "created_at": UTC,
            "source_job_id": nullable(ID),
        },
        (
            "schema", "candidate_id", "workspace_id", "item_kind", "target", "mutation",
            "payload_asset_id", "base", "write_set", "parent_candidate_ids", "source_refs",
            "status", "publication_eligibility", "created_at", "source_job_id",
        ),
    )


def v2_candidate_query_result_schemas() -> dict[str, dict[str, Any]]:
    candidate_record = v2_candidate_record()
    candidate_list_query = obj(
        {"schema": const("candidate-list-query/v2"), "workspace_id": ID, "cursor": nullable(V2_CURSOR), "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
        ("schema", "workspace_id", "cursor", "limit"),
    )
    candidate_list_result = obj(
        {"schema": const("candidate-list-result/v2"), "workspace_id": ID, "items": array(v2_candidate_record()), "next_cursor": nullable(V2_CURSOR), "cursor_domain": const("candidate"), "total": NONNEG_INT},
        ("schema", "workspace_id", "items", "next_cursor", "cursor_domain", "total"),
    )
    candidate_get_query = obj(
        {"schema": const("candidate-get-query/v2"), "workspace_id": ID, "candidate_id": ID},
        ("schema", "workspace_id", "candidate_id"),
    )
    candidate_get_result = obj(
        {"schema": const("candidate-get-result/v2"), "workspace_id": ID, "candidate": v2_candidate_record()},
        ("schema", "workspace_id", "candidate"),
    )
    candidate_preview_query = obj(
        {"schema": const("candidate-preview-query/v2"), "workspace_id": ID, "candidate_id": ID, "offset": NONNEG_INT, "length": {"type": "integer", "minimum": 1, "maximum": 65536}},
        ("schema", "workspace_id", "candidate_id", "offset", "length"),
    )
    candidate_preview_result = obj(
        {"schema": const("candidate-preview-result/v2"), "workspace_id": ID, "candidate_id": ID, "payload_asset_id": ID, "payload_hash": HASH, "offset": NONNEG_INT, "length": NONNEG_INT, "total_length": NONNEG_INT, "base64_chunk": BASE64, "next_offset": nullable(NONNEG_INT), "content_hash": HASH},
        ("schema", "workspace_id", "candidate_id", "payload_asset_id", "payload_hash", "offset", "length", "total_length", "base64_chunk", "next_offset", "content_hash"),
    )
    return {
        "candidate-query-result-v2": v2_union(candidate_record, candidate_list_query, candidate_list_result, candidate_get_query, candidate_get_result, candidate_preview_query, candidate_preview_result),
    }


def v2_core_authority_schemas() -> dict[str, dict[str, Any]]:
    authority_query = obj(
        {"schema": const("core-authority-query/v2"), "workspace_id": ID, "entity_kind": enum("workspace", "document", "node_structure", "relation_set", "revision", "publication"), "entity_id": ID, "include_assets": BOOL},
        ("schema", "workspace_id", "entity_kind", "entity_id", "include_assets"),
    )
    authority_result = obj(
        {"schema": const("core-authority-result/v2"), "workspace_id": ID, "entity_kind": enum("workspace", "document", "node_structure", "relation_set", "revision", "publication"), "entity_id": ID, "revision_id": nullable(ID), "revision_number": NONNEG_INT, "content_asset_id": nullable(ID), "content_hash": nullable(HASH), "derived_from_candidate_id": nullable(ID)},
        ("schema", "workspace_id", "entity_kind", "entity_id", "revision_id", "revision_number", "content_asset_id", "content_hash", "derived_from_candidate_id"),
    )
    publication_command = obj(
        {"schema": const("publication-command/v2"), "publication_operation_key": ID, "workspace_id": ID, "candidate_id": ID, "accepted_by": ID},
        ("schema", "publication_operation_key", "workspace_id", "candidate_id", "accepted_by"),
    )
    publication_cas = obj(
        {
            "base_revision_id": ID,
            "base_content_hash": HASH,
            "revision_id": ID,
            "revision_number": POS_INT,
            "content_hash": HASH,
            # Bind the complete ordered Candidate write_set, not only the
            # Publication target, to this single CAS result.
            "write_set": array(v2_write_set_entry(), min_items=1, unique=True),
        },
        ("base_revision_id", "base_content_hash", "revision_id", "revision_number", "content_hash", "write_set"),
    )
    publication_result = obj(
        {
            "schema": const("publication-result/v2"),
            "publication_id": ID,
            "publication_operation_key": ID,
            "candidate_id": ID,
            "workspace_id": ID,
            "entity_kind": V2_ENTITY_KIND,
            "entity_id": ID,
            "revision_id": ID,
            "revision_number": POS_INT,
            # ``content_hash`` is the final Revision hash returned by Core.
            # The mutation payload hash is intentionally not substituted here.
            "content_hash": HASH,
            "cas": publication_cas,
            "provenance_receipt_id": ID,
            "idempotent": BOOL,
        },
        ("schema", "publication_id", "publication_operation_key", "candidate_id", "workspace_id", "entity_kind", "entity_id", "revision_id", "revision_number", "content_hash", "cas", "provenance_receipt_id", "idempotent"),
    )
    error = obj(
        {"schema": const("core-http-error/v2"), "error_code": enum("malformed_request", "unknown_reference", "cross_workspace", "stale_cas", "duplicate_operation", "candidate_not_publishable", "publication_only_core"), "message": NONEMPTY, "retryable": BOOL, "operation_key": nullable(ID)},
        ("schema", "error_code", "message", "retryable", "operation_key"),
    )
    return {"core-authority-command-query-v2": v2_union(authority_query, authority_result, publication_command, publication_result, error)}


def v2_candidate_review_schemas() -> dict[str, dict[str, Any]]:
    review_query = obj(
        {"schema": const("candidate-review-query/v2"), "workspace_id": ID, "candidate_id": ID, "include_lineage": BOOL, "include_preview": BOOL},
        ("schema", "workspace_id", "candidate_id", "include_lineage", "include_preview"),
    )
    review_command = obj(
        {"schema": const("candidate-review-command/v2"), "operation_key": ID, "workspace_id": ID, "candidate_id": ID, "decision": enum("approve", "reject"), "decided_by": ID, "expected_status": V2_CANDIDATE_STATUS},
        ("schema", "operation_key", "workspace_id", "candidate_id", "decision", "decided_by", "expected_status"),
    )
    review_result = obj(
        {"schema": const("candidate-review-result/v2"), "operation_key": ID, "workspace_id": ID, "candidate_id": ID, "decision": enum("approve", "reject"), "status": enum("reviewed", "rejected"), "publication_eligibility": enum("eligible", "review_only", "none"), "idempotent": BOOL, "review_revision": POS_INT},
        ("schema", "operation_key", "workspace_id", "candidate_id", "decision", "status", "publication_eligibility", "idempotent", "review_revision"),
    )
    return {"candidate-review-v2": v2_union(review_query, review_command, review_result)}


def v2_story_state_projection_schemas() -> dict[str, dict[str, Any]]:
    publication_ref = obj(
        {"publication_id": ID, "candidate_id": ID, "revision_id": ID, "revision_number": POS_INT, "content_hash": HASH},
        ("publication_id", "candidate_id", "revision_id", "revision_number", "content_hash"),
    )
    candidate_ref = obj(
        {"candidate_id": ID, "target": v2_target(), "payload_asset_id": ID, "payload_hash": HASH, "status": V2_CANDIDATE_STATUS},
        ("candidate_id", "target", "payload_asset_id", "payload_hash", "status"),
    )
    revision_ref = obj(
        {"revision_id": ID, "content_asset_id": ID, "content_hash": HASH, "revision_number": POS_INT},
        ("revision_id", "content_asset_id", "content_hash", "revision_number"),
    )
    asset_ref = obj({"asset_id": ID, "sha256": HASH, "role": NONEMPTY}, ("asset_id", "sha256", "role"))
    receipt_ref = obj({"receipt_id": ID, "receipt_hash": HASH, "parent_receipt_ids": array(ID, unique=True)}, ("receipt_id", "receipt_hash", "parent_receipt_ids"))
    projection = obj(
        {"schema": const("story-state-projection-input/v2"), "projection_input_id": ID, "workspace_id": ID, "publication": publication_ref, "candidate": candidate_ref, "current_revision": revision_ref, "assets": array(asset_ref, min_items=1, unique=True), "provenance": obj({"receipt_id": ID, "receipt_hash": HASH, "run_snapshot_hash": HASH, "release_id": HASH, "package_hash": HASH, "parent_receipt_ids": array(ID, unique=True)}, ("receipt_id", "receipt_hash", "run_snapshot_hash", "release_id", "package_hash", "parent_receipt_ids")), "receipt_closure": array(receipt_ref, min_items=1, unique=True), "derived_at": UTC},
        ("schema", "projection_input_id", "workspace_id", "publication", "candidate", "current_revision", "assets", "provenance", "receipt_closure", "derived_at"),
    )
    return {"story-state-projection-input-v2": projection}


def v2_job_snapshot() -> dict[str, Any]:
    stream = obj(
        {"stream_id": ID, "output_role": ID, "target": v2_target(), "acked_prefix_seq": NONNEG_INT, "acked_bytes": NONNEG_INT, "acked_prefix_hash": HASH},
        ("stream_id", "output_role", "target", "acked_prefix_seq", "acked_bytes", "acked_prefix_hash"),
    )
    return obj(
        {"job_id": ID, "workspace_id": ID, "state": enum("queued", "running", "paused", "cancelling", "succeeded", "partial", "failed", "cancelled", "needs_attention"), "job_revision": POS_INT, "writer_epoch": POS_INT, "current_attempt_id": nullable(ID), "candidate_ids": array(ID, unique=True), "checkpoint_id": nullable(ID), "stream_high_waters": array(stream, unique=True), "job_event_high_water": NONNEG_INT, "core_event_high_water": NONNEG_INT, "created_at": UTC, "updated_at": UTC, "snapshot_hash": HASH},
        ("job_id", "workspace_id", "state", "job_revision", "writer_epoch", "current_attempt_id", "candidate_ids", "checkpoint_id", "stream_high_waters", "job_event_high_water", "core_event_high_water", "created_at", "updated_at", "snapshot_hash"),
    )


def v2_job_http_schemas() -> dict[str, dict[str, Any]]:
    error = obj(
        {"schema": const("job-http-error/v2"), "error_code": enum("malformed_request", "unknown_job", "cross_workspace", "stale_revision", "duplicate_operation", "cursor_ahead", "cursor_domain_mismatch", "sse_recovery_required", "terminal_job", "invalid_transition", "candidate_not_ready"), "message": NONEMPTY, "retryable": BOOL, "operation_key": nullable(ID), "cursor_domain": nullable(enum("candidate", "job", "core"))},
        ("schema", "error_code", "message", "retryable", "operation_key", "cursor_domain"),
    )
    list_query = obj({"schema": const("job-list-query/v2"), "workspace_id": ID, "cursor": nullable(V2_CURSOR), "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "state": nullable(enum("queued", "running", "paused", "cancelling", "succeeded", "partial", "failed", "cancelled", "needs_attention"))}, ("schema", "workspace_id", "cursor", "limit", "state"))
    list_result = obj({"schema": const("job-list-result/v2"), "workspace_id": ID, "items": array(v2_job_snapshot()), "next_cursor": nullable(V2_CURSOR), "cursor_domain": const("job"), "total": NONNEG_INT}, ("schema", "workspace_id", "items", "next_cursor", "cursor_domain", "total"))
    get_query = obj({"schema": const("job-snapshot-query/v2"), "workspace_id": ID, "job_id": ID}, ("schema", "workspace_id", "job_id"))
    get_result = obj({"schema": const("job-snapshot-result/v2"), "workspace_id": ID, "job_id": ID, "snapshot": v2_job_snapshot(), "cursor": V2_CURSOR}, ("schema", "workspace_id", "job_id", "snapshot", "cursor"))
    start = obj({"schema": const("job-start-command/v2"), "operation_key": ID, "workspace_id": ID, "job_id": ID, "capability_id": ID, "run_snapshot_asset_id": ID, "writer_epoch": POS_INT}, ("schema", "operation_key", "workspace_id", "job_id", "capability_id", "run_snapshot_asset_id", "writer_epoch"))
    control = obj({"schema": const("job-control-command/v2"), "operation_key": ID, "workspace_id": ID, "job_id": ID, "expected_job_revision": POS_INT, "reason": NONEMPTY, "command": enum("pause", "resume", "cancel"), "resume_intent_id": nullable(ID)}, ("schema", "operation_key", "workspace_id", "job_id", "expected_job_revision", "reason", "command", "resume_intent_id"))
    command_result = obj({"schema": const("job-command-result/v2"), "operation_key": ID, "workspace_id": ID, "job_id": ID, "command": enum("start", "pause", "resume", "cancel"), "accepted": BOOL, "idempotent": BOOL, "terminal_known": BOOL, "state": enum("queued", "running", "paused", "cancelling", "succeeded", "partial", "failed", "cancelled", "needs_attention"), "job_revision": POS_INT, "snapshot_cursor": V2_CURSOR}, ("schema", "operation_key", "workspace_id", "job_id", "command", "accepted", "idempotent", "terminal_known", "state", "job_revision", "snapshot_cursor"))
    event = obj({"event_id": ID, "job_id": ID, "job_event_seq": POS_INT, "event_type": NONEMPTY, "aggregate_revision": POS_INT, "payload_asset_id": nullable(ID), "payload_hash": nullable(HASH), "occurred_at": UTC}, ("event_id", "job_id", "job_event_seq", "event_type", "aggregate_revision", "payload_asset_id", "payload_hash", "occurred_at"))
    event_query = obj({"schema": const("job-event-page-query/v2"), "workspace_id": ID, "job_id": ID, "after_cursor": nullable(V2_CURSOR), "after_job_event_seq": NONNEG_INT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("schema", "workspace_id", "job_id", "after_cursor", "after_job_event_seq", "limit"))
    event_result = obj({"schema": const("job-event-page-result/v2"), "workspace_id": ID, "job_id": ID, "events": array(event), "next_cursor": V2_CURSOR, "high_water_seq": NONNEG_INT, "cursor_domain": const("job")}, ("schema", "workspace_id", "job_id", "events", "next_cursor", "high_water_seq", "cursor_domain"))
    sse_query = obj({"schema": const("job-sse-recovery-query/v2"), "workspace_id": ID, "job_id": ID, "after_seq": NONNEG_INT, "last_event_id": V2_CURSOR, "requested_cursor_domain": const("job")}, ("schema", "workspace_id", "job_id", "after_seq", "last_event_id", "requested_cursor_domain"))
    sse_result = obj({"schema": const("job-sse-recovery-result/v2"), "workspace_id": ID, "job_id": ID, "requested_after_seq": NONNEG_INT, "replay_floor_seq": NONNEG_INT, "durable_high_water_seq": NONNEG_INT, "gap": BOOL, "snapshot_required": BOOL, "snapshot": nullable(v2_job_snapshot()), "snapshot_cursor": V2_CURSOR, "tail": array(event)}, ("schema", "workspace_id", "job_id", "requested_after_seq", "replay_floor_seq", "durable_high_water_seq", "gap", "snapshot_required", "snapshot", "snapshot_cursor", "tail"))
    return {"job-http-command-query-v2": v2_union(error, list_query, list_result, get_query, get_result, start, control, command_result, event_query, event_result, sse_query, sse_result)}


def v2_plugin_api_schemas() -> dict[str, dict[str, Any]]:
    plugin = obj({"plugin_id": ID, "release_id": HASH, "package_hash": HASH, "state": enum("discovered", "installed", "active", "retiring", "retired", "failed"), "generation_id": nullable(ID), "capabilities": array(ID, unique=True), "updated_at": UTC}, ("plugin_id", "release_id", "package_hash", "state", "generation_id", "capabilities", "updated_at"))
    discovery_query = obj({"schema": const("plugin-discovery-query/v2"), "cursor": nullable(V2_CURSOR), "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "state": nullable(enum("discovered", "installed", "active", "retiring", "retired", "failed"))}, ("schema", "cursor", "limit", "state"))
    discovery_result = obj({"schema": const("plugin-discovery-result/v2"), "items": array(plugin), "next_cursor": nullable(V2_CURSOR), "cursor_domain": const("core"), "total": NONNEG_INT}, ("schema", "items", "next_cursor", "cursor_domain", "total"))
    lifecycle = obj({"schema": const("plugin-lifecycle-command/v2"), "operation_key": ID, "plugin_id": ID, "action": enum("install", "upgrade", "retire", "rollback"), "release_id": nullable(HASH), "package_hash": nullable(HASH), "expected_generation_id": nullable(ID), "target_generation_id": nullable(ID)}, ("schema", "operation_key", "plugin_id", "action", "release_id", "package_hash", "expected_generation_id", "target_generation_id"))
    lifecycle_result = obj({"schema": const("plugin-lifecycle-result/v2"), "operation_key": ID, "plugin_id": ID, "action": enum("install", "upgrade", "retire", "rollback"), "state": enum("discovered", "installed", "active", "retiring", "retired", "failed"), "generation_id": nullable(ID), "idempotent": BOOL, "failure_code": nullable(enum("package_invalid", "release_missing", "generation_conflict", "pinned_release", "active_job", "retire_failed", "rollback_failed"))}, ("schema", "operation_key", "plugin_id", "action", "state", "generation_id", "idempotent", "failure_code"))
    error = obj({"schema": const("plugin-http-error/v2"), "error_code": enum("malformed_request", "cursor_domain_mismatch", "unknown_plugin", "release_missing", "generation_conflict", "pinned_release", "active_job", "retire_failed", "rollback_failed", "duplicate_operation", "publication_forbidden"), "message": NONEMPTY, "retryable": BOOL, "operation_key": nullable(ID)}, ("schema", "error_code", "message", "retryable", "operation_key"))
    return {"plugin-api-command-query-v2": v2_union(discovery_query, discovery_result, lifecycle, lifecycle_result, error)}


def core_api_method_matrix_v2() -> dict[str, Any]:
    def route(route_id: str, method: str, path: str, request_schema: str, result_schema: str, *, read_only: bool, operation_key_required: bool, cursor_domain: str | None = None, success_statuses: tuple[int, ...] = (200,), failures: tuple[tuple[int, tuple[str, ...]], ...] = ((400, ("malformed_request",)), (404, ("unknown_reference",)))) -> dict[str, Any]:
        placeholders = re.findall(r"\{([^{}]+)\}", path)
        error_schema = {
            "job": "job-http-error/v2",
            "plugin": "plugin-http-error/v2",
        }.get(route_id.split(".", 1)[0], "core-http-error/v2")
        return {
            "route_id": route_id,
            "method": method,
            "path_template": path,
            "path_identity": placeholders,
            "request_schema": request_schema,
            "result_schema": result_schema,
            "error_schema": error_schema,
            "success_statuses": list(success_statuses),
            "failure_statuses": [{"status": status, "error_codes": list(codes)} for status, codes in failures],
            "read_only": read_only,
            "operation_key_required": operation_key_required,
            "cursor_domain": cursor_domain,
        }

    routes = [
        route("candidate.list", "GET", "/api/v2/core/workspaces/{workspace_id}/candidates", "candidate-list-query/v2", "candidate-list-result/v2", read_only=True, operation_key_required=False, cursor_domain="candidate", failures=((400, ("malformed_request", "cursor_domain_mismatch")), (404, ("unknown_reference",)))),
        route("candidate.get", "GET", "/api/v2/core/workspaces/{workspace_id}/candidates/{candidate_id}", "candidate-get-query/v2", "candidate-get-result/v2", read_only=True, operation_key_required=False, failures=((400, ("malformed_request",)), (404, ("unknown_reference", "cross_workspace")))),
        route("candidate.review", "POST", "/api/v2/core/workspaces/{workspace_id}/candidates/{candidate_id}/review", "candidate-review-command/v2", "candidate-review-result/v2", read_only=False, operation_key_required=True, failures=((400, ("malformed_request",)), (404, ("unknown_reference", "cross_workspace")), (409, ("stale_cas", "duplicate_operation")))),
        route("candidate.preview", "GET", "/api/v2/core/workspaces/{workspace_id}/candidates/{candidate_id}/preview", "candidate-preview-query/v2", "candidate-preview-result/v2", read_only=True, operation_key_required=False, failures=((400, ("malformed_request",)), (404, ("unknown_reference", "cross_workspace")))),
        route("publication.accept", "POST", "/api/v2/core/workspaces/{workspace_id}/publications:accept", "publication-command/v2", "publication-result/v2", read_only=False, operation_key_required=True, success_statuses=(200, 201), failures=((400, ("malformed_request", "candidate_not_publishable")), (404, ("unknown_reference", "cross_workspace")), (409, ("stale_cas", "duplicate_operation")))),
        route("story-state.projection-input", "GET", "/api/v2/core/workspaces/{workspace_id}/story-state/projection-input", "core-authority-query/v2", "story-state-projection-input/v2", read_only=True, operation_key_required=False, failures=((400, ("malformed_request",)), (404, ("unknown_reference", "cross_workspace")))),
        route("job.list", "GET", "/api/v2/jobs/{workspace_id}", "job-list-query/v2", "job-list-result/v2", read_only=True, operation_key_required=False, cursor_domain="job", failures=((400, ("malformed_request", "cursor_domain_mismatch")),)),
        route("job.get", "GET", "/api/v2/jobs/{workspace_id}/{job_id}", "job-snapshot-query/v2", "job-snapshot-result/v2", read_only=True, operation_key_required=False, cursor_domain="job", failures=((400, ("malformed_request", "cursor_domain_mismatch")), (404, ("unknown_job", "cross_workspace")))),
        route("job.start", "POST", "/api/v2/jobs/{workspace_id}/start", "job-start-command/v2", "job-command-result/v2", read_only=False, operation_key_required=True, cursor_domain="job", success_statuses=(201,), failures=((400, ("malformed_request",)), (409, ("duplicate_operation", "invalid_transition")))),
        route("job.pause", "POST", "/api/v2/jobs/{workspace_id}/{job_id}/pause", "job-control-command/v2", "job-command-result/v2", read_only=False, operation_key_required=True, cursor_domain="job", failures=((400, ("malformed_request",)), (404, ("unknown_job", "cross_workspace")), (409, ("stale_revision", "terminal_job", "invalid_transition", "duplicate_operation")))),
        route("job.resume", "POST", "/api/v2/jobs/{workspace_id}/{job_id}/resume", "job-control-command/v2", "job-command-result/v2", read_only=False, operation_key_required=True, cursor_domain="job", failures=((400, ("malformed_request",)), (404, ("unknown_job", "cross_workspace")), (409, ("stale_revision", "terminal_job", "invalid_transition", "duplicate_operation")))),
        route("job.cancel", "POST", "/api/v2/jobs/{workspace_id}/{job_id}/cancel", "job-control-command/v2", "job-command-result/v2", read_only=False, operation_key_required=True, cursor_domain="job", failures=((400, ("malformed_request",)), (404, ("unknown_job", "cross_workspace")), (409, ("stale_revision", "terminal_job", "invalid_transition", "duplicate_operation")))),
        route("job.events", "GET", "/api/v2/jobs/{workspace_id}/{job_id}/events", "job-event-page-query/v2", "job-event-page-result/v2", read_only=True, operation_key_required=False, cursor_domain="job", failures=((400, ("malformed_request", "cursor_domain_mismatch", "cursor_ahead")), (404, ("unknown_job", "cross_workspace")))),
        route("job.sse-recovery", "GET", "/api/v2/jobs/{workspace_id}/{job_id}/events/stream", "job-sse-recovery-query/v2", "job-sse-recovery-result/v2", read_only=True, operation_key_required=False, cursor_domain="job", failures=((400, ("malformed_request", "cursor_domain_mismatch")), (404, ("unknown_job", "cross_workspace")), (409, ("sse_recovery_required", "cursor_ahead")))),
        route("plugin.discovery", "GET", "/api/v2/plugins", "plugin-discovery-query/v2", "plugin-discovery-result/v2", read_only=True, operation_key_required=False, cursor_domain="core", failures=((400, ("malformed_request", "cursor_domain_mismatch")),)),
        route("plugin.install", "POST", "/api/v2/plugins/{plugin_id}/install", "plugin-lifecycle-command/v2", "plugin-lifecycle-result/v2", read_only=False, operation_key_required=True, failures=((400, ("malformed_request",)), (404, ("unknown_plugin", "release_missing")), (409, ("generation_conflict", "duplicate_operation")))),
        route("plugin.upgrade", "POST", "/api/v2/plugins/{plugin_id}/upgrade", "plugin-lifecycle-command/v2", "plugin-lifecycle-result/v2", read_only=False, operation_key_required=True, failures=((400, ("malformed_request",)), (404, ("unknown_plugin", "release_missing")), (409, ("generation_conflict", "duplicate_operation")))),
        route("plugin.retire", "POST", "/api/v2/plugins/{plugin_id}/retire", "plugin-lifecycle-command/v2", "plugin-lifecycle-result/v2", read_only=False, operation_key_required=True, failures=((400, ("malformed_request",)), (404, ("unknown_plugin",)), (409, ("pinned_release", "active_job", "retire_failed", "duplicate_operation")))),
        route("plugin.rollback", "POST", "/api/v2/plugins/{plugin_id}/rollback", "plugin-lifecycle-command/v2", "plugin-lifecycle-result/v2", read_only=False, operation_key_required=True, failures=((400, ("malformed_request",)), (404, ("unknown_plugin",)), (409, ("generation_conflict", "rollback_failed", "duplicate_operation")))),
    ]
    return {
        "schema": "core-api-method-matrix/v2",
        "contract_version": "2.0.0",
        "roots": {"core": "/api/v2/core", "jobs": "/api/v2/jobs", "plugins": "/api/v2/plugins"},
        "routes": routes,
        "cursor_domains": {"candidate": "candidate/*", "job": "job/{job_id}/{seq}", "core": "core/{seq}"},
        "error_codes": ["malformed_request", "unknown_reference", "cross_workspace", "stale_cas", "duplicate_operation", "candidate_not_publishable", "unknown_job", "stale_revision", "cursor_ahead", "cursor_domain_mismatch", "sse_recovery_required", "terminal_job", "invalid_transition", "candidate_not_ready", "unknown_plugin", "release_missing", "generation_conflict", "pinned_release", "active_job", "retire_failed", "rollback_failed", "publication_forbidden"],
        "publication_path": "publication.accept",
        "publication_owner": "core",
        "plugin_publication_allowed": False,
    }


def schema_inventory() -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {
        "plugin-manifest-v1": manifest_schema(),
        **common_manifest_schemas(),
        "run-snapshot-v1": run_snapshot(),
        "broker-invocation-v1": broker_invocation(),
        "result-bundle-v1": result_bundle(),
        "candidate-item-v1": candidate_item(),
        "artifact-item-v1": artifact_item(),
        "diagnostic-item-v1": diagnostic_item(),
        "provenance-receipt-v1": provenance_receipt(),
        **event_schemas(),
        **plan_schemas(),
        **core_api_schemas(),
        **core_http_request_failure_schemas(),
        **ui_schemas(),
        **skill_schemas(),
        **backup_schemas(),
        **job_broker_schemas(),
        **rpc_schemas(),
        **v2_candidate_query_result_schemas(),
        **v2_core_authority_schemas(),
        **v2_candidate_review_schemas(),
        **v2_story_state_projection_schemas(),
        **v2_job_http_schemas(),
        **v2_plugin_api_schemas(),
        "compatibility-v1": compatibility_schema(),
    }
    return schemas


def document(schema_id: str, value: dict[str, Any]) -> dict[str, Any]:
    if "$schema" not in value:
        value = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"https://plotpilot.local/contracts/{schema_id}", "title": f"PlotPilot {schema_id}", **value}
    return value


def render() -> dict[str, bytes]:
    rendered: dict[str, bytes] = {}
    for schema_id, value in schema_inventory().items():
        path = f"{schema_id}.schema.json"
        rendered[path] = (json.dumps(document(schema_id, value), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    matrix = {
        "schema": "rpc-method-matrix/v1",
        "protocol": {"jsonrpc": "2.0", "framing": "content-length-crlf", "encoding": "utf-8", "max_header_bytes": 8192, "max_body_bytes": 8388608, "batch": False},
        "worker_methods": list(WORKER_METHODS),
        "host_methods": list(HOST_METHODS),
        "methods": rpc_method_matrix(),
        "error_codes": {str(1000 + i): name for i, name in enumerate(["unknown", "incompatible_generation", "stale_lease", "cancelled", "deadline_exceeded", "asset_error", "settings_invalid", "migration_failed", "duplicate_request", "uncertain_external_effect", "invalid_transition", "result_contract_mismatch", "data_interpreter_unavailable", "release_retiring", "checkpoint_invalid"], start=0)},
    }
    # 1000 is intentionally reserved for the protocol's generic error.  The
    # normative application codes remain 1001..1014.
    matrix["error_codes"] = {"1001": "incompatible_generation", "1002": "stale_lease", "1003": "cancelled", "1004": "deadline_exceeded", "1005": "asset_error", "1006": "settings_invalid", "1007": "migration_failed", "1008": "duplicate_request", "1009": "uncertain_external_effect", "1010": "invalid_transition", "1011": "result_contract_mismatch", "1012": "data_interpreter_unavailable", "1013": "release_retiring", "1014": "checkpoint_invalid"}
    rendered["rpc-method-matrix.v1.json"] = (json.dumps(matrix, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    compatibility = {"schema": "compatibility-matrix/v1", "core_api": ">=1.0 <2.0", "plugin_rpc": "1", "ui_host": "1", "python": "3.12.*", "handshake_downgrade": False}
    rendered["compatibility-matrix.v1.json"] = (json.dumps(compatibility, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    rendered["core-api-method-matrix.v1.json"] = (json.dumps(core_api_method_matrix(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    rendered["core-api-method-matrix.v2.json"] = (json.dumps(core_api_method_matrix_v2(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    rendered["_common-v1.schema.json"] = (json.dumps(document("common-v1", {"type": "object", "additionalProperties": False, "unevaluatedProperties": False, "properties": {}}), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return rendered


def write() -> None:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    rendered = render()
    for relative, data in rendered.items():
        (SCHEMA_DIR / relative).write_bytes(data)
    PACKAGE_RESOURCE_DIR.mkdir(parents=True, exist_ok=True)
    for name, source in PACKAGE_RESOURCE_SOURCES.items():
        (PACKAGE_RESOURCE_DIR / name).write_bytes(source.read_bytes())
    print(f"generated {len(rendered)} schema/matrix artifacts and {len(PACKAGE_RESOURCE_SOURCES)} package resources")


def check() -> int:
    rendered = render()
    missing = []
    changed = []
    for relative, data in rendered.items():
        path = SCHEMA_DIR / relative
        if not path.exists():
            missing.append(relative)
        elif path.read_bytes() != data:
            changed.append(relative)
    if missing or changed:
        if missing:
            print("missing:", ", ".join(missing))
        if changed:
            print("changed:", ", ".join(changed))
        return 1
    missing_external = sorted(name for name in EXTERNAL_AUTHORED_ARTIFACTS if not (SCHEMA_DIR / name).is_file())
    if missing_external:
        print("missing external authored schema files:", ", ".join(missing_external))
        return 1
    extras = sorted(
        p.name
        for p in SCHEMA_DIR.iterdir()
        if p.is_file() and p.name not in rendered and p.name not in EXTERNAL_AUTHORED_ARTIFACTS
    )
    if extras:
        print("unexpected generated-schema files:", ", ".join(extras))
        return 1
    missing_resources = sorted(name for name, source in PACKAGE_RESOURCE_SOURCES.items() if not (PACKAGE_RESOURCE_DIR / name).is_file() or not source.is_file())
    changed_resources = sorted(name for name, source in PACKAGE_RESOURCE_SOURCES.items() if (PACKAGE_RESOURCE_DIR / name).is_file() and source.is_file() and (PACKAGE_RESOURCE_DIR / name).read_bytes() != source.read_bytes())
    resource_extras = sorted(
        path.relative_to(PACKAGE_RESOURCE_DIR).as_posix()
        for path in PACKAGE_RESOURCE_DIR.rglob("*")
        if path.is_file() and path.relative_to(PACKAGE_RESOURCE_DIR).as_posix() not in PACKAGE_RESOURCE_SOURCES
    ) if PACKAGE_RESOURCE_DIR.is_dir() else []
    if missing_resources or changed_resources or resource_extras:
        if missing_resources:
            print("missing package resources:", ", ".join(missing_resources))
        if changed_resources:
            print("changed package resources:", ", ".join(changed_resources))
        if resource_extras:
            print("unexpected package resources:", ", ".join(resource_extras))
        return 1
    print(f"generated artifacts are deterministic ({len(rendered)} files)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        return check()
    write()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
