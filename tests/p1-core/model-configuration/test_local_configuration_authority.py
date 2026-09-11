from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.plotpilot_plugin_sdk import model_profile_revision_hash
from backend.plotpilot_core.configuration import (
    DuplicateConfigurationOperationError,
    InvalidLocalSecretReferenceError,
    ModelConfigurationAuthority,
    StaleConfigurationCasError,
    UnknownConfigurationReferenceError,
)
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository

NOW = "2030-01-02T03:04:05Z"
RAW_ONE = "UlTrA_RAW_SECRET_9f2873812b3a1c2164"
RAW_TWO = "SECOND_RAW_SECRET_51decf015304a5d380"
PROVIDER_RELEASE = "a" * 64


def _authority(repository: CoreAuthorityRepository) -> ModelConfigurationAuthority:
    ids = iter(
        (
            "model-profile-revision-1",
            "model-profile-revision-2",
            "model-profile-revision-3",
        )
    )
    return ModelConfigurationAuthority(
        repository,
        clock=lambda: NOW,
        revision_id_factory=lambda: next(ids),
    )


def _secret_command(
    *, operation_key: str = "secret-operation-1", value: str = RAW_ONE
) -> dict:
    return {
        "schema": "model-secret-put-command/v2",
        "operation_key": operation_key,
        "secret_id": "provider-main",
        "value": value,
    }


def _profile_command(
    *,
    operation_key: str = "profile-operation-1",
    expected_parent_revision_id: str | None = None,
    api_key_ref: str = "secret://provider-main",
    model_name: str = "planner-model-1",
) -> dict:
    return {
        "schema": "model-profile-revise-command/v2",
        "operation_key": operation_key,
        "profile_id": "model-profile-planner",
        "expected_parent_revision_id": expected_parent_revision_id,
        "provider": {
            "plugin_id": "com.plotpilot.provider.local",
            "release_id": PROVIDER_RELEASE,
            "endpoint": "https://models.example.test/v1",
            "model_name": model_name,
            "options": {
                "temperature": 0.4,
                "top_p": 0.9,
                "max_output_tokens": 4096.0,
                "timeout_seconds": 120.0,
                "max_retries": 0.0,
            },
            "api_key_ref": api_key_ref,
        },
    }


def _logical_raw_locations(
    repository: CoreAuthorityRepository, raw_value: str
) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    with repository.read_connection() as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for table in tables:
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            for row in connection.execute(f'SELECT * FROM "{table}"').fetchall():
                for column, value in zip(columns, row, strict=True):
                    if isinstance(value, str) and raw_value in value:
                        found.add((table, column))
    return found


def test_secret_create_replace_exact_replay_and_raw_value_containment(tmp_path) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    authority = _authority(repository)
    try:
        created = authority.put_secret(_secret_command())
        assert created == {
            "schema": "model-secret-put-result/v2",
            "operation_key": "secret-operation-1",
            "secret_id": "provider-main",
            "api_key_ref": "secret://provider-main",
            "created": True,
            "idempotent": False,
        }
        replay = authority.put_secret(_secret_command())
        assert replay == {**created, "idempotent": True}

        replaced = authority.put_secret(
            _secret_command(operation_key="secret-operation-2", value=RAW_TWO)
        )
        assert replaced["created"] is False
        assert replaced["idempotent"] is False
        assert authority.put_secret(
            _secret_command(operation_key="secret-operation-2", value=RAW_TWO)
        ) == {**replaced, "idempotent": True}
        assert authority.resolve_secret_value("secret://provider-main") == RAW_TWO

        with repository.read_connection() as connection:
            secret = connection.execute(
                "SELECT value,value_hash,revision FROM p1_local_secret_value "
                "WHERE secret_id='provider-main'"
            ).fetchone()
            operations = connection.execute(
                "SELECT request_fingerprint,value_hash,response_json "
                "FROM p1_configuration_operation WHERE route_id='model-secret.put' "
                "ORDER BY operation_key"
            ).fetchall()
        assert tuple(secret) == (
            RAW_TWO,
            hashlib.sha256(RAW_TWO.encode()).hexdigest(),
            2,
        )
        assert len(operations) == 2
        assert all(len(row[0]) == 64 and len(row[1]) == 64 for row in operations)
        assert all(RAW_ONE not in row[2] and RAW_TWO not in row[2] for row in operations)
        assert _logical_raw_locations(repository, RAW_ONE) == set()
        assert _logical_raw_locations(repository, RAW_TWO) == {
            ("p1_local_secret_value", "value")
        }
    finally:
        repository.close()


def test_secret_operation_conflict_is_fixed_and_does_not_change_value(tmp_path) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    authority = _authority(repository)
    try:
        authority.put_secret(_secret_command())
        with pytest.raises(
            DuplicateConfigurationOperationError,
            match=r"^Operation key was reused with different input\.$",
        ):
            authority.put_secret(_secret_command(value=RAW_TWO))
        assert authority.resolve_secret_value("secret://provider-main") == RAW_ONE
        with repository.read_connection() as connection:
            assert connection.execute(
                "SELECT revision FROM p1_local_secret_value"
            ).fetchone()[0] == 1
    finally:
        repository.close()


def test_secret_create_and_replace_roll_back_with_operation_receipt(tmp_path) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    authority = _authority(repository)
    try:
        with repository.read_connection() as connection:
            connection.execute(
                "CREATE TEMP TRIGGER fail_secret_receipt BEFORE INSERT ON "
                "p1_configuration_operation WHEN NEW.route_id='model-secret.put' "
                "BEGIN SELECT RAISE(ABORT,'injected receipt failure'); END"
            )
        with pytest.raises(sqlite3.Error, match="injected receipt failure"):
            authority.put_secret(_secret_command())
        with repository.read_connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM p1_local_secret_value"
            ).fetchone()[0] == 0
            connection.execute("DROP TRIGGER fail_secret_receipt")

        authority.put_secret(_secret_command())
        with repository.read_connection() as connection:
            connection.execute(
                "CREATE TEMP TRIGGER fail_secret_replace_receipt BEFORE INSERT ON "
                "p1_configuration_operation WHEN NEW.operation_key='secret-operation-2' "
                "BEGIN SELECT RAISE(ABORT,'injected replace failure'); END"
            )
        with pytest.raises(sqlite3.Error, match="injected replace failure"):
            authority.put_secret(
                _secret_command(operation_key="secret-operation-2", value=RAW_TWO)
            )
        with repository.read_connection() as connection:
            row = connection.execute(
                "SELECT value,revision FROM p1_local_secret_value"
            ).fetchone()
            assert tuple(row) == (RAW_ONE, 1)
    finally:
        repository.close()


def test_profile_uses_p0a_nested_normalization_hash_and_append_only_tip_cas(
    tmp_path,
) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    authority = _authority(repository)
    try:
        authority.put_secret(_secret_command())
        first_command = _profile_command()
        first = authority.revise_profile(first_command)
        revision = first["revision"]
        assert first["idempotent"] is False
        assert revision["revision_id"] == "model-profile-revision-1"
        assert revision["revision_number"] == 1
        assert revision["parent_revision_id"] is None
        assert revision["provider"]["options"] == {
            "temperature": 0.4,
            "top_p": 0.9,
            "max_output_tokens": 4096,
            "timeout_seconds": 120,
            "max_retries": 0,
        }
        assert revision["revision_hash"] == model_profile_revision_hash(revision)
        assert authority.revise_profile(first_command) == {
            **first,
            "idempotent": True,
        }

        second = authority.revise_profile(
            _profile_command(
                operation_key="profile-operation-2",
                expected_parent_revision_id=revision["revision_id"],
                model_name="planner-model-2",
            )
        )
        assert second["revision"]["revision_number"] == 2
        assert second["revision"]["parent_revision_id"] == revision["revision_id"]
        with repository.read_connection() as connection:
            rows = connection.execute(
                "SELECT payload_json,revision_hash,secret_id FROM "
                "p1_model_profile_revision ORDER BY revision_number"
            ).fetchall()
            assert len(rows) == 2
            assert json.loads(rows[0]["payload_json"]) == revision
            assert rows[0]["revision_hash"] == revision["revision_hash"]
            assert rows[0]["secret_id"] == "provider-main"
            assert RAW_ONE not in rows[0]["payload_json"]
            with pytest.raises(sqlite3.Error, match="immutable"):
                connection.execute(
                    "UPDATE p1_model_profile_revision SET profile_id='changed'"
                )
            with pytest.raises(sqlite3.Error, match="immutable"):
                connection.execute("DELETE FROM p1_model_profile_revision")
    finally:
        repository.close()


def test_profile_missing_local_reference_parent_and_stale_tip_fail_closed(
    tmp_path,
) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    authority = _authority(repository)
    try:
        with pytest.raises(UnknownConfigurationReferenceError):
            authority.revise_profile(_profile_command())
        authority.put_secret(_secret_command())
        with pytest.raises(InvalidLocalSecretReferenceError):
            authority.revise_profile(
                _profile_command(
                    operation_key="profile-path-secret",
                    api_key_ref="secret://provider-main/child",
                )
            )
        with pytest.raises(UnknownConfigurationReferenceError):
            authority.revise_profile(
                _profile_command(
                    operation_key="profile-missing-parent",
                    expected_parent_revision_id="model-profile-revision-missing",
                )
            )
        first = authority.revise_profile(_profile_command())["revision"]
        with pytest.raises(StaleConfigurationCasError):
            authority.revise_profile(
                _profile_command(operation_key="profile-stale-null")
            )
        assert authority.get_profile_revision(first["revision_id"]) == first
    finally:
        repository.close()


def test_concurrent_exact_profile_operation_replays_once_and_stale_racer_loses(
    tmp_path,
) -> None:
    database = tmp_path / "core.db"
    first_repository = CoreAuthorityRepository(database)
    first_authority = ModelConfigurationAuthority(first_repository, clock=lambda: NOW)
    first_authority.put_secret(_secret_command())
    second_repository = CoreAuthorityRepository(database)
    second_authority = ModelConfigurationAuthority(second_repository, clock=lambda: NOW)
    barrier = threading.Barrier(2)
    command = _profile_command()

    def revise(authority: ModelConfigurationAuthority) -> dict:
        barrier.wait(timeout=5)
        return authority.revise_profile(command)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(revise, (first_authority, second_authority)))
        assert sorted(item["idempotent"] for item in results) == [False, True]
        assert results[0]["revision"] == results[1]["revision"]
        with first_repository.read_connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM p1_model_profile_revision"
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT count(*) FROM p1_configuration_operation "
                "WHERE route_id='model-profile.revise'"
            ).fetchone()[0] == 1

        stale = _profile_command(operation_key="profile-stale-racer")
        with pytest.raises(StaleConfigurationCasError):
            second_authority.revise_profile(stale)
    finally:
        second_repository.close()
        first_repository.close()


def test_secret_and_profile_replay_survive_repository_restart(tmp_path) -> None:
    database = tmp_path / "core.db"
    repository = CoreAuthorityRepository(database)
    authority = _authority(repository)
    secret_command = _secret_command()
    profile_command = _profile_command()
    secret = authority.put_secret(secret_command)
    profile = authority.revise_profile(profile_command)
    repository.close()

    reopened = CoreAuthorityRepository(database)
    replay = ModelConfigurationAuthority(reopened, clock=lambda: NOW)
    try:
        assert replay.put_secret(secret_command) == {**secret, "idempotent": True}
        assert replay.revise_profile(profile_command) == {
            **profile,
            "idempotent": True,
        }
        with reopened.read_connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM p1_model_profile_revision"
            ).fetchone()[0] == 1
    finally:
        reopened.close()
