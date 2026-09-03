"""Candidate read/review and Story-State staging authority."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from backend.plotpilot_plugin_sdk import canonical_bytes
from backend.plotpilot_plugin_sdk.canonical import hash_jcs
from backend.plotpilot_plugin_sdk.core_api_v2 import (
    parse_candidate_query_result_v2,
    parse_candidate_review_v2,
    parse_candidate_v2,
    parse_core_authority_v2,
    parse_story_state_projection_input_v2,
)

from ..domain.entities import utc_now
from .service import CandidateService, StagedCandidate


class CandidateApplicationError(RuntimeError):
    error_code = "malformed_request"


class CandidateNotFoundError(CandidateApplicationError):
    error_code = "unknown_reference"


class CandidateCrossWorkspaceError(CandidateApplicationError):
    error_code = "cross_workspace"


class CandidateConflictError(CandidateApplicationError):
    error_code = "duplicate_operation"


class CandidateNotPublishableError(CandidateApplicationError):
    error_code = "candidate_not_publishable"


class CandidateApplication:
    """Thin v2 read/review facade over the sole Core SQLite authority."""

    def __init__(
        self,
        service: CandidateService,
        *,
        execution_authority: Any | None = None,
    ) -> None:
        self.service = service
        self.repository = service.repository
        self.assets = service.assets
        self.execution_authority = execution_authority
        self.repository.ensure_chapter_authority_schema()

    @staticmethod
    def _decode_item(row: sqlite3.Row) -> dict[str, Any]:
        try:
            item = json.loads(row["item_json"])
            canonical, digest = CandidateService._canonical(item)
            if canonical != row["item_json"] or digest != row["item_hash"]:
                raise ValueError("candidate durable encoding drifted")
            return item
        except Exception as exc:
            raise CandidateApplicationError("Candidate authority is malformed") from exc

    @staticmethod
    def _v2_payload_schema(value: str) -> str:
        return "core/document-text/v2" if value == "core/document-text/v1" else value

    @staticmethod
    def _v2_source_ref(value: Mapping[str, Any]) -> dict[str, Any]:
        source_type = value["source_type"]
        # v2 deliberately drops the broad v1 document source form.  Preserve
        # its immutable identity as an external source rather than inventing a
        # revision relationship.
        if source_type == "document":
            source_type = "external"
        return {
            "workspace_id": value["workspace_id"],
            "source_type": source_type,
            "source_id": value["source_id"],
            "revision_or_hash": value["revision_or_hash"],
        }

    @staticmethod
    def _source_job_id(connection: sqlite3.Connection, candidate_id: str) -> str | None:
        row = connection.execute(
            "SELECT source_job_id FROM chapter_candidate_authority "
            "WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        return None if row is None else row["source_job_id"]

    @staticmethod
    def _review_row(
        connection: sqlite3.Connection, candidate_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM candidate_review WHERE candidate_id=?", (candidate_id,)
        ).fetchone()

    def _eligibility(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        item: Mapping[str, Any],
    ) -> str:
        review = self._review_row(connection, row["candidate_id"])
        if review is not None and review["review_status"] == "rejected":
            return "none"
        if row["status"] not in {"staged", "published"}:
            return "none"
        if item["item_kind"] == "incomplete_stream":
            return "eligible"
        return "eligible" if item["status"] == "complete" else "review_only"

    def _candidate_v2(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        expected_workspace_id: str | None = None,
    ) -> dict[str, Any]:
        item = self._decode_item(row)
        target = dict(item["target"])
        workspace_id = target["workspace_id"]
        if expected_workspace_id is not None and workspace_id != expected_workspace_id:
            raise CandidateCrossWorkspaceError("Candidate belongs to another Workspace")
        candidate = {
            "schema": "candidate/v2",
            "candidate_id": row["candidate_id"],
            "workspace_id": workspace_id,
            "item_kind": item["item_kind"],
            "target": target,
            "mutation": {
                "mode": item["mutation"]["mode"],
                "payload_schema": self._v2_payload_schema(
                    item["mutation"]["payload_schema"]
                ),
                "payload_hash": item["mutation"]["payload_hash"],
            },
            "payload_asset_id": item["payload_asset_id"],
            "base": dict(item["base"]),
            "write_set": [dict(value) for value in item["write_set"]],
            "parent_candidate_ids": list(item["parent_candidate_ids"]),
            "source_refs": [
                self._v2_source_ref(value) for value in item["source_refs"]
            ],
            "status": item["status"],
            "publication_eligibility": self._eligibility(connection, row, item),
            "created_at": row["created_at"],
            "source_job_id": self._source_job_id(connection, row["candidate_id"]),
        }
        try:
            return parse_candidate_v2(
                candidate, expected_workspace_id=expected_workspace_id
            )
        except Exception as exc:
            raise CandidateApplicationError("Candidate v2 projection is invalid") from exc

    def _candidate_row(
        self, connection: sqlite3.Connection, candidate_id: str, workspace_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM candidate WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise CandidateNotFoundError("Candidate does not exist")
        if self._decode_item(row)["target"]["workspace_id"] != workspace_id:
            raise CandidateCrossWorkspaceError("Candidate belongs to another Workspace")
        return row

    def list_candidates(self, query: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = parse_candidate_query_result_v2(query)
            if parsed["schema"] != "candidate-list-query/v2":
                raise ValueError("wrong Candidate query variant")
            workspace_id, cursor, limit = (
                parsed["workspace_id"],
                parsed["cursor"],
                parsed["limit"],
            )
        except Exception as exc:
            raise CandidateApplicationError("Candidate list request is malformed") from exc
        offset = 0
        if cursor is not None:
            pieces = cursor.split("/")
            if (
                len(pieces) != 3
                or pieces[0] != "candidate"
                or pieces[1] != workspace_id
                or not pieces[2].isdigit()
            ):
                raise CandidateApplicationError(
                    "Candidate cursor is not bound to its Workspace"
                )
            offset = int(pieces[2])
        with self.repository.read_connection() as connection:
            total = connection.execute(
                "SELECT count(*) FROM candidate "
                "WHERE json_extract(item_json,'$.target.workspace_id')=?",
                (workspace_id,),
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM candidate "
                "WHERE json_extract(item_json,'$.target.workspace_id')=? "
                "ORDER BY created_at,candidate_id LIMIT ? OFFSET ?",
                (workspace_id, limit, offset),
            ).fetchall()
            items = [
                self._candidate_v2(connection, row, expected_workspace_id=workspace_id)
                for row in rows
            ]
        next_offset = offset + len(items)
        return parse_candidate_query_result_v2(
            {
                "schema": "candidate-list-result/v2",
                "workspace_id": workspace_id,
                "items": items,
                "next_cursor": (
                    f"candidate/{workspace_id}/{next_offset}"
                    if next_offset < total
                    else None
                ),
                "cursor_domain": "candidate",
                "total": total,
            }
        )

    def get_candidate(self, query: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = parse_candidate_query_result_v2(query)
            if parsed["schema"] != "candidate-get-query/v2":
                raise ValueError("wrong Candidate query variant")
        except Exception as exc:
            raise CandidateApplicationError("Candidate get request is malformed") from exc
        with self.repository.read_connection() as connection:
            row = self._candidate_row(
                connection, parsed["candidate_id"], parsed["workspace_id"]
            )
            candidate = self._candidate_v2(
                connection, row, expected_workspace_id=parsed["workspace_id"]
            )
        return parse_candidate_query_result_v2(
            {
                "schema": "candidate-get-result/v2",
                "workspace_id": parsed["workspace_id"],
                "candidate": candidate,
            }
        )

    def preview_candidate(self, query: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = parse_candidate_query_result_v2(query)
            if parsed["schema"] != "candidate-preview-query/v2":
                raise ValueError("wrong Candidate query variant")
        except Exception as exc:
            raise CandidateApplicationError(
                "Candidate preview request is malformed"
            ) from exc
        with self.repository.read_connection() as connection:
            row = self._candidate_row(
                connection, parsed["candidate_id"], parsed["workspace_id"]
            )
            item = self._decode_item(row)
        try:
            metadata = self.assets.require(
                item["payload_asset_id"], sha256=item["mutation"]["payload_hash"]
            )
            data = self.assets.read(
                item["payload_asset_id"],
                offset=parsed["offset"],
                length=parsed["length"],
            )
        except Exception as exc:
            raise CandidateNotFoundError("Candidate payload Asset is unavailable") from exc
        end = parsed["offset"] + len(data)
        return parse_candidate_query_result_v2(
            {
                "schema": "candidate-preview-result/v2",
                "workspace_id": parsed["workspace_id"],
                "candidate_id": parsed["candidate_id"],
                "payload_asset_id": metadata.asset_id,
                "payload_hash": metadata.sha256,
                "offset": parsed["offset"],
                "length": len(data),
                "total_length": metadata.size,
                "base64_chunk": base64.b64encode(data).decode("ascii"),
                "next_offset": end if end < metadata.size else None,
                "content_hash": hashlib.sha256(data).hexdigest(),
            }
        )

    def review(self, command: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = parse_candidate_review_v2(command)
            if parsed["schema"] != "candidate-review-command/v2":
                raise ValueError("wrong Candidate review variant")
        except Exception as exc:
            raise CandidateApplicationError(
                "Candidate review request is malformed"
            ) from exc
        request_hash = hashlib.sha256(canonical_bytes(parsed)).hexdigest()
        with self.repository.transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM candidate_review WHERE operation_key=?",
                (parsed["operation_key"],),
            ).fetchone()
            if previous is not None:
                if (
                    previous["request_hash"] != request_hash
                    or previous["candidate_id"] != parsed["candidate_id"]
                    or previous["decision"] != parsed["decision"]
                ):
                    raise CandidateConflictError(
                        "review operation key was reused with different payload"
                    )
                return self._review_result(
                    connection,
                    parsed,
                    previous["review_status"],
                    previous["review_revision"],
                    idempotent=True,
                )

            row = self._candidate_row(
                connection, parsed["candidate_id"], parsed["workspace_id"]
            )
            item = self._decode_item(row)
            if row["status"] != "staged":
                raise CandidateConflictError("Candidate lifecycle changed before review")
            if item["status"] != parsed["expected_status"]:
                raise CandidateConflictError("Candidate status changed before review")
            if self._review_row(connection, parsed["candidate_id"]) is not None:
                raise CandidateConflictError("Candidate already has a terminal review")
            review_status = "reviewed" if parsed["decision"] == "approve" else "rejected"
            connection.execute(
                "INSERT INTO candidate_review"
                "(candidate_id,operation_key,request_hash,decision,decided_by,"
                "expected_status,review_revision,review_status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    parsed["candidate_id"],
                    parsed["operation_key"],
                    request_hash,
                    parsed["decision"],
                    parsed["decided_by"],
                    parsed["expected_status"],
                    1,
                    review_status,
                    utc_now(),
                ),
            )
            return self._review_result(
                connection, parsed, review_status, 1, idempotent=False
            )

    def _review_result(
        self,
        connection: sqlite3.Connection,
        command: Mapping[str, Any],
        review_status: str,
        revision: int,
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        if review_status == "rejected":
            eligibility = "none"
        else:
            row = self._candidate_row(
                connection, command["candidate_id"], command["workspace_id"]
            )
            eligibility = self._eligibility(connection, row, self._decode_item(row))
        return parse_candidate_review_v2(
            {
                "schema": "candidate-review-result/v2",
                "operation_key": command["operation_key"],
                "workspace_id": command["workspace_id"],
                "candidate_id": command["candidate_id"],
                "decision": command["decision"],
                "status": review_status,
                "publication_eligibility": eligibility,
                "idempotent": idempotent,
                "review_revision": revision,
            }
        )

    def stage_story_state_batch(
        self,
        *,
        operation_key: str,
        workspace_id: str,
        items: Iterable[Mapping[str, Any]],
    ) -> tuple[StagedCandidate, ...]:
        return self.service.stage_batch(
            operation_key,
            items,
            expected_workspace_id=workspace_id,
            method="story-state.candidate-batch/v2",
        )

    stage_batch = stage_story_state_batch

    def stage_incomplete_stream(self, **kwargs: Any) -> StagedCandidate:
        if self.execution_authority is None:
            raise CandidateNotPublishableError(
                "Core chapter writer fence is not composed"
            )
        return self.execution_authority.stage_incomplete_stream(**kwargs)

    def story_state_projection_input(self, query: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed = parse_core_authority_v2(query)
            if (
                parsed["schema"] != "core-authority-query/v2"
                or parsed["entity_kind"] != "document"
            ):
                raise ValueError("wrong Story-State query variant")
        except Exception as exc:
            raise CandidateApplicationError(
                "Story-State projection request is malformed"
            ) from exc
        workspace_id, document_id = parsed["workspace_id"], parsed["entity_id"]
        with self.repository.read_connection() as connection:
            document = connection.execute(
                "SELECT * FROM document WHERE document_id=?", (document_id,)
            ).fetchone()
            if document is None:
                raise CandidateNotFoundError("Document does not exist")
            if document["workspace_id"] != workspace_id:
                raise CandidateCrossWorkspaceError(
                    "Document belongs to another Workspace"
                )
            if document["current_revision_id"] is None:
                raise CandidateNotFoundError("Document has no current Revision")
            revision = connection.execute(
                "SELECT * FROM revision WHERE revision_id=?",
                (document["current_revision_id"],),
            ).fetchone()
            if revision is None or revision["source_candidate_id"] is None:
                raise CandidateNotFoundError(
                    "Document has no Candidate-derived Revision"
                )
            publication = connection.execute(
                "SELECT * FROM publication_receipt WHERE revision_id=?",
                (revision["revision_id"],),
            ).fetchone()
            if publication is None:
                raise CandidateNotFoundError(
                    "Candidate-derived Revision has no Publication receipt"
                )
            candidate_row = self._candidate_row(
                connection, revision["source_candidate_id"], workspace_id
            )
            candidate = self._candidate_v2(
                connection, candidate_row, expected_workspace_id=workspace_id
            )
            payload = self.assets.require(
                candidate["payload_asset_id"],
                sha256=candidate["mutation"]["payload_hash"],
            )
            provenance, closure = self._projection_provenance(
                connection, candidate_row["candidate_id"]
            )
            content_asset_id = f"revision-{revision['revision_id']}"
            projection = {
                "schema": "story-state-projection-input/v2",
                "projection_input_id": "projection-"
                + hashlib.sha256(
                    (
                        f"{workspace_id}\n{publication['publication_id']}"
                        f"\n{revision['revision_id']}"
                    ).encode()
                ).hexdigest()[:48],
                "workspace_id": workspace_id,
                "publication": {
                    "publication_id": publication["publication_id"],
                    "candidate_id": candidate_row["candidate_id"],
                    "revision_id": revision["revision_id"],
                    "revision_number": revision["revision_number"],
                    "content_hash": revision["content_hash"],
                },
                "candidate": {
                    "candidate_id": candidate_row["candidate_id"],
                    "target": candidate["target"],
                    "payload_asset_id": payload.asset_id,
                    "payload_hash": payload.sha256,
                    "status": candidate["status"],
                },
                "current_revision": {
                    "revision_id": revision["revision_id"],
                    "content_asset_id": content_asset_id,
                    "content_hash": revision["content_hash"],
                    "revision_number": revision["revision_number"],
                },
                "assets": [
                    {
                        "asset_id": payload.asset_id,
                        "sha256": payload.sha256,
                        "role": "candidate.payload",
                    },
                    {
                        "asset_id": content_asset_id,
                        "sha256": revision["content_hash"],
                        "role": "revision.content",
                    },
                ],
                "provenance": provenance,
                "receipt_closure": closure,
                "derived_at": utc_now(),
            }
        return parse_story_state_projection_input_v2(
            projection, expected_workspace_id=workspace_id
        )

    def _projection_provenance(
        self, connection: sqlite3.Connection, candidate_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        receipt = connection.execute(
            "SELECT r.receipt_id,r.receipt_json,j.run_snapshot_hash,"
            "a.release_id,a.package_hash FROM execution_candidate_binding b "
            "JOIN execution_attempt a ON a.attempt_id=b.attempt_id "
            "JOIN execution_job j ON j.job_id=a.job_id "
            "JOIN execution_receipt r ON r.attempt_id=b.attempt_id "
            "WHERE b.candidate_id=? ORDER BY r.created_at DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if receipt is not None:
            try:
                root = json.loads(receipt["receipt_json"])
                root_id = root["receipt_id"]
                parent_ids = list(root["parent_receipt_ids"])
            except Exception as exc:
                raise CandidateApplicationError(
                    "Candidate provenance receipt is malformed"
                ) from exc
            closure: list[dict[str, Any]] = []
            visiting: set[str] = set()
            visited: set[str] = set()

            def visit(receipt_id: str) -> None:
                if receipt_id in visiting:
                    raise CandidateApplicationError(
                        "Candidate provenance receipt graph is cyclic"
                    )
                if receipt_id in visited:
                    return
                durable = connection.execute(
                    "SELECT receipt_json FROM execution_receipt WHERE receipt_id=?",
                    (receipt_id,),
                ).fetchone()
                if durable is None:
                    raise CandidateApplicationError(
                        "Candidate provenance receipt parent is missing"
                    )
                value = json.loads(durable["receipt_json"])
                parents = list(value["parent_receipt_ids"])
                visiting.add(receipt_id)
                for parent in parents:
                    visit(parent)
                visiting.remove(receipt_id)
                visited.add(receipt_id)
                closure.append(
                    {
                        "receipt_id": receipt_id,
                        "parent_receipt_ids": parents,
                        "receipt_hash": hash_jcs(
                            "story-state-receipt/v2",
                            {
                                "receipt_id": receipt_id,
                                "parent_receipt_ids": parents,
                            },
                        ),
                    }
                )

            visit(root_id)
            root_entry = next(item for item in closure if item["receipt_id"] == root_id)
            return (
                {
                    "receipt_id": root_id,
                    "receipt_hash": root_entry["receipt_hash"],
                    "run_snapshot_hash": receipt["run_snapshot_hash"],
                    "release_id": receipt["release_id"],
                    "package_hash": receipt["package_hash"],
                    "parent_receipt_ids": parent_ids,
                },
                closure,
            )

        core = connection.execute(
            "SELECT provenance_receipt_id FROM chapter_candidate_authority "
            "WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        root_id = (
            core["provenance_receipt_id"]
            if core is not None
            else "core-candidate-"
            + hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:48]
        )
        root = {"receipt_id": root_id, "parent_receipt_ids": []}
        root_hash = hash_jcs("story-state-receipt/v2", root)
        seed = hashlib.sha256(
            ("core-candidate/v2\n" + candidate_id).encode("utf-8")
        ).hexdigest()
        return (
            {
                "receipt_id": root_id,
                "receipt_hash": root_hash,
                "run_snapshot_hash": seed,
                "release_id": seed,
                "package_hash": seed,
                "parent_receipt_ids": [],
            },
            [{**root, "receipt_hash": root_hash}],
        )


__all__ = [
    "CandidateApplication",
    "CandidateApplicationError",
    "CandidateConflictError",
    "CandidateCrossWorkspaceError",
    "CandidateNotFoundError",
    "CandidateNotPublishableError",
]
