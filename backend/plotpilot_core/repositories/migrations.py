from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sqlite3
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Migration:
    migration_id: str
    sql: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


CORE_MIGRATIONS = (
    Migration("0001-core-authority", """
CREATE TABLE workspace(workspace_id TEXT PRIMARY KEY,workspace_kind TEXT NOT NULL,title TEXT NOT NULL,status TEXT NOT NULL,current_plan_revision_id TEXT,metadata_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE document(document_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspace(workspace_id),document_type TEXT NOT NULL,title TEXT NOT NULL,current_revision_id TEXT,metadata_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE node(node_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspace(workspace_id),document_id TEXT REFERENCES document(document_id),node_type TEXT NOT NULL,title TEXT NOT NULL,parent_node_id TEXT REFERENCES node(node_id),position INTEGER NOT NULL,metadata_json TEXT NOT NULL,current_revision_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE revision(revision_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspace(workspace_id),document_id TEXT REFERENCES document(document_id),node_id TEXT REFERENCES node(node_id),parent_revision_id TEXT REFERENCES revision(revision_id),content TEXT NOT NULL,content_hash TEXT NOT NULL,created_by TEXT NOT NULL,source_candidate_id TEXT,created_at TEXT NOT NULL,revision_number INTEGER NOT NULL,payload_schema TEXT,CHECK ((document_id IS NULL) <> (node_id IS NULL)));
CREATE TABLE relation(relation_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspace(workspace_id),relation_type TEXT NOT NULL,source_id TEXT NOT NULL,target_id TEXT NOT NULL,metadata_json TEXT NOT NULL,revision_id TEXT,created_at TEXT NOT NULL);
CREATE INDEX revision_document_history ON revision(document_id,revision_number);
CREATE INDEX revision_node_history ON revision(node_id,revision_number);
CREATE INDEX node_workspace_page ON node(workspace_id,position,node_id);
"""),
    Migration("0002-candidate-publication", """
CREATE TABLE candidate(candidate_id TEXT PRIMARY KEY,item_id TEXT NOT NULL,operation_key TEXT NOT NULL,item_hash TEXT NOT NULL,item_json TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(operation_key,item_id));
CREATE TABLE publication_receipt(publication_id TEXT PRIMARY KEY,operation_key TEXT NOT NULL UNIQUE,candidate_id TEXT NOT NULL UNIQUE REFERENCES candidate(candidate_id),revision_id TEXT NOT NULL REFERENCES revision(revision_id),created_at TEXT NOT NULL);
"""),
)


class MigrationRunner:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def apply(self, migrations: Iterable[Migration] = CORE_MIGRATIONS) -> None:
        self.connection.execute("CREATE TABLE IF NOT EXISTS schema_migration(migration_id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        for migration in migrations:
            row = self.connection.execute("SELECT sha256 FROM schema_migration WHERE migration_id=?", (migration.migration_id,)).fetchone()
            if row:
                if row[0] != migration.sha256:
                    raise RuntimeError(f"migration hash mismatch: {migration.migration_id}")
                continue
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                # ``executescript`` performs an implicit commit in CPython and
                # would leave partial DDL behind on a later failing statement.
                for statement in migration.sql.split(";"):
                    if statement.strip():
                        self.connection.execute(statement)
                self.connection.execute("INSERT INTO schema_migration(migration_id,sha256) VALUES(?,?)", (migration.migration_id,migration.sha256))
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise
