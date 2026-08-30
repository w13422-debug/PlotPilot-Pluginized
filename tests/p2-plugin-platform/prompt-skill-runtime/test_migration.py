from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from plotpilot_prompt_skill_runtime import SQLiteSkillRepository
from plotpilot_prompt_skill_runtime.persistence import _execute_migration

MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "first-party-plugins"
    / "prompt-skill-runtime"
    / "src"
    / "plotpilot_prompt_skill_runtime"
    / "migrations"
    / "001_prompt_skill_runtime.sql"
)
MIGRATION_ID = "p2-prompt-skill-runtime-001"


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


def repository(authority: Authority) -> SQLiteSkillRepository:
    return SQLiteSkillRepository(authority, asset_reader=lambda _asset_id: b"")


def owned_snapshot(connection: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name LIKE 'p2_skill_%' OR tbl_name LIKE 'p2_skill_%' ORDER BY type,name"
        )
    ]


def test_migration_uses_fixed_ledger_and_exact_same_engine_structure() -> None:
    authority = Authority()
    repo = repository(authority)
    repo.migrate()
    first = owned_snapshot(authority.connection)
    assert len(first) == 18  # four tables, explicit/automatic indexes and six triggers
    row = authority.connection.execute(
        "SELECT sha256 FROM schema_migration WHERE migration_id=?", (MIGRATION_ID,)
    ).fetchone()
    assert row == (hashlib.sha256(MIGRATION.read_bytes()).hexdigest(),)
    repo.migrate()
    assert owned_snapshot(authority.connection) == first
    assert authority.connection.execute("PRAGMA foreign_key_check").fetchall() == []


DRIFTS = {
    "case-sensitive CHECK literals": (
        "source IN ('system','user','legacy')",
        "source IN ('SYSTEM','USER','LEGACY')",
    ),
    "CHECK semantics": ("revision>=1", "revision>=0"),
    "missing column": (
        "    files_manifest_sha256 TEXT NOT NULL CHECK(length(files_manifest_sha256)=64 AND files_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),\n",
        "",
    ),
    "missing foreign key": (
        "    revision INTEGER NOT NULL CHECK(revision>=1),\n    FOREIGN KEY(skill_id,active_release_id,package_hash)\n        REFERENCES p2_skill_release(skill_id,release_id,package_hash)\n",
        "    revision INTEGER NOT NULL CHECK(revision>=1)\n",
    ),
    "missing index": (
        "CREATE INDEX p2_skill_release_package ON p2_skill_release(skill_id,package_hash,release_id);\n",
        "",
    ),
    "missing trigger": (
        (
            "CREATE TRIGGER p2_skill_receipt_no_delete BEFORE DELETE ON p2_skill_receipt\n"
            "BEGIN SELECT RAISE(ABORT,'p2_skill_receipt is immutable'); END;\n"
        ),
        "",
    ),
}


@pytest.mark.parametrize(("_name", "change"), DRIFTS.items())
def test_fake_ledger_cannot_hide_structural_or_literal_drift(
    _name: str, change: tuple[str, str]
) -> None:
    authority = Authority()
    accepted_sql = MIGRATION.read_text(encoding="utf-8")
    drifted_sql = accepted_sql.replace(*change)
    assert drifted_sql != accepted_sql
    authority.connection.execute("BEGIN IMMEDIATE")
    _execute_migration(authority.connection, drifted_sql)
    authority.connection.execute(
        "INSERT INTO schema_migration(migration_id,sha256) VALUES(?,?)",
        (MIGRATION_ID, hashlib.sha256(MIGRATION.read_bytes()).hexdigest()),
    )
    authority.connection.commit()
    before = owned_snapshot(authority.connection)
    with pytest.raises(RuntimeError, match="reference"):
        repository(authority).migrate()
    assert owned_snapshot(authority.connection) == before
    assert authority.connection.execute(
        "SELECT count(*) FROM schema_migration WHERE migration_id=?", (MIGRATION_ID,)
    ).fetchone() == (1,)


def test_precreated_weak_table_rolls_back_all_partial_schema_and_ledger_writes() -> None:
    authority = Authority()
    authority.connection.execute("CREATE TABLE p2_skill_release(release_id TEXT)")
    before = owned_snapshot(authority.connection)
    with pytest.raises(sqlite3.OperationalError):
        repository(authority).migrate()
    assert owned_snapshot(authority.connection) == before
    assert authority.connection.execute(
        "SELECT count(*) FROM schema_migration WHERE migration_id=?", (MIGRATION_ID,)
    ).fetchone() == (0,)


def test_bare_connection_is_rejected() -> None:
    with pytest.raises(TypeError, match="bare SQLite"):
        SQLiteSkillRepository(sqlite3.connect(":memory:"), asset_reader=lambda _asset_id: b"")
