from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from backend.plotpilot_plugin_sdk import canonical_bytes
from backend.plotpilot_plugin_sdk.core_api import parse_publication
from backend.plotpilot_plugin_sdk.core_api_v2 import (
    parse_publication_command_v2,
    parse_publication_result_v2,
    validate_publication_v2,
)

from ...candidates.application import CandidateApplication
from ...candidates.service import CandidateService
from ...domain.entities import utc_now
from ...repositories.authority_application.errors import (
    IncompletePublicationError,
    OperationKeyReuseError,
)
from ..service import PublicationPreflight, PublicationReceipt, PublicationService


class PublicationApplication:
    """Typed publication-command/v1 application boundary without HTTP concerns."""

    route_id = "publication.accept"
    route_id_v2 = "publication.accept/v2"

    def __init__(self, service: PublicationService) -> None:
        self.service = service
        self.repository = service.repository
        self.repository.ensure_authority_application_schema()
        self.candidates = CandidateApplication(
            CandidateService(self.repository, service.assets)
        )

    def execute(self, command: Mapping[str, Any]) -> dict[str, Any]:
        return self.accept(command)

    handle = execute

    def accept(self, command: Mapping[str, Any]) -> dict[str, Any]:
        if command.get("schema") == "publication-command/v2":
            return self.accept_v2(command)
        return self._accept_v1(command)

    def _accept_v1(self, command: Mapping[str, Any]) -> dict[str, Any]:
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

    def accept_v2(self, command: Mapping[str, Any]) -> dict[str, Any]:
        """Accept the frozen v2 command while retaining the one Core writer."""
        try:
            parsed = parse_publication_command_v2(command)
        except Exception as exc:
            raise ValueError("PublicationApplication requires publication-command/v2") from exc
        request_json = canonical_bytes(parsed).decode("utf-8")
        payload_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        operation_key = parsed["publication_operation_key"]
        candidate = self.candidates.get_candidate(
            {
                "schema": "candidate-get-query/v2",
                "workspace_id": parsed["workspace_id"],
                "candidate_id": parsed["candidate_id"],
            }
        )["candidate"]

        with self.repository.read_connection() as connection:
            previous = connection.execute(
                "SELECT * FROM core_authority_operation WHERE route_id=? AND operation_key=?",
                (self.route_id_v2, operation_key),
            ).fetchone()
            if previous is not None and (
                previous["payload_hash"] != payload_hash
                or previous["request_json"] != request_json
                or previous["workspace_id"] != parsed["workspace_id"]
            ):
                raise OperationKeyReuseError(
                    "publication key reused with a different command"
                )

        preflight = self.service.preflight(
            parsed["candidate_id"], expected_workspace_id=parsed["workspace_id"]
        )
        with self.repository.transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM core_authority_operation WHERE route_id=? AND operation_key=?",
                (self.route_id_v2, operation_key),
            ).fetchone()
            if previous is not None:
                if (
                    previous["payload_hash"] != payload_hash
                    or previous["request_json"] != request_json
                    or previous["workspace_id"] != parsed["workspace_id"]
                ):
                    raise OperationKeyReuseError(
                        "publication key reused with a different command"
                    )
                receipt = self.service.accept_in_transaction(
                    connection,
                    operation_key,
                    parsed["candidate_id"],
                    created_by=parsed["accepted_by"],
                    preflight=preflight,
                )
                authoritative = self._result_v2(
                    connection,
                    receipt,
                    preflight,
                    candidate,
                    operation_key=operation_key,
                    idempotent=False,
                )
                self._verify_stored_result_v2(
                    previous["response_json"], authoritative, parsed, candidate
                )
                result = dict(authoritative)
                result["idempotent"] = True
                validate_publication_v2(parsed, result, candidate=candidate)
                return parse_publication_result_v2(result)

            receipt = self.service.accept_in_transaction(
                connection,
                operation_key,
                parsed["candidate_id"],
                created_by=parsed["accepted_by"],
                preflight=preflight,
            )
            result = self._result_v2(
                connection,
                receipt,
                preflight,
                candidate,
                operation_key=operation_key,
                idempotent=False,
            )
            validate_publication_v2(parsed, result, candidate=candidate)
            result = parse_publication_result_v2(result)
            response_json = canonical_bytes(result).decode("utf-8")
            connection.execute(
                "INSERT INTO core_authority_operation"
                "(route_id,operation_key,workspace_id,payload_hash,request_json,"
                "success_status,response_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    self.route_id_v2,
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

    @classmethod
    def _result_v2(
        cls,
        connection: Any,
        receipt: PublicationReceipt,
        preflight: PublicationPreflight,
        candidate: Mapping[str, Any],
        *,
        operation_key: str,
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
        provenance_receipt_id = cls._provenance_receipt_id(
            connection, receipt.candidate_id
        )
        result = {
            "schema": "publication-result/v2",
            "publication_id": receipt.publication_id,
            "publication_operation_key": operation_key,
            "candidate_id": receipt.candidate_id,
            "workspace_id": revision["workspace_id"],
            "entity_kind": "document",
            "entity_id": revision["document_id"],
            "revision_id": revision["revision_id"],
            "revision_number": revision["revision_number"],
            "content_hash": revision["content_hash"],
            "cas": {
                "base_revision_id": preflight.item["base"]["revision_id"],
                "base_content_hash": preflight.item["base"]["content_hash"],
                "revision_id": revision["revision_id"],
                "revision_number": revision["revision_number"],
                "content_hash": revision["content_hash"],
                "write_set": [dict(item) for item in preflight.item["write_set"]],
            },
            "provenance_receipt_id": provenance_receipt_id,
            "idempotent": idempotent,
        }
        validate_publication_v2(
            {
                "schema": "publication-command/v2",
                "publication_operation_key": operation_key,
                "workspace_id": revision["workspace_id"],
                "candidate_id": receipt.candidate_id,
                "accepted_by": revision["created_by"],
            },
            result,
            candidate=candidate,
        )
        return result

    @staticmethod
    def _provenance_receipt_id(connection: Any, candidate_id: str) -> str:
        execution = connection.execute(
            "SELECT r.receipt_id FROM execution_candidate_binding b "
            "JOIN execution_receipt r ON r.attempt_id=b.attempt_id "
            "WHERE b.candidate_id=? ORDER BY r.created_at DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if execution is not None:
            return execution["receipt_id"]
        stream = connection.execute(
            "SELECT provenance_receipt_id FROM chapter_candidate_authority "
            "WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if stream is not None:
            return stream["provenance_receipt_id"]
        return "core-candidate-" + hashlib.sha256(
            candidate_id.encode("utf-8")
        ).hexdigest()[:48]

    @staticmethod
    def _verify_stored_result_v2(
        stored_json: str,
        authoritative: Mapping[str, Any],
        command: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> None:
        try:
            stored = json.loads(stored_json)
            parsed = parse_publication_result_v2(stored)
            if parsed["idempotent"] is not False:
                raise ValueError("first result must not be marked idempotent")
            validate_publication_v2(command, parsed, candidate=candidate)
            if canonical_bytes(parsed).decode("utf-8") != stored_json:
                raise ValueError("stored result is not canonical")
            if canonical_bytes(parsed) != canonical_bytes(authoritative):
                raise ValueError("stored result is not bound to authority")
        except Exception as exc:
            raise IncompletePublicationError(
                "committed Publication v2 result is incomplete"
            ) from exc

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
