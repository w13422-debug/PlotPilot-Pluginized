from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import plotpilot_prompt_skill_runtime as runtime_package
import pytest
from plotpilot_plugin_sdk import ContractError
from plotpilot_plugin_sdk.package import build_files_sha256
from plotpilot_prompt_skill_runtime import (
    AssetRef,
    ChainAnchor,
    PreparedSkillRun,
    SkillExecution,
    SkillPackage,
    SkillStep,
    SQLiteSkillRepository,
    build_receipt,
)
from plotpilot_prompt_skill_runtime.chain import _materialize_skill_chain


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


def test_attribution_cannot_be_constructed_from_naked_boolean_or_asset_id() -> None:
    with pytest.raises(ContractError, match="validated ModelReceipt"):
        build_receipt(
            receipt_id="receipt-1",
            chain_id="chain-1",
            chain_index=0,
            run_snapshot_hash="a" * 64,
            skill_id="skill-1",
            release_id="b" * 64,
            package_hash="c" * 64,
            input_asset_id="asset-input",
            input_hash="d" * 64,
            participated=True,
            claim_evidence_asset_id="asset-arbitrary-claim",
            anchor=ChainAnchor.bundle("bundle-1", "item-1"),
        )


def test_no_public_or_defined_local_success_fallback() -> None:
    assert not hasattr(runtime_package, "run_skill_chain")
    assert not hasattr(runtime_package, "execute_skill_chain")
    with pytest.raises(TypeError):
        PreparedSkillRun(  # type: ignore[call-arg]
            job={},
            snapshot={},
            generation={},
            generation_id="generation-1",
            chain_id="chain-1",
            anchor=ChainAnchor.bundleless(),
            input_asset=AssetRef.from_value(b"input"),
            steps=(),
            packages=(),
        )


def test_package_manifest_is_recursively_immutable_and_reparsed() -> None:
    package = skill_package("skill.freeze", prompt=b"rewrite\n")
    with pytest.raises(TypeError):
        package.manifest["actions"][0] = "mutated"
    package.verify_authoritative()


def test_active_selection_revision_cas_rejects_aba() -> None:
    authority = Authority()
    assets: dict[str, AssetRef] = {}
    repo = SQLiteSkillRepository(authority, asset_reader=assets.__getitem__)
    repo.migrate()
    first = skill_package("skill.cas", prompt=b"first\n")
    second = skill_package("skill.cas", prompt=b"second\n")
    repo.record_release(first)
    repo.record_release(second)
    assert repo.set_active(
        skill_id="skill.cas",
        release_id=first.release_id,
        source="user",
        compare_and_swap=True,
        expected_revision=0,
    ) == 1
    assert repo.set_active(
        skill_id="skill.cas",
        release_id=second.release_id,
        source="user",
        compare_and_swap=True,
        expected_active_release_id=first.release_id,
        expected_revision=1,
    ) == 2
    assert repo.set_active(
        skill_id="skill.cas",
        release_id=first.release_id,
        source="user",
        compare_and_swap=True,
        expected_active_release_id=second.release_id,
        expected_revision=2,
    ) == 3
    with pytest.raises(ContractError, match="CAS failed"):
        repo.set_active(
            skill_id="skill.cas",
            release_id=second.release_id,
            source="user",
            compare_and_swap=True,
            expected_active_release_id=first.release_id,
            expected_revision=1,
        )


def _execution(
    chain_id: str,
    step: SkillStep,
    source: AssetRef,
    output: AssetRef,
    *,
    receipt_prefix: str,
):
    return _materialize_skill_chain(
        chain_id=chain_id,
        run_snapshot_hash="a" * 64,
        initial_input=source,
        steps=(step,),
        execute=lambda _step, _asset: SkillExecution(output=output),
        anchor=ChainAnchor.bundle("bundle-1", "item-1"),
        receipt_id_prefix=receipt_prefix,
    )


def test_release_fk_savepoint_and_restart_asset_revalidation() -> None:
    authority = Authority()
    source = AssetRef("asset-input", b"input")
    output = AssetRef("asset-output", b"output")
    assets = {source.asset_id: source, output.asset_id: output}
    repo = SQLiteSkillRepository(authority, asset_reader=assets.__getitem__)
    repo.migrate()
    package = skill_package("skill.persist", prompt=b"persist\n")
    step = SkillStep("skill.persist", package.release_id, package.package_hash, 1)
    first = _execution("chain-first", step, source, output, receipt_prefix="shared-receipt")
    with pytest.raises(ContractError, match="non-durable release"):
        repo.record_chain(first, expected_steps=(step,))
    repo.record_release(package)
    second = _execution("chain-second", step, source, output, receipt_prefix="shared-receipt")
    with repo.transaction() as token:
        repo.record_chain(first, expected_steps=(step,), token=token)
        with pytest.raises(sqlite3.IntegrityError):
            repo.record_chain(second, expected_steps=(step,), token=token)
    assert repo.load_chain("chain-first").chain["chain_id"] == "chain-first"
    with pytest.raises(KeyError):
        repo.load_chain("chain-second")
    assets[output.asset_id] = AssetRef(output.asset_id, b"drifted")
    with pytest.raises(ContractError, match="drift"):
        repo.skill_receipt_reader("chain-first")
