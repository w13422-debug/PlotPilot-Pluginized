from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from backend.plotpilot_plugin_sdk import canonical_bytes
from backend.plotpilot_plugin_sdk.core_api import parse_publication

from ...domain.entities import utc_now
from ...repositories.authority_application.errors import (
    IncompletePublicationError,
    OperationKeyReuseError,
)
from ..service import PublicationPreflight, PublicationReceipt, PublicationService


class PublicationApplication:
    """Typed publication-command/v1 application boundary without HTTP concerns."""

    route_id = "publication.accept"

    def __init__(self, service: PublicationService) -> None:
        self.service = service
        self.repository = service.repository
        self.repository.ensure_authority_application_schema()

    def execute(self, command: Mapping[str, Any]) -> dict[str, Any]:
        return self.accept(command)

    handle = execute

    def accept(self, command: Mapping[str, Any]) -> dict[str, Any]:
        parsed = parse_publication(command)
        if parsed["schema"] != "publication-command/v1":
            raise ValueError("PublicationApplication requires publication-command/v1")
        request_json = canonical_bytes(parsed).decode("utf-8")
        payload_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        operation_key = parsed["publication_operation_key"]

        # A committed key conflict is resolved before touching the candidate;
        # same-command replay still performs the complete durable preflight.
        with self.repository.read_connection() as connection:
            previous = connection.execute(
                "SELECT * FROM core_authority_operation WHERE route_id=? AND operation_key=?",
                (self.route_id, operation_key),
            ).fetchone()
            if previous is not None and (
                previous["payload_hash"] != payload_hash
                or previous["request_json"] != request_json
                or previous["workspace_id"] != parsed["workspace_id"]
            ):
                raise OperationKeyReuseError("publication key reused with a different command")

        preflight = self.service.preflight(
            parsed["candidate_id"], expected_workspace_id=parsed["workspace_id"]
        )
        with self.repository.transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM core_authority_operation WHERE route_id=? AND operation_key=?",
                (self.route_id, operation_key),
            ).fetchone()
            if previous is not None:
                if (
                    previous["payload_hash"] != payload_hash
                    or previous["request_json"] != request_json
                    or previous["workspace_id"] != parsed["workspace_id"]
                ):
                    raise OperationKeyReuseError("publication key reused with a different command")
                receipt = self.service.accept_in_transaction(
                    connection,
                    operation_key,
                    parsed["candidate_id"],
                    created_by=parsed["accepted_by"],
                    preflight=preflight,
                )
                authoritative = self._result(
                    connection, receipt, preflight, idempotent=False
                )
                self._verify_stored_result(previous["response_json"], authoritative, parsed)
                result = dict(authoritative)
                result["idempotent"] = True
                return parse_publication(result, command=parsed)

            receipt = self.service.accept_in_transaction(
                connection,
                operation_key,
                parsed["candidate_id"],
                created_by=parsed["accepted_by"],
                preflight=preflight,
            )
            result = self._result(connection, receipt, preflight, idempotent=False)
            result = parse_publication(result, command=parsed)
            response_json = canonical_bytes(result).decode("utf-8")
            connection.execute(
                "INSERT INTO core_authority_operation(route_id,operation_key,workspace_id,payload_hash,request_json,success_status,response_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    self.route_id,
                    operation_key,
                    parsed["workspace_id"],
                    payload_hash,
                    request_json,
                    200,
                    response_json,
                    utc_now(),
                ),
            )
            return result

    @staticmethod
    def _result(
        connection: Any,
        receipt: PublicationReceipt,
        preflight: PublicationPreflight,
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        revision = connection.execute(
            "SELECT * FROM revision WHERE revision_id=?", (receipt.revision_id,)
        ).fetchone()
        if revision is None:
            raise IncompletePublicationError("Publication Revision is missing")
        target = preflight.item["target"]
        if (
            revision["workspace_id"] != preflight.workspace_id
            or revision["document_id"] != target["entity_id"]
            or revision["node_id"] is not None
            or revision["source_candidate_id"] != receipt.candidate_id
        ):
            raise IncompletePublicationError("Publication result identity drifted")
        return {
            "schema": "publication-result/v1",
            "publication_id": receipt.publication_id,
            "candidate_id": receipt.candidate_id,
            "workspace_id": revision["workspace_id"],
            "entity_kind": "document",
            "entity_id": revision["document_id"],
            "resulting_revision": {
                "revision_id": revision["revision_id"],
                "workspace_id": revision["workspace_id"],
                "entity_kind": "document",
                "entity_id": revision["document_id"],
                "content_hash": revision["content_hash"],
                "revision_number": revision["revision_number"],
            },
            "idempotent": idempotent,
        }

    @staticmethod
    def _verify_stored_result(
        stored_json: str,
        authoritative: dict[str, Any],
        command: dict[str, Any],
    ) -> None:
        try:
            stored = json.loads(stored_json)
            parsed = parse_publication(stored, command=command)
            if parsed["idempotent"] is not False:
                raise ValueError("first result must not be marked idempotent")
            if canonical_bytes(parsed).decode("utf-8") != stored_json:
                raise ValueError("stored result is not canonical")
            if canonical_bytes(parsed) != canonical_bytes(authoritative):
                raise ValueError("stored result is not bound to authority")
        except Exception as exc:
            raise IncompletePublicationError("committed Publication result is incomplete") from exc


__all__ = ["PublicationApplication"]
