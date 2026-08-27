from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
import uuid

from ..domain.entities import Document, Node, Page, Relation, Revision, TextPage, Workspace, utc_now
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
)


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

    def close(self) -> None: self._connection.close()

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
        r=self._connection.execute("SELECT * FROM workspace WHERE workspace_id=?",(workspace_id,)).fetchone()
        if not r: raise NotFoundError(workspace_id)
        return Workspace(r["workspace_id"],r["title"],r["workspace_kind"],r["status"],r["current_plan_revision_id"],self._u(r["metadata_json"]),r["created_at"],r["updated_at"],r["revision"])

    def create_document(self, value: Document) -> Document:
        with self.transaction() as c:
            c.execute("INSERT INTO document VALUES(?,?,?,?,?,?,?,?,?)",(value.document_id,value.workspace_id,value.document_type,value.title,value.current_revision_id,self._j(value.metadata),value.created_at,value.updated_at,value.revision))
        return value

    def get_document(self, document_id: str) -> Document:
        r=self._connection.execute("SELECT * FROM document WHERE document_id=?",(document_id,)).fetchone()
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
        total=self._connection.execute("SELECT count(*) FROM node WHERE workspace_id=?",(workspace_id,)).fetchone()[0]
        rows=self._connection.execute("SELECT * FROM node WHERE workspace_id=? ORDER BY position,node_id LIMIT ? OFFSET ?",(workspace_id,limit,offset)).fetchall()
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
        r=self._connection.execute("SELECT * FROM revision WHERE revision_id=?",(revision_id,)).fetchone()
        if not r: raise NotFoundError(revision_id)
        return Revision(r["revision_id"],r["workspace_id"],r["document_id"],r["node_id"],r["parent_revision_id"],r["content"],r["content_hash"],r["created_by"],r["source_candidate_id"],r["created_at"],r["revision_number"],r["payload_schema"])

    def read_document_text(self,document_id:str,*,offset:int=0,length:int=65536)->TextPage:
        if offset<0 or length<0: raise ValueError("range must be non-negative")
        doc=self.get_document(document_id); text=doc.content or ""; page=text[offset:offset+length]
        return TextPage(page,offset,len(page),len(text))
