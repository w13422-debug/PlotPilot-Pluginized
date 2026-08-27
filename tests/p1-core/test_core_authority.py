from __future__ import annotations

import sqlite3
import pytest

from backend.plotpilot_core.domain import Document, Node, Relation, Workspace
from backend.plotpilot_core.repositories import ConflictError, CoreAuthorityRepository, Migration, MigrationRunner


def test_revision_cas_restart_large_text_and_node_page(tmp_path):
    path=tmp_path/"core.db"; repo=CoreAuthorityRepository(path)
    repo.create_workspace(Workspace("ws-1","Novel")); repo.create_document(Document("doc-1","ws-1","Chapter"))
    for i in range(150): repo.create_node(Node(f"node-{i:03}","ws-1","doc-1",f"Scene {i}",position=i))
    body="章"*1_100_000
    first=repo.publish_revision(document_id="doc-1",content=body,expected_revision_id=None,created_by="user")
    with pytest.raises(ConflictError): repo.publish_revision(document_id="doc-1",content="stale",expected_revision_id=None,created_by="user")
    assert repo.read_document_text("doc-1",offset=999_900,length=500).text==body[999_900:1_000_400]
    assert [n.node_id for n in repo.list_nodes("ws-1",offset=100,limit=25)][:2]==["node-100","node-101"]
    repo.close(); reopened=CoreAuthorityRepository(path)
    assert reopened.get_document("doc-1").current_revision_id==first.revision_id
    assert reopened.get_document("doc-1").content==body


def test_node_and_relation_cannot_cross_workspace(tmp_path):
    repo=CoreAuthorityRepository(tmp_path/"core.db")
    repo.create_workspace(Workspace("a","A")); repo.create_workspace(Workspace("b","B"))
    repo.create_document(Document("doc-a","a","A")); repo.create_document(Document("doc-b","b","B"))
    with pytest.raises(ConflictError,match="outside workspace"): repo.create_node(Node("bad","a","doc-b","bad"))
    repo.create_node(Node("node-a","a","doc-a","A"))
    with pytest.raises(ConflictError,match="outside workspace"): repo.create_relation(Relation("r","a","link","node-a","doc-b"))


def test_migration_failure_is_not_applied_and_hash_drift_fails_closed(tmp_path):
    c=sqlite3.connect(tmp_path/"m.db",isolation_level=None); runner=MigrationRunner(c)
    with pytest.raises(sqlite3.Error): runner.apply([Migration("bad","CREATE TABLE x(id); INVALID SQL;")])
    assert c.execute("SELECT count(*) FROM schema_migration WHERE migration_id='bad'").fetchone()[0]==0
    assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='x'").fetchone()[0]==0
    runner.apply([Migration("ok","CREATE TABLE y(id);")]); runner.apply([Migration("ok","CREATE TABLE y(id);")])
    with pytest.raises(RuntimeError,match="hash mismatch"): runner.apply([Migration("ok","CREATE TABLE z(id);")])
