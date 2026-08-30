"""Write the normative M0 golden vectors and public positive fixtures."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

try:
    import rfc8785
except ModuleNotFoundError:  # pragma: no cover - only for the dependency-light source runner
    class _Rfc8785Fallback:
        @staticmethod
        def dumps(value: Any) -> bytes:
            """Canonicalize the ASCII-only generator fixtures without a package install.

            The checked-in vectors use strings/integers only.  Their JCS form is
            identical to compact, UTF-8, lexicographically sorted JSON.  CI
            still imports the real RFC 8785 implementation when available.
            """

            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    rfc8785 = _Rfc8785Fallback()


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


def contract_publication_vectors() -> None:
    """Write additive Core API/context/export vectors for the M1 publication."""

    out = GOLDEN / "contract-publication-v1"
    h1 = "1" * 64
    h2 = "2" * 64
    release = "a" * 64
    workspace = {
        "schema": "core-workspace/v1", "workspace_id": "ws-1", "workspace_kind": "WritingProject",
        "title": "Golden Workspace", "status": "active", "current_plan_revision_id": None,
        "created_at": "2026-08-27T00:00:00Z", "updated_at": "2026-08-27T00:00:00Z", "revision": 0,
    }
    document = {
        "schema": "core-document/v1", "document_id": "doc-a", "workspace_id": "ws-1",
        "document_type": "core.chapter", "title": "Chapter A", "current_revision_id": "rev-a",
        "created_at": "2026-08-27T00:00:00Z", "updated_at": "2026-08-27T00:00:00Z", "revision": 1,
    }
    node = {
        "schema": "core-node/v1", "node_id": "node-a", "workspace_id": "ws-1", "document_id": "doc-a",
        "node_type": "section", "title": "Scene", "parent_node_id": None, "position": 0,
        "current_revision_id": None, "created_at": "2026-08-27T00:00:00Z", "updated_at": "2026-08-27T00:00:00Z", "revision": 0,
    }
    relation = {
        "schema": "core-relation/v1", "relation_id": "relation-a", "workspace_id": "ws-1",
        "relation_type": "plotpilot.outline.contains", "source_id": "doc-a", "target_id": "node-a",
        "revision_id": None, "created_at": "2026-08-27T00:00:00Z",
    }
    revision = {
        "schema": "core-revision/v1", "revision_id": "rev-a", "workspace_id": "ws-1", "document_id": "doc-a",
        "node_id": None, "parent_revision_id": None, "content_hash": h1, "created_by": "user-a",
        "source_candidate_id": None, "created_at": "2026-08-27T00:00:00Z", "revision_number": 1,
        "payload_schema": "core.document-text/v1",
    }
    node_revision = {
        "schema": "core-revision/v1", "revision_id": "rev-node-a", "workspace_id": "ws-1", "document_id": None,
        "node_id": "node-a", "parent_revision_id": None, "content_hash": h2, "created_by": "user-a",
        "source_candidate_id": None, "created_at": "2026-08-27T00:00:00Z", "revision_number": 1,
        "payload_schema": "core.node-text/v1",
    }
    authority_payloads = [
        workspace, document, node, relation, revision,
        {"schema": "core-workspace-page/v1", "items": [workspace], "offset": 0, "limit": 100, "total": 1, "next_offset": None},
        {"schema": "core-document-page/v1", "items": [document], "offset": 0, "limit": 100, "total": 1, "next_offset": None},
        {"schema": "core-node-page/v1", "items": [node], "offset": 0, "limit": 100, "total": 1, "next_offset": None},
        {"schema": "core-relation-page/v1", "items": [relation], "offset": 0, "limit": 100, "total": 1, "next_offset": None},
        {"schema": "core-revision-page/v1", "items": [revision], "offset": 0, "limit": 100, "total": 1, "next_offset": None},
        {"schema": "core-workspace-query/v1", "workspace_id": None, "offset": 0, "limit": 100},
        {"schema": "core-document-query/v1", "workspace_id": "ws-1", "document_id": None, "offset": 0, "limit": 100},
        {"schema": "core-node-query/v1", "workspace_id": "ws-1", "node_id": None, "document_id": "doc-a", "parent_node_id": None, "offset": 0, "limit": 100},
        {"schema": "core-relation-query/v1", "workspace_id": "ws-1", "relation_id": None, "source_id": None, "target_id": None, "relation_type": None, "offset": 0, "limit": 100},
        {"schema": "core-workspace-get-query/v1", "workspace_id": "ws-1"},
        {"schema": "core-document-get-query/v1", "workspace_id": "ws-1", "document_id": "doc-a"},
        {"schema": "core-node-get-query/v1", "workspace_id": "ws-1", "node_id": "node-a"},
        {"schema": "core-document-revision-query/v1", "workspace_id": "ws-1", "document_id": "doc-a", "revision_id": None, "offset": 0, "limit": 100},
        {"schema": "core-node-revision-query/v1", "workspace_id": "ws-1", "node_id": "node-a", "revision_id": None, "offset": 0, "limit": 100},
        {"schema": "core-revision-get-query/v1", "workspace_id": "ws-1", "revision_id": "rev-a"},
        {"schema": "core-revision-content-query/v1", "workspace_id": "ws-1", "revision_id": "rev-a", "offset": 0, "length": 5},
        {"schema": "core-revision-content-page/v1", "revision_id": "rev-a", "offset": 0, "length": 5, "total_length": 5, "text": "hello", "next_offset": None},
        {"schema": "core-workspace-create-command/v1", "operation_key": "op-workspace-create", "workspace_id": "ws-1", "workspace_kind": "WritingProject", "title": "Golden Workspace"},
        {"schema": "core-workspace-update-command/v1", "operation_key": "op-workspace-update", "workspace_id": "ws-1", "expected_revision": 0, "title": "Golden Workspace 2", "status": None},
        {"schema": "core-workspace-delete-command/v1", "operation_key": "op-workspace-delete", "workspace_id": "ws-1", "expected_revision": 1},
        {"schema": "core-document-create-command/v1", "operation_key": "op-document-create", "document_id": "doc-a", "workspace_id": "ws-1", "document_type": "core.chapter", "title": "Chapter A"},
        {"schema": "core-document-update-command/v1", "operation_key": "op-document-update", "document_id": "doc-a", "workspace_id": "ws-1", "expected_revision": 1, "title": "Chapter A2"},
        {"schema": "core-node-create-command/v1", "operation_key": "op-node-create", "node_id": "node-a", "workspace_id": "ws-1", "document_id": "doc-a", "node_type": "section", "title": "Scene", "parent_node_id": None, "position": 0},
        {"schema": "core-node-update-command/v1", "operation_key": "op-node-update", "node_id": "node-a", "workspace_id": "ws-1", "expected_revision": 0, "title": "Scene 2", "parent_node_id": None, "position": 1},
        {"schema": "core-node-delete-command/v1", "operation_key": "op-node-delete", "node_id": "node-a", "workspace_id": "ws-1", "expected_revision": 1},
        {"schema": "core-relation-create-command/v1", "operation_key": "op-relation-create", "relation_id": "relation-a", "workspace_id": "ws-1", "relation_type": "plotpilot.outline.contains", "source_id": "doc-a", "target_id": "node-a", "revision_id": None},
        {"schema": "core-relation-delete-command/v1", "operation_key": "op-relation-delete", "relation_id": "relation-a", "workspace_id": "ws-1", "expected_revision_id": None},
        {"schema": "core-document-revision-create-command/v1", "operation_key": "op-document-revision-create", "revision_id": "rev-a", "workspace_id": "ws-1", "document_id": "doc-a", "base_revision_id": None, "content": "hello", "created_by": "user-a", "source_candidate_id": None, "payload_schema": "core.document-text/v1"},
        {"schema": "core-node-revision-create-command/v1", "operation_key": "op-node-revision-create", "revision_id": "rev-node-a", "workspace_id": "ws-1", "node_id": "node-a", "base_revision_id": None, "content": "scene", "created_by": "user-a", "source_candidate_id": None, "payload_schema": "core.node-text/v1"},
        {"schema": "core-delete-result/v1", "operation_key": "op-node-delete", "workspace_id": "ws-1", "entity_kind": "node", "entity_id": "node-a", "previous_revision": 1, "deleted": True, "idempotent": False},
    ]

    publication_command = {"schema": "publication-command/v1", "publication_operation_key": "publication-op-1", "workspace_id": "ws-1", "candidate_id": "candidate-1", "accepted_by": "user-a"}
    publication_result = {
        "schema": "publication-result/v1", "publication_id": "publication-1", "candidate_id": "candidate-1",
        "workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-a",
        "resulting_revision": {"revision_id": "rev-a", "workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-a", "content_hash": h1, "revision_number": 1},
        "idempotent": False,
    }
    chunk = b"hello"
    partial_chunk = chunk[1:3]
    asset_query = {"schema": "asset-query/v1", "asset_id": "asset-a"}
    asset_range_query = {"schema": "asset-read-range-query/v1", "asset_id": "asset-a", "offset": 0, "length": len(chunk)}
    chunk_hash = sha256(chunk)
    asset_metadata = {"schema": "asset-metadata/v1", "asset_id": "asset-a", "sha256": chunk_hash, "mime": "text/plain", "size": len(chunk), "logical_role": "revision.content", "provenance": "core-revision:rev-a", "rebuildable": False}
    asset_range = {"schema": "asset-read-range/v1", "asset_id": "asset-a", "offset": 0, "length": len(chunk), "total_size": len(chunk), "base64_chunk": "aGVsbG8=", "next_offset": None, "content_hash": chunk_hash}
    asset_partial = {"schema": "asset-read-range/v1", "asset_id": "asset-a", "offset": 1, "length": len(partial_chunk), "total_size": len(chunk), "base64_chunk": "ZWw=", "next_offset": 3, "content_hash": sha256(partial_chunk)}
    workspace_get = authority_payloads[14]
    document_get = authority_payloads[15]
    node_get = authority_payloads[16]
    revision_get = authority_payloads[19]
    workspace_updated = {**workspace, "title": "Golden Workspace 2", "revision": 1}
    document_updated = {**document, "title": "Chapter A2", "revision": 2}
    node_updated = {**node, "title": "Scene 2", "position": 1, "revision": 1}
    workspace_delete = {"schema": "core-delete-result/v1", "operation_key": "op-workspace-delete", "workspace_id": "ws-1", "entity_kind": "workspace", "entity_id": "ws-1", "previous_revision": 1, "deleted": True, "idempotent": False}
    node_delete = authority_payloads[34]
    relation_delete = {"schema": "core-delete-result/v1", "operation_key": "op-relation-delete", "workspace_id": "ws-1", "entity_kind": "relation", "entity_id": "relation-a", "previous_revision": None, "deleted": True, "idempotent": False}
    node_revision_page = {"schema": "core-revision-page/v1", "items": [node_revision], "offset": 0, "limit": 100, "total": 1, "next_offset": None}
    def error(code: str, message: str, retryable: bool = False) -> dict[str, Any]:
        return {"schema": "core-http-error/v1", "error_code": code, "message": message, "retryable": retryable}

    workspace_update = authority_payloads[23]
    unknown_document = {"schema": "core-document-get-query/v1", "workspace_id": "ws-1", "document_id": "doc-unknown"}
    cross_workspace_document = {"schema": "core-document-get-query/v1", "workspace_id": "ws-other", "document_id": "doc-a"}
    stale_workspace_update = {**workspace_update, "expected_revision": 99}
    reused_workspace_update = {**workspace_update, "title": "Different payload under the same operation key"}
    incomplete_publication = {**publication_command, "candidate_id": "candidate-incomplete"}
    http = {
        "schema": "core-http-fixtures/v1",
        "authority_payloads": authority_payloads,
        "publication": {"command": publication_command, "result": publication_result},
        "assets": {"query": asset_query, "range_query": asset_range_query, "metadata": asset_metadata, "range": asset_range, "partial": asset_partial},
        "exchanges": [
            {"route_id": "workspace.list", "request": authority_payloads[10], "status": 200, "response": authority_payloads[5]},
            {"route_id": "workspace.get", "request": workspace_get, "status": 200, "response": workspace},
            {"route_id": "workspace.create", "request": authority_payloads[22], "status": 201, "response": workspace},
            {"route_id": "workspace.update", "request": workspace_update, "status": 200, "response": workspace_updated},
            {"route_id": "workspace.delete", "request": authority_payloads[24], "status": 200, "response": workspace_delete},
            {"route_id": "document.list", "request": authority_payloads[11], "status": 200, "response": authority_payloads[6]},
            {"route_id": "document.get", "request": document_get, "status": 200, "response": document},
            {"route_id": "document.create", "request": authority_payloads[25], "status": 201, "response": document},
            {"route_id": "document.update", "request": authority_payloads[26], "status": 200, "response": document_updated},
            {"route_id": "node.list", "request": authority_payloads[12], "status": 200, "response": authority_payloads[7]},
            {"route_id": "node.get", "request": node_get, "status": 200, "response": node},
            {"route_id": "node.create", "request": authority_payloads[27], "status": 201, "response": node},
            {"route_id": "node.update", "request": authority_payloads[28], "status": 200, "response": node_updated},
            {"route_id": "node.delete", "request": authority_payloads[29], "status": 200, "response": node_delete},
            {"route_id": "relation.list", "request": authority_payloads[13], "status": 200, "response": authority_payloads[8]},
            {"route_id": "relation.create", "request": authority_payloads[30], "status": 201, "response": relation},
            {"route_id": "relation.delete", "request": authority_payloads[31], "status": 200, "response": relation_delete},
            {"route_id": "document.revision.list", "request": authority_payloads[17], "status": 200, "response": authority_payloads[9]},
            {"route_id": "node.revision.list", "request": authority_payloads[18], "status": 200, "response": node_revision_page},
            {"route_id": "revision.get", "request": revision_get, "status": 200, "response": revision},
            {"route_id": "revision.content", "request": authority_payloads[20], "status": 200, "response": authority_payloads[21]},
            {"route_id": "document.revision.create", "request": authority_payloads[32], "status": 201, "response": revision},
            {"route_id": "node.revision.create", "request": authority_payloads[33], "status": 201, "response": node_revision},
            {"route_id": "publication.accept", "request": publication_command, "status": 200, "response": publication_result},
            {"route_id": "asset.metadata", "request": asset_query, "status": 200, "response": asset_metadata},
            {"route_id": "asset.range", "request": asset_range_query, "status": 200, "response": asset_range},
            {"route_id": "document.get", "request": unknown_document, "status": 404, "response": error("unknown_reference", "document does not exist")},
            {"route_id": "document.get", "request": cross_workspace_document, "status": 404, "response": error("cross_workspace", "document belongs to another workspace")},
            {"route_id": "workspace.update", "request": stale_workspace_update, "status": 409, "response": error("stale_cas", "workspace revision is stale")},
            {"route_id": "workspace.update", "request": reused_workspace_update, "status": 409, "response": error("operation_key_reuse", "operation key was already used with another payload")},
            {"route_id": "publication.accept", "request": incomplete_publication, "status": 422, "response": error("incomplete_publication", "Candidate is not eligible for Publication")},
        ],
    }
    write_json(out / "core-http.json", http)

    metas = [
        {"protocol_version": "1", "generation_id": "generation-1", "plugin_release_id": release, "deadline_at": "2026-08-27T00:05:00Z", "context": "control", "operation_id": "request-control-1"},
        {"protocol_version": "1", "generation_id": "generation-1", "plugin_release_id": release, "deadline_at": "2026-08-27T00:05:00Z", "context": "install", "operation_id": "request-install-1", "install_operation_id": "install-1", "install_lease_epoch": 7},
        {"protocol_version": "1", "generation_id": "generation-1", "plugin_release_id": release, "deadline_at": "2026-08-27T00:05:00Z", "context": "attempt", "operation_id": "request-attempt-1", "job_id": "job-1", "step_id": "step-1", "attempt_id": "attempt-1", "lease_epoch": 9},
    ]
    vectors = []
    for meta in metas:
        projection = {"schema": "operation-context-identity/v1", "protocol_version": "1", "context": meta["context"], "generation_id": meta["generation_id"], "plugin_release_id": meta["plugin_release_id"]}
        if meta["context"] == "install":
            projection["install_operation_id"] = meta["install_operation_id"]
        elif meta["context"] == "attempt":
            projection.update({key: meta[key] for key in ("job_id", "step_id", "attempt_id")})
        identity = sha256(b"plotpilot-operation-context/v1\n" + jcs(projection))
        vectors.append({"profile": meta["context"], "expected_lease_epoch": meta.get("install_lease_epoch", meta.get("lease_epoch")), "meta": meta, "projection": projection, "context_identity": identity})
    write_json(out / "context-identity.json", {"schema": "operation-context-identity-golden/v1", "vectors": vectors})

    ui_tree = {
        "schema": "plugin-ui-tree/v1", "tree_id": "tree-ingress-1", "render_seq": 1,
        "root": {"component": "stack", "key": "root", "props": {"direction": "vertical", "row_gap": 8}, "children": [
            {"component": "button", "key": "run", "props": {"label": "Run", "tone": "primary", "disabled": False}, "children": [], "event_ids": ["click"]}
        ], "event_ids": []},
    }
    freshness = {"generation_id": "generation-1", "plugin_release_id": release, "workspace_id": "ws-1", "workspace_revision_id": None, "plan_revision_id": None}
    ui_intent = {"schema": "plugin-ui-intent/v1", "intent_id": "intent-ingress-1", "intent_seq": 1, "render_seq": 1, "action_id": "click", "event_type": "click", "intent_kind": "set_view_state", "capability_id": None, "payload_asset_id": None, "operation_key": "operation-ui-1", "freshness": freshness}
    ui_ack = {"schema": "plugin-ui-ack/v1", "intent_id": "intent-ingress-1", "accepted": True, "error_code": None, "core_event_seq": 1, "job_id": None}
    job_snapshot = {"schema": "job-snapshot/v1", "job_id": "job-ingress-1", "workspace_id": "ws-1", "job_state": "queued", "job_revision": 1, "steps": [{"step_id": "step-1", "state": "pending", "revision": 1}], "attempts": [{"attempt_id": "attempt-1", "state": "created", "lease_epoch": 1}], "candidate_ids": [], "current_checkpoint_id": None, "stream_high_waters": [], "core_event_high_water": 0, "job_event_high_water": 0, "created_at": "2026-08-27T00:00:00Z", "snapshot_hash": ""}
    job_snapshot["snapshot_hash"] = self_hash("job-snapshot/v1", job_snapshot, "snapshot_hash")
    write_json(out / "ui-ingress.json", {"schema": "plugin-ui-ingress-golden/v1", "tree": ui_tree, "intent": ui_intent, "ack": ui_ack, "job_snapshot": job_snapshot})

    export_value = {
        "schema": "export-current-revisions/v1", "workspace_id": "ws-1", "core_snapshot_revision": 7,
        "ordered_revisions": [
            {"ordinal": 0, "document_id": "doc-a", "document_type": "core.chapter", "title": "Chapter A", "revision_id": "rev-a", "content_asset_id": "asset-content-a", "content_hash": h1, "mime": "text/plain", "encoding": "utf-8"},
            {"ordinal": 1, "document_id": "doc-b", "document_type": "core.chapter", "title": "Chapter B", "revision_id": "rev-b", "content_asset_id": "asset-content-b", "content_hash": h2, "mime": "text/plain", "encoding": "utf-8"},
        ],
        "generated_at": "2026-08-27T00:00:00Z",
    }
    export_bytes = jcs(export_value)
    export_hash = sha256(export_bytes)
    export_snapshot: dict[str, Any] = {
        "schema": "run-snapshot/v1", "snapshot_id": "snapshot-export-1", "core_contract_version": "1.2.0", "workspace_id": "ws-1",
        "scope": {"document_id": None, "node_id": None, "operation": "writing.export/v1"},
        "input_revisions": [
            {"document_id": "doc-a", "revision_id": "rev-a", "content_hash": h1},
            {"document_id": "doc-b", "revision_id": "rev-b", "content_hash": h2},
        ],
        "plan_revision_id": "plan-export-r1", "plugin_releases": [], "plugin_settings_revisions": [], "data_bindings": [], "skill_releases": [],
        "model_profile_revision_id": None, "parameters_asset_id": "asset-export-current-revisions",
        "asset_hashes": [{"asset_id": "asset-export-current-revisions", "sha256": export_hash}],
        "request_key": "", "run_intent_id": "export-intent-1", "created_at": "2026-08-27T00:00:00Z",
    }
    request_bytes = (
        "request-key/v1\nws-1\nwriting.export/v1\nnull\nnull\n"
        f"doc-a=rev-a={h1},doc-b=rev-b={h2}\nplan-export-r1\n{export_hash}\nexport-intent-1\n"
    ).encode("ascii")
    export_snapshot["request_key"] = sha256(request_bytes)
    export_snapshot["snapshot_hash"] = sha256(b"run-snapshot/v1\n" + jcs(export_snapshot))
    write(out / "export-current-revisions.json", export_bytes)
    write_json(out / "export-run-snapshot.json", export_snapshot)
    write_json(out / "expected.json", {"context_identities": {item["profile"]: item["context_identity"] for item in vectors}, "export_asset_sha256": export_hash, "export_snapshot_hash": export_snapshot["snapshot_hash"], "authority_payload_count": len(authority_payloads), "http_exchange_count": len(http["exchanges"]), "http_failure_exchange_count": sum(1 for item in http["exchanges"] if item["status"] >= 400), "ui_ingress_fixture_count": 4})


def v2_public_surface_vectors() -> None:
    """Emit deterministic positive vectors for the additive M4/M5 surface."""

    out = GOLDEN / "m4-m5-public-surface-v2"
    # Candidate preview is a byte-bearing response.  Keep the positive vector
    # honest: the candidate payload hash and the returned range hash are the
    # SHA-256 of the exact five bytes encoded below.
    h_payload = sha256(b"hello")
    h_replace_payload = sha256(b"replace")
    h_structure_payload = sha256(b"structure")
    h_relation_payload = sha256(b"relation")
    h_base = "1" * 64
    h_base_other = "7" * 64
    h_replace_base = "8" * 64
    h_structure_base = "9" * 64
    h_relation_base = "a" * 64
    h_revision = "2" * 64
    h_replace_revision = "b" * 64
    h_structure_revision = "c" * 64
    h_relation_revision = "d" * 64
    h_incomplete_revision = "e" * 64
    h_snapshot = "4" * 64
    h_release = "5" * 64
    h_package = "6" * 64
    h_stream = sha256(b"hello")

    def candidate_variant(
        candidate_id: str,
        item_kind: str,
        entity_kind: str,
        entity_id: str,
        mode: str,
        payload_schema: str,
        payload_hash: str,
        payload_asset_id: str,
        revision_id: str,
        base_hash: str,
        *,
        write_set: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema": "candidate/v2",
            "candidate_id": candidate_id,
            "workspace_id": "ws-1",
            "item_kind": item_kind,
            "target": {"workspace_id": "ws-1", "entity_kind": entity_kind, "entity_id": entity_id},
            "mutation": {"mode": mode, "payload_schema": payload_schema, "payload_hash": payload_hash},
            "payload_asset_id": payload_asset_id,
            "base": {"revision_id": revision_id, "content_hash": base_hash},
            "write_set": write_set or [{"workspace_id": "ws-1", "entity_kind": entity_kind, "entity_id": entity_id, "revision_id": revision_id, "content_hash": base_hash}],
            "parent_candidate_ids": [],
            "source_refs": [{"workspace_id": "ws-source", "source_type": "candidate", "source_id": "source-candidate", "revision_or_hash": "source-rev-1"}],
            "status": "complete",
            "publication_eligibility": "eligible",
            "created_at": "2026-08-30T00:00:00Z",
            "source_job_id": "job-v2",
        }

    # The text-patch Candidate intentionally has two target identities.  The
    # complete tuple order and the per-entry base values are part of the
    # deterministic same-transaction CAS surface.
    candidate = candidate_variant(
        "candidate-v2", "document", "document", "doc-a", "text_patch", "core/document-text/v2", h_payload,
        "asset-candidate-v2", "rev-1", h_base,
        write_set=[
            {"workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-a", "revision_id": "rev-1", "content_hash": h_base},
            {"workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-b", "revision_id": "rev-b", "content_hash": h_base_other},
        ],
    )
    candidate_text_patch = candidate
    candidate_replace = candidate_variant(
        "candidate-replace-v2", "document", "document", "doc-replace", "replace", "core/document-text/v2", h_replace_payload,
        "asset-candidate-replace-v2", "rev-replace-1", h_replace_base,
    )
    candidate_structure_patch = candidate_variant(
        "candidate-structure-v2", "node_structure", "node_structure", "node-a", "structure_patch", "core/node-structure/v2", h_structure_payload,
        "asset-candidate-structure-v2", "rev-structure-1", h_structure_base,
    )
    candidate_relation_patch = candidate_variant(
        "candidate-relation-v2", "relation_set", "relation_set", "relation-a", "relation_patch", "core/relation-set/v2", h_relation_payload,
        "asset-candidate-relation-v2", "rev-relation-1", h_relation_base,
    )
    candidate_cross_workspace_source = {**candidate, "candidate_id": "candidate-cross-source", "source_refs": [{"workspace_id": "ws-other", "source_type": "external", "source_id": "source-doc", "revision_or_hash": h_snapshot}]}
    candidate_list_result = {
        "schema": "candidate-list-result/v2",
        "workspace_id": "ws-1",
        "items": [candidate],
        "next_cursor": "candidate/ws-1/1",
        "cursor_domain": "candidate",
        "total": 1,
    }
    candidate_list_query = {"schema": "candidate-list-query/v2", "workspace_id": "ws-1", "cursor": None, "limit": 50}
    candidate_get_query = {"schema": "candidate-get-query/v2", "workspace_id": "ws-1", "candidate_id": "candidate-v2"}
    candidate_get_result = {"schema": "candidate-get-result/v2", "workspace_id": "ws-1", "candidate": candidate}
    candidate_preview_query = {"schema": "candidate-preview-query/v2", "workspace_id": "ws-1", "candidate_id": "candidate-v2", "offset": 0, "length": 5}
    candidate_preview_result = {
        "schema": "candidate-preview-result/v2",
        "workspace_id": "ws-1",
        "candidate_id": "candidate-v2",
        "payload_asset_id": "asset-candidate-v2",
        "payload_hash": h_payload,
        "offset": 0,
        "length": 5,
        "total_length": 5,
        "base64_chunk": "aGVsbG8=",
        "next_offset": None,
        "content_hash": h_payload,
    }
    candidate_partial = {**candidate, "candidate_id": "candidate-partial", "status": "partial", "publication_eligibility": "review_only"}
    incomplete_candidate = {
        **candidate,
        "schema": "candidate/v2",
        "candidate_id": "candidate-incomplete",
        "item_kind": "incomplete_stream",
        "mutation": {"mode": "replace", "payload_schema": "core/document-text/v2", "payload_hash": h_stream},
        "payload_asset_id": "asset-stream-v2",
        "status": "partial",
        "publication_eligibility": "eligible",
    }
    write_json(out / "candidate.json", {"schema": "m4-m5-public-surface-golden/v2", "candidate": candidate, "candidate_text_patch": candidate_text_patch, "candidate_replace": candidate_replace, "candidate_structure_patch": candidate_structure_patch, "candidate_relation_patch": candidate_relation_patch, "candidate_cross_workspace_source": candidate_cross_workspace_source, "candidate_partial": candidate_partial, "candidate_incomplete_stream": incomplete_candidate, "candidate_list_query": candidate_list_query, "candidate_list_result": candidate_list_result, "candidate_get_query": candidate_get_query, "candidate_get_result": candidate_get_result, "candidate_preview_query": candidate_preview_query, "candidate_preview_result": candidate_preview_result})

    review_query = {"schema": "candidate-review-query/v2", "workspace_id": "ws-1", "candidate_id": "candidate-v2", "include_lineage": True, "include_preview": True}
    review_command = {"schema": "candidate-review-command/v2", "operation_key": "review-op-v2", "workspace_id": "ws-1", "candidate_id": "candidate-v2", "decision": "approve", "decided_by": "user-v2", "expected_status": "complete"}
    review_result = {"schema": "candidate-review-result/v2", "operation_key": "review-op-v2", "workspace_id": "ws-1", "candidate_id": "candidate-v2", "decision": "approve", "status": "reviewed", "publication_eligibility": "eligible", "idempotent": False, "review_revision": 2}
    write_json(out / "review.json", {"schema": "m4-m5-public-surface-golden/v2", "query": review_query, "command": review_command, "result": review_result})

    def publication_pair(candidate_value: dict[str, Any], operation_key: str, publication_id: str, revision_id: str, revision_number: int, final_hash: str, receipt_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        command = {"schema": "publication-command/v2", "publication_operation_key": operation_key, "workspace_id": "ws-1", "candidate_id": candidate_value["candidate_id"], "accepted_by": "user-v2"}
        result = {"schema": "publication-result/v2", "publication_id": publication_id, "publication_operation_key": operation_key, "candidate_id": candidate_value["candidate_id"], "workspace_id": "ws-1", "entity_kind": candidate_value["target"]["entity_kind"], "entity_id": candidate_value["target"]["entity_id"], "revision_id": revision_id, "revision_number": revision_number, "cas": {"base_revision_id": candidate_value["base"]["revision_id"], "base_content_hash": candidate_value["base"]["content_hash"], "revision_id": revision_id, "revision_number": revision_number, "content_hash": final_hash}, "content_hash": final_hash, "provenance_receipt_id": receipt_id, "idempotent": False}
        return command, result

    publication_command, publication_result = publication_pair(candidate, "publication-op-v2", "publication-v2", "rev-2", 2, h_revision, "receipt-v2")
    replace_publication_command, replace_publication_result = publication_pair(candidate_replace, "publication-op-replace-v2", "publication-replace-v2", "rev-replace-2", 2, h_replace_revision, "receipt-replace-v2")
    structure_publication_command, structure_publication_result = publication_pair(candidate_structure_patch, "publication-op-structure-v2", "publication-structure-v2", "rev-structure-2", 2, h_structure_revision, "receipt-structure-v2")
    relation_publication_command, relation_publication_result = publication_pair(candidate_relation_patch, "publication-op-relation-v2", "publication-relation-v2", "rev-relation-2", 2, h_relation_revision, "receipt-relation-v2")
    incomplete_publication_command = {**publication_command, "publication_operation_key": "publication-op-incomplete-v2", "candidate_id": "candidate-incomplete"}
    incomplete_publication_result = {**publication_result, "publication_id": "publication-incomplete-v2", "publication_operation_key": "publication-op-incomplete-v2", "candidate_id": "candidate-incomplete", "revision_id": "rev-3", "revision_number": 3, "cas": {**publication_result["cas"], "revision_id": "rev-3", "revision_number": 3, "content_hash": h_incomplete_revision}, "content_hash": h_incomplete_revision, "provenance_receipt_id": "receipt-incomplete-v2"}
    authority_query = {"schema": "core-authority-query/v2", "workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-a", "include_assets": True}
    authority_result = {"schema": "core-authority-result/v2", "workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-a", "revision_id": "rev-2", "revision_number": 2, "content_asset_id": "asset-revision-v2", "content_hash": h_revision, "derived_from_candidate_id": "candidate-v2"}
    write_json(out / "publication.json", {"schema": "m4-m5-public-surface-golden/v2", "authority_query": authority_query, "authority_result": authority_result, "command_complete": publication_command, "result_complete": publication_result, "command_replace": replace_publication_command, "result_replace": replace_publication_result, "command_structure_patch": structure_publication_command, "result_structure_patch": structure_publication_result, "command_relation_patch": relation_publication_command, "result_relation_patch": relation_publication_result, "command_partial": {**publication_command, "publication_operation_key": "publication-op-partial-v2", "candidate_id": "candidate-partial"}, "command_incomplete_stream": incomplete_publication_command, "result_incomplete_stream": incomplete_publication_result})

    receipt_parent = {"receipt_id": "receipt-parent-v2", "parent_receipt_ids": []}
    receipt_parent["receipt_hash"] = self_hash("story-state-receipt/v2", receipt_parent, "receipt_hash")
    receipt_root = {"receipt_id": "receipt-v2", "parent_receipt_ids": [receipt_parent["receipt_id"]]}
    receipt_root["receipt_hash"] = self_hash("story-state-receipt/v2", receipt_root, "receipt_hash")
    unreachable_receipt = {"receipt_id": "receipt-unreachable-v2", "parent_receipt_ids": []}
    unreachable_receipt["receipt_hash"] = self_hash("story-state-receipt/v2", unreachable_receipt, "receipt_hash")

    projection = {
        "schema": "story-state-projection-input/v2",
        "projection_input_id": "projection-v2",
        "workspace_id": "ws-1",
        "publication": {"publication_id": "publication-v2", "candidate_id": "candidate-v2", "revision_id": "rev-2", "revision_number": 2, "content_hash": h_revision},
        "candidate": {"candidate_id": "candidate-v2", "target": candidate["target"], "payload_asset_id": "asset-candidate-v2", "payload_hash": h_payload, "status": "complete"},
        "current_revision": {"revision_id": "rev-2", "content_asset_id": "asset-revision-v2", "content_hash": h_revision, "revision_number": 2},
        "assets": [{"asset_id": "asset-candidate-v2", "sha256": h_payload, "role": "candidate.payload"}, {"asset_id": "asset-revision-v2", "sha256": h_revision, "role": "revision.content"}],
        "provenance": {"receipt_id": "receipt-v2", "receipt_hash": receipt_root["receipt_hash"], "run_snapshot_hash": h_snapshot, "release_id": h_release, "package_hash": h_package, "parent_receipt_ids": receipt_root["parent_receipt_ids"]},
        "receipt_closure": [receipt_parent, receipt_root],
        "derived_at": "2026-08-30T00:00:01Z",
    }
    projection_with_unreachable = {**projection, "receipt_closure": [*projection["receipt_closure"], unreachable_receipt]}
    write_json(out / "story-state.json", {"schema": "m4-m5-public-surface-golden/v2", "projection": projection, "projection_with_unreachable_receipt": projection_with_unreachable})

    target = {"workspace_id": "ws-1", "entity_kind": "document", "entity_id": "doc-a"}
    stream = {"stream_id": "stream-v2", "output_role": "chapter", "target": target, "acked_prefix_seq": 2, "acked_bytes": 5, "acked_prefix_hash": h_stream}
    snapshot = {"job_id": "job-v2", "workspace_id": "ws-1", "state": "running", "job_revision": 3, "writer_epoch": 1, "current_attempt_id": "attempt-v2", "candidate_ids": ["candidate-v2"], "checkpoint_id": None, "stream_high_waters": [stream], "job_event_high_water": 5, "core_event_high_water": 3, "created_at": "2026-08-30T00:00:00Z", "updated_at": "2026-08-30T00:00:01Z", "snapshot_hash": ""}
    snapshot["snapshot_hash"] = sha256(b"job-snapshot/v2\n" + jcs({key: value for key, value in snapshot.items() if key != "snapshot_hash"}))
    job_list_query = {"schema": "job-list-query/v2", "workspace_id": "ws-1", "cursor": None, "limit": 50, "state": None}
    job_list_result = {"schema": "job-list-result/v2", "workspace_id": "ws-1", "items": [snapshot], "next_cursor": "job/job-v2/5", "cursor_domain": "job", "total": 1}
    job_snapshot_query = {"schema": "job-snapshot-query/v2", "workspace_id": "ws-1", "job_id": "job-v2"}
    job_snapshot_result = {"schema": "job-snapshot-result/v2", "workspace_id": "ws-1", "job_id": "job-v2", "snapshot": snapshot, "cursor": "job/job-v2/5"}
    job_start = {"schema": "job-start-command/v2", "operation_key": "job-start-op-v2", "workspace_id": "ws-1", "job_id": "job-v2", "capability_id": "writing.chapter/v2", "run_snapshot_asset_id": "asset-run-snapshot-v2", "writer_epoch": 1}
    job_control = {"schema": "job-control-command/v2", "operation_key": "job-pause-op-v2", "workspace_id": "ws-1", "job_id": "job-v2", "expected_job_revision": 3, "reason": "user requested pause", "command": "pause", "resume_intent_id": None}
    job_command_result = {"schema": "job-command-result/v2", "operation_key": "job-start-op-v2", "workspace_id": "ws-1", "job_id": "job-v2", "command": "start", "accepted": True, "idempotent": False, "terminal_known": False, "state": "running", "job_revision": 3, "snapshot_cursor": "job/job-v2/5"}
    event1 = {"event_id": "job-event-v2-1", "job_id": "job-v2", "job_event_seq": 1, "event_type": "job.progress", "aggregate_revision": 2, "payload_asset_id": None, "payload_hash": None, "occurred_at": "2026-08-30T00:00:00Z"}
    event2 = {"event_id": "job-event-v2-2", "job_id": "job-v2", "job_event_seq": 2, "event_type": "job.progress", "aggregate_revision": 3, "payload_asset_id": None, "payload_hash": None, "occurred_at": "2026-08-30T00:00:01Z"}
    event5 = {"event_id": "job-event-v2-5", "job_id": "job-v2", "job_event_seq": 5, "event_type": "job.progress", "aggregate_revision": 6, "payload_asset_id": None, "payload_hash": None, "occurred_at": "2026-08-30T00:00:04Z"}
    event6 = {"event_id": "job-event-v2-6", "job_id": "job-v2", "job_event_seq": 6, "event_type": "job.progress", "aggregate_revision": 7, "payload_asset_id": None, "payload_hash": None, "occurred_at": "2026-08-30T00:00:05Z"}
    job_event_query = {"schema": "job-event-page-query/v2", "workspace_id": "ws-1", "job_id": "job-v2", "after_cursor": "job/job-v2/0", "after_job_event_seq": 0, "limit": 50}
    job_event_page = {"schema": "job-event-page-result/v2", "workspace_id": "ws-1", "job_id": "job-v2", "events": [event1, event2], "next_cursor": "job/job-v2/2", "high_water_seq": 2, "cursor_domain": "job"}
    sse_replay_query = {"schema": "job-sse-recovery-query/v2", "workspace_id": "ws-1", "job_id": "job-v2", "after_seq": 1, "last_event_id": "job/job-v2/1", "requested_cursor_domain": "job"}
    sse_replay = {"schema": "job-sse-recovery-result/v2", "workspace_id": "ws-1", "job_id": "job-v2", "requested_after_seq": 1, "replay_floor_seq": 1, "durable_high_water_seq": 2, "gap": False, "snapshot_required": False, "snapshot": None, "snapshot_cursor": "job/job-v2/2", "tail": [event2]}
    gap_snapshot = {**snapshot, "job_revision": 4, "job_event_high_water": 4, "snapshot_hash": ""}
    gap_snapshot["snapshot_hash"] = sha256(b"job-snapshot/v2\n" + jcs({key: value for key, value in gap_snapshot.items() if key != "snapshot_hash"}))
    sse_gap_query = {"schema": "job-sse-recovery-query/v2", "workspace_id": "ws-1", "job_id": "job-v2", "after_seq": 0, "last_event_id": "job/job-v2/0", "requested_cursor_domain": "job"}
    sse_gap = {"schema": "job-sse-recovery-result/v2", "workspace_id": "ws-1", "job_id": "job-v2", "requested_after_seq": 0, "replay_floor_seq": 2, "durable_high_water_seq": 6, "gap": True, "snapshot_required": True, "snapshot": gap_snapshot, "snapshot_cursor": "job/job-v2/4", "tail": [event5, event6]}
    write_json(out / "job.json", {"schema": "m4-m5-public-surface-golden/v2", "snapshot": snapshot, "list_query": job_list_query, "list_result": job_list_result, "snapshot_query": job_snapshot_query, "snapshot_result": job_snapshot_result, "start": job_start, "control": job_control, "command_result": job_command_result, "event_query": job_event_query, "event_page": job_event_page, "sse_replay_query": sse_replay_query, "sse_replay": sse_replay, "sse_gap_query": sse_gap_query, "sse_gap": sse_gap})

    plugin = {"plugin_id": "com.plotpilot.demo", "release_id": h_release, "package_hash": h_package, "state": "active", "generation_id": "generation-1", "capabilities": ["writing.chapter/v2"], "updated_at": "2026-08-30T00:00:00Z"}
    discovery_query = {"schema": "plugin-discovery-query/v2", "cursor": None, "limit": 50, "state": None}
    discovery_result = {"schema": "plugin-discovery-result/v2", "items": [plugin], "next_cursor": "core/5", "cursor_domain": "core", "total": 1}
    discovery_error_request = {"schema": "plugin-discovery-query/v2", "cursor": "core/1", "limit": 50, "state": None}
    discovery_error = {"schema": "plugin-http-error/v2", "error_code": "cursor_domain_mismatch", "message": "plugin discovery cursor must use the core domain", "retryable": False, "operation_key": None}
    install = {"schema": "plugin-lifecycle-command/v2", "operation_key": "plugin-install-op-v2", "plugin_id": "com.plotpilot.demo", "action": "install", "release_id": h_release, "package_hash": h_package, "expected_generation_id": None, "target_generation_id": "generation-1"}
    upgrade = {"schema": "plugin-lifecycle-command/v2", "operation_key": "plugin-upgrade-op-v2", "plugin_id": "com.plotpilot.demo", "action": "upgrade", "release_id": "7" * 64, "package_hash": "8" * 64, "expected_generation_id": "generation-1", "target_generation_id": "generation-2"}
    retire = {"schema": "plugin-lifecycle-command/v2", "operation_key": "plugin-retire-op-v2", "plugin_id": "com.plotpilot.demo", "action": "retire", "release_id": None, "package_hash": None, "expected_generation_id": "generation-2", "target_generation_id": "generation-2"}
    rollback = {"schema": "plugin-lifecycle-command/v2", "operation_key": "plugin-rollback-op-v2", "plugin_id": "com.plotpilot.demo", "action": "rollback", "release_id": h_release, "package_hash": h_package, "expected_generation_id": "generation-2", "target_generation_id": "generation-1"}
    lifecycle_result_install = {"schema": "plugin-lifecycle-result/v2", "operation_key": "plugin-install-op-v2", "plugin_id": "com.plotpilot.demo", "action": "install", "state": "active", "generation_id": "generation-1", "idempotent": False, "failure_code": None}
    lifecycle_result_upgrade = {"schema": "plugin-lifecycle-result/v2", "operation_key": "plugin-upgrade-op-v2", "plugin_id": "com.plotpilot.demo", "action": "upgrade", "state": "active", "generation_id": "generation-2", "idempotent": False, "failure_code": None}
    lifecycle_result_retire = {"schema": "plugin-lifecycle-result/v2", "operation_key": "plugin-retire-op-v2", "plugin_id": "com.plotpilot.demo", "action": "retire", "state": "retired", "generation_id": "generation-2", "idempotent": False, "failure_code": None}
    lifecycle_result_rollback = {"schema": "plugin-lifecycle-result/v2", "operation_key": "plugin-rollback-op-v2", "plugin_id": "com.plotpilot.demo", "action": "rollback", "state": "active", "generation_id": "generation-1", "idempotent": False, "failure_code": None}
    write_json(out / "plugin.json", {"schema": "m4-m5-public-surface-golden/v2", "plugin": plugin, "discovery_query": discovery_query, "discovery_result": discovery_result, "discovery_error": discovery_error, "discovery_error_request": discovery_error_request, "install": install, "upgrade": upgrade, "retire": retire, "rollback": rollback, "lifecycle_result_install": lifecycle_result_install, "lifecycle_result_upgrade": lifecycle_result_upgrade, "lifecycle_result_retire": lifecycle_result_retire, "lifecycle_result_rollback": lifecycle_result_rollback})

    # One frozen success exchange per route keeps the method matrix executable
    # without introducing a second fixture/transport format.  The request and
    # response objects are the same positive DTOs validated above.
    job_pause = {**job_control, "operation_key": "job-pause-op-v2", "command": "pause"}
    job_resume = {**job_control, "operation_key": "job-resume-op-v2", "expected_job_revision": 4, "command": "resume", "resume_intent_id": "resume-intent-v2"}
    job_cancel = {**job_control, "operation_key": "job-cancel-op-v2", "expected_job_revision": 5, "command": "cancel", "resume_intent_id": None}
    job_pause_result = {**job_command_result, "operation_key": "job-pause-op-v2", "command": "pause", "state": "paused", "job_revision": 4}
    job_resume_result = {**job_command_result, "operation_key": "job-resume-op-v2", "command": "resume", "state": "running", "job_revision": 5}
    job_cancel_result = {**job_command_result, "operation_key": "job-cancel-op-v2", "command": "cancel", "state": "cancelling", "job_revision": 6}
    http_exchanges = [
        {"route_id": "candidate.list", "status": 200, "request": candidate_list_query, "response": candidate_list_result},
        {"route_id": "candidate.get", "status": 200, "request": candidate_get_query, "response": candidate_get_result},
        {"route_id": "candidate.review", "status": 200, "request": review_command, "response": review_result},
        {"route_id": "candidate.preview", "status": 200, "request": candidate_preview_query, "response": candidate_preview_result},
        {"route_id": "publication.accept", "status": 200, "request": publication_command, "response": publication_result},
        {"route_id": "story-state.projection-input", "status": 200, "request": authority_query, "response": projection},
        {"route_id": "job.list", "status": 200, "request": job_list_query, "response": job_list_result},
        {"route_id": "job.get", "status": 200, "request": job_snapshot_query, "response": job_snapshot_result},
        {"route_id": "job.start", "status": 201, "request": job_start, "response": job_command_result},
        {"route_id": "job.pause", "status": 200, "request": job_pause, "response": job_pause_result},
        {"route_id": "job.resume", "status": 200, "request": job_resume, "response": job_resume_result},
        {"route_id": "job.cancel", "status": 200, "request": job_cancel, "response": job_cancel_result},
        {"route_id": "job.events", "status": 200, "request": job_event_query, "response": job_event_page},
        {"route_id": "job.sse-recovery", "status": 200, "request": sse_replay_query, "response": sse_replay},
        {"route_id": "plugin.discovery", "status": 200, "request": discovery_query, "response": discovery_result},
        {"route_id": "plugin.install", "status": 200, "request": install, "response": lifecycle_result_install},
        {"route_id": "plugin.upgrade", "status": 200, "request": upgrade, "response": lifecycle_result_upgrade},
        {"route_id": "plugin.retire", "status": 200, "request": retire, "response": lifecycle_result_retire},
        {"route_id": "plugin.rollback", "status": 200, "request": rollback, "response": lifecycle_result_rollback},
    ]
    discovery_error_exchange = {"route_id": "plugin.discovery", "status": 400, "request": discovery_error_request, "response": discovery_error}
    write_json(out / "http.json", {"schema": "m4-m5-public-surface-http-golden/v2", "exchange_count": len(http_exchanges), "exchanges": http_exchanges, "error_exchanges": [discovery_error_exchange]})

    files = {path.name: sha256(path.read_bytes()) for path in sorted(out.glob("*.json")) if path.name != "expected.json"}
    write_json(out / "expected.json", {"schema": "m4-m5-public-surface-golden-index/v2", "fixture_files": files, "candidate_ids": [candidate["candidate_id"], candidate_partial["candidate_id"], incomplete_candidate["candidate_id"], candidate_replace["candidate_id"], candidate_structure_patch["candidate_id"], candidate_relation_patch["candidate_id"]], "mutation_kinds": ["replace", "text_patch", "structure_patch", "relation_patch"], "publication_paths": ["publication.accept"], "cursor_domains": ["candidate", "job", "core"], "http_exchange_count": len(http_exchanges), "snapshot_hash": snapshot["snapshot_hash"], "gap_snapshot_hash": gap_snapshot["snapshot_hash"]})


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
    contract_publication_vectors()
    v2_public_surface_vectors()
    examples(snapshot)
    print("golden vectors and positive fixtures written (including additive v2 surface)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
