from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from plotpilot_plugin_sdk import hash_jcs, sha256_hex
from plotpilot_plugin_sdk.package import build_files_sha256
from plotpilot_plugin_sdk.verifier import request_key, snapshot_hash
from plotpilot_prompt_skill_runtime import (
    AssetRef,
    BrokerSkillResult,
    ChainAnchor,
    FrozenModelInvocation,
    PromptSkillRuntime,
    SkillPackage,
    SQLiteSkillRepository,
)


def skill_package(skill_id: str, *, prompt: bytes) -> SkillPackage:
    files = {
        "skill.json": (
            '{"actions":["rewrite"],"display_name":"Test Skill","schema":"plotpilot-skill/v1",'
            f'"skill_id":"{skill_id}","stage":"draft","version":"1.0.0"}}\n'
        ).encode(),
        "prompt.txt": prompt,
    }
    files["files.sha256"] = build_files_sha256(files)
    return SkillPackage.from_files(files)


class Authority:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute(
            "CREATE TABLE schema_migration("
            "migration_id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,"
            "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        self.lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    @contextmanager
    def read_connection(self):
        with self.lock:
            yield self.connection


def test_runtime_binds_closed_model_receipt_before_durable_attribution() -> None:
    package = skill_package("skill.runtime", prompt=b"runtime\n")
    input_asset = AssetRef("asset-input", b"input")
    output_asset = AssetRef("asset-output", b"output")
    response_asset = AssetRef("asset-response", b"provider-response")
    assets = {
        input_asset.asset_id: input_asset,
        output_asset.asset_id: output_asset,
        response_asset.asset_id: response_asset,
    }
    snapshot = {
        "schema": "run-snapshot/v1",
        "snapshot_id": "snapshot-1",
        "core_contract_version": "1.0.0",
        "workspace_id": "workspace-1",
        "scope": {"document_id": None, "node_id": None, "operation": "rewrite"},
        "input_revisions": [],
        "plan_revision_id": "plan-1",
        "plugin_releases": [
            {
                "plugin_id": "com.plotpilot.prompt-skill-runtime",
                "release_id": "1" * 64,
                "package_hash": "2" * 64,
                "data_generation_id": None,
            }
        ],
        "plugin_settings_revisions": [],
        "data_bindings": [],
        "skill_releases": [
            {
                "skill_id": package.skill_id,
                "release_id": package.release_id,
                "package_hash": package.package_hash,
                "parameters_asset_id": None,
                "order": 1,
            }
        ],
        "model_profile_revision_id": "profile-revision-1",
        "parameters_asset_id": None,
        "asset_hashes": [{"asset_id": input_asset.asset_id, "sha256": input_asset.sha256}],
        "request_key": "",
        "run_intent_id": "intent-1",
        "created_at": "2026-08-28T00:00:00Z",
        "snapshot_hash": "",
    }
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    generation = {
        "schema": "plugin-generation/v1",
        "generation_id": "generation-1",
        "core_api_version": "1.0.0",
        "members": [
            {
                "plugin_id": "com.plotpilot.prompt-skill-runtime",
                "release_id": "1" * 64,
                "package_hash": "2" * 64,
                "data_generation_id": None,
                "ui_bundle_hash": None,
                "global_settings_revision_id": None,
                "settings_schema_hash": None,
                "data_bundle_asset_id": None,
            },
            {
                "plugin_id": "com.plotpilot.provider.test",
                "release_id": "3" * 64,
                "package_hash": "4" * 64,
                "data_generation_id": None,
                "ui_bundle_hash": None,
                "global_settings_revision_id": None,
                "settings_schema_hash": None,
                "data_bundle_asset_id": None,
            },
        ],
        "created_reason": "test",
        "created_at": "2026-08-28T00:00:00Z",
        "health_result_asset_id": "asset-health-1",
        "parent_generation_id": None,
        "base_generation_id": None,
    }

    class Generations:
        def get_generation(self, generation_id: str):
            assert generation_id == generation["generation_id"]
            return generation

        def generation_state(self):
            return {"current_generation_id": generation["generation_id"], "safe_mode": False}

    class Broker:
        execute_calls = 0
        reconcile_calls = 0

        def execute_skill(self, **kwargs):
            self.execute_calls += 1
            context = dict(kwargs["invocation_context"])
            receipt = {
                "schema": "model-receipt/v1",
                "receipt_id": "model-receipt-1",
                "invocation_id": "invocation-1",
                "invocation_key": kwargs["invocation_key"],
                "state": "receipted",
                "request_hash": "5" * 64,
                "response_hash": "6" * 64,
                "profile_revision_id": "profile-revision-1",
                "provider_plugin_id": "com.plotpilot.provider.test",
                "provider_release_id": "3" * 64,
                "endpoint": "https://provider.invalid/v1",
                "model": "test-model",
                "lifecycle": ["prepared", "receipted"],
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
                "cost": "0.001",
                "retry_count": 0,
                "stream_termination": "stop",
                "error": None,
                "input_context": context,
                "profile_revision": {"revision_id": "profile-revision-1"},
                "metadata": {"test": True},
                "response_asset_id": response_asset.asset_id,
                "recovered": False,
                "uncertain": False,
                "receipt_hash": "",
            }
            receipt["receipt_hash"] = hash_jcs(
                "model-receipt/v1",
                {key: value for key, value in receipt.items() if key != "receipt_hash"},
            )
            raw = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
            receipt_asset = AssetRef("asset-model-receipt", raw, sha256_hex(raw))
            assets[receipt_asset.asset_id] = receipt_asset
            invocation = FrozenModelInvocation(
                invocation_id=receipt["invocation_id"],
                invocation_key=receipt["invocation_key"],
                request_hash=receipt["request_hash"],
                response_asset_id=receipt["response_asset_id"],
                response_hash=receipt["response_hash"],
                profile_revision_id=receipt["profile_revision_id"],
                provider_plugin_id=receipt["provider_plugin_id"],
                provider_release_id=receipt["provider_release_id"],
                input_context=context,
                endpoint=receipt["endpoint"],
                model=receipt["model"],
                profile_revision=receipt["profile_revision"],
                max_retries=0,
                terminal_receipt_id=receipt["receipt_id"],
                terminal_receipt_hash=receipt["receipt_hash"],
            )
            return BrokerSkillResult(output_asset, receipt_asset, invocation, model_claimed=True)

        def reconcile_skill(self, **_kwargs):
            self.reconcile_calls += 1
            raise AssertionError("reconcile not expected")

    authority = Authority()
    repository = SQLiteSkillRepository(authority, asset_reader=assets.__getitem__)
    repository.migrate()
    repository.record_release(package)
    broker = Broker()
    runtime = PromptSkillRuntime(
        repository=repository,
        generation_port=Generations(),
        broker=broker,
        asset_reader=assets.__getitem__,
        package_reader=lambda _skill_id, _release_id: package,
    )
    prepared = runtime.prepare(
        job={"job_id": "job-1", "step_id": "step-1"},
        snapshot=snapshot,
        generation_id="generation-1",
        chain_id="chain-1",
        input_asset_id=input_asset.asset_id,
        anchor=ChainAnchor.bundle("bundle-1", "item-1"),
    )
    result = runtime.execute(prepared)
    assert result.receipts[0]["participated"] is True
    assert result.receipts[0]["model_claimed"] is True
    assert repository.skill_receipt_reader("chain-1")[0]["receipt_hash"] == result.receipts[0]["receipt_hash"]
    assert broker.execute_calls == 1
    assert runtime.execute(prepared).chain["chain_id"] == "chain-1"
    assert broker.execute_calls == 1
