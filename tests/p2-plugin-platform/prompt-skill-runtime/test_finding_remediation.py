from __future__ import annotations

import copy
import json
import sqlite3
import threading
from contextlib import contextmanager

import pytest
from plotpilot_plugin_sdk import ContractError, hash_jcs, sha256_hex
from plotpilot_plugin_sdk.package import build_files_sha256
from plotpilot_plugin_sdk.verifier import request_key, snapshot_hash
from plotpilot_prompt_skill_runtime import (
    AssetRef,
    BrokerSkillResult,
    ChainAnchor,
    FrozenModelInvocation,
    PreparedSkillRun,
    PromptSkillRuntime,
    SkillPackage,
    SQLiteSkillRepository,
    verify_model_receipt_asset,
)


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


def skill_package() -> SkillPackage:
    files = {
        "skill.json": (
            b'{"actions":["rewrite"],"display_name":"Finding Skill",'
            b'"schema":"plotpilot-skill/v1","skill_id":"skill.finding",'
            b'"stage":"draft","version":"1.0.0"}\n'
        ),
        "prompt.txt": b"finding remediation\n",
    }
    files["files.sha256"] = build_files_sha256(files)
    return SkillPackage.from_files(files)


class Generations:
    def __init__(self, generation: dict) -> None:
        self.generation = generation

    def get_generation(self, generation_id: str):
        assert generation_id == self.generation["generation_id"]
        return self.generation

    def generation_state(self):
        return {"current_generation_id": self.generation["generation_id"], "safe_mode": False}


class Harness:
    def __init__(self, broker: object) -> None:
        self.package = skill_package()
        self.input_asset = AssetRef("asset-input-finding", b"input")
        self.output_asset = AssetRef("asset-output-finding", b"output")
        self.response_asset = AssetRef("asset-response-finding", b"provider response")
        self.assets: dict[str, AssetRef] = {
            self.input_asset.asset_id: self.input_asset,
            self.output_asset.asset_id: self.output_asset,
            self.response_asset.asset_id: self.response_asset,
        }
        self.snapshot = {
            "schema": "run-snapshot/v1",
            "snapshot_id": "snapshot-finding",
            "core_contract_version": "1.0.0",
            "workspace_id": "workspace-finding",
            "scope": {"document_id": None, "node_id": None, "operation": "rewrite"},
            "input_revisions": [],
            "plan_revision_id": "plan-finding",
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
                    "skill_id": self.package.skill_id,
                    "release_id": self.package.release_id,
                    "package_hash": self.package.package_hash,
                    "parameters_asset_id": None,
                    "order": 1,
                }
            ],
            "model_profile_revision_id": "profile-finding",
            "parameters_asset_id": None,
            "asset_hashes": [
                {"asset_id": self.input_asset.asset_id, "sha256": self.input_asset.sha256}
            ],
            "request_key": "",
            "run_intent_id": "intent-finding",
            "created_at": "2026-08-30T00:00:00Z",
            "snapshot_hash": "",
        }
        self.snapshot["request_key"] = request_key(self.snapshot)
        self.snapshot["snapshot_hash"] = snapshot_hash(self.snapshot)
        self.generation = {
            "schema": "plugin-generation/v1",
            "generation_id": "generation-finding",
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
                    "plugin_id": "com.plotpilot.provider.finding",
                    "release_id": "3" * 64,
                    "package_hash": "4" * 64,
                    "data_generation_id": None,
                    "ui_bundle_hash": None,
                    "global_settings_revision_id": None,
                    "settings_schema_hash": None,
                    "data_bundle_asset_id": None,
                },
            ],
            "created_reason": "finding-remediation",
            "created_at": "2026-08-30T00:00:00Z",
            "health_result_asset_id": "asset-health-finding",
            "parent_generation_id": None,
            "base_generation_id": None,
        }
        self.authority = Authority()
        self.repository = SQLiteSkillRepository(self.authority, asset_reader=self.assets.__getitem__)
        self.repository.migrate()
        self.repository.record_release(self.package)
        self.broker = broker
        self.runtime = self.new_runtime()

    def new_runtime(self) -> PromptSkillRuntime:
        return PromptSkillRuntime(
            repository=self.repository,
            generation_port=Generations(self.generation),
            broker=self.broker,
            asset_reader=self.assets.__getitem__,
            package_reader=lambda _skill_id, _release_id: self.package,
        )

    def prepare(self, *, runtime: PromptSkillRuntime | None = None, chain_id: str = "chain-finding"):
        return (runtime or self.runtime).prepare(
            job={"job_id": "job-finding", "step_id": "step-finding"},
            snapshot=self.snapshot,
            generation_id=self.generation["generation_id"],
            chain_id=chain_id,
            input_asset_id=self.input_asset.asset_id,
            anchor=ChainAnchor.bundle("bundle-finding", "item-finding"),
        )

    def model_result(self, kwargs: dict, *, state: str = "executed") -> BrokerSkillResult:
        context = dict(kwargs["invocation_context"])
        receipt_state = "receipted" if state == "executed" else state
        response_asset_id = self.response_asset.asset_id if state == "executed" else None
        response_hash = "6" * 64 if state == "executed" else None
        receipt = {
            "schema": "model-receipt/v1",
            "receipt_id": f"model-receipt-{state}-{context['chain_id']}",
            "invocation_id": f"invocation-{state}-{context['chain_id']}",
            "invocation_key": kwargs["invocation_key"],
            "state": receipt_state,
            "request_hash": "5" * 64,
            "response_hash": response_hash,
            "profile_revision_id": "profile-finding",
            "provider_plugin_id": "com.plotpilot.provider.finding",
            "provider_release_id": "3" * 64,
            "endpoint": "https://provider.invalid/v1",
            "model": "finding-model",
            "lifecycle": ["prepared", receipt_state],
            "prompt_tokens": 5 if state == "executed" else None,
            "completion_tokens": 3 if state == "executed" else None,
            "total_tokens": 8 if state == "executed" else None,
            "cost": "0.001" if state == "executed" else None,
            "retry_count": 0,
            "stream_termination": "stop" if state == "executed" else None,
            "error": None if state == "executed" else state,
            "input_context": context,
            "profile_revision": {"revision_id": "profile-finding"},
            "metadata": {"finding": True, "state": state},
            "response_asset_id": response_asset_id,
            "recovered": False,
            "uncertain": state == "uncertain",
            "receipt_hash": "",
        }
        receipt["receipt_hash"] = hash_jcs(
            "model-receipt/v1",
            {key: value for key, value in receipt.items() if key != "receipt_hash"},
        )
        content = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        receipt_asset = AssetRef(
            f"asset-model-receipt-{state}-{context['chain_id']}",
            content,
            sha256_hex(content),
        )
        self.assets[receipt_asset.asset_id] = receipt_asset
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
        output = self.output_asset if state == "executed" else None
        return BrokerSkillResult(
            output,
            receipt_asset,
            invocation,
            model_claimed=state == "executed",
            step_state=state,
        )


class NormalBroker:
    def __init__(self) -> None:
        self.harness: Harness | None = None
        self.calls: list[str] = []

    def reconcile_skill(self, **_kwargs):
        self.calls.append("reconcile")

    def execute_skill(self, **kwargs):
        self.calls.append("execute")
        assert self.harness is not None
        return self.harness.model_result(kwargs)


def normal_harness() -> Harness:
    broker = NormalBroker()
    harness = Harness(broker)
    broker.harness = harness
    return harness


@pytest.mark.parametrize("_evidence", [pytest.param(None, id="psr-opaque-handle-ownership")])
def test_opaque_handle_ownership(_evidence: None) -> None:
    harness = normal_harness()
    handle = harness.prepare()
    assert not hasattr(handle, "__dict__")
    with pytest.raises(AttributeError):
        handle.fingerprint = "forged"  # type: ignore[attr-defined]
    with pytest.raises(TypeError, match="cannot be copied"):
        copy.copy(handle)
    harness.runtime.execute(handle)
    with pytest.raises(TypeError, match="already consumed"):
        harness.runtime.execute(handle)
    assert harness.broker.calls == ["reconcile", "execute"]


@pytest.mark.parametrize(
    "_evidence", [pytest.param(None, id="psr-forged-cross-runtime-handle-negative")]
)
def test_forged_cross_runtime_handle_negative(_evidence: None) -> None:
    harness = normal_harness()
    other = harness.new_runtime()
    handle = harness.prepare()
    forged = object.__new__(PreparedSkillRun)
    with pytest.raises(TypeError, match="forged"):
        other.execute(forged)
    with pytest.raises(TypeError, match="cross-Runtime"):
        other.execute(handle)
    assert harness.broker.calls == []
    harness.runtime.execute(handle)


@pytest.mark.parametrize("_evidence", [pytest.param(None, id="psr-prepare-execute-regression")])
def test_prepare_execute_regression(_evidence: None) -> None:
    harness = normal_harness()
    result = harness.runtime.execute(harness.prepare())
    assert result.chain["chain_status"] == "succeeded"
    assert harness.repository.load_chain("chain-finding").chain == result.chain


@pytest.mark.parametrize("_evidence", [pytest.param(None, id="psr-terminal-receipt-pin-required")])
def test_terminal_receipt_pin_required(_evidence: None) -> None:
    with pytest.raises(ContractError, match="terminal_receipt_id"):
        FrozenModelInvocation(
            invocation_id="invocation-no-pin",
            invocation_key="key-no-pin",
            request_hash="5" * 64,
            response_asset_id=None,
            response_hash=None,
            profile_revision_id="profile-finding",
            provider_plugin_id="com.plotpilot.provider.finding",
            provider_release_id="3" * 64,
            input_context={"job_id": "job-finding"},
        )


@pytest.mark.parametrize("_evidence", [pytest.param(None, id="psr-full-model-receipt-drift-negative")])
def test_full_model_receipt_drift_negative(_evidence: None) -> None:
    harness = normal_harness()
    context = {
        "job_id": "job-finding",
        "step_id": "step-finding",
        "run_snapshot_hash": harness.snapshot["snapshot_hash"],
        "generation_id": harness.generation["generation_id"],
        "chain_id": "chain-drift",
        "chain_index": 0,
        "skill_id": harness.package.skill_id,
        "release_id": harness.package.release_id,
        "package_hash": harness.package.package_hash,
        "parameters_asset_id": None,
        "parameters_hash": None,
        "input_asset_id": harness.input_asset.asset_id,
        "input_hash": harness.input_asset.sha256,
    }
    kwargs = {
        "invocation_key": hash_jcs("prompt-skill-model-invocation/v1", context),
        "invocation_context": context,
    }
    result = harness.model_result(kwargs)
    original = json.loads(result.model_receipt_asset.content)
    drifts = {
        "receipt_id": "model-receipt-drifted",
        "lifecycle": ["prepared", "sent", "receipted"],
        "prompt_tokens": 99,
        "completion_tokens": 88,
        "total_tokens": 187,
        "cost": "9.9",
        "retry_count": 1,
        "profile_revision": {"revision_id": "profile-drifted"},
        "metadata": {"finding": False},
        "recovered": True,
    }
    for field, replacement in drifts.items():
        drifted = copy.deepcopy(original)
        drifted[field] = replacement
        drifted["receipt_hash"] = hash_jcs(
            "model-receipt/v1",
            {key: value for key, value in drifted.items() if key != "receipt_hash"},
        )
        content = json.dumps(drifted, sort_keys=True, separators=(",", ":")).encode()
        asset = AssetRef("asset-drifted-receipt", content, sha256_hex(content))
        with pytest.raises(ContractError):
            verify_model_receipt_asset(asset, invocation=result.invocation)


@pytest.mark.parametrize("_evidence", [pytest.param(None, id="psr-attribution-reload-regression")])
def test_attribution_reload_regression(_evidence: None) -> None:
    harness = normal_harness()
    result = harness.runtime.execute(harness.prepare(chain_id="chain-reload"))
    assert harness.repository.load_chain("chain-reload").chain == result.chain
    asset_id = result.receipts[0]["claim_evidence_asset_id"]
    original = harness.assets[asset_id]
    harness.assets[asset_id] = AssetRef(asset_id, original.content + b" ")
    with pytest.raises(ContractError, match="identity/hash drift"):
        harness.repository.load_chain("chain-reload")


@pytest.mark.parametrize("_evidence", [pytest.param(None, id="psr-reconcile-before-execute")])
def test_reconcile_before_execute(_evidence: None) -> None:
    harness = normal_harness()
    harness.runtime.execute(harness.prepare(chain_id="chain-order"))
    assert harness.broker.calls == ["reconcile", "execute"]


@pytest.mark.parametrize(
    "_evidence", [pytest.param(None, id="psr-uncertain-crash-window-convergence")]
)
def test_uncertain_crash_window_convergence(_evidence: None) -> None:
    class CrashWindowBroker(NormalBroker):
        terminal: BrokerSkillResult | None = None

        def reconcile_skill(self, **_kwargs):
            self.calls.append("reconcile")
            return self.terminal

        def execute_skill(self, **kwargs):
            self.calls.append("execute")
            assert self.harness is not None
            self.terminal = self.harness.model_result(kwargs)
            raise RuntimeError("crash after Broker durability")

    broker = CrashWindowBroker()
    harness = Harness(broker)
    broker.harness = harness
    result = harness.runtime.execute(harness.prepare(chain_id="chain-crash-window"))
    assert result.chain["chain_status"] == "succeeded"
    assert broker.calls == ["reconcile", "execute", "reconcile"]
    assert harness.repository.load_chain("chain-crash-window").chain == result.chain


@pytest.mark.parametrize("_evidence", [pytest.param(None, id="psr-terminal-state-regression")])
def test_terminal_state_regression(_evidence: None) -> None:
    class TerminalBroker(NormalBroker):
        def __init__(self, terminal_state: str) -> None:
            super().__init__()
            self.terminal_state = terminal_state

        def reconcile_skill(self, **kwargs):
            self.calls.append("reconcile")
            assert self.harness is not None
            return self.harness.model_result(kwargs, state=self.terminal_state)

        def execute_skill(self, **_kwargs):
            raise AssertionError("terminal reconciliation must suppress dispatch")

    for state in ("failed", "cancelled", "uncertain"):
        broker = TerminalBroker(state)
        harness = Harness(broker)
        broker.harness = harness
        chain_id = f"chain-terminal-{state}"
        if state == "uncertain":
            with pytest.raises(ContractError, match="remains uncertain"):
                harness.runtime.execute(harness.prepare(chain_id=chain_id))
        else:
            harness.runtime.execute(harness.prepare(chain_id=chain_id))
        persisted = harness.repository.load_chain(chain_id)
        if state == "cancelled":
            assert persisted.chain["chain_status"] == "cancelled"
            assert persisted.receipts[0]["step_state"] == "skipped"
            assert persisted.receipts[0]["warnings"][0]["code"] == "broker_cancelled"
        elif state == "uncertain":
            assert persisted.chain["chain_status"] == "failed"
            assert persisted.receipts[0]["warnings"][0]["code"] == "uncertain_external_effect"
        else:
            assert persisted.chain["chain_status"] == "failed"
            assert persisted.receipts[0]["step_state"] == "failed"
        assert broker.calls == ["reconcile"]
