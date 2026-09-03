from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from backend.plotpilot_plugin_sdk.verifier import assert_valid

from ..assets import AssetStore
from ..domain.entities import utc_now
from ..repositories import CoreAuthorityRepository


class CandidateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StagedCandidate:
    item_id: str
    candidate_id: str | None
    stage_status: str
    publication_eligibility: str


class CandidateService:
    """Durably stage Candidate authority without granting a plugin publication."""

    def __init__(self, repository: CoreAuthorityRepository, assets: AssetStore) -> None:
        self.repository, self.assets = repository, assets

    @staticmethod
    def _canonical(item: Mapping[str, Any]) -> tuple[str, str]:
        value = dict(item)
        if value.get("schema") != "candidate-item/v1":
            raise CandidateError("unsupported candidate schema")
        required = {
            "item_id",
            "item_kind",
            "target",
            "mutation",
            "payload_asset_id",
            "base",
            "write_set",
            "parent_candidate_ids",
            "source_refs",
            "status",
        }
        if set(value) != required | {"schema"}:
            raise CandidateError("candidate fields do not match v1")
        try:
            assert_valid("candidate-item/v1", value)
        except Exception as exc:
            raise CandidateError("candidate item does not match v1") from exc
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _scoped_operation_key(
        operation_key: str,
        *,
        context_identity: str | None,
        method: str,
    ) -> str:
        if context_identity is None:
            return operation_key
        return hashlib.sha256(
            f"candidate-operation/v1\n{context_identity}\n{method}\n{operation_key}".encode()
        ).hexdigest()

    @staticmethod
    def _eligibility(item: Mapping[str, Any]) -> str:
        if item.get("item_kind") == "incomplete_stream":
            return "eligible"
        return "eligible" if item.get("status") == "complete" else "review_only"

    def stage(self, operation_key: str, item: dict[str, Any]) -> StagedCandidate:
        with self.repository.transaction() as connection:
            return self.stage_in_transaction(connection, operation_key, item)

    def stage_batch(
        self,
        operation_key: str,
        items: Iterable[Mapping[str, Any]],
        *,
        expected_workspace_id: str | None = None,
        context_identity: str | None = None,
        method: str = "candidate.batch.stage/v1",
        initial_status: str = "staged",
    ) -> tuple[StagedCandidate, ...]:
        with self.repository.transaction() as connection:
            return self.stage_batch_in_transaction(
                connection,
                operation_key,
                items,
                expected_workspace_id=expected_workspace_id,
                context_identity=context_identity,
                method=method,
                initial_status=initial_status,
            )

    def stage_batch_in_transaction(
        self,
        connection: Any,
        operation_key: str,
        items: Iterable[Mapping[str, Any]],
        *,
        expected_workspace_id: str | None = None,
        context_identity: str | None = None,
        method: str = "candidate.batch.stage/v1",
        initial_status: str = "staged",
        allow_incomplete_stream: bool = False,
    ) -> tuple[StagedCandidate, ...]:
        """Stage a full Candidate batch atomically with an immutable replay key."""
        values = [dict(item) for item in items]
        if not values:
            raise CandidateError("candidate batch cannot be empty")
        item_ids = [value.get("item_id") for value in values]
        if not all(isinstance(item_id, str) and item_id for item_id in item_ids):
            raise CandidateError("candidate batch item identity is invalid")
        if len(set(item_ids)) != len(item_ids):
            raise CandidateError("candidate batch contains duplicate item identities")

        scoped_key = self._scoped_operation_key(
            operation_key, context_identity=context_identity, method=method
        )
        raw = json.dumps(
            values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        payload_hash = hashlib.sha256(raw).hexdigest()
        workspace_id = expected_workspace_id or str(
            values[0].get("target", {}).get("workspace_id", "")
        )
        item_ids_json = json.dumps(item_ids, ensure_ascii=False, separators=(",", ":"))
        existing = connection.execute(
            "SELECT workspace_id,payload_hash,item_ids_json "
            "FROM candidate_batch_operation WHERE operation_key=?",
            (scoped_key,),
        ).fetchone()
        if existing is not None:
            if (
                existing["workspace_id"] != workspace_id
                or existing["payload_hash"] != payload_hash
                or existing["item_ids_json"] != item_ids_json
            ):
                raise CandidateError(
                    "candidate batch operation key reused with different payload"
                )
        else:
            connection.execute(
                "INSERT INTO candidate_batch_operation"
                "(operation_key,workspace_id,payload_hash,item_ids_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (scoped_key, workspace_id, payload_hash, item_ids_json, utc_now()),
            )

        # Any failure below exits the caller-owned transaction and removes the
        # ledger row plus every previously staged Candidate in this batch.
        return tuple(
            self.stage_in_transaction(
                connection,
                operation_key,
                item,
                initial_status=initial_status,
                context_identity=context_identity,
                method=method,
                expected_workspace_id=expected_workspace_id,
                allow_incomplete_stream=allow_incomplete_stream,
            )
            for item in values
        )

    def stage_in_transaction(
        self,
        connection: Any,
        operation_key: str,
        item: dict[str, Any],
        *,
        initial_status: str = "staged",
        context_identity: str | None = None,
        method: str = "candidate.stage/v1",
        expected_workspace_id: str | None = None,
        allow_incomplete_stream: bool = False,
    ) -> StagedCandidate:
        """Stage under a caller-owned P1 transaction.

        Prepared Candidates remain invisible to Publication until the terminal
        transaction promotes them.  The incomplete-stream opt-in is reserved
        to the fenced Core chapter writer.
        """
        if initial_status not in {"prepared", "staged"}:
            raise CandidateError("invalid candidate initial status")
        raw, item_hash = self._canonical(item)
        item_id = item["item_id"]
        scoped_key = self._scoped_operation_key(
            operation_key, context_identity=context_identity, method=method
        )
        existing = connection.execute(
            "SELECT candidate_id,item_hash,item_json,status FROM candidate "
            "WHERE operation_key=? AND item_id=?",
            (scoped_key, item_id),
        ).fetchone()
        if existing is not None:
            if existing["item_hash"] != item_hash:
                raise CandidateError("operation key reused with different payload")
            saved = json.loads(existing["item_json"])
            return StagedCandidate(
                item_id, existing["candidate_id"], "idempotent", self._eligibility(saved)
            )
        if item["status"] in {"failed", "skipped"}:
            return StagedCandidate(item_id, None, "not_created", "ineligible")

        target = item["target"]
        incomplete = item["item_kind"] == "incomplete_stream"
        if incomplete and not allow_incomplete_stream:
            # The pre-existing plugin/direct path stays closed.
            raise CandidateError("incomplete_stream is Core durable-stream authority only")
        if incomplete:
            if (
                item["status"] != "partial"
                or target["entity_kind"] != "document"
                or item["mutation"]["mode"] != "replace"
                or item["mutation"]["payload_schema"] != "core/document-text/v1"
            ):
                raise CandidateError(
                    "incomplete_stream must be a Core partial document replacement"
                )
        elif item["item_kind"] != "document" or target["entity_kind"] != "document":
            raise CandidateError("first Core slice supports document publication only")
        allowed_modes = {"replace"} if incomplete else {"replace", "append_text"}
        if item["mutation"]["mode"] not in allowed_modes:
            raise CandidateError("unsupported document mutation")
        if (
            expected_workspace_id is not None
            and target["workspace_id"] != expected_workspace_id
        ):
            raise CandidateError("candidate target is outside the RunSnapshot workspace")

        asset = self.assets.describe(item["payload_asset_id"])
        if asset.sha256 != item["mutation"]["payload_hash"]:
            raise CandidateError("payload hash mismatch")
        self.assets.require(
            item["payload_asset_id"], sha256=item["mutation"]["payload_hash"]
        )
        document = connection.execute(
            "SELECT workspace_id,current_revision_id FROM document WHERE document_id=?",
            (target["entity_id"],),
        ).fetchone()
        if document is None:
            raise CandidateError("target document missing")
        if document["workspace_id"] != target["workspace_id"]:
            raise CandidateError("target workspace mismatch")
        base = item["base"]
        if document["current_revision_id"] != base["revision_id"]:
            raise CandidateError("stale candidate base")
        revision = connection.execute(
            "SELECT workspace_id,revision_id,content_hash FROM revision WHERE revision_id=?",
            (base["revision_id"],),
        ).fetchone()
        if revision is None or revision["content_hash"] != base["content_hash"]:
            raise CandidateError("base hash mismatch")
        if revision["workspace_id"] != target["workspace_id"] or (
            expected_workspace_id is not None
            and revision["workspace_id"] != expected_workspace_id
        ):
            raise CandidateError("candidate base is outside the RunSnapshot workspace")
        expected_write = {
            "workspace_id": target["workspace_id"],
            "entity_kind": "document",
            "entity_id": target["entity_id"],
            "revision_id": base["revision_id"],
            "content_hash": base["content_hash"],
        }
        if len(item["write_set"]) != 1 or item["write_set"][0] != expected_write:
            raise CandidateError("write-set is not the exact target base")

        self._validate_source_refs(connection, item["source_refs"])
        for parent in item["parent_candidate_ids"]:
            row = connection.execute(
                "SELECT status FROM candidate WHERE candidate_id=?", (parent,)
            ).fetchone()
            if row is None or row["status"] in {
                "rejected",
                "deleted",
                "expired",
                "prepared",
            }:
                raise CandidateError("invalid parent candidate")

        candidate_id = f"candidate-{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO candidate VALUES(?,?,?,?,?,?,?)",
            (
                candidate_id,
                item_id,
                scoped_key,
                item_hash,
                raw,
                initial_status,
                utc_now(),
            ),
        )
        return StagedCandidate(
            item_id, candidate_id, "created", self._eligibility(item)
        )

    @staticmethod
    def _validate_source_refs(connection: Any, source_refs: Iterable[Mapping[str, Any]]) -> None:
        for source in source_refs:
            if set(source) != {
                "workspace_id",
                "source_type",
                "source_id",
                "revision_or_hash",
            }:
                raise CandidateError("invalid source reference fields")
            workspace_id = source["workspace_id"]
            if workspace_id is None:
                continue
            if not connection.execute(
                "SELECT 1 FROM workspace WHERE workspace_id=?", (workspace_id,)
            ).fetchone():
                raise CandidateError("source workspace missing")
            if source["source_type"] == "document":
                row = connection.execute(
                    "SELECT workspace_id FROM document WHERE document_id=?",
                    (source["source_id"],),
                ).fetchone()
                if row is None:
                    raise CandidateError("source document missing")
                if row["workspace_id"] != workspace_id:
                    raise CandidateError("source workspace mismatch")
            elif source["source_type"] == "revision":
                row = connection.execute(
                    "SELECT workspace_id,revision_id,content_hash FROM revision "
                    "WHERE revision_id=?",
                    (source["source_id"],),
                ).fetchone()
                if row is None:
                    raise CandidateError("source revision missing")
                if row["workspace_id"] != workspace_id or source[
                    "revision_or_hash"
                ] not in {row["revision_id"], row["content_hash"]}:
                    raise CandidateError("source revision mismatch")
            else:
                raise CandidateError("unsupported source reference type")
