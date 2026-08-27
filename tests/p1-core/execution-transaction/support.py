from __future__ import annotations

import hashlib

from backend.plotpilot_plugin_sdk import canonical_bytes
from backend.plotpilot_plugin_sdk.verifier import hash_without_field


RELEASE = "e" * 64
PACKAGE = "a" * 64


def make_candidate_bundle(stack, *, partial=False, item_status="complete"):
    payload = stack["assets"].put(
        b"new text", mime="text/plain", logical_role="candidate_payload", provenance="plugin:test"
    )
    item = {
        "schema": "candidate-item/v1", "item_id": "item-1", "item_kind": "document",
        "target": {"workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-1"},
        "mutation": {"mode": "replace", "payload_schema": "core/document-text/v1", "payload_hash": payload.sha256},
        "payload_asset_id": payload.asset_id,
        "base": {"revision_id": stack["base"].revision_id, "content_hash": stack["base"].content_hash},
        "write_set": [{"workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-1", "revision_id": stack["base"].revision_id, "content_hash": stack["base"].content_hash}],
        "parent_candidate_ids": [], "source_refs": [], "status": item_status,
    }
    bundle = {
        "schema": "result-bundle/v1", "contract_id": "candidate-batch/v1", "bundle_id": "bundle-1",
        "bundle_type": "candidate_batch",
        "producer": {"plugin_id": "com.plotpilot.demo", "release_id": RELEASE, "capability_id": "writing.chapter.draft/v1", "job_id": "job-1", "step_id": "step-1", "attempt_id": "attempt-1", "lease_epoch": 1},
        "input_snapshot_hash": stack["snapshot"]["snapshot_hash"], "items": [item], "warnings": [],
        "partial": partial, "provenance_receipt_id": "receipt-1", "skill_chain_result_refs": [],
    }
    raw = canonical_bytes(bundle)
    bundle_asset = stack["assets"].put(raw, mime="application/json", logical_role="result_bundle", provenance="plugin:test")
    receipt = {
        "schema": "provenance-receipt/v1", "receipt_id": "receipt-1", "plugin_id": "com.plotpilot.demo",
        "release_id": RELEASE, "package_hash": PACKAGE, "capability_id": "writing.chapter.draft/v1",
        "job_id": "job-1", "step_id": "step-1", "attempt_id": "attempt-1", "lease_epoch": 1,
        "run_snapshot_hash": stack["snapshot"]["snapshot_hash"], "bundle_id": "bundle-1",
        "bundle_hash": hashlib.sha256(raw).hexdigest(), "parent_receipt_ids": [], "model_receipt_ids": [],
        "skill_chain_result_refs": [], "staged_items": ["item-1"], "created_at": "2026-08-28T00:00:00Z",
    }
    receipt["receipt_hash"] = hash_without_field(receipt, "receipt_hash", "provenance-receipt/v1")
    return bundle_asset, receipt, item


def complete_kwargs(bundle_asset, receipt, *, outcome="succeeded"):
    return {
        "job_id": "job-1", "step_id": "step-1", "attempt_id": "attempt-1",
        "lease_epoch": 1, "operation_key": "complete-1", "worker_run_id": "worker-run-1",
        "outcome": outcome, "result_bundle_asset_id": bundle_asset.asset_id,
        "candidate_stage_operation_key": "stage-1", "terminal_detail_asset_id": None,
        "local_seq": 1, "provenance_receipt": receipt,
        "operation_meta": {
            "protocol_version": "1", "generation_id": "generation-1",
            "plugin_release_id": RELEASE, "deadline_at": "2026-08-28T01:00:00Z",
            "context": "attempt", "operation_id": "rpc-complete-1", "job_id": "job-1",
            "step_id": "step-1", "attempt_id": "attempt-1", "lease_epoch": 1,
        },
    }


def bundleless_receipt(receipt):
    value = dict(receipt)
    value["bundle_id"] = None
    value["bundle_hash"] = None
    value["staged_items"] = []
    value["receipt_hash"] = hash_without_field(value, "receipt_hash", "provenance-receipt/v1")
    return value


def authority_rows(repository):
    tables = ("candidate", "publication_receipt", "execution_receipt", "execution_candidate_binding", "execution_publication_binding", "execution_job_event", "execution_core_event", "execution_outcome", "p3_host_operation_ledger")
    return {table: [tuple(row) for row in repository._connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()] for table in tables}
