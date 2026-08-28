from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import sys

import pytest

from backend.plotpilot_core.events import CoreEventStore, JobEventStore
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_plugin_sdk import ContractError, ErrorCode, assert_valid

sys.path.insert(0, str(Path(__file__).parent))
from conftest import append_core, core_event, job_event  # noqa: E402


def test_core_append_is_atomic_idempotent_and_monotonic(event_stack) -> None:
    repository = event_stack["repository"]
    store = CoreEventStore(repository)

    with pytest.raises(RuntimeError):
        with repository.transaction() as connection:
            assert store.append(core_event(1), connection=connection)["core_event_seq"] == 1
            raise RuntimeError("aggregate mutation crashed")
    assert store.high_water() == 0
    with pytest.raises(ContractError) as standalone:
        store.append(core_event(1))
    assert standalone.value.code == int(ErrorCode.INVALID_TRANSITION)

    first = append_core(store, core_event(1))
    replay = append_core(store, {**core_event(1), "core_event_seq": 1})
    second = append_core(store, core_event(2))
    assert first == replay
    assert [first["core_event_seq"], second["core_event_seq"]] == [1, 2]

    with pytest.raises(ContractError) as drift:
        append_core(store, {**core_event(1), "event_type": "backup.completed"})
    assert drift.value.code == int(ErrorCode.DUPLICATE_REQUEST)


def test_retention_never_rewinds_core_or_job_high_water(event_stack) -> None:
    core = CoreEventStore(event_stack["repository"])
    jobs = JobEventStore(event_stack["repository"])
    for index in range(1, 4):
        append_core(core, core_event(index))
        jobs.append(job_event(index))

    assert core.prune_through(2) == 2
    assert jobs.prune_through("job-1", 2) == 2
    assert core.high_water() == 3
    assert jobs.high_water("job-1") == 3
    assert core.window(0).gap and core.window(0).replay_floor_seq == 2
    assert jobs.window("job-1", 0).gap and jobs.window("job-1", 0).replay_floor_seq == 2

    append_core(core, core_event(4))
    jobs.append(job_event(4))
    assert core.high_water() == 4
    assert jobs.high_water("job-1") == 4


def test_core_and_job_sequences_are_distinct_cursor_domains(event_stack) -> None:
    core = CoreEventStore(event_stack["repository"])
    jobs = JobEventStore(event_stack["repository"])
    assert append_core(core, core_event(1))["core_event_seq"] == 1
    assert jobs.append(job_event(1))["job_event_seq"] == 1
    assert core.window(0).events[0]["schema"] == "core-event/v1"
    assert jobs.window("job-1", 0).events[0]["schema"] == "plugin-job-event/v1"

    with pytest.raises(ContractError) as ahead:
        jobs.window("job-1", 2)
    assert ahead.value.code == int(ErrorCode.INVALID_TRANSITION)


def test_job_local_sequence_and_attempt_binding_are_enforced(event_stack) -> None:
    jobs = JobEventStore(event_stack["repository"])
    jobs.append(job_event(1))
    with pytest.raises(ContractError) as reused:
        jobs.append({**job_event(2), "local_seq": 1})
    assert reused.value.code == int(ErrorCode.DUPLICATE_REQUEST)

    with pytest.raises(ContractError) as outside:
        jobs.append({**job_event(2), "attempt_id": "attempt-missing"})
    assert outside.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)

    with pytest.raises(ContractError) as plugin_drift:
        jobs.append({**job_event(2), "event_type": "plugin.other.job.progress"})
    assert plugin_drift.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)

    with pytest.raises(ContractError) as reserved:
        jobs.append(
            {
                **job_event(2),
                "plugin_id": "generation",
                "event_type": "plugin.generation.changed",
            }
        )
    assert reserved.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)

    with pytest.raises(ContractError) as reserved_prefix:
        jobs.append(
            {
                **job_event(2),
                "plugin_id": "generation",
                "event_type": "plugin.generation.progress",
            }
        )
    assert reserved_prefix.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)

    with pytest.raises(ContractError) as payload_pair:
        jobs.append({**job_event(2), "payload_asset_id": "asset-only"})
    assert payload_pair.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)


def test_job_insert_and_high_water_cas_roll_back_together(event_stack) -> None:
    repository = event_stack["repository"]
    jobs = JobEventStore(repository)
    with repository.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER inject_job_high_water_failure "
            "BEFORE UPDATE OF job_event_high_water ON execution_job "
            "BEGIN SELECT RAISE(ABORT, 'injected CAS failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected CAS failure"):
        jobs.append(job_event(1))

    with repository.read_connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM execution_job_event WHERE job_id='job-1'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT job_event_high_water FROM execution_job WHERE job_id='job-1'"
        ).fetchone()[0] == 0
    with repository.transaction() as connection:
        connection.execute("DROP TRIGGER inject_job_high_water_failure")
    assert jobs.append(job_event(1))["job_event_seq"] == 1

    with repository.transaction() as connection:
        assert jobs.append(job_event(2), connection=connection)["job_event_seq"] == 2
    assert jobs.high_water("job-1") == 2

    external = sqlite3.connect(event_stack["database"], isolation_level=None)
    try:
        with pytest.raises(ContractError) as autocommit:
            jobs.append(job_event(3), connection=external)
        assert autocommit.value.code == int(ErrorCode.INVALID_TRANSITION)
    finally:
        external.close()
    assert jobs.high_water("job-1") == 2


def test_nullable_global_core_event_fails_closed_until_authority_delta(event_stack) -> None:
    repository = event_stack["repository"]
    core = CoreEventStore(repository)
    value = {**core_event(1), "workspace_id": None}
    assert_valid("core-event-v1", {**value, "core_event_seq": 1})
    with pytest.raises(ContractError) as blocked:
        append_core(core, value)
    assert blocked.value.code == int(ErrorCode.MIGRATION_FAILED)
    assert "nullable-workspace authority migration" in str(blocked.value)
    assert core.high_water() == 0
    with repository.read_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM execution_core_event").fetchone()[0] == 0


def test_high_water_survives_repository_reopen(event_stack) -> None:
    database = event_stack["database"]
    core = CoreEventStore(event_stack["repository"])
    jobs = JobEventStore(event_stack["repository"])
    append_core(core, core_event(1))
    jobs.append(job_event(1))
    event_stack["repository"].close()

    reopened = CoreAuthorityRepository(database)
    event_stack["repository"] = reopened
    assert append_core(CoreEventStore(reopened), core_event(2))["core_event_seq"] == 2
    assert JobEventStore(reopened).append(job_event(2))["job_event_seq"] == 2


def test_replay_fails_closed_when_row_and_json_identity_drift(event_stack) -> None:
    repository = event_stack["repository"]
    core = CoreEventStore(repository)
    jobs = JobEventStore(repository)
    append_core(core, core_event(1))
    jobs.append(job_event(1))
    with repository.transaction() as connection:
        core_value = json.loads(
            connection.execute(
                "SELECT event_json FROM execution_core_event WHERE core_event_seq=1"
            ).fetchone()[0]
        )
        core_value["aggregate_id"] = "other"
        connection.execute(
            "UPDATE execution_core_event SET event_json=? WHERE core_event_seq=1",
            (json.dumps(core_value),),
        )
    with pytest.raises(ContractError) as core_drift:
        core.window(0)
    assert core_drift.value.code == int(ErrorCode.ASSET_ERROR)

    with repository.transaction() as connection:
        job_value = json.loads(
            connection.execute(
                "SELECT event_json FROM execution_job_event WHERE job_id='job-1' AND job_event_seq=1"
            ).fetchone()[0]
        )
        job_value["job_id"] = "other"
        connection.execute(
            "UPDATE execution_job_event SET event_json=? WHERE job_id='job-1' AND job_event_seq=1",
            (json.dumps(job_value),),
        )
    with pytest.raises(ContractError) as job_drift:
        jobs.window("job-1", 0)
    assert job_drift.value.code == int(ErrorCode.ASSET_ERROR)
