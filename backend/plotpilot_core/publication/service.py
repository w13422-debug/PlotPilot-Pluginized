from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid
from typing import Any

from backend.plotpilot_plugin_sdk import assert_valid

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
        with self.repository.transaction() as connection:
            return self.accept_in_transaction(
                connection,
                operation_key,
                candidate_id,
                created_by=created_by,
            )

    def accept_in_transaction(
        self,
        connection: Any,
        operation_key: str,
        candidate_id: str,
        *,
        created_by: str,
    ) -> PublicationReceipt:
        """Publish inside a caller-owned authority transaction when needed."""
        previous=connection.execute("SELECT * FROM publication_receipt WHERE operation_key=?",(operation_key,)).fetchone()
        if previous:
            if previous["candidate_id"]!=candidate_id: raise ConflictError("publication key reused")
            candidate = connection.execute("SELECT status,item_json FROM candidate WHERE candidate_id=?",(candidate_id,)).fetchone()
            revision = connection.execute("SELECT source_candidate_id,document_id FROM revision WHERE revision_id=?",(previous["revision_id"],)).fetchone()
            event_id = "core-event-" + hashlib.sha256(f"revision.published\n{operation_key}".encode()).hexdigest()[:48]
            event = connection.execute("SELECT event_json FROM execution_core_event WHERE event_id=?",(event_id,)).fetchone()
            if candidate is None or candidate["status"] != "published" or revision is None or revision["source_candidate_id"] != candidate_id or event is None:
                raise ConflictError("committed publication authority is incomplete")
            event_value = json.loads(event["event_json"])
            assert_valid("core-event-v1", event_value)
            item = json.loads(candidate["item_json"])
            if (
                event_value["event_type"] != "revision.published"
                or event_value["aggregate_id"] != revision["document_id"]
                or event_value["correlation_id"] != operation_key
                or event_value["causation_id"] != candidate_id
                or event_value["payload_asset_id"] != item["payload_asset_id"]
                or event_value["payload_hash"] != item["mutation"]["payload_hash"]
            ):
                raise ConflictError("committed publication Event lineage drift")
            execution_binding = connection.execute(
                "SELECT job_id,item_id FROM execution_candidate_binding WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if execution_binding is not None and connection.execute(
                "SELECT 1 FROM execution_publication_binding WHERE job_id=? AND item_id=? AND publication_id=?",
                (execution_binding["job_id"], execution_binding["item_id"], previous["publication_id"]),
            ).fetchone() is None:
                raise ConflictError("committed execution publication binding is incomplete")
            return PublicationReceipt(previous["publication_id"],candidate_id,previous["revision_id"])
        row=connection.execute("SELECT * FROM candidate WHERE candidate_id=?",(candidate_id,)).fetchone()
        if not row: raise NotFoundError(candidate_id)
        if row["status"] == "prepared":
            raise ConflictError("candidate is not visible before terminal commit")
        if row["status"] != "staged":
            raise ConflictError("candidate is not available for publication")
        item=json.loads(row["item_json"])
        if item["status"]!="complete" and item["item_kind"]!="incomplete_stream": raise ConflictError("candidate is not publishable")
        target=item["target"]; base=item["base"]
        doc=connection.execute("SELECT * FROM document WHERE document_id=?",(target["entity_id"],)).fetchone()
        if not doc or doc["workspace_id"]!=target["workspace_id"]: raise ConflictError("candidate target missing")
        if doc["current_revision_id"]!=base["revision_id"]: raise ConflictError("stale candidate base")
        payload=self.assets.read(item["payload_asset_id"]).decode("utf-8")
        if item["mutation"]["mode"]=="append_text":
            current=connection.execute("SELECT content FROM revision WHERE revision_id=?",(base["revision_id"],)).fetchone(); payload=current[0]+payload
        digest=hashlib.sha256(payload.encode()).hexdigest(); rid=f"rev-{uuid.uuid4().hex}"; now=utc_now()
        number=connection.execute("SELECT count(*) FROM revision WHERE document_id=?",(doc["document_id"],)).fetchone()[0]+1
        connection.execute("INSERT INTO revision VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(rid,doc["workspace_id"],doc["document_id"],None,base["revision_id"],payload,digest,created_by,candidate_id,now,number,item["mutation"]["payload_schema"]))
        if connection.execute("UPDATE document SET current_revision_id=?,updated_at=?,revision=revision+1 WHERE document_id=? AND current_revision_id=?",(rid,now,doc["document_id"],base["revision_id"])).rowcount!=1: raise ConflictError("stale candidate base")
        pub=f"publication-{uuid.uuid4().hex}"
        connection.execute("INSERT INTO publication_receipt VALUES(?,?,?,?,?)",(pub,operation_key,candidate_id,rid,now)); connection.execute("UPDATE candidate SET status='published' WHERE candidate_id=?",(candidate_id,))
        execution_binding = connection.execute(
            "SELECT job_id,item_id FROM execution_candidate_binding WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if execution_binding is not None:
            connection.execute(
                "INSERT INTO execution_publication_binding(job_id,item_id,publication_id) VALUES(?,?,?)",
                (execution_binding["job_id"], execution_binding["item_id"], pub),
            )
        core_event_id = "core-event-" + hashlib.sha256(f"revision.published\n{operation_key}".encode()).hexdigest()[:48]
        core_event = {
            "schema": "core-event/v1",
            "event_id": core_event_id,
            "workspace_id": doc["workspace_id"],
            "core_event_seq": 0,
            "aggregate_id": doc["document_id"],
            "aggregate_revision": doc["revision"] + 1,
            "event_type": "revision.published",
            "producer": {"producer_type": "core", "producer_id": "publication-service", "release_id": None},
            "correlation_id": operation_key,
            "causation_id": candidate_id,
            "payload_asset_id": item["payload_asset_id"],
            "payload_hash": item["mutation"]["payload_hash"],
            "occurred_at": now,
        }
        connection.execute(
            "INSERT INTO execution_core_event(event_id,workspace_id,aggregate_id,aggregate_revision,event_json) VALUES(?,?,?,?,?)",
            (core_event_id, doc["workspace_id"], doc["document_id"], doc["revision"] + 1, json.dumps(core_event,ensure_ascii=False,sort_keys=True,separators=(",",":"))),
        )
        core_event["core_event_seq"] = connection.execute("SELECT core_event_seq FROM execution_core_event WHERE event_id=?",(core_event_id,)).fetchone()[0]
        assert_valid("core-event-v1", core_event)
        connection.execute("UPDATE execution_core_event SET event_json=? WHERE event_id=?",(json.dumps(core_event,ensure_ascii=False,sort_keys=True,separators=(",",":")),core_event_id))
        return PublicationReceipt(pub,candidate_id,rid)
