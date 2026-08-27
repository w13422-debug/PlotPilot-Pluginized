from __future__ import annotations

from dataclasses import dataclass
import json
import uuid

from ..assets import AssetStore
from ..domain.entities import utc_now
from ..repositories import ConflictError, CoreAuthorityRepository, NotFoundError


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    publication_id: str
    candidate_id: str
    revision_id: str


class PublicationService:
    """Core-native internal accept; no plugin-callable wire method is defined here."""
    def __init__(self, repository:CoreAuthorityRepository, assets:AssetStore)->None: self.repository,self.assets=repository,assets

    def accept(self,operation_key:str,candidate_id:str,*,created_by:str)->PublicationReceipt:
        with self.repository.transaction() as c:
            previous=c.execute("SELECT * FROM publication_receipt WHERE operation_key=?",(operation_key,)).fetchone()
            if previous:
                if previous["candidate_id"]!=candidate_id: raise ConflictError("publication key reused")
                return PublicationReceipt(previous["publication_id"],candidate_id,previous["revision_id"])
            row=c.execute("SELECT * FROM candidate WHERE candidate_id=?",(candidate_id,)).fetchone()
            if not row: raise NotFoundError(candidate_id)
            item=json.loads(row["item_json"])
            if item["status"]!="complete" and item["item_kind"]!="incomplete_stream": raise ConflictError("candidate is not publishable")
            target=item["target"]; base=item["base"]
            doc=c.execute("SELECT * FROM document WHERE document_id=?",(target["entity_id"],)).fetchone()
            if not doc or doc["workspace_id"]!=target["workspace_id"]: raise ConflictError("candidate target missing")
            if doc["current_revision_id"]!=base["revision_id"]: raise ConflictError("stale candidate base")
            payload=self.assets.read(item["payload_asset_id"]).decode("utf-8")
            if item["mutation"]["mode"]=="append_text":
                current=c.execute("SELECT content FROM revision WHERE revision_id=?",(base["revision_id"],)).fetchone(); payload=current[0]+payload
            import hashlib
            digest=hashlib.sha256(payload.encode()).hexdigest(); rid=f"rev-{uuid.uuid4().hex}"; now=utc_now()
            number=c.execute("SELECT count(*) FROM revision WHERE document_id=?",(doc["document_id"],)).fetchone()[0]+1
            c.execute("INSERT INTO revision VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(rid,doc["workspace_id"],doc["document_id"],None,base["revision_id"],payload,digest,created_by,candidate_id,now,number,item["mutation"]["payload_schema"]))
            if c.execute("UPDATE document SET current_revision_id=?,updated_at=?,revision=revision+1 WHERE document_id=? AND current_revision_id=?",(rid,now,doc["document_id"],base["revision_id"])).rowcount!=1: raise ConflictError("stale candidate base")
            pub=f"publication-{uuid.uuid4().hex}"
            c.execute("INSERT INTO publication_receipt VALUES(?,?,?,?,?)",(pub,operation_key,candidate_id,rid,now)); c.execute("UPDATE candidate SET status='published' WHERE candidate_id=?",(candidate_id,))
        return PublicationReceipt(pub,candidate_id,rid)
