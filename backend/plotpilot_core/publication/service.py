from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid
from typing import Any

from backend.plotpilot_plugin_sdk import assert_valid, parse_json_bytes, verify_result_bundle, verify_snapshot
from backend.plotpilot_plugin_sdk.verifier import hash_without_field

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
                "SELECT job_id,attempt_id,item_id FROM execution_candidate_binding WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if execution_binding is not None:
                self._require_execution_evidence(connection, candidate_id, item)
                if connection.execute(
                    "SELECT 1 FROM execution_publication_binding WHERE candidate_id=? AND publication_id=?",
                    (candidate_id, previous["publication_id"]),
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
        if item["status"]!="complete" or item["item_kind"]=="incomplete_stream": raise ConflictError("candidate is not publishable")
        self._require_execution_evidence(connection, candidate_id, item)
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
            "SELECT job_id,attempt_id,item_id FROM execution_candidate_binding WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if execution_binding is not None:
            connection.execute(
                "INSERT INTO execution_publication_binding(job_id,attempt_id,item_id,candidate_id,publication_id) VALUES(?,?,?,?,?)",
                (execution_binding["job_id"], execution_binding["attempt_id"], execution_binding["item_id"], candidate_id, pub),
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

    def _require_execution_evidence(self, connection: Any, candidate_id: str, item: dict[str, Any]) -> None:
        binding = connection.execute(
            "SELECT * FROM execution_candidate_binding WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if binding is None:
            return
        authority = connection.execute(
            "SELECT o.*,j.workspace_id,j.run_snapshot_hash,j.run_snapshot_json,j.job_state,a.lease_epoch,a.plugin_id,a.release_id,a.package_hash,a.capability_id,r.receipt_hash,r.receipt_json "
            "FROM execution_outcome o JOIN execution_job j ON j.job_id=o.job_id "
            "JOIN execution_attempt a ON a.attempt_id=o.attempt_id "
            "JOIN execution_receipt r ON r.receipt_id=o.provenance_receipt_id "
            "WHERE o.attempt_id=? AND o.job_id=?",
            (binding["attempt_id"], binding["job_id"]),
        ).fetchone()
        if (
            authority is None
            or authority["job_state"] not in {"succeeded", "partial"}
            or authority["outcome"] not in {"succeeded", "partial"}
        ):
            raise ConflictError("execution-bound Candidate lacks a terminal Job outcome")
        try:
            snapshot = json.loads(authority["run_snapshot_json"])
            verify_snapshot(snapshot)
            if snapshot["snapshot_hash"] != authority["run_snapshot_hash"] or snapshot["workspace_id"] != authority["workspace_id"]:
                raise ValueError("RunSnapshot drift")
            bundle_bytes = self.assets.read(authority["result_bundle_asset_id"])
            bundle = parse_json_bytes(bundle_bytes)
            if not isinstance(bundle, dict):
                raise ValueError("Bundle is not an object")
            known_parents = {
                parent
                for value in bundle["items"]
                for parent in value["parent_candidate_ids"]
                if connection.execute(
                    "SELECT 1 FROM candidate WHERE candidate_id=? AND status NOT IN ('prepared','rejected','deleted','expired')",
                    (parent,),
                ).fetchone()
            }
            verify_result_bundle(
                bundle,
                snapshot_hash_value=authority["run_snapshot_hash"],
                snapshot_workspace_id=authority["workspace_id"],
                known_parent_ids=known_parents,
            )
            matches = [value for value in bundle["items"] if value["item_id"] == binding["item_id"]]
            if len(matches) != 1 or matches[0] != item or bundle["bundle_id"] != binding["bundle_id"]:
                raise ValueError("Candidate/Bundle item drift")
            receipt = json.loads(authority["receipt_json"])
            assert_valid("provenance-receipt/v1", receipt)
            if (
                receipt["receipt_hash"] != authority["receipt_hash"]
                or receipt["receipt_hash"] != hash_without_field(receipt, "receipt_hash", "provenance-receipt/v1")
            ):
                raise ValueError("receipt row drift")
            expected = (
                authority["job_id"], authority["step_id"], authority["attempt_id"], authority["lease_epoch"],
                authority["plugin_id"], authority["release_id"], authority["package_hash"], authority["capability_id"],
                bundle["bundle_id"], hashlib.sha256(bundle_bytes).hexdigest(),
            )
            actual = (
                receipt["job_id"], receipt["step_id"], receipt["attempt_id"], receipt["lease_epoch"],
                receipt["plugin_id"], receipt["release_id"], receipt["package_hash"], receipt["capability_id"],
                receipt["bundle_id"], receipt["bundle_hash"],
            )
            if actual != expected:
                raise ValueError("execution receipt lineage drift")
        except Exception as exc:
            raise ConflictError("execution-bound Candidate evidence is incomplete") from exc
