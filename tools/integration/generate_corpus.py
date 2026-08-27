"""Generate additive corpus records without rewriting frozen M0 §84.13 bytes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "contracts" / "corpus"


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
    print(f"generated 14 negative groups and {len(publication_cases['cases'])} contract-publication probes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
