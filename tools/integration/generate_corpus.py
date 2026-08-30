"""Generate additive corpus records without rewriting frozen M0 §84.13 bytes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "contracts" / "corpus"
V2_CORPUS = CORPUS / "m4-m5-public-surface-v2"


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


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


def main() -> int:
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
    print(f"generated 14 negative groups, {len(publication_cases['cases'])} contract-publication probes and {len(v2_groups())} v2 groups")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
