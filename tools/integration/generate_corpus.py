"""Generate the shared positive/negative corpus index for M0 §84.13."""
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


def main() -> int:
    groups = [
        {"group_id": "84.13-01", "title": "JCS set/ordered permutation and package/request/Skill golden", "positive": ["package-golden", "skill-golden", "run-snapshot-golden"], "negative": [{"case_id": "skill-order-permutation", "kind": "hash_must_change", "target": "run-snapshot", "expected": "snapshot_hash_changes"}, {"case_id": "package-crlf", "kind": "bytes_change", "target": "package/files.sha256", "expected": "package_hash_changes"}]},
        {"group_id": "84.13-02", "title": "Result profile and Candidate target/mutation/base/write-set/parent", "positive": ["result-bundle"], "negative": [{"case_id": "artifact-with-candidate-item", "kind": "profile_mismatch", "expected_error_code": 1011}, {"case_id": "candidate-target-outside-write-set", "kind": "candidate_semantic", "expected_error_code": 1011}, {"case_id": "candidate-parent-cycle", "kind": "parent_cycle", "expected_error_code": 1011}]},
        {"group_id": "84.13-03", "title": "Broker ACK-loss, child cancel and receipt propagation", "positive": ["broker-invocation"], "negative": [{"case_id": "broker-different-input-same-key", "kind": "duplicate_operation", "expected_error_code": 1008}, {"case_id": "cancel-after-child-terminal", "kind": "terminal_rewrite", "expected_error_code": 1010}]},
        {"group_id": "84.13-04", "title": "Data format/interpreter/Plan/Snapshot mismatch", "positive": ["run-snapshot"], "negative": [{"case_id": "unsupported-data-format", "kind": "interpreter_unavailable", "expected_error_code": 1012}, {"case_id": "snapshot-parameters-not-in-assets", "kind": "asset_binding", "expected_error_code": 1011}]},
        {"group_id": "84.13-05", "title": "Operation key and chunk upload ACK-loss", "positive": ["rpc-upload"], "negative": [{"case_id": "same-key-different-payload", "kind": "duplicate_operation", "expected_error_code": 1008}, {"case_id": "upload-offset-ahead", "kind": "upload_offset", "expected_error_code": 1005}, {"case_id": "upload-final-hash-mismatch", "kind": "upload_hash", "expected_error_code": 1005}]},
        {"group_id": "84.13-06", "title": "Attempt cancel/complete race, fresh resume and checkpoint", "positive": ["checkpoint"], "negative": [{"case_id": "checkpoint-different-snapshot", "kind": "checkpoint_binding", "expected_error_code": 1014}, {"case_id": "stale-attempt-lease", "kind": "lease_fencing", "expected_error_code": 1002}, {"case_id": "terminal-attempt-resume", "kind": "state_transition", "expected_error_code": 1010}]},
        {"group_id": "84.13-07", "title": "Bundle producer/snapshot/lease/receipt/staging/outcome transaction", "positive": ["result-bundle", "provenance-receipt"], "negative": [{"case_id": "bundle-producer-mismatch", "kind": "producer_binding", "expected_error_code": 1011}, {"case_id": "succeeded-partial-item", "kind": "outcome_matrix", "expected_error_code": 1011}, {"case_id": "failed-candidate-bundle", "kind": "outcome_matrix", "expected_error_code": 1011}]},
        {"group_id": "84.13-08", "title": "Install base CAS, crash points, rollback once and safe mode", "positive": ["plugin-lifecycle-transition"], "negative": [{"case_id": "concurrent-install-base-changed", "kind": "cas", "expected_error_code": 1010}, {"case_id": "rollback-second-attempt", "kind": "rollback_once", "expected_error_code": 1010}, {"case_id": "invalid-settings-validator", "kind": "settings_invalid", "expected_error_code": 1006}]},
        {"group_id": "84.13-09", "title": "Retire/new pin race and post-delete Candidate Publication", "positive": ["release-retirement", "release-pin"], "negative": [{"case_id": "pin-while-retiring", "kind": "retire_pin_race", "expected_error_code": 1013}, {"case_id": "retire-with-recoverable-attempt", "kind": "pin_barrier", "expected_error_code": 1013}]},
        {"group_id": "84.13-10", "title": "Aggregate/Core Event and SSE cursor convergence", "positive": ["core-event", "sse-recovery", "core-snapshot"], "negative": [{"case_id": "job-cursor-on-core-stream", "kind": "cursor_domain", "expected_error_code": 1010}, {"case_id": "cursor-ahead", "kind": "cursor_ahead", "expected_error_code": 1010}, {"case_id": "gap-without-snapshot", "kind": "snapshot_pair", "expected_error_code": 1011}]},
        {"group_id": "84.13-11", "title": "Durable stream prefix and unique incomplete Candidate", "positive": ["stream-prefix"], "negative": [{"case_id": "prefix-not-extension", "kind": "stream_prefix", "expected_error_code": 1011}, {"case_id": "old-stream-id", "kind": "stream_binding", "expected_error_code": 1002}, {"case_id": "duplicate-incomplete-candidate", "kind": "candidate_uniqueness", "expected_error_code": 1010}]},
        {"group_id": "84.13-12", "title": "Skill executed/failed/skipped/model claim/verified patch", "positive": ["skill-run-receipt", "skill-chain-result"], "negative": [{"case_id": "skipped-participated", "kind": "attribution", "expected_error_code": 1011}, {"case_id": "model-claim-without-evidence", "kind": "attribution", "expected_error_code": 1011}, {"case_id": "tampered-patch", "kind": "receipt_hash", "expected_error_code": 1011}]},
        {"group_id": "84.13-13", "title": "UI immutable URL/CSP, stale/duplicate intent and tree whitelist", "positive": ["plugin-ui-tree", "plugin-ui-intent", "plugin-ui-message"], "negative": [{"case_id": "unknown-component", "kind": "ui_tree_whitelist", "expected_error_code": 1011}, {"case_id": "stale-intent", "kind": "ui_freshness", "expected_error_code": 1010}, {"case_id": "duplicate-intent-different-payload", "kind": "ui_idempotency", "expected_error_code": 1008}]},
        {"group_id": "84.13-14", "title": "Backup modes, restore, compatibility/history and Windows paths", "positive": ["backup-golden", "compatibility-valid", "history-v1"], "negative": [{"case_id": "workspace-with-package", "kind": "backup_mode", "expected_error_code": 1011}, {"case_id": "reserved-device-name", "kind": "windows_path", "expected_error_code": 1005}, {"case_id": "compatibility-or", "kind": "compatibility", "expected_error_code": 1011}, {"case_id": "history-reserialized", "kind": "raw_bytes", "expected_error_code": 1011}]},
    ]
    for group in groups:
        dump(CORPUS / "negative" / "84.13" / f"{group['group_id'].split('-')[-1]}.json", group)
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
    dump(CORPUS / "manifest.json", {"schema": "contract-corpus/v1", "negative_groups": [group["group_id"] for group in groups], "required_group_count": 14, "path_corpus": "paths/windows-paths.json", "compatibility": ["compatibility/valid.json", "compatibility/invalid.json"], "history": "history/v1-raw.json"})
    print("generated 14 negative groups and shared path/compatibility/history corpus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
