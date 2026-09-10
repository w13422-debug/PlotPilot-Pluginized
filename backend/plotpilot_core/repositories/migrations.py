from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass


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
    Migration("0003-chapter-authority", """
CREATE TABLE candidate_review(
    candidate_id TEXT PRIMARY KEY REFERENCES candidate(candidate_id),
    operation_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
    decided_by TEXT NOT NULL,
    expected_status TEXT NOT NULL CHECK(expected_status IN ('complete','partial')),
    review_revision INTEGER NOT NULL CHECK(review_revision >= 1),
    review_status TEXT NOT NULL CHECK(review_status IN ('reviewed','rejected')),
    created_at TEXT NOT NULL
);
CREATE TABLE candidate_batch_operation(
    operation_key TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    item_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE chapter_candidate_authority(
    candidate_id TEXT PRIMARY KEY REFERENCES candidate(candidate_id),
    source_job_id TEXT,
    source_attempt_id TEXT,
    writer_epoch INTEGER,
    provenance_receipt_id TEXT NOT NULL,
    operation_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    CHECK(writer_epoch IS NULL OR writer_epoch >= 1)
);
CREATE TABLE chapter_writer_fence(
    workspace_id TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL UNIQUE REFERENCES candidate(candidate_id),
    job_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    writer_epoch INTEGER NOT NULL CHECK(writer_epoch >= 1),
    operation_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(workspace_id,entity_kind,entity_id)
);
CREATE INDEX candidate_review_operation ON candidate_review(operation_key);
CREATE INDEX chapter_candidate_source_job ON chapter_candidate_authority(source_job_id,source_attempt_id);
CREATE INDEX chapter_writer_fence_attempt ON chapter_writer_fence(attempt_id,writer_epoch);
"""),
    Migration("0004-chapter-generation-writer-mode-fence", """
CREATE TABLE chapter_generation_writer_fence(
    workspace_id TEXT NOT NULL REFERENCES workspace(workspace_id),
    chapter_document_id TEXT NOT NULL REFERENCES document(document_id),
    operation TEXT NOT NULL,
    writer_mode TEXT NOT NULL CHECK(writer_mode IN ('legacy','plugin')),
    job_id TEXT NOT NULL,
    operation_key TEXT NOT NULL UNIQUE,
    writer_epoch INTEGER NOT NULL CHECK(writer_epoch >= 1),
    state TEXT NOT NULL CHECK(state IN ('active','released')),
    release_operation_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY(workspace_id,chapter_document_id,operation,writer_epoch),
    CHECK(
        (state='active' AND release_operation_key IS NULL AND released_at IS NULL)
        OR
        (state='released' AND release_operation_key IS NOT NULL AND released_at IS NOT NULL)
    )
);
CREATE UNIQUE INDEX chapter_generation_writer_fence_one_active
ON chapter_generation_writer_fence(workspace_id,chapter_document_id,operation)
WHERE state='active';
"""),
)


PRODUCTION_SUPERVISOR_MIGRATIONS = (
    Migration("0005-production-supervisor-authority", """
CREATE TABLE plugin_supervisor_claim(
    worker_id TEXT NOT NULL,
    pin_epoch INTEGER NOT NULL CHECK(pin_epoch >= 1),
    pin_id TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    package_hash TEXT NOT NULL,
    data_generation_id TEXT,
    retire_epoch INTEGER NOT NULL CHECK(retire_epoch >= 1),
    state TEXT NOT NULL CHECK(state IN ('active','released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    released_at TEXT,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    PRIMARY KEY(worker_id,pin_epoch),
    FOREIGN KEY(pin_id) REFERENCES p2_plugin_release_pin(pin_id),
    CHECK(
        (state='active' AND released_at IS NULL)
        OR
        (state='released' AND released_at IS NOT NULL)
    )
);
CREATE UNIQUE INDEX plugin_supervisor_claim_one_active
ON plugin_supervisor_claim(worker_id)
WHERE state='active';
CREATE INDEX plugin_supervisor_claim_release
ON plugin_supervisor_claim(release_id,retire_epoch,state);
"""),
)


PRODUCTION_JOB_MIGRATIONS = (
    Migration("0006-production-job-command-receipt", """
CREATE TABLE execution_job_start_receipt(
    operation_key TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
    request_json TEXT NOT NULL,
    run_snapshot_asset_id TEXT NOT NULL,
    run_snapshot_hash TEXT NOT NULL CHECK(length(run_snapshot_hash) = 64),
    step_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    release_id TEXT NOT NULL CHECK(length(release_id) = 64),
    package_hash TEXT NOT NULL CHECK(length(package_hash) = 64),
    capability_id TEXT NOT NULL,
    result_contract TEXT NOT NULL CHECK(result_contract IN ('candidate-batch/v1','artifact-bundle/v1','diagnostic-bundle/v1')),
    writer_epoch INTEGER NOT NULL CHECK(writer_epoch >= 1),
    worker_run_id TEXT,
    state TEXT NOT NULL CHECK(state IN ('reserved','worker_acquired','attempt_committed','completed','uncertain','reconciled')),
    response_json TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id,job_id),
    UNIQUE(workspace_id,request_key),
    CHECK(
        (state='reserved' AND worker_run_id IS NULL AND response_json IS NULL)
        OR (state='worker_acquired' AND worker_run_id IS NOT NULL AND response_json IS NULL)
        OR (state='attempt_committed' AND worker_run_id IS NOT NULL AND response_json IS NULL)
        OR (state='completed' AND worker_run_id IS NOT NULL AND response_json IS NOT NULL)
        OR (state IN ('uncertain','reconciled') AND response_json IS NULL)
    )
);
CREATE INDEX execution_job_start_receipt_state
ON execution_job_start_receipt(state,worker_run_id);
"""),
)

PRODUCTION_CORE_MIGRATIONS = (
    CORE_MIGRATIONS
    + PRODUCTION_SUPERVISOR_MIGRATIONS
    + PRODUCTION_JOB_MIGRATIONS
)


class MigrationRunner:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def apply(
        self, migrations: Iterable[Migration] = PRODUCTION_CORE_MIGRATIONS
    ) -> None:
        self.connection.execute("CREATE TABLE IF NOT EXISTS schema_migration(migration_id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        for migration in migrations:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute("SELECT sha256 FROM schema_migration WHERE migration_id=?", (migration.migration_id,)).fetchone()
                if row:
                    if row[0] != migration.sha256:
                        raise RuntimeError(f"migration hash mismatch: {migration.migration_id}")
                    self.connection.commit()
                    continue
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
