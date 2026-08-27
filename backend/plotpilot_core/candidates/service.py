from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid
from typing import Any

from ..assets import AssetStore
from ..domain.entities import utc_now
from ..repositories import CoreAuthorityRepository


class CandidateError(ValueError): pass


@dataclass(frozen=True, slots=True)
class StagedCandidate:
    item_id: str
    candidate_id: str | None
    stage_status: str
    publication_eligibility: str


class CandidateService:
    """Consumes the frozen candidate-item/v1 contract without inventing a wire DTO."""
    def __init__(self, repository: CoreAuthorityRepository, assets: AssetStore) -> None:
        self.repository, self.assets = repository, assets

    @staticmethod
    def _canonical(item: dict) -> tuple[str, str]:
        if item.get("schema") != "candidate-item/v1": raise CandidateError("unsupported candidate schema")
        required={"item_id","item_kind","target","mutation","payload_asset_id","base","write_set","parent_candidate_ids","source_refs","status"}
        if set(item) != required|{"schema"}: raise CandidateError("candidate fields do not match v1")
        raw=json.dumps(item,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
        return raw.decode(),hashlib.sha256(raw).hexdigest()

    def stage(self, operation_key: str, item: dict) -> StagedCandidate:
        with self.repository.transaction() as connection:
            return self.stage_in_transaction(connection, operation_key, item)

    def stage_in_transaction(
        self,
        connection: Any,
        operation_key: str,
        item: dict,
        *,
        initial_status: str = "staged",
    ) -> StagedCandidate:
        """Stage under a caller-owned P1 transaction.

        ``prepared`` is used by Job completion so Publication cannot observe a
        candidate until the terminal transaction promotes it to ``staged``.
        """
        if initial_status not in {"prepared", "staged"}:
            raise CandidateError("invalid candidate initial status")
        raw,item_hash=self._canonical(item); item_id=item["item_id"]
        existing=connection.execute("SELECT candidate_id,item_hash,item_json,status FROM candidate WHERE operation_key=? AND item_id=?",(operation_key,item_id)).fetchone()
        if existing:
            if existing["item_hash"]!=item_hash: raise CandidateError("operation key reused with different payload")
            saved=json.loads(existing["item_json"])
            eligibility="eligible" if saved["status"]=="complete" or saved["item_kind"]=="incomplete_stream" else "review_only"
            return StagedCandidate(item_id,existing["candidate_id"],"idempotent",eligibility)
        if item["status"] in {"failed","skipped"}:
            return StagedCandidate(item_id,None,"not_created","ineligible")
        target=item["target"]
        if item["item_kind"] not in {"document","incomplete_stream"} or target["entity_kind"]!="document":
            raise CandidateError("first Core slice supports document publication only")
        if item["mutation"]["mode"] not in {"replace","append_text"}: raise CandidateError("unsupported document mutation")
        asset=self.assets.describe(item["payload_asset_id"])
        if asset.sha256 != item["mutation"]["payload_hash"]: raise CandidateError("payload hash mismatch")
        self.assets.require(item["payload_asset_id"], sha256=item["mutation"]["payload_hash"])
        doc=connection.execute("SELECT workspace_id,current_revision_id FROM document WHERE document_id=?",(target["entity_id"],)).fetchone()
        if not doc: raise CandidateError("target document missing")
        if doc["workspace_id"] != target["workspace_id"]: raise CandidateError("target workspace mismatch")
        base=item["base"]
        if doc["current_revision_id"] != base["revision_id"]: raise CandidateError("stale candidate base")
        current=connection.execute("SELECT workspace_id,revision_id,content_hash FROM revision WHERE revision_id=?",(base["revision_id"],)).fetchone()
        if not current or current["content_hash"] != base["content_hash"]: raise CandidateError("base hash mismatch")
        if len(item["write_set"])!=1 or item["write_set"][0] != {"workspace_id":target["workspace_id"],"entity_kind":"document","entity_id":target["entity_id"],"revision_id":base["revision_id"],"content_hash":base["content_hash"]}:
            raise CandidateError("write-set is not the exact target base")
        for source in item["source_refs"]:
            if set(source)!={"workspace_id","source_type","source_id","revision_or_hash"}: raise CandidateError("invalid source reference fields")
            source_workspace=source["workspace_id"]
            if source_workspace is not None:
                if not connection.execute("SELECT 1 FROM workspace WHERE workspace_id=?",(source_workspace,)).fetchone():
                    raise CandidateError("source workspace missing")
                if source["source_type"]=="document":
                    source_doc=connection.execute("SELECT workspace_id FROM document WHERE document_id=?",(source["source_id"],)).fetchone()
                    if not source_doc: raise CandidateError("source document missing")
                    if source_doc["workspace_id"]!=source_workspace: raise CandidateError("source workspace mismatch")
                elif source["source_type"]=="revision":
                    source_revision=connection.execute("SELECT workspace_id,revision_id,content_hash FROM revision WHERE revision_id=?",(source["source_id"],)).fetchone()
                    if not source_revision: raise CandidateError("source revision missing")
                    if source_revision["workspace_id"]!=source_workspace or source["revision_or_hash"] not in {source_revision["revision_id"],source_revision["content_hash"]}: raise CandidateError("source revision mismatch")
        eligibility="eligible" if item["status"]=="complete" or item["item_kind"]=="incomplete_stream" else "review_only"
        for parent in item["parent_candidate_ids"]:
            row=connection.execute("SELECT status FROM candidate WHERE candidate_id=?",(parent,)).fetchone()
            if not row or row["status"] in {"rejected","deleted","expired","prepared"}: raise CandidateError("invalid parent candidate")
        cid=f"candidate-{uuid.uuid4().hex}"
        connection.execute("INSERT INTO candidate VALUES(?,?,?,?,?,?,?)",(cid,item_id,operation_key,item_hash,raw,initial_status,utc_now()))
        return StagedCandidate(item_id,cid,"created",eligibility)
