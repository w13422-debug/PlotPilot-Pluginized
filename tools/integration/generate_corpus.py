"""Generate additive corpus records without rewriting frozen M0 §84.13 bytes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

if __package__:
    from .contract_inventory import contract_file_paths, relative
else:
    from contract_inventory import contract_file_paths, relative


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "contracts" / "corpus"
V2_CORPUS = CORPUS / "m4-m5-public-surface-v2"
MACRO_CORPUS = CORPUS / "macro-planning-host-v1"
MODEL_PROVIDER_CORPUS = CORPUS / "model-provider-rpc-v2"
MODEL_PROVIDER_GOLDEN = ROOT / "contracts" / "golden" / "model-provider-rpc-v2"
CORPUS_V2_MANIFEST = CORPUS / "manifest-v2.json"
FROZEN_V1_MANIFEST_SHA256 = "bcaafab242980366546c34c824256a0396ed370345d70f3e5b1582090a68ce77"
JSON_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_CHECK_MODE = False
_DRIFT: list[str] = []
_EXPECTED_BYTES: dict[Path, bytes] = {}


def dump(path: Path, value: Any) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _EXPECTED_BYTES[path.resolve()] = data
    if _CHECK_MODE:
        if not path.is_file() or path.read_bytes() != data:
            _DRIFT.append(relative(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_bytes() != data:
        path.write_bytes(data)


def _canonical_bytes(value: Any) -> bytes:
    """Canonical bytes for the ASCII-keyed P0B fixture domain."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _hash_jcs(prefix: str, value: Any) -> str:
    return hashlib.sha256(prefix.encode("ascii") + b"\n" + _canonical_bytes(value)).hexdigest()


def load_frozen_m0_groups() -> list[dict[str, Any]]:
    """Read the immutable M0 groups in numeric order and fail on inventory drift."""

    groups: list[dict[str, Any]] = []
    for index in range(1, 15):
        path = CORPUS / "negative" / "84.13" / f"{index:02d}.json"
        group = json.loads(path.read_text(encoding="utf-8"))
        expected_id = f"84.13-{index:02d}"
        if group.get("group_id") != expected_id:
            raise ValueError(f"frozen M0 corpus identity drift: {path} != {expected_id}")
        groups.append(group)
    return groups


def v2_groups() -> list[dict[str, Any]]:
    """Return the executable metadata for the additive M4/M5 corpus.

    The corpus deliberately stores mutations and semantic assertions rather
    than a second validator implementation.  The existing Python/TypeScript
    gates resolve the fixture IDs and perform the actual checks.
    """

    return [
        {
            "schema": "m4-m5-public-surface-corpus/v2",
            "group_id": "m4-m5-v2-01-candidate",
            "title": "Candidate closed shape, Workspace bindings and lineage",
            "positive": [
                "candidate.record",
                "candidate.text_patch",
                "candidate.replace",
                "candidate.structure_patch",
                "candidate.relation_patch",
                "candidate.list_result",
                "candidate.cross_workspace_source_ref",
                "candidate.get_result",
                "candidate.preview_result",
            ],
            "negative": [
                {"case_id": "v2-candidate-closed-extra-field", "fixture": "candidate.record", "kind": "closed_schema", "mutation": {"op": "set", "path": ["unexpected"], "value": True}, "expected": "closed_schema_rejection"},
                {"case_id": "v2-candidate-target-cross-workspace", "fixture": "candidate.record", "kind": "candidate_semantics", "mutation": {"op": "set", "path": ["target", "workspace_id"], "value": "ws-other"}, "expected": "cross_workspace"},
                {"case_id": "v2-candidate-write-set-cross-workspace", "fixture": "candidate.record", "kind": "candidate_semantics", "mutation": {"op": "set", "path": ["write_set", 0, "workspace_id"], "value": "ws-other"}, "expected": "cross_workspace"},
                {"case_id": "v2-candidate-write-set-duplicate-full-identity", "fixture": "candidate.record", "kind": "candidate_semantics", "mutation": {"op": "set", "path": ["write_set", 1, "entity_id"], "value": "doc-a"}, "expected": "duplicate_entity_identity"},
                {"case_id": "v2-candidate-write-set-unstable-order", "fixture": "candidate.record", "kind": "candidate_semantics", "mutation": {"op": "set", "path": ["write_set", 0, "entity_id"], "value": "doc-z"}, "expected": "stable_entity_order"},
                {"case_id": "v2-candidate-target-write-set-mismatch", "fixture": "candidate.record", "kind": "candidate_semantics", "mutation": {"op": "set", "path": ["write_set", 0, "entity_id"], "value": "doc-other"}, "expected": "target_write_set_binding"},
                {"case_id": "v2-candidate-base-hash-mismatch", "fixture": "candidate.record", "kind": "candidate_semantics", "mutation": {"op": "set", "path": ["base", "content_hash"], "value": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}, "expected": "base_write_set_binding"},
                {"case_id": "v2-candidate-failed-query-surface", "fixture": "candidate.record", "kind": "closed_schema", "mutation": {"op": "set", "path": ["status"], "value": "failed"}, "expected": "staging_outcome_rejection"},
                {"case_id": "v2-candidate-skipped-review-surface", "fixture": "candidate.list_result", "kind": "closed_schema", "mutation": {"op": "set", "path": ["items", 0, "status"], "value": "skipped"}, "expected": "staging_outcome_rejection"},
                {"case_id": "v2-candidate-parent-cycle", "fixture": "candidate.record", "kind": "candidate_parent_cycle", "mutation": {"op": "cycle"}, "expected": "parent_cycle"},
                {"case_id": "v2-candidate-preview-invalid-base64", "fixture": "candidate.preview_result", "kind": "candidate_preview", "mutation": {"op": "set", "path": ["base64_chunk"], "value": "not-base64"}, "expected": "preview_bytes"},
                {"case_id": "v2-candidate-preview-length-mismatch", "fixture": "candidate.preview_result", "kind": "candidate_preview", "mutation": {"op": "set", "path": ["length"], "value": 4}, "expected": "preview_bytes"},
                {"case_id": "v2-candidate-preview-content-hash-mismatch", "fixture": "candidate.preview_result", "kind": "candidate_preview", "mutation": {"op": "set", "path": ["content_hash"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "preview_bytes"},
                {"case_id": "v2-candidate-preview-next-offset-gap", "fixture": "candidate.preview_result", "kind": "candidate_preview", "mutation": {"op": "set", "path": ["next_offset"], "value": 4}, "expected": "preview_range"},
                {"case_id": "v2-candidate-preview-payload-hash-mismatch", "fixture": "candidate.preview_result", "kind": "candidate_preview", "mutation": {"op": "set", "path": ["payload_hash"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "preview_payload_binding"},
            ],
        },
        {
            "schema": "m4-m5-public-surface-corpus/v2",
            "group_id": "m4-m5-v2-02-review-publication",
            "title": "Review is read/review only and Publication is Core-only",
            "positive": [
                "review.query",
                "review.command",
                "review.result",
                "publication.command.complete",
                "publication.result.complete",
                "publication.command.replace",
                "publication.result.replace",
                "publication.command.structure_patch",
                "publication.result.structure_patch",
                "publication.command.relation_patch",
                "publication.result.relation_patch",
                "publication.command.incomplete_stream",
                "publication.result.incomplete_stream",
            ],
            "negative": [
                {"case_id": "v2-review-closed-extra-field", "fixture": "review.command", "kind": "closed_schema", "mutation": {"op": "set", "path": ["unexpected"], "value": True}, "expected": "closed_schema_rejection"},
                {"case_id": "v2-publication-normal-partial", "fixture": "publication.command.partial", "kind": "publication_semantics", "mutation": {"op": "noop"}, "expected": "candidate_not_publishable"},
                {"case_id": "v2-publication-cross-workspace", "fixture": "publication.command.complete", "kind": "publication_semantics", "mutation": {"op": "set", "path": ["workspace_id"], "value": "ws-other"}, "expected": "cross_workspace"},
                {"case_id": "v2-publication-operation-key-reuse", "fixture": "publication.command.complete", "kind": "operation_key_reuse", "mutation": {"op": "set", "path": ["candidate_id"], "value": "candidate-other"}, "expected": "duplicate_operation"},
                {"case_id": "v2-publication-result-binding", "fixture": "publication.result.complete", "kind": "publication_semantics", "mutation": {"op": "set", "path": ["entity_id"], "value": "doc-other"}, "expected": "publication_binding"},
                {"case_id": "v2-review-http-candidate-binding", "fixture": "http.candidate.review", "route_id": "candidate.review", "kind": "http_exchange", "mutation": {"op": "set", "path": ["response", "candidate_id"], "value": "candidate-other"}, "expected": "exchange_binding"},
                {"case_id": "v2-publication-http-payload-binding", "fixture": "http.publication.accept", "route_id": "publication.accept", "kind": "http_exchange", "mutation": {"op": "set", "path": ["response", "content_hash"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "publication_binding"},
                {"case_id": "v2-publication-cas-final-hash-binding", "fixture": "publication.result.complete", "kind": "publication_semantics", "mutation": {"op": "set", "path": ["cas", "content_hash"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "cas_revision_binding"},
                {"case_id": "v2-publication-cas-base-write-set-binding", "fixture": "publication.result.complete", "kind": "publication_semantics", "mutation": {"op": "set", "path": ["cas", "base_content_hash"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "cas_write_set_binding"},
                {"case_id": "v2-publication-cas-non-target-revision-binding", "fixture": "candidate.record", "kind": "publication_write_set", "mutation": {"op": "set", "path": ["write_set", 1, "revision_id"], "value": "rev-non-target-stale"}, "expected": "cas_write_set_binding"},
                {"case_id": "v2-publication-cas-non-target-content-hash-binding", "fixture": "candidate.record", "kind": "publication_write_set", "mutation": {"op": "set", "path": ["write_set", 1, "content_hash"], "value": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}, "expected": "cas_write_set_binding"},
                {"case_id": "v2-publication-ledger-response-drift", "fixture": "http.publication.accept", "route_id": "publication.accept", "kind": "operation_response_drift", "mutation": {"op": "set", "path": ["response", "idempotent"], "value": True}, "expected": "duplicate_operation"},
            ],
        },
        {
            "schema": "m4-m5-public-surface-corpus/v2",
            "group_id": "m4-m5-v2-03-story-state",
            "title": "Story-State is a closed Core-derived projection",
            "positive": ["story_state.projection"],
            "negative": [
                {"case_id": "v2-projection-closed-extra-field", "fixture": "story_state.projection", "kind": "closed_schema", "mutation": {"op": "set", "path": ["unexpected"], "value": True}, "expected": "closed_schema_rejection"},
                {"case_id": "v2-projection-missing-receipt-closure", "fixture": "story_state.projection", "kind": "projection_semantics", "mutation": {"op": "set", "path": ["receipt_closure"], "value": []}, "expected": "provenance_closure"},
                {"case_id": "v2-projection-publication-candidate-mismatch", "fixture": "story_state.projection", "kind": "projection_semantics", "mutation": {"op": "set", "path": ["publication", "candidate_id"], "value": "candidate-other"}, "expected": "projection_binding"},
                {"case_id": "v2-projection-cross-workspace-asset", "fixture": "story_state.projection", "kind": "projection_semantics", "mutation": {"op": "set", "path": ["assets", 0, "sha256"], "value": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}, "expected": "asset_closure"},
                {"case_id": "v2-projection-duplicate-asset-identity", "fixture": "story_state.projection", "kind": "projection_semantics", "mutation": {"op": "set", "path": ["assets", 1, "asset_id"], "value": "asset-candidate-v2"}, "expected": "asset_identity_uniqueness"},
                {"case_id": "v2-projection-extra-unreachable-receipt", "fixture": "story_state.projection_with_unreachable_receipt", "kind": "projection_semantics", "mutation": {"op": "noop"}, "expected": "receipt_reachability"},
                {"case_id": "v2-projection-duplicate-receipt-identity", "fixture": "story_state.projection", "kind": "projection_semantics", "mutation": {"op": "set", "path": ["receipt_closure", 1, "receipt_id"], "value": "receipt-parent-v2"}, "expected": "receipt_identity_uniqueness"},
                {"case_id": "v2-projection-missing-parent-receipt", "fixture": "story_state.projection", "kind": "projection_semantics", "mutation": {"op": "delete", "path": ["receipt_closure", 0]}, "expected": "receipt_parent_closure"},
                {"case_id": "v2-projection-tampered-receipt-hash", "fixture": "story_state.projection", "kind": "projection_semantics", "mutation": {"op": "set", "path": ["receipt_closure", 1, "receipt_hash"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "receipt_content_hash"},
            ],
        },
        {
            "schema": "m4-m5-public-surface-corpus/v2",
            "group_id": "m4-m5-v2-04-job-sse",
            "title": "Durable Job snapshots and disjoint SSE cursor recovery",
            "positive": ["job.snapshot", "job.list_result", "job.command.start", "job.event_page", "job.sse.replay", "job.sse.gap"],
            "negative": [
                {"case_id": "v2-job-closed-extra-field", "fixture": "job.snapshot", "kind": "closed_schema", "mutation": {"op": "set", "path": ["unexpected"], "value": True}, "expected": "closed_schema_rejection"},
                {"case_id": "v2-job-cursor-domain-mix", "fixture": "job.event_page", "kind": "job_cursor", "mutation": {"op": "set", "path": ["next_cursor"], "value": "candidate/cursor-9"}, "expected": "cursor_domain_mismatch"},
                {"case_id": "v2-job-cursor-ahead", "fixture": "job.sse.replay", "kind": "job_cursor", "mutation": {"op": "set", "path": ["requested_after_seq"], "value": 9}, "expected": "cursor_ahead"},
                {"case_id": "v2-job-sse-gap-without-snapshot", "fixture": "job.sse.gap", "kind": "job_sse", "mutation": {"op": "set", "path": ["snapshot_required"], "value": False}, "expected": "sse_recovery_required"},
                {"case_id": "v2-job-event-high-water-regression", "fixture": "job.event_page", "kind": "job_cursor", "mutation": {"op": "set", "path": ["high_water_seq"], "value": 0}, "expected": "cursor_regression"},
                {"case_id": "v2-job-next-cursor-ahead", "fixture": "job.event_page", "kind": "job_cursor", "mutation": {"op": "set", "path": ["next_cursor"], "value": "job/job-v2/999"}, "expected": "cursor_ahead"},
                {"case_id": "v2-job-event-payload-pair", "fixture": "job.event_page", "kind": "job_cursor", "mutation": {"op": "set", "path": ["events", 0, "payload_hash"], "value": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}, "expected": "event_payload_binding"},
                {"case_id": "v2-job-event-query-cursor-pair", "fixture": "http.job.events", "route_id": "job.events", "kind": "http_request", "mutation": {"op": "set", "path": ["request", "after_cursor"], "value": "job/job-v2/1"}, "expected": "cursor_pair"},
                {"case_id": "v2-job-sse-query-cursor-pair", "fixture": "http.job.sse-recovery", "route_id": "job.sse-recovery", "kind": "http_request", "mutation": {"op": "set", "path": ["request", "after_seq"], "value": 0}, "expected": "cursor_pair"},
                {"case_id": "v2-job-sse-snapshot-workspace-binding", "fixture": "job.sse.gap", "kind": "job_sse", "mutation": {"op": "set", "path": ["snapshot", "workspace_id"], "value": "ws-other"}, "expected": "snapshot_binding"},
                {"case_id": "v2-job-sse-snapshot-cursor-binding", "fixture": "job.sse.gap", "kind": "job_sse", "mutation": {"op": "set", "path": ["snapshot_cursor"], "value": "job/job-v2/3"}, "expected": "snapshot_binding"},
                {"case_id": "v2-job-sse-gap-tail-starts-after-snapshot", "fixture": "job.sse.gap", "kind": "job_sse", "mutation": {"op": "set", "path": ["tail", 0, "job_event_seq"], "value": 4}, "expected": "sse_snapshot_baseline"},
                {"case_id": "v2-job-sse-gap-tail-continuity", "fixture": "job.sse.gap", "kind": "job_sse", "mutation": {"op": "set", "path": ["tail", 1, "job_event_seq"], "value": 7}, "expected": "sse_tail_continuity"},
                {"case_id": "v2-job-sse-gap-tail-must-reach-high-water", "fixture": "job.sse.gap", "kind": "job_sse", "mutation": {"op": "set", "path": ["tail"], "value": []}, "expected": "sse_high_water_convergence"},
                {"case_id": "v2-job-command-snapshot-domain", "fixture": "http.job.start", "route_id": "job.start", "kind": "http_exchange", "mutation": {"op": "set", "path": ["response", "snapshot_cursor"], "value": "candidate/candidate-v2"}, "expected": "cursor_domain_mismatch"},
            ],
        },
        {
            "schema": "m4-m5-public-surface-corpus/v2",
            "group_id": "m4-m5-v2-05-plugin-lifecycle",
            "title": "Plugin generation lifecycle CAS and publication isolation",
            "positive": ["plugin.discovery", "plugin.discovery_error", "plugin.lifecycle.install", "plugin.lifecycle.upgrade", "plugin.lifecycle.retire", "plugin.lifecycle.rollback", "plugin.lifecycle.result.install", "plugin.lifecycle.result.upgrade", "plugin.lifecycle.result.retire", "plugin.lifecycle.result.rollback"],
            "negative": [
                {"case_id": "v2-plugin-closed-extra-field", "fixture": "plugin.lifecycle.install", "kind": "closed_schema", "mutation": {"op": "set", "path": ["unexpected"], "value": True}, "expected": "closed_schema_rejection"},
                {"case_id": "v2-plugin-generation-race", "fixture": "plugin.lifecycle.upgrade", "kind": "plugin_lifecycle", "mutation": {"op": "set", "path": ["expected_generation_id"], "value": "generation-stale"}, "expected": "generation_conflict"},
                {"case_id": "v2-plugin-retire-active-job", "fixture": "plugin.lifecycle.retire", "kind": "plugin_lifecycle", "mutation": {"op": "set", "path": ["target_generation_id"], "value": "generation-active-job"}, "expected": "active_job"},
                {"case_id": "v2-plugin-publication-forbidden", "fixture": "plugin.lifecycle.install", "kind": "plugin_publication", "mutation": {"op": "set", "path": ["action"], "value": "publish"}, "expected": "publication_forbidden"},
                {"case_id": "v2-plugin-operation-key-reuse", "fixture": "plugin.lifecycle.upgrade", "kind": "operation_key_reuse", "mutation": {"op": "set", "path": ["package_hash"], "value": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}, "expected": "duplicate_operation"},
                {"case_id": "v2-plugin-install-missing-release", "fixture": "plugin.lifecycle.install", "kind": "plugin_lifecycle", "mutation": {"op": "set", "path": ["release_id"], "value": None}, "expected": "release_missing"},
                {"case_id": "v2-plugin-http-action-binding", "fixture": "http.plugin.install", "route_id": "plugin.install", "kind": "http_exchange", "mutation": {"op": "set", "path": ["request", "action"], "value": "upgrade"}, "expected": "route_binding"},
                {"case_id": "v2-plugin-http-generation-binding", "fixture": "http.plugin.upgrade", "route_id": "plugin.upgrade", "kind": "http_exchange", "mutation": {"op": "set", "path": ["response", "generation_id"], "value": "generation-other"}, "expected": "generation_binding"},
                {"case_id": "v2-plugin-http-response-id-binding", "fixture": "http.plugin.upgrade", "route_id": "plugin.upgrade", "kind": "http_exchange", "mutation": {"op": "set", "path": ["response", "plugin_id"], "value": "com.plotpilot.other"}, "expected": "exchange_binding"},
                {"case_id": "v2-plugin-discovery-cursor-error-schema", "fixture": "http.plugin.discovery.error", "route_id": "plugin.discovery", "kind": "http_exchange", "mutation": {"op": "set", "path": ["response", "schema"], "value": "core-http-error/v2"}, "expected": "plugin_http_error_binding"},
                {"case_id": "v2-plugin-discovery-cursor-request-domain", "fixture": "http.plugin.discovery.error", "route_id": "plugin.discovery", "kind": "http_request", "mutation": {"op": "set", "path": ["request", "cursor"], "value": "candidate/ws-1/1"}, "expected": "cursor_domain_mismatch"},
            ],
        },
    ]


def model_provider_golden() -> dict[str, Any]:
    """Return the one deterministic P0B Provider/receipt bridge fixture."""

    planner_context = {
        "operation_key": "execute-op-1",
        "workspace_id": "ws-1",
        "plugin_id": "com.plotpilot.prompt-skill",
        "plugin_release_id": "a" * 64,
        "plugin_package_hash": "b" * 64,
        "generation_id": "generation-1",
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "lease_epoch": 1,
        "chain_id": "chain-1",
        "chain_asset_id": "asset-chain-1",
        "chain_content_hash": "1" * 64,
        "run_snapshot_id": "snapshot-1",
        "run_snapshot_asset_id": "asset-snapshot-1",
        "run_snapshot_hash": "b" * 64,
        "model_profile_revision_id": "model-profile-rev-1",
        "input_asset_id": "asset-input-1",
        "input_content_hash": "c" * 64,
    }
    model_request_payload = {
        "schema": "model-provider-request-asset/v1",
        "profile_revision_id": "model-profile-rev-1",
        "messages": [{"role": "user", "content": "Create the macro plan."}],
        "options": {"temperature": 0, "stream": False},
    }
    model_request_bytes = _canonical_bytes(model_request_payload)
    model_request_asset = {
        "asset_id": "asset-model-request-1",
        "content_hash": hashlib.sha256(model_request_bytes).hexdigest(),
    }
    provider_body = {
        "model": "planner-model-1",
        "messages": model_request_payload["messages"],
        "temperature": 0,
        "stream": False,
    }
    provider_request_hash = _hash_jcs(
        "openai-compatible-request/v1",
        {
            "endpoint": "https://models.example.test/v1",
            "model": "planner-model-1",
            "body": provider_body,
            "profile_revision_id": "model-profile-rev-1",
            "provider_plugin_id": "com.plotpilot.provider.openai-compatible",
            "provider_release_id": "9" * 64,
        },
    )
    response_payload = {
        "schema": "model-provider-response-asset/v1",
        "text": "Setting, bible, and outline.",
    }
    response_asset_bytes = _canonical_bytes(response_payload)
    response_asset = {
        "asset_id": "asset-model-response-1",
        "content_hash": hashlib.sha256(response_asset_bytes).hexdigest(),
    }
    response_hashes = {
        "receipted": _hash_jcs(
            "model-response/v1",
            {
                "text": "Setting, bible, and outline.",
                "chunks": [],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                    "cost": "0.0025",
                },
                "termination": "response",
                "finish_reason": "stop",
                "response_id": "provider-response-1",
            },
        ),
        "failed": _hash_jcs(
            "model-response/v1",
            {
                "status_code": 503,
                "body": {"error": {"message": "provider unavailable"}},
            },
        ),
        "cancelled": None,
        "uncertain": _hash_jcs(
            "model-response/v1",
            {"partial": "sent without a verifiable response Asset"},
        ),
    }

    receipt_context = {
        "job_id": planner_context["job_id"],
        "step_id": planner_context["step_id"],
        "attempt_id": planner_context["attempt_id"],
        "lease_epoch": planner_context["lease_epoch"],
        "run_snapshot_hash": planner_context["run_snapshot_hash"],
        "generation_id": planner_context["generation_id"],
        "chain_id": planner_context["chain_id"],
        "chain_index": 0,
        "skill_id": "skill.prompt",
        "release_id": "c" * 64,
        "package_hash": "d" * 64,
        "parameters_asset_id": None,
        "parameters_hash": None,
        "input_asset_id": planner_context["input_asset_id"],
        "input_hash": planner_context["input_content_hash"],
        "workspace_id": planner_context["workspace_id"],
        "plugin_id": planner_context["plugin_id"],
        "plugin_release_id": planner_context["plugin_release_id"],
        "plugin_package_hash": planner_context["plugin_package_hash"],
        "operation_key": planner_context["operation_key"],
        "run_snapshot_id": planner_context["run_snapshot_id"],
        "run_snapshot_asset_id": planner_context["run_snapshot_asset_id"],
        "chain_asset_id": planner_context["chain_asset_id"],
        "chain_content_hash": planner_context["chain_content_hash"],
    }

    terminal_receipts: dict[str, dict[str, Any]] = {}
    receipt_asset_identities: dict[str, dict[str, str]] = {}
    receipt_asset_bytes_hex: dict[str, str] = {}
    terminal_results: dict[str, dict[str, Any]] = {}
    terminal_successes: dict[str, dict[str, Any]] = {}
    request_id = "123e4567-e89b-42d3-a456-426614174100"
    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "model.provider.invoke/v1",
        "meta": {
            "protocol_version": "1",
            "context": "attempt",
            "operation_id": planner_context["operation_key"],
            "generation_id": planner_context["generation_id"],
            "job_id": planner_context["job_id"],
            "step_id": planner_context["step_id"],
            "attempt_id": planner_context["attempt_id"],
            "lease_epoch": planner_context["lease_epoch"],
        },
        "params": {
            "schema": "model-provider-invoke-request/v2",
            "planner_context": planner_context,
            "model_request_asset": model_request_asset,
        },
    }

    terminal_configuration = {
        "receipted": {
            "response_asset": response_asset,
            "response_asset_id": response_asset["asset_id"],
            "error": None,
            "uncertain": False,
            "lifecycle": ["prepared", "sent", "receipted"],
            "stream_termination": "response",
        },
        "failed": {
            "response_asset": None,
            "response_asset_id": None,
            "error": "provider unavailable",
            "uncertain": False,
            "lifecycle": ["prepared", "sent", "failed"],
            "stream_termination": "http_error",
        },
        "cancelled": {
            "response_asset": None,
            "response_asset_id": None,
            "error": "cancelled",
            "uncertain": False,
            "lifecycle": ["prepared", "cancelled"],
            "stream_termination": "cancelled",
        },
        "uncertain": {
            "response_asset": None,
            "response_asset_id": None,
            "error": "sent without terminal acknowledgement",
            "uncertain": True,
            "lifecycle": ["prepared", "sent", "uncertain"],
            "stream_termination": "transport_unknown",
        },
    }
    for state, configuration in terminal_configuration.items():
        receipted = state == "receipted"
        receipt = {
            "schema": "model-receipt/v1",
            "receipt_id": f"model-receipt-{state}-1",
            "invocation_id": "provider-invocation-1",
            "invocation_key": "provider-invocation-key-1",
            "state": state,
            "request_hash": provider_request_hash,
            "response_hash": response_hashes[state],
            "profile_revision_id": "model-profile-rev-1",
            "provider_plugin_id": "com.plotpilot.provider.openai-compatible",
            "provider_release_id": "9" * 64,
            "endpoint": "https://models.example.test/v1",
            "model": "planner-model-1",
            "lifecycle": configuration["lifecycle"],
            "prompt_tokens": 11 if receipted else None,
            "completion_tokens": 7 if receipted else None,
            "total_tokens": 18 if receipted else None,
            "cost": "0.0025" if receipted else None,
            "retry_count": 0,
            "stream_termination": configuration["stream_termination"],
            "error": configuration["error"],
            "input_context": receipt_context,
            "profile_revision": {
                "profile_revision_id": "model-profile-rev-1",
                "provider_plugin_id": "com.plotpilot.provider.openai-compatible",
                "provider_release_id": "9" * 64,
                "endpoint": "https://models.example.test/v1",
                "model_name": "planner-model-1",
            },
            "metadata": {"fixture": "p0b", "terminal_state": state},
            "response_asset_id": configuration["response_asset_id"],
            "recovered": False,
            "uncertain": configuration["uncertain"],
            "receipt_hash": "",
        }
        receipt["receipt_hash"] = _hash_jcs(
            "model-receipt/v1",
            {key: value for key, value in receipt.items() if key != "receipt_hash"},
        )
        receipt_bytes = _canonical_bytes(receipt)
        receipt_asset = {
            "asset_id": f"asset-model-receipt-{state}-1",
            "content_hash": hashlib.sha256(receipt_bytes).hexdigest(),
        }
        anchor = {
            "receipt_id": receipt["receipt_id"],
            **receipt_asset,
            "receipt_hash": receipt["receipt_hash"],
            **planner_context,
        }
        result = {
            "schema": "model-provider-invoke-result/v2",
            "model_receipt_anchor": anchor,
            "model_request_asset": model_request_asset,
            "provider_transport_request_hash": provider_request_hash,
            "response_asset": configuration["response_asset"],
            "provider_transport_response_hash": response_hashes[state],
            "provider_terminal_state": state,
        }
        terminal_receipts[state] = receipt
        receipt_asset_identities[state] = receipt_asset
        receipt_asset_bytes_hex[state] = receipt_bytes.hex()
        terminal_results[state] = result
        terminal_successes[state] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }

    receipted = terminal_results["receipted"]
    distinct_hashes = {
        model_request_asset["content_hash"],
        receipted["model_receipt_anchor"]["content_hash"],
        receipted["provider_transport_request_hash"],
        receipted["provider_transport_response_hash"],
        receipted["response_asset"]["content_hash"],
        receipted["model_receipt_anchor"]["receipt_hash"],
    }
    if len(distinct_hashes) != 6:
        raise ValueError("P0B golden does not separate all six hash domains")

    bridge_projection = {
        "method": request["method"],
        "request": request,
        "terminal_results": terminal_results,
        "canonical_receipt_identities": {
            state: {
                field: receipt[field]
                for field in (
                    "receipt_id",
                    "receipt_hash",
                    "state",
                    "request_hash",
                    "response_hash",
                    "response_asset_id",
                    "profile_revision_id",
                    "provider_plugin_id",
                    "provider_release_id",
                )
            }
            for state, receipt in terminal_receipts.items()
        },
        "prompt_skill_anchor": terminal_results["receipted"][
            "model_receipt_anchor"
        ],
    }
    operation_bytes = _canonical_bytes(request["params"])
    return {
        "schema": "model-provider-rpc-golden/v2",
        "request": request,
        "result": terminal_results["receipted"],
        "success": terminal_successes["receipted"],
        "error": {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": 1001,
                "message": "Provider endpoint unavailable before a terminal receipt formed",
                "data": {
                    "error_id": "model-provider-error-1",
                    "retryable": True,
                    "details_asset_id": None,
                },
            },
        },
        "terminal_results": terminal_results,
        "terminal_successes": terminal_successes,
        "canonical_receipts": terminal_receipts,
        "receipt_asset_identities": receipt_asset_identities,
        "receipt_asset_bytes_hex": receipt_asset_bytes_hex,
        "model_request_asset_bytes_hex": model_request_bytes.hex(),
        "response_asset_bytes_hex": response_asset_bytes.hex(),
        "operation_canonical_bytes_hex": operation_bytes.hex(),
        "operation_digest": _hash_jcs("model-provider-invoke/v2", request["params"]),
        "bridge_projection": bridge_projection,
        "bridge_digest": _hash_jcs(
            "model-provider-rpc-bridge/v2", bridge_projection
        ),
    }


def model_provider_groups(golden: dict[str, Any]) -> list[dict[str, Any]]:
    """Executable P0B negatives shared by Python and Node gates."""

    cases: list[dict[str, Any]] = []

    def add(
        case_id: str,
        fixture: str,
        kind: str,
        mutation: dict[str, Any],
        expected: str,
    ) -> None:
        cases.append(
            {
                "case_id": case_id,
                "fixture": fixture,
                "kind": kind,
                "mutation": mutation,
                "expected": expected,
            }
        )

    set_value = lambda path, value: {"op": "set", "path": path, "value": value}
    delete = lambda path: {"op": "delete", "path": path}
    noop = {"op": "noop"}

    add("provider-request-extra-field", "invoke.request", "request", set_value(["unexpected"], True), "closed_schema")
    add("provider-request-missing-id", "invoke.request", "request", delete(["id"]), "request_id")
    add("provider-request-invalid-id", "invoke.request", "request", set_value(["id"], "not-a-uuid"), "request_id")
    add("provider-request-wrong-method", "invoke.request", "request", set_value(["method"], "host.model.invoke/v1"), "reserved_method")
    add("provider-request-wrong-discriminator", "invoke.request", "request", set_value(["params", "schema"], "model-provider-invoke-request/v1"), "discriminator")
    add("provider-request-extra-params-field", "invoke.request", "request", set_value(["params", "unexpected"], True), "closed_schema")
    add("provider-request-extra-planner-field", "invoke.request", "request", set_value(["params", "planner_context", "unexpected"], True), "closed_schema")
    add("provider-request-missing-planner-field", "invoke.request", "request", delete(["params", "planner_context", "chain_id"]), "closed_schema")
    add("provider-request-partial-asset", "invoke.request", "request", delete(["params", "model_request_asset", "content_hash"]), "asset_pair")
    add("provider-request-unsafe-lease", "invoke.request", "request", set_value(["params", "planner_context", "lease_epoch"], JSON_MAX_SAFE_INTEGER + 1), "safe_integer")
    add("provider-meta-extra-field", "invoke.request", "request", set_value(["meta", "unexpected"], True), "closed_schema")
    add("provider-meta-wrong-context", "invoke.request", "request", set_value(["meta", "context"], "control"), "meta_binding")
    add("provider-meta-wrong-protocol", "invoke.request", "request", set_value(["meta", "protocol_version"], "2"), "meta_binding")
    for meta_field, replacement in (
        ("operation_id", "execute-op-other"),
        ("generation_id", "generation-other"),
        ("job_id", "job-other"),
        ("step_id", "step-other"),
        ("attempt_id", "attempt-other"),
        ("lease_epoch", 2),
    ):
        add(
            f"provider-meta-{meta_field.replace('_', '-')}-drift",
            "invoke.request",
            "request",
            set_value(["meta", meta_field], replacement),
            "meta_binding",
        )

    add("provider-result-extra-field", "invoke.result.receipted", "result", set_value(["unexpected"], True), "closed_schema")
    add("provider-result-wrong-discriminator", "invoke.result.receipted", "result", set_value(["schema"], "model-provider-invoke-result/v1"), "discriminator")
    add("provider-result-extra-anchor-field", "invoke.result.receipted", "result", set_value(["model_receipt_anchor", "unexpected"], True), "closed_schema")
    add("provider-result-missing-anchor-receipt-id", "invoke.result.receipted", "result", delete(["model_receipt_anchor", "receipt_id"]), "closed_schema")
    add("provider-result-response-asset-id-only", "invoke.result.receipted", "result", delete(["response_asset", "content_hash"]), "asset_pair")
    add("provider-result-response-asset-hash-only", "invoke.result.receipted", "result", delete(["response_asset", "asset_id"]), "asset_pair")
    add("provider-result-invalid-terminal-state", "invoke.result.receipted", "result", set_value(["provider_terminal_state"], "succeeded"), "terminal_state")
    add("provider-success-extra-field", "invoke.success.receipted", "response", set_value(["unexpected"], True), "closed_schema")
    add("provider-success-id-mismatch", "invoke.success.receipted", "response", set_value(["id"], "123e4567-e89b-42d3-a456-426614174101"), "response_id")
    add("provider-success-missing-id", "invoke.success.receipted", "response", delete(["id"]), "response_id")
    add("provider-error-id-mismatch", "invoke.error", "response", set_value(["id"], "123e4567-e89b-42d3-a456-426614174101"), "response_id")
    add("provider-error-null-id-after-parse", "invoke.error", "response", set_value(["id"], None), "response_id")
    add("provider-response-mixed-result-error", "invoke.success.receipted", "response", set_value(["error"], golden["error"]["error"]), "envelope_exactly_one")
    add("provider-envelope-bare-result", "invoke.bare-result", "envelope", noop, "envelope_required")
    add("provider-envelope-request-result-mix", "invoke.request", "envelope", set_value(["result"], golden["result"]), "envelope_exactly_one")
    add("provider-success-without-receipt-authority", "invoke.success.receipted", "response-no-evidence", noop, "canonical_receipt_required")
    add("provider-error-with-terminal-evidence", "invoke.error", "error-with-evidence", noop, "rpc_error_without_receipt")

    anchor = golden["result"]["model_receipt_anchor"]
    context_hash_fields = {
        "plugin_release_id",
        "plugin_package_hash",
        "chain_content_hash",
        "run_snapshot_hash",
        "input_content_hash",
    }
    for index, field in enumerate(
        (
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
        ),
        start=2,
    ):
        if field == "lease_epoch":
            replacement: Any = 2
        elif field in context_hash_fields:
            replacement = f"{index % 10}" * 64
            if replacement == anchor[field]:
                replacement = "f" * 64
        else:
            replacement = f"{field.replace('_', '-')}-other"
        add(
            f"provider-anchor-{field.replace('_', '-')}-drift",
            "invoke.result.receipted",
            "result",
            set_value(["model_receipt_anchor", field], replacement),
            "planner_context_binding",
        )

    add("provider-result-model-request-asset-id-drift", "invoke.result.receipted", "result", set_value(["model_request_asset", "asset_id"], "asset-model-request-other"), "request_asset_binding")
    add("provider-result-model-request-asset-hash-drift", "invoke.result.receipted", "result", set_value(["model_request_asset", "content_hash"], "e" * 64), "request_asset_binding")
    add("provider-request-asset-authority-hash-substitution", "invoke.exchange.receipted", "exchange", {"op": "set-many", "changes": [{"path": ["request", "params", "model_request_asset", "content_hash"], "value": golden["result"]["provider_transport_request_hash"]}, {"path": ["response", "result", "model_request_asset", "content_hash"], "value": golden["result"]["provider_transport_request_hash"]}]}, "asset_content_hash")
    add("provider-receipt-asset-content-hash-substitution", "invoke.result.receipted", "result", set_value(["model_receipt_anchor", "content_hash"], golden["result"]["model_receipt_anchor"]["receipt_hash"]), "receipt_asset_hash")
    add("provider-transport-request-host-hash-substitution", "invoke.result.receipted", "result", set_value(["provider_transport_request_hash"], golden["request"]["params"]["model_request_asset"]["content_hash"]), "transport_request_hash")
    add("provider-transport-response-asset-hash-substitution", "invoke.result.receipted", "result", set_value(["provider_transport_response_hash"], golden["result"]["response_asset"]["content_hash"]), "transport_response_hash")
    add("provider-response-asset-transport-hash-substitution", "invoke.result.receipted", "result", set_value(["response_asset", "content_hash"], golden["result"]["provider_transport_response_hash"]), "asset_content_hash")
    add("provider-receipt-self-hash-asset-hash-substitution", "invoke.result.receipted", "result", set_value(["model_receipt_anchor", "receipt_hash"], golden["result"]["model_receipt_anchor"]["content_hash"]), "receipt_self_hash")
    add("provider-receipt-id-mirror-drift", "invoke.result.receipted", "result", set_value(["model_receipt_anchor", "receipt_id"], "model-receipt-other"), "canonical_receipt_binding")
    add("provider-terminal-state-mirror-drift", "invoke.result.receipted", "result", set_value(["provider_terminal_state"], "failed"), "canonical_receipt_binding")
    add("provider-profile-revision-mirror-drift", "invoke.result.receipted", "result", set_value(["model_receipt_anchor", "model_profile_revision_id"], "model-profile-rev-other"), "canonical_receipt_binding")
    add("provider-response-asset-id-mirror-drift", "invoke.result.receipted", "result", set_value(["response_asset", "asset_id"], "asset-model-response-other"), "canonical_receipt_binding")
    add("provider-null-response-asset-with-receipt-id", "invoke.result.receipted", "result", set_value(["response_asset"], None), "canonical_receipt_binding")
    add("provider-nonnull-response-asset-with-null-receipt-id", "invoke.result.failed", "result", set_value(["response_asset"], golden["result"]["response_asset"]), "canonical_receipt_binding")
    add("provider-ledger-request-drift", "invoke.ledger", "ledger", {"op": "request-drift"}, "exact_replay")
    add("provider-ledger-result-drift", "invoke.ledger", "ledger", {"op": "result-drift"}, "exact_replay")
    add("provider-ledger-unknown-replay", "invoke.ledger", "ledger", {"op": "unknown-replay"}, "exact_replay")
    add("provider-reserved-plan-binding", "reserved.plan.binding", "reserved", noop, "reserved_boundary")
    add("provider-reserved-plan-synthesizer", "reserved.plan.synthesizer", "reserved", noop, "reserved_boundary")
    add("provider-reserved-capability-descriptor", "reserved.descriptor", "reserved", noop, "reserved_boundary")
    add("provider-reserved-plugin-manifest", "reserved.manifest", "reserved", noop, "reserved_boundary")

    return [
        {
            "schema": "model-provider-rpc-corpus/v2",
            "group_id": "model-provider-rpc-v2-01",
            "title": "Provider overlay, receipt authority, hash domains and reserved boundary",
            "positive": [
                "invoke.request",
                "invoke.result.receipted",
                "invoke.result.failed",
                "invoke.result.cancelled",
                "invoke.result.uncertain",
                "invoke.success.receipted",
                "invoke.success.failed",
                "invoke.success.cancelled",
                "invoke.success.uncertain",
                "invoke.error",
                "ordinary.plan",
                "ordinary.descriptor",
            ],
            "negative": cases,
        }
    ]


def macro_planning_groups() -> list[dict[str, Any]]:
    """Executable P0A negatives shared by the Python and Node gates."""

    return [
        {
            "schema": "macro-planning-host-corpus/v1",
            "group_id": "macro-planning-host-v1-01",
            "title": "Host planning identity, CAS, hash and secret boundaries",
            "positive": [
                "model_secret_put_command",
                "model_secret_put_result",
                "model_profile_revise_command",
                "model_profile_revision",
                "model_profile_revise_result",
                "workspace_plan_selection_command",
                "workspace_plan_selection_result",
                "project_planning_query",
                "project_planning_availability_ready",
                "project_planning_availability_unavailable",
                "project_planning_start_command",
                "project_planning_start_result",
                "project_planner_runtime_input",
                "project_planner_model_output",
            ],
            "negative": [
                {"case_id": "macro-unknown-field-secret-command", "fixture": "model_secret_put_command", "kind": "parse", "mutation": {"op": "set", "path": ["unexpected"], "value": True}, "expected": "closed_schema_rejection"},
                {"case_id": "macro-wrong-discriminator", "fixture": "project_planner_model_output", "kind": "parse", "mutation": {"op": "set", "path": ["schema"], "value": "project-planner-model-output/v2"}, "expected": "discriminator_rejection"},
                {"case_id": "macro-raw-secret-ref-profile-command", "fixture": "model_profile_revise_command", "kind": "parse", "mutation": {"op": "set", "path": ["provider", "api_key_ref"], "value": "sk-live-not-a-reference"}, "expected": "opaque_secret_reference"},
                {"case_id": "macro-alias-secret-ref-profile-command", "fixture": "model_profile_revise_command", "kind": "parse", "mutation": {"op": "set", "path": ["provider", "api_key_ref"], "value": "provider-main"}, "expected": "opaque_secret_reference"},
                {"case_id": "macro-whitespace-secret-ref-profile-command", "fixture": "model_profile_revise_command", "kind": "parse", "mutation": {"op": "set", "path": ["provider", "api_key_ref"], "value": " secret://provider-main"}, "expected": "opaque_secret_reference"},
                {"case_id": "macro-malformed-secret-ref-profile-command", "fixture": "model_profile_revise_command", "kind": "parse", "mutation": {"op": "set", "path": ["provider", "api_key_ref"], "value": "secret:///provider-main"}, "expected": "opaque_secret_reference"},
                {"case_id": "macro-wrapped-secret-result-leak", "fixture": "secret_success_exchange", "kind": "secret_exchange", "mutation": {"op": "set", "path": ["response", "api_key_ref"], "value": "secret://provider-main/rawSecret47A"}, "expected": "structural_secret_response_provenance"},
                {"case_id": "macro-wrapped-secret-error-leak", "fixture": "secret_error_exchange", "kind": "secret_exchange", "mutation": {"op": "set", "path": ["response", "message"], "value": "rejected:rawSecret47A:wrapped"}, "expected": "fixed_error_mapping"},
                {"case_id": "macro-missing-trusted-path-params", "fixture": "http.model-secret.put", "kind": "http_exchange", "mutation": {"op": "set", "path": ["path_params"], "value": None}, "expected": "trusted_path_required"},
                {"case_id": "macro-extra-trusted-path-param", "fixture": "http.model-secret.put", "kind": "http_exchange", "mutation": {"op": "set", "path": ["path_params", "extra"], "value": "forbidden"}, "expected": "trusted_path_exact"},
                {"case_id": "macro-path-body-mismatch", "fixture": "http.model-secret.put", "kind": "http_exchange", "mutation": {"op": "set", "path": ["path_params", "secret_id"], "value": "provider-other"}, "expected": "trusted_path_binding"},
                {"case_id": "macro-cross-workspace-response", "fixture": "http.project-planning.start", "kind": "http_exchange", "mutation": {"op": "set", "path": ["response", "workspace_id"], "value": "workspace-other"}, "expected": "cross_workspace"},
                {"case_id": "macro-stale-brief-revision-cas", "fixture": "project_planning_start_command", "kind": "project_brief_cas", "mutation": {"op": "set", "path": ["expected_project_brief", "revision_id"], "value": "revision-project-brief-stale"}, "expected": "stale_brief_cas"},
                {"case_id": "macro-stale-brief-hash-cas", "fixture": "project_planning_start_command", "kind": "project_brief_cas", "mutation": {"op": "set", "path": ["expected_project_brief", "content_hash"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "stale_brief_cas"},
                {"case_id": "macro-stale-workspace-plan-cas", "fixture": "workspace_plan_selection_command", "kind": "plan_cas", "mutation": {"op": "set", "path": ["expected_workspace_revision"], "value": 6}, "expected": "stale_plan_cas"},
                {"case_id": "macro-plan-hash-tamper", "fixture": "plan_selection_exchange", "kind": "plan_exchange", "mutation": {"op": "set", "path": ["command", "plan_revision_hash"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "plan_hash_binding"},
                {"case_id": "macro-profile-hash-tamper", "fixture": "model_profile_revision", "kind": "model_profile", "mutation": {"op": "set", "path": ["revision_hash"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "profile_hash_binding"},
                {"case_id": "macro-runtime-input-hash-tamper", "fixture": "project_planner_runtime_input", "kind": "runtime_input", "mutation": {"op": "set", "path": ["input_hash"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "runtime_hash_binding"},
                {"case_id": "macro-caller-job-authority-injection", "fixture": "project_planning_start_command", "kind": "parse", "mutation": {"op": "set", "path": ["job_id"], "value": "caller-job"}, "expected": "caller_authority_rejection"},
                {"case_id": "macro-caller-generation-authority-injection", "fixture": "project_planning_start_command", "kind": "parse", "mutation": {"op": "set", "path": ["generation_id"], "value": "caller-generation"}, "expected": "caller_authority_rejection"},
                {"case_id": "macro-legacy-planner-input", "fixture": "project_planner_runtime_input", "kind": "runtime_input", "mutation": {"op": "set", "path": ["schema"], "value": "project-planner-runtime-input/v1"}, "expected": "legacy_input_rejection"},
                {"case_id": "macro-runtime-attempt-authority-injection", "fixture": "project_planner_runtime_input", "kind": "runtime_input", "mutation": {"op": "set", "path": ["attempt_id"], "value": "caller-attempt"}, "expected": "caller_authority_rejection"},
                {"case_id": "macro-empty-model-output", "fixture": "project_planner_model_output", "kind": "model_output", "mutation": {"op": "set", "path": ["setting"], "value": "   "}, "expected": "nonblank_output"},
                {"case_id": "macro-output-auto-accept-injection", "fixture": "project_planner_model_output", "kind": "model_output", "mutation": {"op": "set", "path": ["auto_accept"], "value": True}, "expected": "manual_publication_only"},
                {"case_id": "macro-availability-fail-open", "fixture": "project_planning_availability_unavailable", "kind": "availability", "mutation": {"op": "set", "path": ["available"], "value": True}, "expected": "fail_closed_availability"},
                {"case_id": "macro-profile-parent-missing", "fixture": "model_profile_revision", "kind": "model_profile", "mutation": {"op": "set", "path": ["revision_number"], "value": 2}, "expected": "append_only_parent"},
                {"case_id": "macro-automatic-plan-selection", "fixture": "workspace_plan_selection_command", "kind": "parse", "mutation": {"op": "set", "path": ["selection_mode"], "value": "automatic"}, "expected": "explicit_selection_only"},
                {"case_id": "macro-planning-result-operation-mismatch", "fixture": "planning_start_exchange", "kind": "planning_start_exchange", "mutation": {"op": "set", "path": ["result", "operation_key"], "value": "planning-operation-other"}, "expected": "operation_binding"},
                {"case_id": "macro-error-path-identity-mismatch", "fixture": "http.model-profile.revise.error", "kind": "http_exchange", "mutation": {"op": "set", "path": ["response", "profile_id"], "value": "model-profile-other"}, "expected": "trusted_path_binding"},
                {"case_id": "macro-unsafe-profile-revision-number", "fixture": "model_profile_revision", "kind": "model_profile", "mutation": {"op": "set", "path": ["revision_number"], "value": 9_007_199_254_740_992}, "expected": "json_safe_integer"},
                {"case_id": "macro-unsafe-plan-expected-workspace-revision", "fixture": "workspace_plan_selection_command", "kind": "parse", "mutation": {"op": "set", "path": ["expected_workspace_revision"], "value": 9_007_199_254_740_992}, "expected": "json_safe_integer"},
                {"case_id": "macro-unsafe-plan-workspace-revision-result", "fixture": "workspace_plan_selection_result", "kind": "parse", "mutation": {"op": "set", "path": ["workspace_revision"], "value": 9_007_199_254_740_992}, "expected": "json_safe_integer"},
                {"case_id": "macro-unsafe-planning-start-writer-epoch", "fixture": "project_planning_start_result", "kind": "parse", "mutation": {"op": "set", "path": ["writer_epoch"], "value": 9_007_199_254_740_992}, "expected": "json_safe_integer"},
                {"case_id": "macro-unsafe-runtime-input-writer-epoch", "fixture": "project_planner_runtime_input", "kind": "runtime_input", "mutation": {"op": "set", "path": ["writer_epoch"], "value": 9_007_199_254_740_992}, "expected": "json_safe_integer"},
                {"case_id": "macro-wire-whitespace-endpoint-u0085", "fixture": "model_profile_revise_command", "kind": "parse", "mutation": {"op": "set", "path": ["provider", "endpoint"], "value": "https://models.example.test/v1\u0085suffix"}, "expected": "wire_whitespace"},
                {"case_id": "macro-wire-whitespace-endpoint-ufeff", "fixture": "model_profile_revise_command", "kind": "parse", "mutation": {"op": "set", "path": ["provider", "endpoint"], "value": "https://models.example.test/v1\ufeffsuffix"}, "expected": "wire_whitespace"},
                {"case_id": "macro-wire-whitespace-endpoint-u00a0", "fixture": "model_profile_revise_command", "kind": "parse", "mutation": {"op": "set", "path": ["provider", "endpoint"], "value": "https://models.example.test/v1\u00a0suffix"}, "expected": "wire_whitespace"},
                {"case_id": "macro-wire-whitespace-model-name-u0085", "fixture": "model_profile_revise_command", "kind": "parse", "mutation": {"op": "set", "path": ["provider", "model_name"], "value": "\u0085planner-model-1"}, "expected": "wire_whitespace"},
                {"case_id": "macro-wire-whitespace-model-name-ufeff", "fixture": "model_profile_revise_command", "kind": "parse", "mutation": {"op": "set", "path": ["provider", "model_name"], "value": "\ufeffplanner-model-1"}, "expected": "wire_whitespace"},
                {"case_id": "macro-wire-whitespace-model-name-u00a0", "fixture": "model_profile_revise_command", "kind": "parse", "mutation": {"op": "set", "path": ["provider", "model_name"], "value": "\u00a0planner-model-1"}, "expected": "wire_whitespace"},
                {"case_id": "macro-wire-whitespace-error-message-u0085", "fixture": "model_profile_error", "kind": "parse", "mutation": {"op": "set", "path": ["message"], "value": "\u0085"}, "expected": "wire_nonblank"},
                {"case_id": "macro-wire-whitespace-error-message-ufeff", "fixture": "model_profile_error", "kind": "parse", "mutation": {"op": "set", "path": ["message"], "value": "\ufeff"}, "expected": "wire_nonblank"},
                {"case_id": "macro-wire-whitespace-error-message-u00a0", "fixture": "model_profile_error", "kind": "parse", "mutation": {"op": "set", "path": ["message"], "value": "\u00a0"}, "expected": "wire_nonblank"},
                {"case_id": "macro-wire-whitespace-output-setting-u0085", "fixture": "project_planner_model_output", "kind": "model_output", "mutation": {"op": "set", "path": ["setting"], "value": "\u0085"}, "expected": "wire_nonblank"},
                {"case_id": "macro-wire-whitespace-output-bible-ufeff", "fixture": "project_planner_model_output", "kind": "model_output", "mutation": {"op": "set", "path": ["bible"], "value": "\ufeff"}, "expected": "wire_nonblank"},
                {"case_id": "macro-wire-whitespace-output-outline-u00a0", "fixture": "project_planner_model_output", "kind": "model_output", "mutation": {"op": "set", "path": ["outline"], "value": "\u00a0"}, "expected": "wire_nonblank"},
            ],
        }
    ]


def macro_integer_representations() -> dict[str, Any]:
    """Return the one raw-token source for P0A mathematical integers."""

    fields = [
        {"field_id": "model-profile-revision-number", "fixture": "model_profile_revision", "path": ["revision_number"], "minimum": 1, "maximum": JSON_MAX_SAFE_INTEGER},
        {"field_id": "plan-expected-workspace-revision", "fixture": "workspace_plan_selection_command", "path": ["expected_workspace_revision"], "minimum": 0, "maximum": JSON_MAX_SAFE_INTEGER},
        {"field_id": "plan-result-workspace-revision", "fixture": "workspace_plan_selection_result", "path": ["workspace_revision"], "minimum": 1, "maximum": JSON_MAX_SAFE_INTEGER},
        {"field_id": "planning-start-writer-epoch", "fixture": "project_planning_start_result", "path": ["writer_epoch"], "minimum": 1, "maximum": JSON_MAX_SAFE_INTEGER},
        {"field_id": "planner-runtime-writer-epoch", "fixture": "project_planner_runtime_input", "path": ["writer_epoch"], "minimum": 1, "maximum": JSON_MAX_SAFE_INTEGER},
        {"field_id": "model-option-max-output-tokens", "fixture": "model_profile_revision", "path": ["provider", "options", "max_output_tokens"], "minimum": 1, "maximum": 10_000_000},
        {"field_id": "model-option-timeout-seconds", "fixture": "model_profile_revision", "path": ["provider", "options", "timeout_seconds"], "minimum": 1, "maximum": 86_400},
        {"field_id": "model-option-max-retries", "fixture": "model_profile_revision", "path": ["provider", "options", "max_retries"], "minimum": 0, "maximum": 16},
    ]

    def accept(
        case_id: str,
        field_id: str,
        fixture: str,
        path: list[str],
        raw_token: str,
        normalized: int,
        *,
        equivalence_group: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "case_id": case_id,
            "field_id": field_id,
            "fixture": fixture,
            "path": path,
            "raw_token": raw_token,
            "expected": "accept",
            "normalized": normalized,
        }
        if equivalence_group is not None:
            result["hash_equivalence_group"] = equivalence_group
        return result

    def reject(
        case_id: str,
        field_id: str,
        fixture: str,
        path: list[str],
        raw_token: str,
    ) -> dict[str, Any]:
        return {
            "case_id": case_id,
            "field_id": field_id,
            "fixture": fixture,
            "path": path,
            "raw_token": raw_token,
            "expected": "reject",
        }

    profile_revision = ["revision_number"]
    profile_result_revision = ["revision", "revision_number"]
    runtime_epoch = ["writer_epoch"]
    max_output_tokens = ["provider", "options", "max_output_tokens"]
    timeout_seconds = ["provider", "options", "timeout_seconds"]
    max_retries = ["provider", "options", "max_retries"]
    vectors = [
        accept("profile-revision-one-integer", "model-profile-revision-number", "model_profile_revision", profile_revision, "1", 1, equivalence_group="profile-revision-one"),
        accept("profile-revision-one-decimal", "model-profile-revision-number", "model_profile_revision", profile_revision, "1.0", 1, equivalence_group="profile-revision-one"),
        accept("profile-revision-one-exponent", "model-profile-revision-number", "model_profile_revision", profile_revision, "1e0", 1, equivalence_group="profile-revision-one"),
        accept("profile-result-revision-one-decimal", "model-profile-revision-number", "model_profile_revise_result", profile_result_revision, "1.0", 1, equivalence_group="profile-revision-one"),
        accept("runtime-writer-max-integer", "planner-runtime-writer-epoch", "project_planner_runtime_input", runtime_epoch, "9007199254740991", JSON_MAX_SAFE_INTEGER, equivalence_group="runtime-writer-max"),
        accept("runtime-writer-max-decimal", "planner-runtime-writer-epoch", "project_planner_runtime_input", runtime_epoch, "9007199254740991.0", JSON_MAX_SAFE_INTEGER, equivalence_group="runtime-writer-max"),
        accept("plan-expected-zero-integer", "plan-expected-workspace-revision", "workspace_plan_selection_command", ["expected_workspace_revision"], "0", 0),
        accept("plan-expected-zero-decimal", "plan-expected-workspace-revision", "workspace_plan_selection_command", ["expected_workspace_revision"], "0.0", 0),
        accept("plan-result-minimum-decimal", "plan-result-workspace-revision", "workspace_plan_selection_result", ["workspace_revision"], "1.0", 1),
        accept("planning-start-writer-minimum-exponent", "planning-start-writer-epoch", "project_planning_start_result", ["writer_epoch"], "1e0", 1),
        accept("profile-max-output-4096-integer", "model-option-max-output-tokens", "model_profile_revision", max_output_tokens, "4096", 4096, equivalence_group="profile-max-output-4096"),
        accept("profile-max-output-4096-decimal", "model-option-max-output-tokens", "model_profile_revision", max_output_tokens, "4096.0", 4096, equivalence_group="profile-max-output-4096"),
        accept("profile-timeout-120-integer", "model-option-timeout-seconds", "model_profile_revision", timeout_seconds, "120", 120, equivalence_group="profile-timeout-120"),
        accept("profile-timeout-120-exponent", "model-option-timeout-seconds", "model_profile_revision", timeout_seconds, "1.2e2", 120, equivalence_group="profile-timeout-120"),
        accept("profile-max-retries-zero-integer", "model-option-max-retries", "model_profile_revision", max_retries, "0", 0, equivalence_group="profile-max-retries-zero"),
        accept("profile-max-retries-zero-decimal", "model-option-max-retries", "model_profile_revision", max_retries, "0.0", 0, equivalence_group="profile-max-retries-zero"),
        accept("command-max-output-minimum-exponent", "model-option-max-output-tokens", "model_profile_revise_command", max_output_tokens, "1e0", 1),
        accept("command-timeout-maximum-decimal", "model-option-timeout-seconds", "model_profile_revise_command", timeout_seconds, "86400.0", 86_400),
        accept("command-max-retries-maximum-exponent", "model-option-max-retries", "model_profile_revise_command", max_retries, "16e0", 16),
        reject("profile-revision-bool", "model-profile-revision-number", "model_profile_revision", profile_revision, "true"),
        reject("profile-revision-non-integral", "model-profile-revision-number", "model_profile_revision", profile_revision, "1.5"),
        reject("profile-revision-non-finite", "model-profile-revision-number", "model_profile_revision", profile_revision, "1e999"),
        reject("profile-revision-overflow-integer", "model-profile-revision-number", "model_profile_revision", profile_revision, "9007199254740992"),
        reject("profile-revision-overflow-decimal", "model-profile-revision-number", "model_profile_revision", profile_revision, "9007199254740992.0"),
        reject("plan-expected-below-minimum", "plan-expected-workspace-revision", "workspace_plan_selection_command", ["expected_workspace_revision"], "-1"),
        reject("plan-result-below-minimum-decimal", "plan-result-workspace-revision", "workspace_plan_selection_result", ["workspace_revision"], "0.0"),
        reject("planning-start-writer-non-integral", "planning-start-writer-epoch", "project_planning_start_result", ["writer_epoch"], "1.25"),
        reject("runtime-writer-overflow-decimal", "planner-runtime-writer-epoch", "project_planner_runtime_input", runtime_epoch, "9007199254740992.0"),
        reject("profile-max-output-below-minimum", "model-option-max-output-tokens", "model_profile_revision", max_output_tokens, "0"),
        reject("profile-max-output-overflow-decimal", "model-option-max-output-tokens", "model_profile_revision", max_output_tokens, "10000001.0"),
        reject("profile-timeout-below-minimum", "model-option-timeout-seconds", "model_profile_revision", timeout_seconds, "0e0"),
        reject("profile-timeout-overflow", "model-option-timeout-seconds", "model_profile_revision", timeout_seconds, "86401"),
        reject("profile-max-retries-below-minimum", "model-option-max-retries", "model_profile_revision", max_retries, "-1.0"),
        reject("profile-max-retries-overflow", "model-option-max-retries", "model_profile_revision", max_retries, "17e0"),
    ]
    accepted = sum(vector["expected"] == "accept" for vector in vectors)
    return {
        "schema": "macro-planning-integer-representations/v1",
        "json_schema_dialect": "https://json-schema.org/draft/2020-12/schema",
        "semantic_authority": "mathematical-json-integer",
        "field_count": len(fields),
        "fields": fields,
        "vector_count": len(vectors),
        "accepted_count": accepted,
        "rejected_count": len(vectors) - accepted,
        "hash_equivalence_groups": sorted(
            {
                vector["hash_equivalence_group"]
                for vector in vectors
                if "hash_equivalence_group" in vector
            }
        ),
        "vectors": vectors,
    }


def write_v2_corpus() -> None:
    groups = v2_groups()
    for group in groups:
        dump(V2_CORPUS / f"{group['group_id'].removeprefix('m4-m5-v2-')}.json", group)
    dump(
        V2_CORPUS / "manifest.json",
        {
            "schema": "m4-m5-public-surface-corpus-manifest/v2",
            "group_ids": [group["group_id"] for group in groups],
            "group_count": len(groups),
            "negative_case_count": sum(len(group["negative"]) for group in groups),
            "golden_root": "contracts/golden/m4-m5-public-surface-v2",
            "semantic_assertions": [
                "closed_schema_rejection",
                "cross_workspace",
                "target_write_set_binding",
                "base_write_set_binding",
                "duplicate_entity_identity",
                "stable_entity_order",
                "parent_cycle",
                "preview_bytes",
                "preview_range",
                "preview_payload_binding",
                "candidate_not_publishable",
                "staging_outcome_rejection",
                "exchange_binding",
                "publication_binding",
                "cas_revision_binding",
                "cas_write_set_binding",
                "duplicate_operation",
                "provenance_closure",
                "asset_identity_uniqueness",
                "receipt_identity_uniqueness",
                "receipt_parent_closure",
                "receipt_content_hash",
                "receipt_reachability",
                "operation_key_reuse",
                "cursor_domain_mismatch",
                "cursor_pair",
                "cursor_ahead",
                "event_payload_binding",
                "snapshot_binding",
                "sse_recovery_required",
                "sse_snapshot_baseline",
                "sse_tail_continuity",
                "sse_high_water_convergence",
                "generation_conflict",
                "generation_binding",
                "route_binding",
                "release_missing",
                "publication_forbidden",
                "plugin_http_error_binding",
            ],
        },
    )


def write_model_provider_corpus() -> None:
    golden = model_provider_golden()
    groups = model_provider_groups(golden)
    if len(groups) != 1:
        raise ValueError("P0B corpus must contain exactly one group")

    invoke_path = MODEL_PROVIDER_GOLDEN / "invoke.json"
    group_path = MODEL_PROVIDER_CORPUS / "01-invoke.json"
    dump(invoke_path, golden)
    dump(group_path, groups[0])
    invoke_bytes = _EXPECTED_BYTES[invoke_path.resolve()]
    group_bytes = _EXPECTED_BYTES[group_path.resolve()]
    case_ids = [case["case_id"] for case in groups[0]["negative"]]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("P0B corpus case IDs must be unique")
    case_digest = hashlib.sha256(
        json.dumps(
            sorted(case_ids), ensure_ascii=True, separators=(",", ":")
        ).encode("ascii")
    ).hexdigest()
    dump(
        MODEL_PROVIDER_CORPUS / "manifest.json",
        {
            "schema": "model-provider-rpc-corpus-manifest/v2",
            "group_ids": [groups[0]["group_id"]],
            "group_count": 1,
            "negative_case_count": len(case_ids),
            "negative_case_digest": case_digest,
            "positive_fixture_count": len(groups[0]["positive"]),
            "golden": "contracts/golden/model-provider-rpc-v2/invoke.json",
            "expected": "contracts/golden/model-provider-rpc-v2/expected.json",
            "semantic_assertions": sorted(
                {case["expected"] for case in groups[0]["negative"]}
            ),
        },
    )
    matrix_path = (
        ROOT
        / "contracts"
        / "json-schema"
        / "model-provider-rpc-method-matrix.v2.json"
    )
    dump(
        MODEL_PROVIDER_GOLDEN / "expected.json",
        {
            "schema": "model-provider-rpc-golden-expected/v2",
            "fixture_files": {
                "invoke.json": hashlib.sha256(invoke_bytes).hexdigest()
            },
            "corpus_files": {
                "01-invoke.json": hashlib.sha256(group_bytes).hexdigest()
            },
            "method_matrix_sha256": hashlib.sha256(
                matrix_path.read_bytes()
            ).hexdigest(),
            "terminal_states": [
                "receipted",
                "failed",
                "cancelled",
                "uncertain",
            ],
            "terminal_states_use_jsonrpc_success": True,
            "planner_context_field_count": 19,
            "model_receipt_anchor_field_count": 23,
            "canonical_receipt_field_count": 27,
            "hash_domains": [
                "model_request_asset.content_hash",
                "model_receipt_anchor.content_hash",
                "provider_transport_request_hash",
                "provider_transport_response_hash",
                "response_asset.content_hash",
                "model_receipt_anchor.receipt_hash",
            ],
            "operation_canonical_bytes_hex": golden[
                "operation_canonical_bytes_hex"
            ],
            "operation_digest": golden["operation_digest"],
            "bridge_digest": golden["bridge_digest"],
            "negative_case_count": len(case_ids),
            "negative_case_digest": case_digest,
        },
    )


def write_macro_planning_corpus() -> None:
    groups = macro_planning_groups()
    integer_representations = macro_integer_representations()
    integer_path = MACRO_CORPUS / "integer-representations.json"
    for group in groups:
        dump(MACRO_CORPUS / "negative.json", group)
    dump(integer_path, integer_representations)
    integer_bytes = _EXPECTED_BYTES[integer_path.resolve()]
    dump(
        MACRO_CORPUS / "manifest.json",
        {
            "schema": "macro-planning-host-corpus-manifest/v1",
            "group_ids": [group["group_id"] for group in groups],
            "group_count": len(groups),
            "negative_case_count": sum(len(group["negative"]) for group in groups),
            "positive_fixture": "contracts/examples/fixtures/macro-planning-host-positive.json",
            "golden": "contracts/golden/macro-planning-host-v1/positive.json",
            "integer_representations": {
                "path": "contracts/corpus/macro-planning-host-v1/integer-representations.json",
                "sha256": hashlib.sha256(integer_bytes).hexdigest(),
                "field_count": integer_representations["field_count"],
                "vector_count": integer_representations["vector_count"],
                "accepted_count": integer_representations["accepted_count"],
                "rejected_count": integer_representations["rejected_count"],
            },
            "semantic_assertions": [
                "closed_schema_rejection",
                "discriminator_rejection",
                "opaque_secret_reference",
                "structural_secret_response_provenance",
                "fixed_error_mapping",
                "trusted_path_required",
                "trusted_path_exact",
                "trusted_path_binding",
                "cross_workspace",
                "stale_brief_cas",
                "stale_plan_cas",
                "plan_hash_binding",
                "profile_hash_binding",
                "runtime_hash_binding",
                "caller_authority_rejection",
                "legacy_input_rejection",
                "nonblank_output",
                "manual_publication_only",
                "fail_closed_availability",
                "append_only_parent",
                "explicit_selection_only",
                "operation_binding",
                "json_safe_integer",
                "wire_whitespace",
                "wire_nonblank",
                "mathematical_json_integer_normalization",
            ],
        },
    )


def _expected_or_current_bytes(path: Path) -> bytes:
    expected = _EXPECTED_BYTES.get(path.resolve())
    if expected is not None:
        return expected
    if not path.is_file():
        raise FileNotFoundError(relative(path))
    return path.read_bytes()


def write_corpus_v2_manifest() -> None:
    frozen_v1 = CORPUS / "manifest.json"
    frozen_bytes = _expected_or_current_bytes(frozen_v1)
    frozen_hash = hashlib.sha256(frozen_bytes).hexdigest()
    if frozen_hash != FROZEN_V1_MANIFEST_SHA256:
        raise ValueError("frozen corpus/manifest.json bytes drifted")

    paths = [
        path
        for path in contract_file_paths("v2")
        if path.is_relative_to(CORPUS) and path.resolve() != CORPUS_V2_MANIFEST.resolve()
    ]
    records = []
    for path in paths:
        data = _expected_or_current_bytes(path)
        records.append(
            {
                "path": path.relative_to(CORPUS).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    records.sort(key=lambda item: item["path"].encode("utf-8"))
    route_paths = [
        "m4-m5-public-surface-v2/manifest.json",
        "prompt-skill-rpc-v2/manifest.json",
        "macro-planning-host-v1/manifest.json",
        "model-provider-rpc-v2/manifest.json",
    ]
    routes = []
    for route_path in route_paths:
        path = CORPUS / route_path
        data = _expected_or_current_bytes(path)
        routes.append(
            {
                "manifest": route_path,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    dump(
        CORPUS_V2_MANIFEST,
        {
            "schema": "contract-corpus/v2",
            "frozen_v1": {
                "manifest": "manifest.json",
                "sha256": frozen_hash,
            },
            "routes": routes,
            "file_count_excluding_router": len(records),
            "router_self_excluded": True,
            "files": records,
        },
    )


def main() -> int:
    global _CHECK_MODE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    _CHECK_MODE = args.check
    _DRIFT.clear()
    _EXPECTED_BYTES.clear()
    groups = load_frozen_m0_groups()
    path_cases = {
        "schema": "windows-path-corpus/v1",
        "valid": ["plugin.json", "data/rules.json", "目录/规则.json"],
        "invalid": ["", "/absolute", "C:/drive", "a\\b", "a//b", "a/../b", "CON.txt", "foo.txt.", "foo.txt ", "a:b", "a|b", "a\u0000b", "a" * 241],
    }
    dump(CORPUS / "paths" / "windows-paths.json", path_cases)
    dump(CORPUS / "compatibility" / "valid.json", {"core_api": ">=1.0 <2.0", "plugin_rpc": "1", "ui_host": "1", "python": "3.12.*"})
    dump(CORPUS / "compatibility" / "invalid.json", [{"core_api": ">=1.0 <2.0 || >=2.0 <3.0", "plugin_rpc": "1", "ui_host": "1", "python": "3.12.*"}, {"core_api": ">=01.0 <2.0", "plugin_rpc": "1", "ui_host": "1", "python": "3.12.*"}, {"core_api": ">=1.0 <2.0", "plugin_rpc": "1", "ui_host": "1", "python": "3.12"}])
    raw_history = bytes.fromhex("7b226b223a312c2278223a22726177227d0a")
    dump(CORPUS / "history" / "v1-raw.json", {"schema": "history-fixture/v1", "raw_asset_bytes_hex": raw_history.hex(), "raw_sha256": hashlib.sha256(raw_history).hexdigest()})
    publication_cases = {
        "schema": "contract-publication-negative-corpus/v1",
        "positive_root": "contracts/golden/contract-publication-v1",
        "cases": [
            {"case_id": "core-authority-extra-field", "validator": "core_authority", "fixture": "core-http.json#/authority_payloads/0", "mutation": {"op": "set", "path": ["unexpected"], "value": True}, "expected": "closed_schema_rejection"},
            {"case_id": "core-workspace-update-noop", "validator": "core_authority", "fixture": "core-http.json#/authority_payloads/23", "mutation": {"op": "set", "path": ["title"], "value": None}, "expected": "semantic_rejection"},
            {"case_id": "core-revision-two-targets", "validator": "core_authority", "fixture": "core-http.json#/authority_payloads/4", "mutation": {"op": "set", "path": ["node_id"], "value": "node-a"}, "expected": "semantic_rejection"},
            {"case_id": "core-cross-workspace", "validator": "core_authority_expected_workspace", "fixture": "core-http.json#/authority_payloads/1", "mutation": {"op": "set", "path": ["workspace_id"], "value": "ws-other"}, "expected": "workspace_rejection"},
            {"case_id": "publication-cross-workspace", "validator": "publication_expected_workspace", "fixture": "core-http.json#/publication/result", "mutation": {"op": "set", "path": ["workspace_id"], "value": "ws-other"}, "expected": "workspace_rejection"},
            {"case_id": "publication-revision-target-mismatch", "validator": "publication", "fixture": "core-http.json#/publication/result", "mutation": {"op": "set", "path": ["resulting_revision", "entity_id"], "value": "doc-other"}, "expected": "identity_rejection"},
            {"case_id": "publication-command-result-candidate-mismatch", "validator": "publication_command_binding", "fixture": "core-http.json#/publication/result", "mutation": {"op": "set", "path": ["candidate_id"], "value": "candidate-other"}, "expected": "command_binding_rejection"},
            {"case_id": "asset-range-self-hash-tamper", "validator": "asset", "fixture": "core-http.json#/assets/range", "mutation": {"op": "set", "path": ["content_hash"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "hash_rejection"},
            {"case_id": "asset-partial-range-self-hash-tamper", "validator": "asset", "fixture": "core-http.json#/assets/partial", "mutation": {"op": "set", "path": ["content_hash"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "hash_rejection"},
            {"case_id": "asset-range-next-offset-tamper", "validator": "asset", "fixture": "core-http.json#/assets/range", "mutation": {"op": "set", "path": ["next_offset"], "value": 5}, "expected": "range_rejection"},
            {"case_id": "asset-metadata-range-identity-mismatch", "validator": "asset_pair", "fixture": "core-http.json#/assets/range", "mutation": {"op": "set", "path": ["asset_id"], "value": "asset-other"}, "expected": "asset_binding_rejection"},
            {"case_id": "asset-metadata-range-hash-mismatch", "validator": "asset_pair_metadata", "fixture": "core-http.json#/assets/metadata", "mutation": {"op": "set", "path": ["sha256"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "asset_binding_rejection"},
            {"case_id": "core-http-stale-cas-request", "validator": "http_fixture_request", "route_id": "workspace.update", "fixture": "core-http.json#/exchanges/3/request", "mutation": {"op": "set", "path": ["expected_revision"], "value": 999}, "expected": "frozen_request_rejection"},
            {"case_id": "core-http-operation-key-payload-reuse", "validator": "http_fixture_request", "route_id": "workspace.update", "fixture": "core-http.json#/exchanges/3/request", "mutation": {"op": "set", "path": ["title"], "value": "Unfrozen payload under the same operation key"}, "expected": "operation_key_payload_reuse_rejection"},
            {"case_id": "core-relation-delete-cas-required", "validator": "core_authority", "fixture": "core-http.json#/authority_payloads/31", "mutation": {"op": "delete", "path": ["expected_revision_id"]}, "expected": "closed_schema_rejection"},
            {"case_id": "operation-context-projection-epoch", "validator": "context_projection_schema", "fixture": "context-identity.json#/vectors/2/projection", "mutation": {"op": "set", "path": ["lease_epoch"], "value": 9}, "expected": "closed_schema_rejection"},
            {"case_id": "operation-context-stale-epoch", "validator": "context_stale_epoch", "fixture": "context-identity.json#/vectors/2/meta", "mutation": {"op": "set", "path": ["lease_epoch"], "value": 8}, "expected": "stale_lease"},
            {"case_id": "operation-context-install-missing-current-epoch", "validator": "context_missing_expected_epoch", "fixture": "context-identity.json#/vectors/1/meta", "mutation": {"op": "noop"}, "expected": "missing_fencing_rejection"},
            {"case_id": "operation-context-attempt-missing-current-epoch", "validator": "context_missing_expected_epoch", "fixture": "context-identity.json#/vectors/2/meta", "mutation": {"op": "noop"}, "expected": "missing_fencing_rejection"},
            {"case_id": "operation-context-invalid-deadline", "validator": "context_identity", "fixture": "context-identity.json#/vectors/0/meta", "mutation": {"op": "set", "path": ["deadline_at"], "value": "not-utc"}, "expected": "meta_rejection"},
            {"case_id": "export-noncontiguous-ordinal", "validator": "export", "fixture": "export-current-revisions.json#", "mutation": {"op": "set", "path": ["ordered_revisions", 1, "ordinal"], "value": 3}, "expected": "ordering_rejection"},
            {"case_id": "export-duplicate-document", "validator": "export", "fixture": "export-current-revisions.json#", "mutation": {"op": "set", "path": ["ordered_revisions", 1, "document_id"], "value": "doc-a"}, "expected": "identity_rejection"},
            {"case_id": "export-cross-workspace", "validator": "export_expected_workspace", "fixture": "export-current-revisions.json#", "mutation": {"op": "set", "path": ["workspace_id"], "value": "ws-other"}, "expected": "workspace_rejection"},
            {"case_id": "export-revision-not-in-snapshot", "validator": "export_snapshot_binding", "fixture": "export-current-revisions.json#", "mutation": {"op": "set", "path": ["ordered_revisions", 1, "revision_id"], "value": "rev-other"}, "expected": "snapshot_rejection"},
            {"case_id": "export-title-tamper-raw-asset", "validator": "export_raw_variant", "fixture": "export-current-revisions.json#", "mutation": {"op": "set", "path": ["ordered_revisions", 0, "title"], "value": "Tampered title"}, "expected": "asset_hash_rejection"},
            {"case_id": "export-raw-bom", "validator": "export_raw_variant", "fixture": "export-current-revisions.json#", "mutation": {"op": "raw_variant", "variant": "bom"}, "expected": "strict_raw_rejection"},
            {"case_id": "export-raw-whitespace", "validator": "export_raw_variant", "fixture": "export-current-revisions.json#", "mutation": {"op": "raw_variant", "variant": "whitespace"}, "expected": "strict_raw_rejection"},
            {"case_id": "export-raw-duplicate-key", "validator": "export_raw_variant", "fixture": "export-current-revisions.json#", "mutation": {"op": "raw_variant", "variant": "duplicate_key"}, "expected": "strict_raw_rejection"},
            {"case_id": "export-asset-hash-not-frozen", "validator": "export_snapshot_record", "fixture": "export-run-snapshot.json#", "mutation": {"op": "set", "path": ["asset_hashes", 0, "sha256"], "value": "0000000000000000000000000000000000000000000000000000000000000000"}, "expected": "snapshot_asset_binding_rejection"},
            {"case_id": "export-object-caller-hash-is-not-binding", "validator": "export_object_caller_hash", "fixture": "export-current-revisions.json#", "mutation": {"op": "noop"}, "expected": "object_binding_rejection"},
            {"case_id": "plugin-ui-tree-event-outside-component", "validator": "typescript_plugin_ui_tree", "fixture": "contracts/examples/fixtures/plugin-ui-tree.json#", "mutation": {"op": "set", "path": ["root", "event_ids"], "value": ["change"]}, "expected": "event_allowlist_rejection"},
            {"case_id": "plugin-ui-intent-extra-field", "validator": "typescript_plugin_ui_intent", "fixture": "contracts/examples/fixtures/plugin-ui-intent.json#", "mutation": {"op": "set", "path": ["unexpected"], "value": True}, "expected": "closed_schema_rejection"},
            {"case_id": "job-snapshot-extra-field", "validator": "typescript_job_snapshot", "fixture": "contracts/examples/fixtures/job-snapshot.json#", "mutation": {"op": "set", "path": ["unexpected"], "value": True}, "expected": "closed_schema_rejection"},
            {"case_id": "typescript-prototype-backed-ingress", "validator": "typescript_runtime_probe", "fixture": None, "mutation": {"op": "prototype"}, "expected": "plain_object_rejection"},
            {"case_id": "typescript-getter-ingress", "validator": "typescript_runtime_probe", "fixture": None, "mutation": {"op": "getter"}, "expected": "data_property_rejection"},
        ],
    }
    publication_path = "contract-publication-v1/negative.json"
    dump(CORPUS / publication_path, publication_cases)
    dump(CORPUS / "manifest.json", {"schema": "contract-corpus/v1", "negative_groups": [group["group_id"] for group in groups], "required_group_count": 14, "additional_negative_profiles": [publication_path], "path_corpus": "paths/windows-paths.json", "compatibility": ["compatibility/valid.json", "compatibility/invalid.json"], "history": "history/v1-raw.json"})
    write_v2_corpus()
    write_macro_planning_corpus()
    write_model_provider_corpus()
    write_corpus_v2_manifest()
    if _DRIFT:
        print("corpus drift: " + ", ".join(sorted(set(_DRIFT))))
        return 1
    action = "checked" if _CHECK_MODE else "generated"
    print(
        f"{action} 14 frozen groups, {len(publication_cases['cases'])} contract-publication probes, "
        f"{len(v2_groups())} M4/M5 groups, {sum(len(group['negative']) for group in macro_planning_groups())} "
        f"macro-planning negatives, {macro_integer_representations()['vector_count']} raw integer vectors, and "
        f"{sum(len(group['negative']) for group in model_provider_groups(model_provider_golden()))} Provider RPC negatives"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
