"""Write the normative M0 golden vectors and public positive fixtures."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import rfc8785


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "contracts" / "golden"
EXAMPLES = ROOT / "contracts" / "examples"


def jcs(value: Any) -> bytes:
    return rfc8785.dumps(value)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def self_hash(prefix: str, value: dict[str, Any], field: str) -> str:
    unsigned = {k: v for k, v in value.items() if k != field}
    return sha256(prefix.encode("ascii") + b"\n" + jcs(unsigned))


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_json(path: Path, value: Any) -> None:
    write(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def package_vector() -> dict[str, str]:
    plugin_bytes = b'{"capabilities":[{"capability_id":"demo.echo/v1","operations":["run"],"result_contract":"artifact-bundle/v1"}],"compatibility":{"core_api":">=1.0 <2.0","plugin_rpc":"1","ui_host":"1"},"data":{"format":"demo-echo/v1","root":"data/rules.json"},"display_name":"Golden Echo","kind":"data","needs":[],"plugin_id":"com.plotpilot.golden.echo","schema":"plotpilot-plugin/v1","version":"1.0.0"}\n'
    rules_bytes = b'{"message":"hello"}\n'
    files_bytes = (
        f"{sha256(rules_bytes)}  data/rules.json\n"
        f"{sha256(plugin_bytes)}  plugin.json\n"
    ).encode("ascii")
    package_hash = sha256(b"plotpilot-package/v1\n" + files_bytes)
    release_id = sha256(b"plotpilot-release/v1\ncom.plotpilot.golden.echo\n1.0.0\n" + package_hash.encode("ascii") + b"\n")
    write(GOLDEN / "package" / "plugin.json", plugin_bytes)
    write(GOLDEN / "package" / "data" / "rules.json", rules_bytes)
    write(GOLDEN / "package" / "files.sha256", files_bytes)
    vector = {
        "files_sha256": files_bytes.decode("ascii"),
        "package_hash": package_hash,
        "release_id": release_id,
        "expected_from_design": {
            "package_hash": "987e80013fe0cddd463eb8976fd75b62dbabef4f8e0e321ae6ad82a54f09b068",
            "release_id": "00d365d816a45108cac08b5dc26eae015a44401c04aed0e9d512ed8fb24d88dd",
        },
    }
    write_json(GOLDEN / "package" / "expected.json", vector)
    return {"package_hash": package_hash, "release_id": release_id}


def skill_vector() -> dict[str, str]:
    skill_bytes = b'{"actions":["rewrite"],"display_name":"Golden Skill","schema":"plotpilot-skill/v1","skill_id":"com.plotpilot.skill.golden","stage":"draft","version":"1.0.0"}\n'
    prompt_bytes = b"Rewrite clearly.\n"
    files_bytes = (
        f"{sha256(prompt_bytes)}  prompt.txt\n"
        f"{sha256(skill_bytes)}  skill.json\n"
    ).encode("ascii")
    package_hash = sha256(b"plotpilot-skill-package/v1\n" + files_bytes)
    release_id = sha256(b"plotpilot-skill-release/v1\ncom.plotpilot.skill.golden\n1.0.0\n" + package_hash.encode("ascii") + b"\n")
    write(GOLDEN / "skill" / "skill.json", skill_bytes)
    write(GOLDEN / "skill" / "prompt.txt", prompt_bytes)
    write(GOLDEN / "skill" / "files.sha256", files_bytes)
    vector = {
        "files_sha256": files_bytes.decode("ascii"),
        "skill_package_hash": package_hash,
        "skill_release_id": release_id,
        "expected_from_design": {
            "skill_package_hash": "5cf3df6acea3c792ee36fa3c0c5757f87c8219aba88cfc568bd4a67738983b7f",
            "skill_release_id": "abe35d45644a2ebac3b3c914876c613342b8d5a3e2421334241a06ec887436b7",
        },
    }
    write_json(GOLDEN / "skill" / "expected.json", vector)
    return {"skill_package_hash": package_hash, "skill_release_id": release_id}


def snapshot_vector(package: dict[str, str], skill: dict[str, str]) -> dict[str, Any]:
    request_lines = (
        "request-key/v1\n"
        "ws-1\n"
        "writing.chapter.draft/v1\n"
        "doc-a\n"
        "node-a\n"
        "doc-a=rev-a=1111111111111111111111111111111111111111111111111111111111111111,doc-b=rev-b=2222222222222222222222222222222222222222222222222222222222222222\n"
        "plan-r7\n"
        "3333333333333333333333333333333333333333333333333333333333333333\n"
        "intent-1\n"
    ).encode("ascii")
    request_key = sha256(request_lines)
    snapshot: dict[str, Any] = {
        "schema": "run-snapshot/v1",
        "snapshot_id": "snap-golden",
        "core_contract_version": "1.2.0",
        "workspace_id": "ws-1",
        "scope": {"document_id": "doc-a", "node_id": "node-a", "operation": "writing.chapter.draft/v1"},
        "input_revisions": [
            {"content_hash": "1111111111111111111111111111111111111111111111111111111111111111", "document_id": "doc-a", "revision_id": "rev-a"},
            {"content_hash": "2222222222222222222222222222222222222222222222222222222222222222", "document_id": "doc-b", "revision_id": "rev-b"},
        ],
        "plan_revision_id": "plan-r7",
        "plugin_releases": [
            {"data_generation_id": "dg-a", "package_hash": "4444444444444444444444444444444444444444444444444444444444444444", "plugin_id": "com.plotpilot.alpha", "release_id": "rel-a"},
            {"data_generation_id": "dg-b", "package_hash": "5555555555555555555555555555555555555555555555555555555555555555", "plugin_id": "com.plotpilot.beta", "release_id": "rel-b"},
        ],
        "plugin_settings_revisions": [
            {"plugin_id": "com.plotpilot.alpha", "schema_hash": "6666666666666666666666666666666666666666666666666666666666666666", "scope": "global", "scope_id": None, "settings_revision_id": "settings-a", "validated_by_release_id": "rel-a"},
            {"plugin_id": "com.plotpilot.beta", "schema_hash": "7777777777777777777777777777777777777777777777777777777777777777", "scope": "global", "scope_id": None, "settings_revision_id": "settings-b", "validated_by_release_id": "rel-b"},
        ],
        "data_bindings": [
            {"bundle_asset_id": "asset-data-a", "bundle_hash": "8888888888888888888888888888888888888888888888888888888888888888", "data_plugin_id": "com.plotpilot.data.alpha", "data_release_id": "data-rel-a", "format_id": "plot-rules/v1", "interpreter_binding_id": "binding-alpha", "order": 10},
            {"bundle_asset_id": "asset-data-b", "bundle_hash": "9999999999999999999999999999999999999999999999999999999999999999", "data_plugin_id": "com.plotpilot.data.beta", "data_release_id": "data-rel-b", "format_id": "style-rules/v1", "interpreter_binding_id": "binding-beta", "order": 20},
        ],
        "skill_releases": [
            {"order": 10, "package_hash": "4444444444444444444444444444444444444444444444444444444444444444", "parameters_asset_id": "asset-skill-a", "release_id": "skill-rel-a", "skill_id": "com.plotpilot.skill.alpha"},
            {"order": 20, "package_hash": "5555555555555555555555555555555555555555555555555555555555555555", "parameters_asset_id": "asset-skill-b", "release_id": "skill-rel-b", "skill_id": "com.plotpilot.skill.beta"},
        ],
        "model_profile_revision_id": "model-r1",
        "parameters_asset_id": "asset-params",
        "asset_hashes": [
            {"asset_id": "asset-data-a", "sha256": "8888888888888888888888888888888888888888888888888888888888888888"},
            {"asset_id": "asset-data-b", "sha256": "9999999999999999999999999999999999999999999999999999999999999999"},
            {"asset_id": "asset-params", "sha256": "3333333333333333333333333333333333333333333333333333333333333333"},
            {"asset_id": "asset-skill-a", "sha256": "4444444444444444444444444444444444444444444444444444444444444444"},
            {"asset_id": "asset-skill-b", "sha256": "5555555555555555555555555555555555555555555555555555555555555555"},
        ],
        "request_key": request_key,
        "run_intent_id": "intent-1",
        "created_at": "2026-08-26T00:00:00Z",
    }
    snapshot_hash = sha256(b"run-snapshot/v1\n" + jcs(snapshot))
    snapshot["snapshot_hash"] = snapshot_hash
    write(GOLDEN / "run-snapshot" / "request-key.txt", request_lines)
    write(GOLDEN / "run-snapshot" / "snapshot.jcs", jcs(snapshot))
    write_json(GOLDEN / "run-snapshot" / "snapshot.json", snapshot)
    write_json(
        GOLDEN / "run-snapshot" / "expected.json",
        {
            "request_key": request_key,
            "snapshot_hash": snapshot_hash,
            "expected_from_design": {
                "request_key": "5e346b6254626cb314a0d4040a64e8a9797c0a890537ce07e9920a911a525e61",
                "snapshot_hash": "5a7e60677a5ced4803c96f7b7db657409051ba8f3f5b18aa8ac28978e2b4a1d2",
            },
        },
    )
    return {"request_key": request_key, "snapshot_hash": snapshot_hash, "snapshot": snapshot}


def backup_vector(snapshot: dict[str, Any]) -> dict[str, Any]:
    backup: dict[str, Any] = {
        "schema": "backup-bundle/v1",
        "backup_id": "backup-golden",
        "library_root_id": "library-golden",
        "backup_epoch": 7,
        "mode": "workspace",
        "workspace_ids": ["ws-1"],
        "core_contract_version": "1.2.0",
        "core_snapshot_hash": snapshot["snapshot_hash"],
        "workspace_snapshot_hash": snapshot["snapshot_hash"],
        "current_generation_id": "generation-1",
        "lkg_generation_id": "generation-0",
        "asset_closure_root": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "plugin_releases": [],
        "projection_rebuild_required": [{"plugin_id": "com.plotpilot.demo", "release_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "reason": "plugin_projection_rebuild_required"}],
        "files": [{"path": "core/core.db", "size": 128, "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "role": "core_db"}],
        "created_at": "2026-08-26T00:00:00Z",
        "verification": {"databases_valid": True, "assets_valid": True, "files_valid": True, "compatible": True, "verified_at": "2026-08-26T00:00:00Z"},
    }
    backup["bundle_hash"] = sha256(b"plotpilot-backup/v1\n" + jcs(backup))
    write_json(GOLDEN / "backup" / "backup.json", backup)
    write_json(GOLDEN / "backup" / "expected.json", {"bundle_hash": backup["bundle_hash"], "mode": backup["mode"], "root_immutable": True})
    return backup


def examples(snapshot: dict[str, Any]) -> None:
    snapshot_value = snapshot["snapshot"]
    candidate = {
        "schema": "candidate-item/v1", "item_id": "candidate-item-1", "item_kind": "document",
        "target": {"workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-a"},
        "mutation": {"mode": "replace", "payload_schema": "core/document-text/v1", "payload_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},
        "payload_asset_id": "asset-candidate-payload",
        "base": {"revision_id": "rev-a", "content_hash": "1111111111111111111111111111111111111111111111111111111111111111"},
        "write_set": [{"workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-a", "revision_id": "rev-a", "content_hash": "1111111111111111111111111111111111111111111111111111111111111111"}],
        "parent_candidate_ids": [], "source_refs": [], "status": "complete",
    }
    bundle = {
        "schema": "result-bundle/v1", "contract_id": "candidate-batch/v1", "bundle_id": "bundle-golden", "bundle_type": "candidate_batch",
        "producer": {"plugin_id": "com.plotpilot.demo", "release_id": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "capability_id": "writing.chapter.draft/v1", "job_id": "job-1", "step_id": "step-1", "attempt_id": "attempt-1", "lease_epoch": 1},
        "input_snapshot_hash": snapshot["snapshot_hash"], "items": [candidate], "warnings": [], "partial": False, "provenance_receipt_id": "receipt-1", "skill_chain_result_refs": [],
    }
    ui_tree = {"schema": "plugin-ui-tree/v1", "tree_id": "tree-1", "render_seq": 1, "root": {"component": "stack", "key": "root", "props": {"direction": "vertical", "row_gap": 8}, "children": [{"component": "text", "key": "title", "props": {"text": "Golden", "tone": "default"}, "children": [], "event_ids": []}], "event_ids": []}}
    write_json(EXAMPLES / "run-snapshot.json", snapshot_value)
    write_json(EXAMPLES / "candidate-item.json", candidate)
    write_json(EXAMPLES / "result-bundle.json", bundle)
    write_json(EXAMPLES / "plugin-ui-tree.json", ui_tree)


def main() -> int:
    package = package_vector()
    skill = skill_vector()
    snapshot = snapshot_vector(package, skill)
    backup_vector(snapshot)
    examples(snapshot)
    print("golden vectors and positive fixtures written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
