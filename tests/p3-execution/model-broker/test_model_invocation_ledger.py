from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from test_model_broker import (
    CALLER_PACKAGE_HASH,
    CALLER_PLUGIN_ID,
    CALLER_RELEASE_ID,
    GENERATION_ID,
    FakeProviderPort,
    _broker,
    _build_authority,
    _host_request,
    _row,
)

from backend.plotpilot_core.model import ModelInvocationLedger
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode


def test_model_invocation_migration_receipt_inventory_and_single_database(
    tmp_path: Path,
) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    try:
        repository.ensure_model_invocation_schema()
        with repository.read_connection() as connection:
            receipt = connection.execute(
                "SELECT sha256 FROM schema_migration "
                "WHERE migration_id='p2a-model-invocation-001'"
            ).fetchone()
            objects = {
                (row["type"], row["name"])
                for row in connection.execute(
                    "SELECT type,name FROM sqlite_schema "
                    "WHERE name='model_invocation' OR name LIKE 'model_invocation_%'"
                )
            }
            assert connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE type='table' "
                "AND name='execution_receipt'"
            ).fetchone()[0] == 1
        migration = Path(
            "backend/plotpilot_core/model/migrations/001_model_invocation_ledger.sql"
        )
        manifest = json.loads(
            Path("backend/plotpilot_core/model/migrations/manifest.json").read_text(
                encoding="utf-8"
            )
        )
        migration_text = migration.read_text(encoding="utf-8")
        digest = hashlib.sha256(migration.read_bytes()).hexdigest()
        assert (
            "OLD.state='dispatching' AND NEW.provider_request_json IS NOT "
            "OLD.provider_request_json"
        ) in migration_text
        assert receipt is not None and receipt["sha256"] == digest
        assert manifest["steps"] == [
            {
                "id": "p2a-model-invocation-001",
                "path": "001_model_invocation_ledger.sql",
                "sha256": digest,
                "transactional": True,
            }
        ]
        assert objects == {
            ("table", "model_invocation"),
            ("index", "model_invocation_workspace_invocation_id"),
            ("index", "model_invocation_workspace_invocation_key"),
            ("index", "model_invocation_state"),
            ("trigger", "model_invocation_transition_guard"),
            ("trigger", "model_invocation_no_delete"),
        }
    finally:
        repository.close()


def test_t1_t2_t3_state_machine_and_terminal_immutability(tmp_path: Path) -> None:
    stack = _build_authority(tmp_path, "ledger-state-machine")
    port = FakeProviderPort(stack)
    broker = _broker(stack, port)
    try:
        prepared = broker.prepare_host_invocation(_host_request(stack))
        dispatching = _row(stack)
        assert dispatching["state"] == "dispatching"
        assert dispatching["provider_request_json"] is not None
        assert dispatching["provider_success_json"] is None
        provider_request_json = str(dispatching["provider_request_json"])
        with (
            stack.repository.transaction() as connection,
            pytest.raises(sqlite3.IntegrityError),
        ):
            connection.execute(
                "UPDATE model_invocation SET state='uncertain',"
                "provider_request_json='{}',rpc_error_json='{}',"
                "uncertainty_json='{}' WHERE state='dispatching'"
            )
        after_replacement = _row(stack)
        assert after_replacement["state"] == "dispatching"
        assert after_replacement["provider_request_json"] == provider_request_json
        prepared.commit()
        terminal = _row(stack)
        assert terminal["state"] == "received"
        assert terminal["host_result_json"] is not None
        assert terminal["receipt_asset_id"] is not None
        with (
            stack.repository.transaction() as connection,
            pytest.raises(sqlite3.IntegrityError),
        ):
            connection.execute("UPDATE model_invocation SET state='dispatching'")
        with (
            stack.repository.transaction() as connection,
            pytest.raises(sqlite3.IntegrityError),
        ):
            connection.execute("DELETE FROM model_invocation")
    finally:
        stack.close()


def test_workspace_invocation_uniqueness_and_old_uncertain_never_reopens(
    tmp_path: Path,
) -> None:
    stack = _build_authority(tmp_path, "ledger-unique")
    port = FakeProviderPort(stack)
    request = _host_request(stack)
    broker = _broker(stack, port, phase_hook=lambda phase: (_ for _ in ()).throw(
        SystemExit("after barrier")
    ) if phase == "after_t2" else None)
    try:
        with pytest.raises(SystemExit):
            broker.prepare_host_invocation(request)
        ledger = ModelInvocationLedger(stack.repository)
        assert ledger.recover_dispatching() == 1
        row = _row(stack)
        assert row["state"] == "uncertain"
        with (
            stack.repository.transaction() as connection,
            pytest.raises(sqlite3.IntegrityError),
        ):
            connection.execute(
                "UPDATE model_invocation SET state='reserved',"
                "rpc_error_json=NULL,uncertainty_json=NULL"
            )
        with pytest.raises(ContractError) as replay:
            _broker(stack, port).prepare_host_invocation(request)
        assert replay.value.code == int(ErrorCode.UNCERTAIN_EXTERNAL_EFFECT)
        assert port.invoke_calls == 0

        with stack.repository.transaction() as connection:
            connection.execute(
                "UPDATE execution_attempt SET state='fenced',revision=revision+1 "
                "WHERE attempt_id='attempt-1'"
            )
        stack.execution.start_attempt(
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-2",
            worker_run_id="worker-2",
            plugin_id=CALLER_PLUGIN_ID,
            release_id=CALLER_RELEASE_ID,
            package_hash=CALLER_PACKAGE_HASH,
            capability_id="prompt.skill.execute/v2",
            generation_id=GENERATION_ID,
            lease_epoch=2,
            expected_result_contract="artifact-bundle/v1",
        )
        manual = _host_request(
            stack,
            id="123e4567-e89b-42d3-a456-426614174201",
            meta__attempt_id="attempt-2",
            meta__lease_epoch=2,
            meta__operation_id="manual-operation-2",
            params__operation_key="manual-operation-2",
            params__invocation_id="provider-invocation-2",
            params__invocation_key="provider-invocation-key-2",
        )
        prepared = _broker(stack, port).prepare_host_invocation(manual)
        prepared.commit()
        with stack.repository.read_connection() as connection:
            assert [
                tuple(item)
                for item in connection.execute(
                    "SELECT attempt_id,state FROM model_invocation ORDER BY attempt_id"
                )
            ] == [("attempt-1", "uncertain"), ("attempt-2", "received")]
        assert port.invoke_calls == 1
    finally:
        stack.close()
