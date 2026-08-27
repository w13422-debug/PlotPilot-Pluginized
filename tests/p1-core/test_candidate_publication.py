from __future__ import annotations

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.candidates import CandidateError, CandidateService
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.publication import PublicationService
from backend.plotpilot_core.repositories import ConflictError, CoreAuthorityRepository


def _item(base,payload,*,item_id="item-1"):
    target={"workspace_id":"ws-1","entity_kind":"document","entity_id":"doc-1"}
    return {"schema":"candidate-item/v1","item_id":item_id,"item_kind":"document","target":target,"mutation":{"mode":"replace","payload_schema":"core/document-text/v1","payload_hash":payload.sha256},"payload_asset_id":payload.asset_id,"base":{"revision_id":base.revision_id,"content_hash":base.content_hash},"write_set":[{**target,"revision_id":base.revision_id,"content_hash":base.content_hash}],"parent_candidate_ids":[],"source_refs":[],"status":"complete"}


def test_candidate_does_not_mutate_and_publication_is_cas_idempotent(tmp_path):
    repo=CoreAuthorityRepository(tmp_path/"core.db"); assets=AssetStore(tmp_path/"assets")
    repo.create_workspace(Workspace("ws-1","Novel")); repo.create_document(Document("doc-1","ws-1","Chapter"))
    base=repo.publish_revision(document_id="doc-1",content="old",expected_revision_id=None,created_by="user")
    payload=assets.put("new正文".encode(),mime="text/plain",logical_role="candidate_payload",provenance="plugin:p")
    staged=CandidateService(repo,assets).stage("stage-op",_item(base,payload))
    assert repo.get_document("doc-1").content=="old"
    pub=PublicationService(repo,assets); first=pub.accept("accept-op",staged.candidate_id,created_by="user"); second=pub.accept("accept-op",staged.candidate_id,created_by="user")
    assert first==second and repo.get_document("doc-1").content=="new正文"
    assert CandidateService(repo,assets).stage("stage-op",_item(base,payload)).candidate_id==staged.candidate_id


def test_stale_candidate_is_atomic_and_operation_key_is_bound(tmp_path):
    repo=CoreAuthorityRepository(tmp_path/"core.db"); assets=AssetStore(tmp_path/"assets")
    repo.create_workspace(Workspace("ws-1","Novel")); repo.create_document(Document("doc-1","ws-1","Chapter"))
    base=repo.publish_revision(document_id="doc-1",content="v1",expected_revision_id=None,created_by="user")
    a=assets.put(b"a",mime="text/plain",logical_role="candidate_payload",provenance="p")
    staged=CandidateService(repo,assets).stage("op",_item(base,a))
    repo.publish_revision(document_id="doc-1",content="v2",expected_revision_id=base.revision_id,created_by="user")
    with pytest.raises(ConflictError,match="stale"): PublicationService(repo,assets).accept("accept",staged.candidate_id,created_by="user")
    assert repo.get_document("doc-1").content=="v2"
    b=assets.put(b"b",mime="text/plain",logical_role="candidate_payload",provenance="p")
    with pytest.raises(CandidateError): CandidateService(repo,assets).stage("op",_item(base,b))
