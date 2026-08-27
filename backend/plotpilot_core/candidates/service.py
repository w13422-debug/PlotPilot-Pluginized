from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid

from ..assets import AssetStore
from ..domain.entities import utc_now
from ..repositories import CoreAuthorityRepository, NotFoundError


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
        raw,item_hash=self._canonical(item); item_id=item["item_id"]
        if item["status"] in {"failed","skipped"}:
            return StagedCandidate(item_id,None,"not_created","ineligible")
        target=item["target"]
        if item["item_kind"] not in {"document","incomplete_stream"} or target["entity_kind"]!="document":
            raise CandidateError("first Core slice supports document publication only")
        if item["mutation"]["mode"] not in {"replace","append_text"}: raise CandidateError("unsupported document mutation")
        asset=self.assets.describe(item["payload_asset_id"])
        if asset.sha256 != item["mutation"]["payload_hash"]: raise CandidateError("payload hash mismatch")
        doc=self.repository.get_document(target["entity_id"])
        if doc.workspace_id != target["workspace_id"]: raise CandidateError("target workspace mismatch")
        base=item["base"]
        if doc.current_revision_id != base["revision_id"]: raise CandidateError("stale candidate base")
        current=self.repository.get_revision(base["revision_id"])
        if current.content_hash != base["content_hash"]: raise CandidateError("base hash mismatch")
        if len(item["write_set"])!=1 or item["write_set"][0] != {"workspace_id":target["workspace_id"],"entity_kind":"document","entity_id":target["entity_id"],"revision_id":base["revision_id"],"content_hash":base["content_hash"]}:
            raise CandidateError("write-set is not the exact target base")
        eligibility="eligible" if item["status"]=="complete" or item["item_kind"]=="incomplete_stream" else "review_only"
        with self.repository.transaction() as c:
            existing=c.execute("SELECT candidate_id,item_hash FROM candidate WHERE operation_key=? AND item_id=?",(operation_key,item_id)).fetchone()
            if existing:
                if existing["item_hash"]!=item_hash: raise CandidateError("operation key reused with different payload")
                return StagedCandidate(item_id,existing["candidate_id"],"idempotent",eligibility)
            for parent in item["parent_candidate_ids"]:
                row=c.execute("SELECT status FROM candidate WHERE candidate_id=?",(parent,)).fetchone()
                if not row or row["status"] in {"rejected","deleted","expired"}: raise CandidateError("invalid parent candidate")
            cid=f"candidate-{uuid.uuid4().hex}"
            c.execute("INSERT INTO candidate VALUES(?,?,?,?,?,?,?)",(cid,item_id,operation_key,item_hash,raw,"staged",utc_now()))
        return StagedCandidate(item_id,cid,"created",eligibility)
