from __future__ import annotations

import sqlite3

import pytest

from backend.plotpilot_core.domain import Workspace
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.repositories.migrations import (
    CORE_MIGRATIONS,
    MODEL_CONFIGURATION_MIGRATION,
    MODEL_CONFIGURATION_MIGRATIONS,
    MODEL_CONFIGURATION_TABLES,
    PRODUCTION_CORE_MIGRATIONS,
    Migration,
    MigrationRunner,
)


EXPECTED_INDEXES = {
    "p1_configuration_operation_created",
    "p1_model_profile_tip",
    "p1_plan_reference_generation",
    "p1_workspace_plan_history",
}
EXPECTED_TRIGGERS = {
    "p1_configuration_operation_no_delete",
    "p1_configuration_operation_no_update",
    "p1_local_secret_value_no_delete",
    "p1_local_secret_value_update_guard",
    "p1_model_profile_revision_no_delete",
    "p1_model_profile_revision_no_update",
    "p1_plan_revision_reference_no_delete",
    "p1_plan_revision_reference_no_update",
    "p1_workspace_plan_selection_no_delete",
    "p1_workspace_plan_selection_no_update",
}


def _named_schema(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type=? AND name LIKE 'p1_%'",
            (kind,),
        ).fetchall()
    }


def test_0007_is_separate_append_only_production_migration_with_exact_schema(
    tmp_path,
) -> None:
    # Frozen 0001..0006 remains byte/addressable for existing acceptance tests;
    # the repository applies the additive 0007 receipt in the same database.
    assert tuple(item.migration_id for item in PRODUCTION_CORE_MIGRATIONS) == (
        "0001-core-authority",
        "0002-candidate-publication",
        "0003-chapter-authority",
        "0004-chapter-generation-writer-mode-fence",
        "0005-production-supervisor-authority",
        "0006-production-job-command-receipt",
    )
    assert MODEL_CONFIGURATION_MIGRATIONS == (MODEL_CONFIGURATION_MIGRATION,)
    assert (
        MODEL_CONFIGURATION_MIGRATION.migration_id
        == "0007-model-configuration-authority"
    )

    repository = CoreAuthorityRepository(tmp_path / "core.db")
    try:
        repository.create_workspace(Workspace("workspace-preserved", "Preserved"))
        with repository.read_connection() as connection:
            assert _named_schema(connection, "table") == MODEL_CONFIGURATION_TABLES
            assert _named_schema(connection, "index") == EXPECTED_INDEXES
            assert _named_schema(connection, "trigger") == EXPECTED_TRIGGERS
            receipt = connection.execute(
                "SELECT sha256 FROM schema_migration WHERE migration_id=?",
                (MODEL_CONFIGURATION_MIGRATION.migration_id,),
            ).fetchone()
            assert receipt is not None
            assert receipt[0] == MODEL_CONFIGURATION_MIGRATION.sha256
            databases = connection.execute("PRAGMA database_list").fetchall()
            assert [(row[1], row[2]) for row in databases if row[1] == "main"] == [
                ("main", str(tmp_path / "core.db"))
            ]
            assert all(row[1] in {"main", "temp"} for row in databases)
        assert repository.get_workspace("workspace-preserved").title == "Preserved"
    finally:
        repository.close()


def test_0007_reapply_is_idempotent_and_keeps_one_receipt(tmp_path) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    try:
        MigrationRunner(repository._connection).apply(MODEL_CONFIGURATION_MIGRATIONS)
        MigrationRunner(repository._connection).apply(MODEL_CONFIGURATION_MIGRATIONS)
        with repository.read_connection() as connection:
            assert connection.execute(
                "SELECT count(*) FROM schema_migration WHERE migration_id=?",
                (MODEL_CONFIGURATION_MIGRATION.migration_id,),
            ).fetchone()[0] == 1
            assert _named_schema(connection, "table") == MODEL_CONFIGURATION_TABLES
    finally:
        repository.close()


def test_0007_injected_failure_rolls_back_all_p1_schema_and_preserves_core_data(
    tmp_path,
) -> None:
    connection = sqlite3.connect(tmp_path / "rollback.db", isolation_level=None)
    try:
        runner = MigrationRunner(connection)
        runner.apply((CORE_MIGRATIONS[0],))
        connection.execute(
            "INSERT INTO workspace VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "workspace-old",
                "WritingProject",
                "Old",
                "active",
                None,
                "{}",
                "2030-01-02T03:04:05Z",
                "2030-01-02T03:04:05Z",
                0,
            ),
        )
        injected = Migration(
            "0007-injected-failure",
            MODEL_CONFIGURATION_MIGRATION.sql + "\nTHIS IS NOT VALID SQL;",
        )
        with pytest.raises(sqlite3.Error):
            runner.apply((injected,))

        assert _named_schema(connection, "table") == set()
        assert _named_schema(connection, "index") == set()
        assert _named_schema(connection, "trigger") == set()
        assert connection.execute(
            "SELECT title FROM workspace WHERE workspace_id='workspace-old'"
        ).fetchone()[0] == "Old"
        assert connection.execute(
            "SELECT count(*) FROM schema_migration WHERE migration_id=?",
            (injected.migration_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_0007_hash_drift_fails_before_schema_mutation(tmp_path) -> None:
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    try:
        drift = Migration(
            MODEL_CONFIGURATION_MIGRATION.migration_id,
            MODEL_CONFIGURATION_MIGRATION.sql + "\n-- drift\n",
        )
        with pytest.raises(RuntimeError, match="migration hash mismatch"):
            MigrationRunner(repository._connection).apply((drift,))
        with repository.read_connection() as connection:
            assert _named_schema(connection, "table") == MODEL_CONFIGURATION_TABLES
    finally:
        repository.close()
