from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from backend.plotpilot_plugin_sdk import (
    assert_valid,
    canonical_bytes,
    parse_json_bytes,
    verify_result_bundle,
    verify_snapshot,
)
from backend.plotpilot_plugin_sdk.verifier import (
    hash_without_field,
    validate_rpc_result,
)

from ..assets import AssetStore
from ..candidates.service import CandidateService
from ..domain.entities import utc_now
from ..repositories import CoreAuthorityRepository
from ..repositories.authority import verify_attempt_snapshot_binding
from ..repositories.authority_application.errors import (
    CrossWorkspaceError,
    IncompletePublicationError,
    OperationKeyReuseError,
    StaleCasError,
    UnknownReferenceError,
)


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    publication_id: str
    candidate_id: str
    revision_id: str


@dataclass(frozen=True, slots=True)
class CandidateFingerprint:
    candidate_id: str
    item_id: str
    item_hash: str
    item_json: str
    status: str


@dataclass(frozen=True, slots=True)
class PublicationPreflight:
    candidate_id: str
    workspace_id: str
    item: dict[str, Any]
    payload_bytes: bytes
    payload_text: str
    fingerprints: tuple[CandidateFingerprint, ...]
    root_status: str


class PublicationService:
    """Core-native complete-preflight then single-transaction Publication."""

    _VISIBLE_PARENT_STATES = frozenset({"staged", "published"})

    def __init__(self, repository: CoreAuthorityRepository, assets: AssetStore) -> None:
        self.repository, self.assets = repository, assets

    def preflight(self, candidate_id: str, *, expected_workspace_id: str | None = None) -> PublicationPreflight:
        """Validate the complete immutable input graph before any durable write."""
        with self.repository.read_connection() as connection:
            return self.verify_candidate_closure(
                connection,
                candidate_id,
                expected_workspace_id=expected_workspace_id,
            )

    def verify_candidate_closure(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        *,
        expected_workspace_id: str | None = None,
    ) -> PublicationPreflight:
        """Rehydrate one complete Candidate authority closure on ``connection``.

        Publication and source-Candidate Revision provenance deliberately use
        this same verifier.  Callers that already own the Core transaction can
        therefore validate the exact snapshot they will mutate without a
        second Candidate decoder or an Asset-only approximation.
        """
        visited: dict[str, tuple[dict[str, Any], bytes, CandidateFingerprint]] = {}
        active: set[str] = set()
        root_workspace: list[str | None] = [expected_workspace_id]

        def visit(current_id: str, *, root: bool = False) -> None:
            if current_id in active:
                raise IncompletePublicationError("Candidate parent graph contains a cycle")
            if current_id in visited:
                return
            row = connection.execute("SELECT * FROM candidate WHERE candidate_id=?", (current_id,)).fetchone()
            if row is None:
                if root:
                    raise UnknownReferenceError("Candidate does not exist")
                raise UnknownReferenceError("Candidate parent closure is missing an ancestor")
            status = row["status"]
            if status not in self._VISIBLE_PARENT_STATES:
                raise IncompletePublicationError("Candidate lifecycle is not publication-visible")
            if status == "staged" and connection.execute(
                "SELECT 1 FROM publication_receipt WHERE candidate_id=?", (current_id,)
            ).fetchone() is not None:
                raise IncompletePublicationError("staged Candidate already has a publication receipt")
            if status == "staged" and connection.execute(
                "SELECT 1 FROM execution_publication_binding WHERE candidate_id=?", (current_id,)
            ).fetchone() is not None:
                raise IncompletePublicationError("staged Candidate already has a publication binding")
            item = self._decode_candidate_row(row)
            workspace_id = item["target"]["workspace_id"]
            if root_workspace[0] is None:
                root_workspace[0] = workspace_id
            if workspace_id != root_workspace[0]:
                raise CrossWorkspaceError("Candidate parent graph crosses workspace")
            payload = self._verify_candidate_authority(connection, item, root=root, row_status=status)
            fingerprint = CandidateFingerprint(
                current_id, row["item_id"], row["item_hash"], row["item_json"], status
            )
            visited[current_id] = (item, payload, fingerprint)
            active.add(current_id)
            try:
                for parent_id in item["parent_candidate_ids"]:
                    visit(parent_id)
            finally:
                active.remove(current_id)
            if status == "published":
                self._verify_published_candidate(connection, current_id, item, payload)

        try:
            visit(candidate_id, root=True)
            closure_ids = set(visited)
            # Execution evidence is intentionally checked only after the
            # complete durable parent graph has been traversed.  The
            # result-bundle verifier therefore receives every known ancestor,
            # not merely the current item's direct parents.
            for current_id, (current_item, _payload, _fingerprint) in visited.items():
                self._verify_execution_evidence(
                    connection,
                    current_id,
                    current_item,
                    known_parent_ids=closure_ids,
                )
            item, payload, _ = visited[candidate_id]
            payload_text = payload.decode("utf-8")
        except (IncompletePublicationError, CrossWorkspaceError, UnknownReferenceError, StaleCasError):
            raise
        except Exception as exc:
            raise IncompletePublicationError("durable Candidate authority is incomplete") from exc

        return PublicationPreflight(
            candidate_id=candidate_id,
            workspace_id=str(root_workspace[0]),
            item=item,
            payload_bytes=payload,
            payload_text=payload_text,
            fingerprints=tuple(value[2] for value in visited.values()),
            root_status=visited[candidate_id][2].status,
        )

    def accept(self, operation_key: str, candidate_id: str, *, created_by: str) -> PublicationReceipt:
        preflight = self.preflight(candidate_id)
        with self.repository.transaction() as connection:
            return self.accept_in_transaction(
                connection,
                operation_key,
                candidate_id,
                created_by=created_by,
                preflight=preflight,
            )

    def accept_in_transaction(
        self,
        connection: sqlite3.Connection,
        operation_key: str,
        candidate_id: str,
        *,
        created_by: str,
        preflight: PublicationPreflight | None = None,
    ) -> PublicationReceipt:
        """Commit a previously verified plan under the caller's one transaction."""
        # Keep the historical internal helper callable for existing Core
        # callers; new application paths always supply a preflight built
        # before opening this write transaction.
        if preflight is None:
            preflight = self.preflight(candidate_id)
        if preflight.candidate_id != candidate_id:
            raise IncompletePublicationError("Publication preflight identity drifted")
        previous = connection.execute(
            "SELECT * FROM publication_receipt WHERE operation_key=?", (operation_key,)
        ).fetchone()
        if previous is not None:
            if previous["candidate_id"] != candidate_id:
                raise OperationKeyReuseError("publication key reused with a different command")
            return self._verify_committed_receipt(
                connection, previous, preflight, operation_key=operation_key, created_by=created_by
            )

        # A same-key winner may have committed after this caller completed its
        # staged preflight.  Receipt/replay is authoritative in that case;
        # only a genuinely new operation must still match every fingerprint.
        self._recheck_fingerprints(connection, preflight)

        if preflight.root_status != "staged":
            raise IncompletePublicationError("published Candidate lacks its operation receipt")
        item = preflight.item
        target, base = item["target"], item["base"]
        document = connection.execute(
            "SELECT * FROM document WHERE document_id=?", (target["entity_id"],)
        ).fetchone()
        if document is None:
            raise UnknownReferenceError("candidate target does not exist")
        if document["workspace_id"] != target["workspace_id"]:
            raise CrossWorkspaceError("candidate target crosses workspace")
        if document["current_revision_id"] != base["revision_id"]:
            raise StaleCasError("stale candidate base")
        base_revision = connection.execute(
            "SELECT * FROM revision WHERE revision_id=?", (base["revision_id"],)
        ).fetchone()
        if (
            base_revision is None
            or base_revision["workspace_id"] != target["workspace_id"]
            or base_revision["document_id"] != target["entity_id"]
            or base_revision["content_hash"] != base["content_hash"]
            or not isinstance(base_revision["content"], str)
            or hashlib.sha256(base_revision["content"].encode("utf-8")).hexdigest()
            != base_revision["content_hash"]
        ):
            raise StaleCasError("candidate base authority drifted")

        payload = preflight.payload_text
        if item["mutation"]["mode"] == "append_text":
            payload = base_revision["content"] + payload
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        revision_id = f"rev-{uuid.uuid4().hex}"
        now = utc_now()
        revision_number = connection.execute(
            "SELECT count(*) FROM revision WHERE document_id=?", (document["document_id"],)
        ).fetchone()[0] + 1
        connection.execute(
            "INSERT INTO revision VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                document["workspace_id"],
                document["document_id"],
                None,
                base["revision_id"],
                payload,
                digest,
                created_by,
                candidate_id,
                now,
                revision_number,
                item["mutation"]["payload_schema"],
            ),
        )
        changed = connection.execute(
            "UPDATE document SET current_revision_id=?,updated_at=?,revision=revision+1 "
            "WHERE document_id=? AND current_revision_id=?",
            (revision_id, now, document["document_id"], base["revision_id"]),
        ).rowcount
        if changed != 1:
            raise StaleCasError("stale candidate base")

        publication_id = f"publication-{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO publication_receipt VALUES(?,?,?,?,?)",
            (publication_id, operation_key, candidate_id, revision_id, now),
        )
        if connection.execute(
            "UPDATE candidate SET status='published' WHERE candidate_id=? AND status='staged'",
            (candidate_id,),
        ).rowcount != 1:
            raise StaleCasError("Candidate lifecycle changed")

        execution_binding = connection.execute(
            "SELECT job_id,attempt_id,item_id FROM execution_candidate_binding WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if execution_binding is not None:
            connection.execute(
                "INSERT INTO execution_publication_binding(job_id,attempt_id,item_id,candidate_id,publication_id) VALUES(?,?,?,?,?)",
                (
                    execution_binding["job_id"],
                    execution_binding["attempt_id"],
                    execution_binding["item_id"],
                    candidate_id,
                    publication_id,
                ),
            )

        core_event_id = self._event_id(operation_key)
        core_event = {
            "schema": "core-event/v1",
            "event_id": core_event_id,
            "workspace_id": document["workspace_id"],
            "core_event_seq": 0,
            "aggregate_id": document["document_id"],
            "aggregate_revision": document["revision"] + 1,
            "event_type": "revision.published",
            "producer": {
                "producer_type": "core",
                "producer_id": "publication-service",
                "release_id": None,
            },
            "correlation_id": operation_key,
            "causation_id": candidate_id,
            "payload_asset_id": item["payload_asset_id"],
            "payload_hash": item["mutation"]["payload_hash"],
            "occurred_at": now,
        }
        try:
            connection.execute(
                "INSERT INTO execution_core_event(event_id,workspace_id,aggregate_id,aggregate_revision,event_json) VALUES(?,?,?,?,?)",
                (
                    core_event_id,
                    document["workspace_id"],
                    document["document_id"],
                    document["revision"] + 1,
                    self._canonical_json(core_event),
                ),
            )
            core_event["core_event_seq"] = connection.execute(
                "SELECT core_event_seq FROM execution_core_event WHERE event_id=?", (core_event_id,)
            ).fetchone()[0]
            assert_valid("core-event-v1", core_event)
            connection.execute(
                "UPDATE execution_core_event SET event_json=? WHERE event_id=?",
                (self._canonical_json(core_event), core_event_id),
            )
        except sqlite3.IntegrityError:
            # Preserve SQLite's trigger/error text for the existing transaction
            # failure contract; the repository transaction still rolls back all
            # preceding Revision/Candidate/receipt writes atomically.
            raise
        except Exception as exc:
            raise IncompletePublicationError("Publication Event is invalid") from exc
        return PublicationReceipt(publication_id, candidate_id, revision_id)

    def _decode_candidate_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            raw = row["item_json"]
            if not isinstance(raw, str):
                raise TypeError("item_json is not text")
            item = parse_json_bytes(raw.encode("utf-8"))
            if not isinstance(item, dict):
                raise TypeError("Candidate is not an object")
            assert_valid("candidate-item/v1", item)
            canonical, item_hash = CandidateService._canonical(item)
            if canonical != raw or item_hash != row["item_hash"] or item["item_id"] != row["item_id"]:
                raise ValueError("Candidate row/hash drift")
            return item
        except Exception as exc:
            raise IncompletePublicationError("durable Candidate decode is incomplete") from exc

    def _verify_candidate_authority(
        self,
        connection: sqlite3.Connection,
        item: dict[str, Any],
        *,
        root: bool,
        row_status: str,
    ) -> bytes:
        try:
            target, base = item["target"], item["base"]
            if item["item_kind"] != "document" or target["entity_kind"] != "document":
                raise ValueError("stopped publication kind")
            # Every Candidate in a publication closure must be a complete,
            # document-scoped result.  ``staged`` parents remain valid (the
            # F012 compatibility contract), but partial/failed/skipped items
            # are never promoted as publication authority.
            if item["status"] != "complete" or row_status not in self._VISIBLE_PARENT_STATES:
                raise ValueError("Candidate is not publication eligible")
            if item["mutation"]["mode"] not in {"replace", "append_text"}:
                raise ValueError("unsupported document mutation")
            expected_write = {
                "workspace_id": target["workspace_id"],
                "entity_kind": "document",
                "entity_id": target["entity_id"],
                "revision_id": base["revision_id"],
                "content_hash": base["content_hash"],
            }
            if item["write_set"] != [expected_write]:
                raise ValueError("Candidate write-set drift")
            document = connection.execute(
                "SELECT workspace_id,current_revision_id FROM document WHERE document_id=?",
                (target["entity_id"],),
            ).fetchone()
            if document is None:
                raise UnknownReferenceError("Candidate target does not exist")
            if document["workspace_id"] != target["workspace_id"]:
                raise CrossWorkspaceError("Candidate target crosses workspace")
            revision = connection.execute(
                "SELECT workspace_id,document_id,content,content_hash FROM revision WHERE revision_id=?",
                (base["revision_id"],),
            ).fetchone()
            if revision is None:
                raise UnknownReferenceError("Candidate base does not exist")
            if revision["workspace_id"] != target["workspace_id"]:
                raise CrossWorkspaceError("Candidate base crosses workspace")
            if (
                revision["document_id"] != target["entity_id"]
                or revision["content_hash"] != base["content_hash"]
                or not isinstance(revision["content"], str)
                or hashlib.sha256(revision["content"].encode("utf-8")).hexdigest()
                != revision["content_hash"]
            ):
                raise ValueError("Candidate base authority drift")
            if root and row_status == "staged" and document["current_revision_id"] != base["revision_id"]:
                raise StaleCasError("stale candidate base")
            self._verify_source_refs(connection, item["source_refs"])
            return self._verify_asset(item["payload_asset_id"], item["mutation"]["payload_hash"], utf8=True)
        except (UnknownReferenceError, CrossWorkspaceError, StaleCasError):
            raise
        except Exception as exc:
            raise IncompletePublicationError("durable Candidate authority is incomplete") from exc

    @staticmethod
    def _verify_source_refs(connection: sqlite3.Connection, source_refs: list[dict[str, Any]]) -> None:
        for source in source_refs:
            if source["source_type"] not in {"document", "revision"}:
                raise ValueError("unknown Candidate source reference type")
            workspace_id = source["workspace_id"]
            if workspace_id is None:
                continue
            if connection.execute("SELECT 1 FROM workspace WHERE workspace_id=?", (workspace_id,)).fetchone() is None:
                raise UnknownReferenceError("Candidate source workspace does not exist")
            if source["source_type"] == "document":
                row = connection.execute(
                    "SELECT workspace_id,current_revision_id FROM document WHERE document_id=?", (source["source_id"],)
                ).fetchone()
                if row is None:
                    raise UnknownReferenceError("Candidate source document does not exist")
                if row["workspace_id"] != workspace_id:
                    raise CrossWorkspaceError("Candidate source document crosses workspace")
                if source["revision_or_hash"] != row["current_revision_id"]:
                    revision = connection.execute(
                        "SELECT 1 FROM revision WHERE document_id=? AND (revision_id=? OR content_hash=?)",
                        (source["source_id"], source["revision_or_hash"], source["revision_or_hash"]),
                    ).fetchone()
                    if revision is None:
                        raise IncompletePublicationError("Candidate source document closure drifted")
            elif source["source_type"] == "revision":
                row = connection.execute(
                    "SELECT workspace_id,revision_id,content_hash FROM revision WHERE revision_id=?", (source["source_id"],)
                ).fetchone()
                if row is None:
                    raise UnknownReferenceError("Candidate source Revision does not exist")
                if row["workspace_id"] != workspace_id:
                    raise CrossWorkspaceError("Candidate source Revision crosses workspace")
                if source["revision_or_hash"] not in {row["revision_id"], row["content_hash"]}:
                    raise IncompletePublicationError("Candidate source Revision closure drifted")

    def _verify_asset(self, asset_id: str, expected_hash: str | None, *, utf8: bool) -> bytes:
        try:
            metadata = self.assets.describe(asset_id)
            if (
                not isinstance(metadata.asset_id, str)
                or not isinstance(metadata.sha256, str)
                or not isinstance(metadata.mime, str)
                or not metadata.mime
                or type(metadata.size) is not int
                or metadata.size < 0
                or not isinstance(metadata.logical_role, str)
                or not metadata.logical_role
                or not isinstance(metadata.provenance, str)
                or not metadata.provenance
                or type(metadata.rebuildable) is not bool
                or metadata.asset_id != asset_id
                or (expected_hash is not None and metadata.sha256 != expected_hash)
            ):
                raise ValueError("Asset metadata shape or identity drift")
            data = self.assets.read(asset_id)
            if metadata.size != len(data) or hashlib.sha256(data).hexdigest() != metadata.sha256:
                raise ValueError("Asset metadata/bytes/hash closure drift")
            if utf8:
                data.decode("utf-8")
            return data
        except Exception as exc:
            raise IncompletePublicationError("durable Asset metadata/bytes/hash is incomplete") from exc

    def _verify_published_candidate(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        item: dict[str, Any],
        payload: bytes,
    ) -> None:
        """Verify the receipt, resulting Revision and Event for a published parent.

        A published parent is still part of the immutable input closure.  Its
        lifecycle flag alone is not authority: the receipt, revision and
        event must all agree on candidate, target, content and lineage.
        """
        try:
            receipt = connection.execute(
                "SELECT * FROM publication_receipt WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if receipt is None:
                raise IncompletePublicationError("published Candidate lacks its publication receipt")
            operation_key = receipt["operation_key"]
            if (
                receipt["candidate_id"] != candidate_id
                or not isinstance(operation_key, str)
                or not operation_key
                or not isinstance(receipt["publication_id"], str)
                or not receipt["publication_id"]
            ):
                raise ValueError("published Candidate receipt identity drift")

            target, base = item["target"], item["base"]
            revision = connection.execute(
                "SELECT * FROM revision WHERE revision_id=?", (receipt["revision_id"],)
            ).fetchone()
            if revision is None:
                raise IncompletePublicationError("published Candidate Revision is missing")
            base_revision = connection.execute(
                "SELECT * FROM revision WHERE revision_id=?", (base["revision_id"],)
            ).fetchone()
            if base_revision is None:
                raise UnknownReferenceError("published Candidate base Revision is missing")
            if (
                not isinstance(base_revision["content"], str)
                or hashlib.sha256(base_revision["content"].encode("utf-8")).hexdigest()
                != base_revision["content_hash"]
            ):
                raise ValueError("published Candidate base Revision content hash drift")
            content = payload.decode("utf-8")
            if item["mutation"]["mode"] == "append_text":
                content = base_revision["content"] + content
            expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if (
                revision["revision_id"] != receipt["revision_id"]
                or revision["source_candidate_id"] != candidate_id
                or revision["workspace_id"] != target["workspace_id"]
                or revision["document_id"] != target["entity_id"]
                or revision["node_id"] is not None
                or revision["parent_revision_id"] != base["revision_id"]
                or revision["payload_schema"] != item["mutation"]["payload_schema"]
                or revision["content"] != content
                or revision["content_hash"] != expected_hash
                or type(revision["revision_number"]) is not int
                or revision["revision_number"] < 1
            ):
                raise ValueError("published Candidate Revision lineage drift")

            document = connection.execute(
                "SELECT workspace_id FROM document WHERE document_id=?", (target["entity_id"],)
            ).fetchone()
            if document is None:
                raise UnknownReferenceError("published Candidate target does not exist")
            if document["workspace_id"] != target["workspace_id"]:
                raise CrossWorkspaceError("published Candidate target crosses workspace")

            event = connection.execute(
                "SELECT * FROM execution_core_event WHERE event_id=?", (self._event_id(operation_key),)
            ).fetchone()
            if event is None:
                raise IncompletePublicationError("published Candidate Event is incomplete or missing")
            event_value = self._decode_durable_json(event["event_json"], "Core Event")
            assert_valid("core-event-v1", event_value)
            if self._canonical_json(event_value) != event["event_json"]:
                raise ValueError("published Candidate Event is not canonical")
            expected_event = {
                "schema": "core-event/v1",
                "event_id": self._event_id(operation_key),
                "workspace_id": target["workspace_id"],
                "core_event_seq": event["core_event_seq"],
                "aggregate_id": target["entity_id"],
                "aggregate_revision": revision["revision_number"],
                "event_type": "revision.published",
                "producer": {
                    "producer_type": "core",
                    "producer_id": "publication-service",
                    "release_id": None,
                },
                "correlation_id": operation_key,
                "causation_id": candidate_id,
                "payload_asset_id": item["payload_asset_id"],
                "payload_hash": item["mutation"]["payload_hash"],
                "occurred_at": event_value["occurred_at"],
            }
            if (
                event["event_id"] != event_value["event_id"]
                or event["workspace_id"] != event_value["workspace_id"]
                or event["aggregate_id"] != event_value["aggregate_id"]
                or event["aggregate_revision"] != event_value["aggregate_revision"]
                or event_value != expected_event
            ):
                raise ValueError("published Candidate Event lineage drift")

            execution_binding = connection.execute(
                "SELECT job_id,attempt_id,item_id,candidate_id "
                "FROM execution_candidate_binding WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            publication_binding = connection.execute(
                "SELECT job_id,attempt_id,item_id,candidate_id,publication_id "
                "FROM execution_publication_binding WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if execution_binding is None:
                if publication_binding is not None:
                    raise ValueError("unexpected execution publication binding")
            elif publication_binding is None or tuple(publication_binding) != (
                execution_binding["job_id"],
                execution_binding["attempt_id"],
                execution_binding["item_id"],
                candidate_id,
                receipt["publication_id"],
            ):
                raise ValueError("published Candidate execution binding drift")
        except (IncompletePublicationError, UnknownReferenceError, CrossWorkspaceError):
            raise
        except Exception as exc:
            raise IncompletePublicationError("published Candidate authority is incomplete") from exc

    def _verify_execution_evidence(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        item: dict[str, Any],
        *,
        known_parent_ids: set[str] | None = None,
    ) -> None:
        binding = connection.execute(
            "SELECT * FROM execution_candidate_binding WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if binding is None:
            return
        authority = connection.execute(
            "SELECT o.*,"
            "j.workspace_id,j.run_snapshot_hash,j.run_snapshot_json,j.job_state,"
            "j.result_bundle_asset_id AS job_result_bundle_asset_id,"
            "j.provenance_receipt_id AS job_provenance_receipt_id,"
            "j.output_step_id,"
            "s.state AS step_state,s.active_attempt_id,s.is_output,"
            "a.step_id AS attempt_step_id,a.state AS attempt_state,"
            "a.lease_epoch,a.plugin_id,a.release_id,a.package_hash,a.capability_id,a.generation_id,"
            "a.preallocated_receipt_id,"
            "r.receipt_id AS receipt_row_id,r.job_id AS receipt_job_id,r.step_id AS receipt_step_id,"
            "r.attempt_id AS receipt_attempt_id,r.receipt_hash,r.receipt_json "
            "FROM execution_outcome o JOIN execution_job j ON j.job_id=o.job_id "
            "JOIN execution_attempt a ON a.attempt_id=o.attempt_id "
            "JOIN execution_step s ON s.job_id=o.job_id AND s.step_id=o.step_id "
            "JOIN execution_receipt r ON r.receipt_id=o.provenance_receipt_id "
            "WHERE o.attempt_id=? AND o.job_id=?",
            (binding["attempt_id"], binding["job_id"]),
        ).fetchone()
        try:
            if (
                authority is None
                or authority["job_state"] not in {"succeeded", "partial"}
                or authority["outcome"] not in {"succeeded", "partial"}
                or authority["provenance_receipt_id"] is None
                or authority["result_bundle_asset_id"] is None
                or authority["workspace_id"] != item["target"]["workspace_id"]
                or authority["step_id"] != authority["attempt_step_id"]
                or authority["step_state"] != authority["outcome"]
                or authority["attempt_state"] != authority["outcome"]
                or authority["active_attempt_id"] != authority["attempt_id"]
            ):
                raise ValueError("terminal outcome missing")
            if (
                binding["candidate_id"] != candidate_id
                or binding["job_id"] != authority["job_id"]
                or binding["attempt_id"] != authority["attempt_id"]
            ):
                raise ValueError("execution Candidate binding identity drift")
            response = self._decode_durable_json(authority["response_json"], "execution outcome")
            validate_rpc_result("host.job.complete/v1", response)
            if (
                response["accepted"] is not True
                or response["attempt_state"] != authority["outcome"]
                or response["step_state"] != authority["outcome"]
                or response["provenance_receipt_id"] != authority["provenance_receipt_id"]
                or type(response["job_event_seq"]) is not int
                or response["job_event_seq"] < 1
                or type(response["core_event_high_water"]) is not int
                or response["core_event_high_water"] < 1
            ):
                raise ValueError("execution outcome response drift")
            snapshot = self._decode_durable_json(authority["run_snapshot_json"], "RunSnapshot")
            if self._canonical_json(snapshot) != authority["run_snapshot_json"]:
                raise ValueError("RunSnapshot is not canonical")
            verify_snapshot(snapshot)
            if snapshot["snapshot_hash"] != authority["run_snapshot_hash"] or snapshot["workspace_id"] != authority["workspace_id"]:
                raise ValueError("RunSnapshot drift")
            verify_attempt_snapshot_binding(snapshot, authority)
            bundle_bytes = self._verify_asset(authority["result_bundle_asset_id"], None, utf8=False)
            bundle = parse_json_bytes(bundle_bytes)
            if not isinstance(bundle, dict):
                raise TypeError("Bundle is not an object")
            if canonical_bytes(bundle) != bundle_bytes:
                raise ValueError("Bundle is not canonical")
            verify_result_bundle(
                bundle,
                snapshot_hash_value=authority["run_snapshot_hash"],
                snapshot_workspace_id=authority["workspace_id"],
                known_parent_ids=known_parent_ids,
            )
            matches = [value for value in bundle["items"] if value["item_id"] == binding["item_id"]]
            if len(matches) != 1 or matches[0] != item or bundle["bundle_id"] != binding["bundle_id"]:
                raise ValueError("Candidate/Bundle item drift")
            producer = bundle["producer"]
            producer_identity = tuple(
                producer[name]
                for name in ("job_id", "step_id", "attempt_id", "lease_epoch", "plugin_id", "release_id", "capability_id")
            )
            expected_producer_identity = (
                authority["job_id"], authority["step_id"], authority["attempt_id"], authority["lease_epoch"],
                authority["plugin_id"], authority["release_id"], authority["capability_id"],
            )
            if producer_identity != expected_producer_identity:
                raise ValueError("Bundle producer identity drift")
            receipt = self._decode_durable_json(authority["receipt_json"], "provenance receipt")
            assert_valid("provenance-receipt/v1", receipt)
            if self._canonical_json(receipt) != authority["receipt_json"]:
                raise ValueError("provenance receipt is not canonical")
            if (
                receipt["receipt_hash"] != authority["receipt_hash"]
                or receipt["receipt_hash"] != hash_without_field(receipt, "receipt_hash", "provenance-receipt/v1")
                or receipt["receipt_id"] != authority["provenance_receipt_id"]
                or authority["receipt_row_id"] != authority["provenance_receipt_id"]
                or authority["receipt_job_id"] != authority["job_id"]
                or authority["receipt_step_id"] != authority["step_id"]
                or authority["receipt_attempt_id"] != authority["attempt_id"]
                or receipt["job_id"] != authority["job_id"]
                or receipt["step_id"] != authority["step_id"]
                or receipt["attempt_id"] != authority["attempt_id"]
                or receipt["run_snapshot_hash"] != authority["run_snapshot_hash"]
                or bundle["provenance_receipt_id"] != receipt["receipt_id"]
            ):
                raise ValueError("receipt row drift")
            expected = (
                authority["job_id"], authority["step_id"], authority["attempt_id"], authority["lease_epoch"],
                authority["plugin_id"], authority["release_id"], authority["package_hash"], authority["capability_id"],
                bundle["bundle_id"], hashlib.sha256(bundle_bytes).hexdigest(),
                authority["run_snapshot_hash"], authority["provenance_receipt_id"], True,
            )
            actual = (
                receipt["job_id"], receipt["step_id"], receipt["attempt_id"], receipt["lease_epoch"],
                receipt["plugin_id"], receipt["release_id"], receipt["package_hash"], receipt["capability_id"],
                receipt["bundle_id"], receipt["bundle_hash"], receipt["run_snapshot_hash"], receipt["receipt_id"],
                binding["item_id"] in receipt["staged_items"],
            )
            if actual != expected:
                raise ValueError("execution receipt lineage drift")
        except (IncompletePublicationError, CrossWorkspaceError, UnknownReferenceError):
            raise
        except Exception as exc:
            raise IncompletePublicationError("execution-bound Candidate evidence is incomplete") from exc

    @staticmethod
    def _recheck_fingerprints(connection: sqlite3.Connection, preflight: PublicationPreflight) -> None:
        for expected in preflight.fingerprints:
            actual = connection.execute(
                "SELECT candidate_id,item_id,item_hash,item_json,status FROM candidate WHERE candidate_id=?",
                (expected.candidate_id,),
            ).fetchone()
            if actual is None or tuple(actual) != (
                expected.candidate_id,
                expected.item_id,
                expected.item_hash,
                expected.item_json,
                expected.status,
            ):
                raise StaleCasError("Candidate closure changed after preflight")

    def _verify_committed_receipt(
        self,
        connection: sqlite3.Connection,
        receipt: sqlite3.Row,
        preflight: PublicationPreflight,
        *,
        operation_key: str,
        created_by: str,
    ) -> PublicationReceipt:
        try:
            if (
                receipt["operation_key"] != operation_key
                or receipt["candidate_id"] != preflight.candidate_id
                or not isinstance(receipt["revision_id"], str)
                or not receipt["revision_id"]
            ):
                raise ValueError("committed publication receipt identity drift")
            candidate = connection.execute(
                "SELECT status FROM candidate WHERE candidate_id=?", (preflight.candidate_id,)
            ).fetchone()
            if candidate is None or candidate["status"] != "published":
                raise ValueError("committed publication Candidate closure missing")

            # Re-run the exact receipt/Revision/Event binding used for a
            # published parent, then enforce the caller's creator identity.
            self._verify_published_candidate(
                connection,
                preflight.candidate_id,
                preflight.item,
                preflight.payload_bytes,
            )
            revision = connection.execute(
                "SELECT * FROM revision WHERE revision_id=?", (receipt["revision_id"],)
            ).fetchone()
            if revision is None:
                raise ValueError("committed publication Revision is missing")
            item = preflight.item
            target, base = item["target"], item["base"]
            content = preflight.payload_text
            if item["mutation"]["mode"] == "append_text":
                base_row = connection.execute(
                    "SELECT content FROM revision WHERE revision_id=?", (base["revision_id"],)
                ).fetchone()
                if base_row is None:
                    raise ValueError("base Revision missing")
                content = base_row["content"] + content
            if (
                revision["source_candidate_id"] != preflight.candidate_id
                or revision["workspace_id"] != target["workspace_id"]
                or revision["document_id"] != target["entity_id"]
                or revision["node_id"] is not None
                or revision["parent_revision_id"] != base["revision_id"]
                or revision["created_by"] != created_by
                or revision["payload_schema"] != item["mutation"]["payload_schema"]
                or revision["content"] != content
                or revision["content_hash"] != hashlib.sha256(content.encode("utf-8")).hexdigest()
                or type(revision["revision_number"]) is not int
                or revision["revision_number"] < 1
            ):
                raise ValueError("committed Revision identity drift")
            execution_binding = connection.execute(
                "SELECT job_id,attempt_id,item_id,candidate_id FROM execution_candidate_binding WHERE candidate_id=?",
                (preflight.candidate_id,),
            ).fetchone()
            publication_binding = connection.execute(
                "SELECT job_id,attempt_id,item_id,candidate_id,publication_id "
                "FROM execution_publication_binding WHERE candidate_id=?",
                (preflight.candidate_id,),
            ).fetchone()
            if execution_binding is None:
                if publication_binding is not None:
                    raise ValueError("unexpected execution publication binding")
            elif publication_binding is None or tuple(publication_binding) != (
                execution_binding["job_id"],
                execution_binding["attempt_id"],
                execution_binding["item_id"],
                preflight.candidate_id,
                receipt["publication_id"],
            ):
                raise ValueError("execution publication binding is incomplete")
        except Exception as exc:
            raise IncompletePublicationError("committed publication authority is incomplete") from exc
        return PublicationReceipt(receipt["publication_id"], preflight.candidate_id, receipt["revision_id"])

    @staticmethod
    def _event_id(operation_key: str) -> str:
        return "core-event-" + hashlib.sha256(f"revision.published\n{operation_key}".encode()).hexdigest()[:48]

    @staticmethod
    def _decode_durable_json(raw: Any, label: str) -> dict[str, Any]:
        """Decode a durable JSON object with the same strictness as Assets.

        Durable rows are authority, not a caller-controlled request.  Reject
        non-text values, duplicate keys, invalid UTF-8/JSON and non-canonical
        object encodings through one typed Publication failure boundary.
        """
        try:
            if not isinstance(raw, str):
                raise TypeError(f"{label} JSON is not text")
            value = parse_json_bytes(raw.encode("utf-8"))
            if not isinstance(value, dict):
                raise TypeError(f"{label} JSON is not an object")
            return value
        except Exception as exc:
            raise IncompletePublicationError(f"durable {label} is incomplete") from exc

    @staticmethod
    def _canonical_json(value: dict[str, Any]) -> str:
        return canonical_bytes(value).decode("utf-8")


__all__ = [
    "CandidateFingerprint",
    "PublicationPreflight",
    "PublicationReceipt",
    "PublicationService",
]
