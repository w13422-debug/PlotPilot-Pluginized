"""Generate representative positive fixtures for every M0 public seam."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import rfc8785


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "contracts" / "examples" / "fixtures"
H = "a" * 64


def digest(prefix: str, value: dict[str, Any], field: str) -> str:
    unsigned = {key: item for key, item in value.items() if key != field}
    return hashlib.sha256(prefix.encode("ascii") + b"\n" + rfc8785.dumps(unsigned)).hexdigest()


def write(name: str, value: Any) -> None:
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    write("plugin-manifest-code.json", {
        "schema": "plotpilot-plugin/v1", "plugin_id": "com.plotpilot.fixture.code", "version": "1.0.0", "display_name": "Fixture Code", "kind": "code",
        "compatibility": {"core_api": ">=1.0 <2.0", "plugin_rpc": "1", "ui_host": "1", "python": "3.12.*"},
        "capabilities": [{"capability_id": "fixture.echo/v1", "operations": ["run"], "result_contract": "artifact-bundle/v1"}], "settings": None,
        "needs": ["host.asset.read/v1"], "backend": {"entrypoint": "fixture_worker:main", "wheel": "backend/fixture.whl", "requirements_lock": "backend/requirements.lock", "wheelhouse": "backend/wheels", "max_concurrency": 1},
        "storage": {"schema_version": 1, "migration_policy": "transactional-shadow", "migration_manifest": "migrations/manifest.json"}, "ui": None, "data": None,
    })
    write("plugin-manifest-data.json", {
        "schema": "plotpilot-plugin/v1", "plugin_id": "com.plotpilot.fixture.data", "version": "1.0.0", "display_name": "Fixture Data", "kind": "data",
        "compatibility": {"core_api": ">=1.0 <2.0", "plugin_rpc": "1", "ui_host": "1"},
        "capabilities": [{"capability_id": "fixture.rules/v1", "operations": ["validate"], "result_contract": "diagnostic-bundle/v1"}], "settings": None,
        "needs": [], "data": {"format": "fixture-rules/v1", "root": "data/rules.json"},
    })
    write("capability-provider.json", {"schema": "capability-provider/v1", "capability_id": "fixture.echo/v1", "provider": {"plugin_id": "com.plotpilot.fixture.code", "release_id": "rel-fixture"}, "input_schema": "fixture/input/v1", "output_schema": "fixture/output/v1", "result_contract": "artifact-bundle/v1", "supports": ["run"], "deterministic": True, "accepted_data_formats": []})
    write("plugin-data-bundle.json", {"schema": "plugin-data-bundle/v1", "bundle_id": "data-bundle-1", "data_plugin_id": "com.plotpilot.fixture.data", "data_release_id": "data-release-1", "package_hash": H, "format_id": "fixture-rules/v1", "root_path": "data/rules.json", "files": [{"path": "data/rules.json", "asset_id": "asset-data-1", "sha256": H, "mime": "application/json", "size": 18}], "bundle_hash": H})
    write("broker-invocation.json", {"schema": "broker-invocation/v1", "invocation_id": "invoke-1", "parent_job_id": "job-1", "parent_step_id": "step-1", "parent_attempt_id": "attempt-1", "invoke_operation_key": "op-invoke-1", "binding_id": "binding-1", "input_asset_id": "asset-input-1", "input_hash": H, "parameters_asset_id": None, "parameters_hash": None, "expected_result_contract": "candidate-batch/v1", "required": True, "propagate_cancel": True})
    write("provenance-receipt.json", {"schema": "provenance-receipt/v1", "receipt_id": "receipt-1", "plugin_id": "com.plotpilot.fixture.code", "release_id": H, "package_hash": H, "capability_id": "fixture.echo/v1", "job_id": "job-1", "step_id": "step-1", "attempt_id": "attempt-1", "lease_epoch": 1, "run_snapshot_hash": H, "bundle_id": None, "bundle_hash": None, "parent_receipt_ids": [], "model_receipt_ids": [], "skill_chain_result_refs": [], "staged_items": [], "created_at": "2026-08-26T00:00:00Z", "receipt_hash": H})
    write("core-event.json", {"schema": "core-event/v1", "event_id": "event-1", "core_event_seq": 1, "event_type": "workspace.created", "aggregate_id": "ws-1", "aggregate_revision": 1, "workspace_id": "ws-1", "occurred_at": "2026-08-26T00:00:00Z", "producer": {"producer_type": "core", "producer_id": "core", "release_id": None}, "causation_id": None, "correlation_id": "corr-1", "payload_asset_id": None, "payload_hash": None})
    write("plugin-job-event.json", {"schema": "plugin-job-event/v1", "event_id": "plugin-event-1", "job_id": "job-1", "step_id": "step-1", "attempt_id": "attempt-1", "job_event_seq": 1, "event_type": "plugin.com.plotpilot.fixture.code.started", "plugin_id": "com.plotpilot.fixture.code", "release_id": H, "local_seq": 1, "payload_asset_id": None, "payload_hash": None, "occurred_at": "2026-08-26T00:00:00Z"})
    write("job-event-page.json", {"schema": "job-event-page/v1", "job_id": "job-1", "after_job_event_seq": 0, "events": [], "next_job_event_seq": 1, "high_water_seq": 0})
    write("job-snapshot.json", {"schema": "job-snapshot/v1", "job_id": "job-1", "workspace_id": "ws-1", "job_state": "queued", "job_revision": 1, "steps": [{"step_id": "step-1", "state": "pending", "revision": 1}], "attempts": [{"attempt_id": "attempt-1", "state": "created", "lease_epoch": 1}], "candidate_ids": [], "current_checkpoint_id": None, "stream_high_waters": [], "core_event_high_water": 0, "job_event_high_water": 0, "created_at": "2026-08-26T00:00:00Z", "snapshot_hash": H})
    write("core-snapshot.json", {"schema": "core-snapshot/v1", "snapshot_id": "core-snap-1", "subscription_scope": {"workspace_id": "ws-1", "event_types": ["workspace.created"]}, "core_snapshot_revision": 1, "core_event_high_water": 1, "coverage_complete": True, "covered_aggregates": [], "created_at": "2026-08-26T00:00:00Z", "snapshot_hash": H})
    write("sse-recovery.json", {"schema": "sse-recovery/v1", "stream_kind": "job_event", "aggregate_id": "job-1", "requested_after_seq": 0, "replay_floor_seq": 0, "durable_high_water_seq": 0, "gap": False, "snapshot_required": False, "snapshot_schema": None, "snapshot_revision": None, "snapshot_asset_id": None, "snapshot_hash": None})
    write("checkpoint.json", {"schema": "checkpoint/v1", "checkpoint_id": "checkpoint-1", "checkpoint_seq": 1, "job_id": "job-1", "step_id": "step-1", "source_attempt_id": "attempt-1", "lease_epoch": 1, "run_snapshot_hash": H, "replay_policy": "checkpoint_resume", "completed_units": 1, "total_units": 2, "unit_set_hash": H, "state_asset_id": "asset-checkpoint-1", "created_at": "2026-08-26T00:00:00Z", "checkpoint_hash": H})
    write("stream-prefix.json", {"schema": "stream-prefix/v1", "stream_id": "stream-1", "job_id": "job-1", "step_id": "step-1", "output_role": "draft", "target": {"workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-a"}, "attempt_id": "attempt-1", "lease_epoch": 1, "prefix_seq": 1, "prefix_asset_id": "asset-stream-1", "prefix_hash": H, "byte_length": 5, "encoding": "utf-8"})
    write("plugin-plan.json", {"schema": "plugin-plan/v1", "plan_id": "plan-1", "revision": 1, "name": "Fixture Plan", "description": "A deterministic plan", "bindings": [{"binding_id": "binding-1", "capability_id": "fixture.echo/v1", "plugin_id": "com.plotpilot.fixture.code", "release_requirement": "1.0.0", "order": 10, "enabled": True, "required": True, "propagate_cancel": True, "parameters_asset_id": None}], "data_bindings": [], "skill_preset_revision_id": None, "result_mode": "separate", "synthesizer": None, "model_profile_revision_id": None, "ui_defaults": []})
    write("plugin-generation.json", {"schema": "plugin-generation/v1", "generation_id": "generation-1", "core_api_version": "1.2.0", "members": [], "created_reason": "fixture", "created_at": "2026-08-26T00:00:00Z", "health_result_asset_id": "asset-health-1", "parent_generation_id": None, "base_generation_id": None})
    write("settings-revision.json", {"schema": "settings-revision/v1", "settings_revision_id": "settings-1", "plugin_id": "com.plotpilot.fixture.code", "plugin_release_id": H, "schema_hash": H, "payload_asset_id": "asset-settings-1", "payload_hash": H, "parent_revision_id": None, "migrated_from_revision_id": None, "migration_receipt_hash": None, "validation_receipt_id": None, "validation_receipt_hash": None, "state": "draft", "created_at": "2026-08-26T00:00:00Z"})
    write("settings-validation-receipt.json", {"schema": "settings-validation-receipt/v1", "receipt_id": "settings-receipt-1", "plugin_id": "com.plotpilot.fixture.code", "plugin_release_id": H, "settings_revision_id": "settings-1", "schema_hash": H, "payload_hash": H, "valid": True, "details_asset_id": None, "created_at": "2026-08-26T00:00:00Z", "receipt_hash": H})
    write("settings-migration-manifest.json", {"schema": "settings-migration-manifest/v1", "from_schema_hash": H, "to_schema_hash": "b" * 64, "steps": [{"operation": "rename", "from_pointer": "/old", "to_pointer": "/new", "value_asset_id": None}]})
    write("plugin-lifecycle-transition.json", {"schema": "plugin-lifecycle-transition/v1", "install_operation_id": "install-1", "base_generation_id": None, "base_lkg_generation_id": None, "target_generation_id": "generation-1", "state": "selected", "package_store_status": "staged", "shadow_data_generation_id": None, "target_settings_revision_ids": [], "qualification_id": None, "rollback_attempt": 0, "rollback_token": None, "failure_code": None, "created_at": "2026-08-26T00:00:00Z", "updated_at": "2026-08-26T00:00:00Z"})
    write("release-retirement.json", {"schema": "release-retirement/v1", "release_id": H, "state": "installed", "retire_epoch": 1, "package_present": True, "started_at": None, "completed_at": None})
    write("release-pin.json", {"schema": "release-pin/v1", "pin_id": "pin-1", "release_id": H, "retire_epoch": 1, "pin_kind": "current_generation", "owner_id": "generation-1", "created_at": "2026-08-26T00:00:00Z", "released_at": None})
    tree = {"schema": "plugin-ui-tree/v1", "tree_id": "tree-1", "render_seq": 1, "root": {"component": "button", "key": "run", "props": {"label": "Run", "tone": "primary", "disabled": False}, "children": [], "event_ids": ["click"]}}
    write("plugin-ui-tree.json", tree)
    write("plugin-ui-init.json", {"schema": "plugin-ui-init/v1", "ui_session_id": "ui-session-1", "initial_render_seq": 0, "initial_intent_seq": 0, "contribution_config_asset_id": None})
    freshness = {"generation_id": "generation-1", "plugin_release_id": H, "workspace_id": "ws-1", "workspace_revision_id": None, "plan_revision_id": None}
    write("plugin-ui-event.json", {"schema": "plugin-ui-event/v1", "event_id": "ui-event-1", "event_seq": 1, "render_seq": 1, "action_id": "click", "event_type": "click", "payload_asset_id": None, "freshness": freshness})
    intent = {"schema": "plugin-ui-intent/v1", "intent_id": "intent-ui-1", "intent_seq": 1, "render_seq": 1, "action_id": "click", "event_type": "click", "intent_kind": "set_view_state", "capability_id": None, "payload_asset_id": None, "operation_key": "op-ui-1", "freshness": freshness}
    write("plugin-ui-intent.json", intent)
    write("plugin-ui-message.json", {"schema": "plugin-ui-message/v1", "message_id": "message-1", "direction": "worker_to_host", "message_seq": 1, "message_type": "render", "worker_instance_id": "worker-1", "plugin_release_id": H, "generation_id": "generation-1", "contribution_id": "contribution-1", "slot": "workbench.writing-assets.panel", "workspace_id": "ws-1", "workspace_revision_id": None, "plan_revision_id": None, "body": tree})
    write("skill-manifest.json", {"schema": "plotpilot-skill/v1", "skill_id": "com.plotpilot.fixture.skill", "version": "1.0.0", "display_name": "Fixture Skill", "stage": "draft", "actions": ["rewrite"]})
    receipt = {"schema": "skill-run-receipt/v1", "receipt_id": "skill-receipt-1", "chain_id": "skill-chain-1", "chain_index": 0, "run_snapshot_hash": H, "result_bundle_id": "bundle-golden", "result_item_id": "candidate-item-1", "stream_id": None, "acked_prefix_hash": None, "skill_id": "com.plotpilot.fixture.skill", "release_id": H, "package_hash": H, "parameters_asset_id": None, "input_asset_id": "asset-input-1", "input_hash": H, "output_asset_id": "asset-output-1", "output_hash": "b" * 64, "step_state": "executed", "frozen": True, "participated": True, "model_claimed": False, "verified_patch": False, "claim_evidence_asset_id": None, "patches": [], "warnings": [], "previous_receipt_hash": None, "receipt_hash": ""}
    receipt["receipt_hash"] = digest("skill-run-receipt/v1", receipt, "receipt_hash")
    write("skill-run-receipt.json", receipt)
    chain = {"schema": "skill-chain-result/v1", "chain_id": "skill-chain-1", "run_snapshot_hash": H, "result_bundle_id": "bundle-golden", "result_item_id": "candidate-item-1", "stream_id": None, "acked_prefix_hash": None, "receipt_ids": [receipt["receipt_id"]], "receipt_hashes": [receipt["receipt_hash"]], "chain_status": "succeeded", "input_hash": H, "final_output_asset_id": "asset-output-1", "final_output_hash": "b" * 64, "chain_hash": ""}
    chain["chain_hash"] = hashlib.sha256(b"skill-chain/v1\n" + receipt["receipt_hash"].encode("ascii") + b"\n" + chain["final_output_hash"].encode("ascii") + b"\n").hexdigest()
    write("skill-chain-result.json", chain)
    write("restore-report.json", {"schema": "restore-report/v1", "restore_id": "restore-1", "backup_id": "backup-golden", "source_root_id": "library-old", "target_root_id": "library-new", "state": "restore_ready", "verified_files": ["core/core.db"], "missing_release_ids": [], "projection_rebuild_required": [], "errors": [], "created_at": "2026-08-26T00:00:00Z", "completed_at": "2026-08-26T00:00:00Z"})
    write("rpc-request.json", {"jsonrpc": "2.0", "id": "123e4567-e89b-12d3-a456-426614174000", "method": "host.asset.create/v1", "meta": {"protocol_version": "1", "generation_id": "generation-1", "plugin_release_id": H, "deadline_at": "2026-08-26T00:00:00Z", "context": "attempt", "operation_id": "op-1", "job_id": "job-1", "step_id": "step-1", "attempt_id": "attempt-1", "lease_epoch": 1}, "params": {"operation_key": "upload-op-1", "upload_id": "upload-1", "offset": 0, "mime": "application/json", "total_size": 2, "expected_hash": H, "chunk_hash": H, "base64_chunk": "eA==", "final": False}})
    write("rpc-notification.json", {"jsonrpc": "2.0", "method": "runtime.heartbeat", "meta": {"protocol_version": "1", "generation_id": "generation-1", "plugin_release_id": H, "deadline_at": "2026-08-26T00:00:00Z", "context": "attempt", "operation_id": "op-heartbeat", "job_id": "job-1", "step_id": "step-1", "attempt_id": "attempt-1", "lease_epoch": 1}, "params": {"worker_instance_id": "worker-1", "observed_at": "2026-08-26T00:00:00Z", "local_seq": 1}})
    write("rpc-success.json", {"jsonrpc": "2.0", "id": "123e4567-e89b-12d3-a456-426614174000", "result": {"base64_chunk": "eA==", "next_offset": None, "content_hash": H}})
    write("rpc-error.json", {"jsonrpc": "2.0", "id": "123e4567-e89b-12d3-a456-426614174000", "error": {"code": 1005, "message": "fixture asset error", "data": {"error_id": "error-1", "retryable": False, "details_asset_id": None}}})
    print("generated representative contract fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
