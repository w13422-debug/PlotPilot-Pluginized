from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from backend.plotpilot_plugin_sdk import verify_snapshot

from ..domain.entities import (
    Document,
    Node,
    Page,
    Relation,
    Revision,
    TextPage,
    Workspace,
    utc_now,
)
from .migrations import Migration, MigrationRunner


def _load_p3_job_migrations() -> tuple[Migration, ...]:
    """Register the accepted P3 ledger DDL without copying a second schema."""
    root = Path(__file__).parent.parent / "jobs" / "migrations"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "p3-module-migrations/v1" or manifest.get("module") != "plotpilot_core.jobs":
        raise RuntimeError("invalid P3 Job migration manifest identity")
    steps = manifest.get("steps")
    if not isinstance(steps, list) or len(steps) != 1:
        raise RuntimeError("unexpected P3 Job migration manifest")
    step = steps[0]
    if step.get("id") != "p3-jobs-001" or step.get("path") != "001_host_operation_ledger.sql" or step.get("transactional") is not True:
        raise RuntimeError("unexpected P3 Job migration step")
    raw = (root / step["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != step.get("sha256"):
        raise RuntimeError("P3 Job migration hash mismatch")
    return (Migration(step["id"], raw.decode("utf-8")),)


P3_JOB_MIGRATIONS = _load_p3_job_migrations()


def verify_attempt_snapshot_binding(snapshot: Mapping[str, object], attempt: Mapping[str, object]) -> None:
    """Bind an Attempt to the one immutable plugin release in its RunSnapshot."""
    verify_snapshot(snapshot)
    releases = [
        release
        for release in snapshot["plugin_releases"]  # type: ignore[index]
        if release["plugin_id"] == attempt["plugin_id"]  # type: ignore[index]
    ]
    if len(releases) != 1:
        raise ValueError("Attempt plugin is not uniquely bound by the RunSnapshot")
    release = releases[0]
    expected = (
        release["release_id"],
        release["package_hash"],
        release["data_generation_id"],
        snapshot["scope"]["operation"],  # type: ignore[index]
    )
    actual = (
        attempt["release_id"],
        attempt["package_hash"],
        attempt["generation_id"],
        attempt["capability_id"],
    )
    if actual != expected:
        raise ValueError("Attempt release identity is not bound by the RunSnapshot")


EXECUTION_MIGRATIONS = (
    Migration(
        "0003-execution-authority",
        """
CREATE TABLE IF NOT EXISTS execution_job(
    job_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace(workspace_id),
    request_key TEXT NOT NULL,
    run_intent_id TEXT NOT NULL,
    run_snapshot_hash TEXT NOT NULL,
    run_snapshot_asset_id TEXT,
    run_snapshot_json TEXT NOT NULL,
    job_state TEXT NOT NULL,
    job_revision INTEGER NOT NULL,
    result_bundle_asset_id TEXT,
    provenance_receipt_id TEXT,
    core_event_high_water INTEGER NOT NULL DEFAULT 0,
    job_event_high_water INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id,request_key)
);
CREATE TABLE IF NOT EXISTS execution_step(
    step_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES execution_job(job_id),
    state TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id,step_id)
);
CREATE TABLE IF NOT EXISTS execution_attempt(
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES execution_job(job_id),
    step_id TEXT NOT NULL,
    state TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL CHECK(lease_epoch >= 1),
    worker_run_id TEXT,
    plugin_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    package_hash TEXT,
    capability_id TEXT NOT NULL,
    generation_id TEXT,
    preallocated_receipt_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(job_id,step_id) REFERENCES execution_step(job_id,step_id)
);
CREATE TABLE IF NOT EXISTS execution_receipt(
    receipt_id TEXT PRIMARY KEY,
    receipt_hash TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES execution_job(job_id),
    step_id TEXT NOT NULL REFERENCES execution_step(step_id),
    attempt_id TEXT NOT NULL REFERENCES execution_attempt(attempt_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_job_event(
    job_id TEXT NOT NULL REFERENCES execution_job(job_id),
    job_event_seq INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    attempt_id TEXT NOT NULL REFERENCES execution_attempt(attempt_id),
    local_seq INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY(job_id,job_event_seq),
    UNIQUE(attempt_id,local_seq)
);
CREATE TABLE IF NOT EXISTS execution_core_event(
    core_event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    workspace_id TEXT NOT NULL REFERENCES workspace(workspace_id),
    aggregate_id TEXT NOT NULL,
    aggregate_revision INTEGER NOT NULL,
    event_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_outcome(
    job_id TEXT NOT NULL REFERENCES execution_job(job_id),
    step_id TEXT NOT NULL REFERENCES execution_step(step_id),
    attempt_id TEXT PRIMARY KEY REFERENCES execution_attempt(attempt_id),
    context_identity TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    outcome TEXT NOT NULL,
    result_bundle_asset_id TEXT,
    provenance_receipt_id TEXT,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(context_identity,operation_key)
);
CREATE TABLE IF NOT EXISTS execution_candidate_binding(
    job_id TEXT NOT NULL REFERENCES execution_job(job_id),
    item_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES candidate(candidate_id),
    stage_operation_key TEXT NOT NULL,
    PRIMARY KEY(job_id,item_id),
    UNIQUE(candidate_id)
);
CREATE TABLE IF NOT EXISTS execution_publication_binding(
    job_id TEXT NOT NULL REFERENCES execution_job(job_id),
    item_id TEXT NOT NULL,
    publication_id TEXT NOT NULL REFERENCES publication_receipt(publication_id),
    PRIMARY KEY(job_id,item_id),
    UNIQUE(publication_id)
);
CREATE TABLE IF NOT EXISTS p3_broker_operation(
    context_identity TEXT NOT NULL,
    method TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    envelope_asset_id TEXT,
    child_creation_json TEXT,
    response BLOB,
    PRIMARY KEY(context_identity,method,operation_key),
    CHECK(child_creation_json IS NULL OR envelope_asset_id IS NOT NULL),
    CHECK(response IS NULL OR child_creation_json IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS p3_broker_child_record(
    child_job_id TEXT PRIMARY KEY REFERENCES execution_job(job_id),
    context_identity TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    record_json TEXT NOT NULL,
    UNIQUE(context_identity,operation_key)
);
CREATE TABLE IF NOT EXISTS execution_child_creation(
    context_identity TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    child_job_id TEXT NOT NULL UNIQUE REFERENCES execution_job(job_id),
    result_json TEXT NOT NULL,
    PRIMARY KEY(context_identity,operation_key)
);
CREATE INDEX IF NOT EXISTS execution_attempt_job ON execution_attempt(job_id,step_id,attempt_id);
CREATE INDEX IF NOT EXISTS execution_event_job ON execution_job_event(job_id,job_event_seq);
CREATE INDEX IF NOT EXISTS execution_outcome_job ON execution_outcome(job_id,attempt_id);
CREATE INDEX IF NOT EXISTS execution_core_event_workspace ON execution_core_event(workspace_id,core_event_seq);
""",
    ),
    Migration(
        "0004-execution-remediation",
        """
ALTER TABLE execution_job ADD COLUMN plan_frozen INTEGER NOT NULL DEFAULT 0 CHECK(plan_frozen IN (0,1));
ALTER TABLE execution_job ADD COLUMN output_step_id TEXT;
ALTER TABLE execution_step ADD COLUMN active_attempt_id TEXT;
ALTER TABLE execution_step ADD COLUMN next_lease_epoch INTEGER NOT NULL DEFAULT 1 CHECK(next_lease_epoch >= 1);
ALTER TABLE execution_step ADD COLUMN step_ordinal INTEGER NOT NULL DEFAULT 1 CHECK(step_ordinal >= 1);
ALTER TABLE execution_step ADD COLUMN dependency_step_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE execution_step ADD COLUMN expected_result_contract TEXT;
ALTER TABLE execution_step ADD COLUMN is_output INTEGER NOT NULL DEFAULT 0 CHECK(is_output IN (0,1));
ALTER TABLE execution_attempt ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 1 CHECK(ordinal >= 1);
ALTER TABLE execution_attempt ADD COLUMN owner_instance_id TEXT;
ALTER TABLE execution_attempt ADD COLUMN lease_expires_at TEXT;
ALTER TABLE execution_attempt ADD COLUMN expected_result_contract TEXT;
UPDATE execution_attempt SET owner_instance_id=worker_run_id,lease_expires_at='9999-12-31T23:59:59Z' WHERE worker_run_id IS NOT NULL;
UPDATE execution_attempt SET expected_result_contract='candidate-batch/v1' WHERE expected_result_contract IS NULL AND capability_id LIKE 'writing.%';
UPDATE execution_attempt SET expected_result_contract=(SELECT json_extract(r.record_json,'$.result_contract') FROM p3_broker_child_record r WHERE r.child_job_id=execution_attempt.job_id) WHERE EXISTS(SELECT 1 FROM p3_broker_child_record r WHERE r.child_job_id=execution_attempt.job_id);
UPDATE execution_step SET active_attempt_id=(SELECT a.attempt_id FROM execution_attempt a WHERE a.step_id=execution_step.step_id ORDER BY a.lease_epoch DESC,a.created_at DESC LIMIT 1),next_lease_epoch=COALESCE((SELECT MAX(a.lease_epoch)+1 FROM execution_attempt a WHERE a.step_id=execution_step.step_id),1),expected_result_contract=(SELECT a.expected_result_contract FROM execution_attempt a WHERE a.step_id=execution_step.step_id ORDER BY a.lease_epoch DESC,a.created_at DESC LIMIT 1);
UPDATE execution_job SET output_step_id=(SELECT s.step_id FROM execution_step s WHERE s.job_id=execution_job.job_id ORDER BY s.created_at,s.step_id LIMIT 1);
ALTER TABLE execution_candidate_binding RENAME TO execution_candidate_binding_0003;
CREATE TABLE execution_candidate_binding(
    job_id TEXT NOT NULL REFERENCES execution_job(job_id),
    attempt_id TEXT NOT NULL REFERENCES execution_attempt(attempt_id),
    bundle_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES candidate(candidate_id),
    stage_operation_key TEXT NOT NULL,
    PRIMARY KEY(job_id,attempt_id,item_id),
    UNIQUE(candidate_id)
);
INSERT INTO execution_candidate_binding(job_id,attempt_id,bundle_id,item_id,candidate_id,stage_operation_key)
SELECT b.job_id,o.attempt_id,COALESCE(json_extract(r.receipt_json,'$.bundle_id'),'bundleless'),b.item_id,b.candidate_id,b.stage_operation_key
FROM execution_candidate_binding_0003 b
JOIN execution_outcome o ON o.job_id=b.job_id AND o.provenance_receipt_id IS NOT NULL
JOIN execution_receipt r ON r.receipt_id=o.provenance_receipt_id
GROUP BY b.candidate_id;
DROP TABLE execution_candidate_binding_0003;
ALTER TABLE execution_publication_binding RENAME TO execution_publication_binding_0003;
CREATE TABLE execution_publication_binding(
    job_id TEXT NOT NULL REFERENCES execution_job(job_id),
    attempt_id TEXT NOT NULL REFERENCES execution_attempt(attempt_id),
    item_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES candidate(candidate_id),
    publication_id TEXT NOT NULL REFERENCES publication_receipt(publication_id),
    PRIMARY KEY(candidate_id),
    UNIQUE(publication_id)
);
INSERT INTO execution_publication_binding(job_id,attempt_id,item_id,candidate_id,publication_id)
SELECT b.job_id,b.attempt_id,b.item_id,b.candidate_id,p.publication_id FROM execution_publication_binding_0003 p
JOIN execution_candidate_binding b ON b.job_id=p.job_id AND b.item_id=p.item_id;
DROP TABLE execution_publication_binding_0003;
ALTER TABLE p3_broker_operation RENAME TO p3_broker_operation_0003;
CREATE TABLE p3_broker_operation(
    context_identity TEXT NOT NULL,
    method TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    envelope_asset_id TEXT,
    child_creation_json TEXT,
    response BLOB,
    PRIMARY KEY(context_identity,method,operation_key),
    CHECK(child_creation_json IS NULL OR envelope_asset_id IS NOT NULL),
    CHECK(method <> 'host.capability.invoke/v1' OR response IS NULL OR child_creation_json IS NOT NULL)
);
INSERT INTO p3_broker_operation SELECT * FROM p3_broker_operation_0003;
DROP TABLE p3_broker_operation_0003;
CREATE INDEX execution_step_plan ON execution_step(job_id,step_ordinal,step_id);
CREATE INDEX execution_candidate_job ON execution_candidate_binding(job_id,attempt_id,item_id);
        """,
    ),
    Migration(
        "0005-durable-checkpoint-authority",
        """
ALTER TABLE execution_job ADD COLUMN current_checkpoint_id TEXT;
ALTER TABLE execution_attempt ADD COLUMN resume_of_attempt_id TEXT;
ALTER TABLE execution_attempt ADD COLUMN resume_checkpoint_id TEXT;
CREATE TABLE IF NOT EXISTS execution_checkpoint(
    checkpoint_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES execution_job(job_id),
    step_id TEXT NOT NULL,
    source_attempt_id TEXT NOT NULL REFERENCES execution_attempt(attempt_id),
    checkpoint_seq INTEGER NOT NULL CHECK(checkpoint_seq >= 1),
    lease_epoch INTEGER NOT NULL CHECK(lease_epoch >= 1),
    run_snapshot_hash TEXT NOT NULL CHECK(length(run_snapshot_hash) = 64),
    replay_policy TEXT NOT NULL,
    completed_units INTEGER NOT NULL CHECK(completed_units >= 0),
    total_units INTEGER CHECK(total_units IS NULL OR total_units >= 0),
    unit_set_hash TEXT,
    state_asset_id TEXT,
    checkpoint_hash TEXT NOT NULL CHECK(length(checkpoint_hash) = 64),
    checkpoint_json TEXT NOT NULL,
    job_event_seq INTEGER NOT NULL CHECK(job_event_seq >= 1),
    operation_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    created_at TEXT NOT NULL,
    UNIQUE(job_id, step_id, checkpoint_seq),
    UNIQUE(job_id, operation_key),
    FOREIGN KEY(job_id, step_id) REFERENCES execution_step(job_id, step_id)
);
CREATE INDEX IF NOT EXISTS execution_checkpoint_latest
    ON execution_checkpoint(job_id, step_id, checkpoint_seq DESC);
CREATE TABLE IF NOT EXISTS execution_checkpoint_operation(
    job_id TEXT NOT NULL REFERENCES execution_job(job_id),
    operation_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    checkpoint_id TEXT NOT NULL REFERENCES execution_checkpoint(checkpoint_id),
    job_event_seq INTEGER NOT NULL CHECK(job_event_seq >= 1),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(job_id, operation_key)
);
CREATE TABLE IF NOT EXISTS execution_control_operation(
    job_id TEXT NOT NULL REFERENCES execution_job(job_id),
    operation_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    attempt_id TEXT,
    lease_epoch INTEGER,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(job_id, operation_key)
);
CREATE TABLE IF NOT EXISTS execution_orchestration_owner(
    workspace_id TEXT PRIMARY KEY REFERENCES workspace(workspace_id),
    owner_instance_id TEXT NOT NULL,
    owner_token TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL CHECK(lease_epoch >= 1),
    lease_expires_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_checkpoint_event
    ON execution_checkpoint(job_id, job_event_seq);
""",
    ),
)


AUTHORITY_APPLICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS core_authority_operation(
    route_id TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    success_status INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(route_id,operation_key)
)
"""


class ConflictError(RuntimeError): pass
class NotFoundError(KeyError): pass


class CoreAuthorityRepository:
    """The sole transaction writer for Core creative authority."""
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.database, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        MigrationRunner(self._connection).apply()
        MigrationRunner(self._connection).apply(P3_JOB_MIGRATIONS)
        MigrationRunner(self._connection).apply(EXECUTION_MIGRATIONS)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def ensure_authority_application_schema(self) -> None:
        """Create the P1 application ledger in the sole Core database.

        The table is initialized lazily by the application seam.  This keeps
        the accepted H_C repository/migration surface unchanged for callers
        that do not compose Core HTTP authority, while ensuring every composed
        command uses the same connection and transaction root as the mutation.
        """
        with self.transaction() as connection:
            connection.execute(AUTHORITY_APPLICATION_SCHEMA)

    @contextmanager
    def read_connection(self):
        """Serialize shared-connection readers behind the writer lock.

        sqlite exposes a connection's own uncommitted writes.  All production
        adapters therefore use this small, re-entrant gate for committed-only
        visibility rather than reading the shared handle directly.
        """
        with self._lock:
            yield self._connection

    @contextmanager
    def transaction(self):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                self._connection.commit()
            except BaseException:
                self._connection.rollback(); raise

    @staticmethod
    def _j(value) -> str: return json.dumps(dict(value),ensure_ascii=False,sort_keys=True,separators=(",",":"))
    @staticmethod
    def _u(value: str): return json.loads(value)

    def create_workspace(self, value: Workspace) -> Workspace:
        with self.transaction() as c:
            c.execute("INSERT INTO workspace VALUES(?,?,?,?,?,?,?,?,?)",(value.workspace_id,value.workspace_kind,value.title,value.status,value.current_plan_revision_id,self._j(value.metadata),value.created_at,value.updated_at,value.revision))
        return value

    def get_workspace(self, workspace_id: str) -> Workspace:
        with self.read_connection() as connection:
            r=connection.execute("SELECT * FROM workspace WHERE workspace_id=?",(workspace_id,)).fetchone()
        if not r: raise NotFoundError(workspace_id)
        return Workspace(r["workspace_id"],r["title"],r["workspace_kind"],r["status"],r["current_plan_revision_id"],self._u(r["metadata_json"]),r["created_at"],r["updated_at"],r["revision"])

    def create_document(self, value: Document) -> Document:
        with self.transaction() as c:
            c.execute("INSERT INTO document VALUES(?,?,?,?,?,?,?,?,?)",(value.document_id,value.workspace_id,value.document_type,value.title,value.current_revision_id,self._j(value.metadata),value.created_at,value.updated_at,value.revision))
        return value

    def get_document(self, document_id: str) -> Document:
        with self.read_connection() as connection:
            r=connection.execute("SELECT * FROM document WHERE document_id=?",(document_id,)).fetchone()
            if not r: raise NotFoundError(document_id)
            content=None
            if r["current_revision_id"]: content=self.get_revision(r["current_revision_id"]).content
        return Document(r["document_id"],r["workspace_id"],r["title"],r["document_type"],r["current_revision_id"],self._u(r["metadata_json"]),r["created_at"],r["updated_at"],r["revision"],content)

    def create_node(self,value:Node)->Node:
        with self.transaction() as c:
            if value.document_id:
                owner=c.execute("SELECT workspace_id FROM document WHERE document_id=?",(value.document_id,)).fetchone()
                if not owner or owner[0]!=value.workspace_id: raise ConflictError("node document is outside workspace")
            if value.parent_node_id:
                owner=c.execute("SELECT workspace_id FROM node WHERE node_id=?",(value.parent_node_id,)).fetchone()
                if not owner or owner[0]!=value.workspace_id: raise ConflictError("node parent is outside workspace")
            c.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(value.node_id,value.workspace_id,value.document_id,value.node_type,value.title,value.parent_node_id,value.position,self._j(value.metadata),value.current_revision_id,value.created_at,value.updated_at,value.revision))
        return value

    def list_nodes(self,workspace_id:str,*,offset:int=0,limit:int=100)->Page:
        with self.read_connection() as connection:
            total=connection.execute("SELECT count(*) FROM node WHERE workspace_id=?",(workspace_id,)).fetchone()[0]
            rows=connection.execute("SELECT * FROM node WHERE workspace_id=? ORDER BY position,node_id LIMIT ? OFFSET ?",(workspace_id,limit,offset)).fetchall()
        items=[Node(r["node_id"],r["workspace_id"],r["document_id"],r["title"],r["node_type"],r["parent_node_id"],r["position"],self._u(r["metadata_json"]),r["current_revision_id"],r["created_at"],r["updated_at"],r["revision"]) for r in rows]
        return Page(items,offset,limit,total)

    def create_relation(self,value:Relation)->Relation:
        with self.transaction() as c:
            for endpoint in (value.source_id,value.target_id):
                owner=c.execute("SELECT workspace_id FROM document WHERE document_id=? UNION ALL SELECT workspace_id FROM node WHERE node_id=?",(endpoint,endpoint)).fetchone()
                if not owner or owner[0]!=value.workspace_id: raise ConflictError("relation endpoint is outside workspace")
            c.execute("INSERT INTO relation VALUES(?,?,?,?,?,?,?,?)",(value.relation_id,value.workspace_id,value.relation_type,value.source_id,value.target_id,self._j(value.metadata),value.revision_id,value.created_at))
        return value

    def publish_revision(self,*,document_id:str|None=None,node_id:str|None=None,content:str,expected_revision_id:str|None,created_by:str,source_candidate_id:str|None=None,payload_schema:str|None=None,revision_id:str|None=None)->Revision:
        if (document_id is None)==(node_id is None): raise ValueError("exactly one revision target required")
        table,key,target=("document","document_id",document_id) if document_id else ("node","node_id",node_id)
        digest=hashlib.sha256(content.encode()).hexdigest(); now=utc_now()
        with self.transaction() as c:
            row=c.execute(f"SELECT workspace_id,current_revision_id,revision FROM {table} WHERE {key}=?",(target,)).fetchone()
            if not row: raise NotFoundError(target)
            if row["current_revision_id"] != expected_revision_id: raise ConflictError("stale revision base")
            number=c.execute(f"SELECT count(*) FROM revision WHERE {key}=?",(target,)).fetchone()[0]+1
            rid=revision_id or f"rev-{uuid.uuid4().hex}"
            c.execute("INSERT INTO revision VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(rid,row["workspace_id"],document_id,node_id,expected_revision_id,content,digest,created_by,source_candidate_id,now,number,payload_schema))
            changed=c.execute(f"UPDATE {table} SET current_revision_id=?,updated_at=?,revision=revision+1 WHERE {key}=? AND current_revision_id IS ?",(rid,now,target,expected_revision_id)).rowcount
            if changed!=1: raise ConflictError("stale revision base")
        return Revision(rid,row["workspace_id"],document_id,node_id,expected_revision_id,content,digest,created_by,source_candidate_id,now,number,payload_schema)

    def get_revision(self,revision_id:str)->Revision:
        with self.read_connection() as connection:
            r=connection.execute("SELECT * FROM revision WHERE revision_id=?",(revision_id,)).fetchone()
        if not r: raise NotFoundError(revision_id)
        return Revision(r["revision_id"],r["workspace_id"],r["document_id"],r["node_id"],r["parent_revision_id"],r["content"],r["content_hash"],r["created_by"],r["source_candidate_id"],r["created_at"],r["revision_number"],r["payload_schema"])

    def read_document_text(self,document_id:str,*,offset:int=0,length:int=65536)->TextPage:
        if offset<0 or length<0: raise ValueError("range must be non-negative")
        doc=self.get_document(document_id); text=doc.content or ""; page=text[offset:offset+length]
        return TextPage(page,offset,len(page),len(text))
