from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from backend.plotpilot_plugin_sdk import canonical_bytes
from backend.plotpilot_plugin_sdk.core_api import parse_core_authority

from ...domain.entities import utc_now
from ..authority import CoreAuthorityRepository
from .errors import (
    CrossWorkspaceError,
    IncompletePublicationError,
    OperationKeyReuseError,
    StaleCasError,
    UnknownReferenceError,
)

if TYPE_CHECKING:
    from ...publication.service import PublicationService


_COMMAND_ROUTES = {
    "core-workspace-create-command/v1": "workspace.create",
    "core-workspace-update-command/v1": "workspace.update",
    "core-workspace-delete-command/v1": "workspace.delete",
    "core-document-create-command/v1": "document.create",
    "core-document-update-command/v1": "document.update",
    "core-node-create-command/v1": "node.create",
    "core-node-update-command/v1": "node.update",
    "core-node-delete-command/v1": "node.delete",
    "core-relation-create-command/v1": "relation.create",
    "core-relation-delete-command/v1": "relation.delete",
    "core-document-revision-create-command/v1": "document.revision.create",
    "core-node-revision-create-command/v1": "node.revision.create",
}

_QUERY_ROUTES = {
    "core-workspace-query/v1": "workspace.list",
    "core-workspace-get-query/v1": "workspace.get",
    "core-document-query/v1": "document.list",
    "core-document-get-query/v1": "document.get",
    "core-node-query/v1": "node.list",
    "core-node-get-query/v1": "node.get",
    "core-relation-query/v1": "relation.list",
    "core-document-revision-query/v1": "document.revision.list",
    "core-node-revision-query/v1": "node.revision.list",
    "core-revision-get-query/v1": "revision.get",
    "core-revision-content-query/v1": "revision.content",
}

_SUCCESS_STATUS = {
    route: 201
    for route in (
        "workspace.create",
        "document.create",
        "node.create",
        "relation.create",
        "document.revision.create",
        "node.revision.create",
    )
}


def _canonical(value: Mapping[str, Any]) -> tuple[str, str]:
    raw = canonical_bytes(value)
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


class CoreAuthorityApplication:
    """Production P1 authority seam for the frozen 23-route Core matrix.

    It is deliberately transport-free.  The future HTTP router validates
    ingress then delegates every route here; all SQL remains behind the sole
    ``CoreAuthorityRepository`` transaction root.
    """

    command_routes = frozenset(_COMMAND_ROUTES.values())
    query_routes = frozenset(_QUERY_ROUTES.values())

    def __init__(
        self,
        repository: CoreAuthorityRepository,
        publication_service: PublicationService | None = None,
    ) -> None:
        self.repository = repository
        if publication_service is not None and publication_service.repository is not repository:
            raise ValueError("Publication verifier must share the Core authority repository")
        self.publication_service = publication_service
        repository.ensure_authority_application_schema()

    def handle(self, route_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        schema = request.get("schema")
        if schema in _COMMAND_ROUTES:
            if _COMMAND_ROUTES[schema] != route_id:
                raise ValueError("authority command schema is not bound to route")
            return self.execute_command(request)
        if schema in _QUERY_ROUTES:
            if _QUERY_ROUTES[schema] != route_id:
                raise ValueError("authority query schema is not bound to route")
            return self.query(request)
        raise ValueError("unsupported authority request schema")

    dispatch = handle

    def execute(self, route_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.handle(route_id, request)

    def execute_command(self, command: Mapping[str, Any]) -> dict[str, Any]:
        parsed = parse_core_authority(command)
        route_id = _COMMAND_ROUTES.get(parsed["schema"])
        if route_id is None:
            raise ValueError("not an authority command")
        request_json, payload_hash = _canonical(parsed)
        operation_key = parsed["operation_key"]
        workspace_id = parsed["workspace_id"]

        with self.repository.transaction() as connection:
            previous = connection.execute(
                "SELECT * FROM core_authority_operation WHERE route_id=? AND operation_key=?",
                (route_id, operation_key),
            ).fetchone()
            if previous is not None:
                if (
                    previous["payload_hash"] != payload_hash
                    or previous["request_json"] != request_json
                    or previous["workspace_id"] != workspace_id
                ):
                    raise OperationKeyReuseError("operation key reused with a different command")
                try:
                    result = json.loads(previous["response_json"])
                    parsed_result = parse_core_authority(result, expected_workspace_id=workspace_id)
                except Exception as exc:
                    raise StaleCasError("committed authority operation is incomplete") from exc
                response_json, _ = _canonical(parsed_result)
                if response_json != previous["response_json"]:
                    raise StaleCasError("committed authority response is not canonical")
                return parsed_result

            result = self._mutate(connection, route_id, parsed)
            result = parse_core_authority(result, expected_workspace_id=workspace_id)
            response_json, _ = _canonical(result)
            try:
                connection.execute(
                    "INSERT INTO core_authority_operation(route_id,operation_key,workspace_id,payload_hash,request_json,success_status,response_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route_id,
                        operation_key,
                        workspace_id,
                        payload_hash,
                        request_json,
                        _SUCCESS_STATUS.get(route_id, 200),
                        response_json,
                        utc_now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise OperationKeyReuseError("operation key was committed concurrently") from exc
            return result

    def query(self, query: Mapping[str, Any]) -> dict[str, Any]:
        parsed = parse_core_authority(query)
        route_id = _QUERY_ROUTES.get(parsed["schema"])
        if route_id is None:
            raise ValueError("not an authority query")
        with self.repository.read_connection() as connection:
            result = self._query(connection, route_id, parsed)
        expected = parsed.get("workspace_id")
        return parse_core_authority(result, expected_workspace_id=expected)

    @staticmethod
    def _workspace(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": "core-workspace/v1",
            "workspace_id": row["workspace_id"],
            "workspace_kind": row["workspace_kind"],
            "title": row["title"],
            "status": row["status"],
            "current_plan_revision_id": row["current_plan_revision_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "revision": row["revision"],
        }

    @staticmethod
    def _document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": "core-document/v1",
            "document_id": row["document_id"],
            "workspace_id": row["workspace_id"],
            "document_type": row["document_type"],
            "title": row["title"],
            "current_revision_id": row["current_revision_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "revision": row["revision"],
        }

    @staticmethod
    def _node(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": "core-node/v1",
            "node_id": row["node_id"],
            "workspace_id": row["workspace_id"],
            "document_id": row["document_id"],
            "node_type": row["node_type"],
            "title": row["title"],
            "parent_node_id": row["parent_node_id"],
            "position": row["position"],
            "current_revision_id": row["current_revision_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "revision": row["revision"],
        }

    @staticmethod
    def _relation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": "core-relation/v1",
            "relation_id": row["relation_id"],
            "workspace_id": row["workspace_id"],
            "relation_type": row["relation_type"],
            "source_id": row["source_id"],
            "target_id": row["target_id"],
            "revision_id": row["revision_id"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _revision(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": "core-revision/v1",
            "revision_id": row["revision_id"],
            "workspace_id": row["workspace_id"],
            "document_id": row["document_id"],
            "node_id": row["node_id"],
            "parent_revision_id": row["parent_revision_id"],
            "content_hash": row["content_hash"],
            "created_by": row["created_by"],
            "source_candidate_id": row["source_candidate_id"],
            "created_at": row["created_at"],
            "revision_number": row["revision_number"],
            "payload_schema": row["payload_schema"],
        }

    @staticmethod
    def _page(schema: str, rows: list[sqlite3.Row], offset: int, limit: int, total: int, project) -> dict[str, Any]:
        items = [project(row) for row in rows]
        end = offset + len(items)
        return {
            "schema": schema,
            "items": items,
            "offset": offset,
            "limit": limit,
            "total": total,
            "next_offset": end if end < total else None,
        }

    @staticmethod
    def _require_workspace(connection: sqlite3.Connection, workspace_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM workspace WHERE workspace_id=?", (workspace_id,)).fetchone()
        if row is None:
            raise UnknownReferenceError("workspace does not exist")
        return row

    @staticmethod
    def _require_owned(row: sqlite3.Row | None, workspace_id: str, label: str) -> sqlite3.Row:
        if row is None:
            raise UnknownReferenceError(f"{label} does not exist")
        if row["workspace_id"] != workspace_id:
            raise CrossWorkspaceError(f"{label} is outside workspace")
        return row

    def _query(self, connection: sqlite3.Connection, route_id: str, value: dict[str, Any]) -> dict[str, Any]:
        if route_id == "workspace.list":
            where = ""
            params: list[Any] = []
            if value["workspace_id"] is not None:
                self._require_workspace(connection, value["workspace_id"])
                where = " WHERE workspace_id=?"
                params.append(value["workspace_id"])
            total = connection.execute(f"SELECT count(*) FROM workspace{where}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM workspace{where} ORDER BY workspace_id LIMIT ? OFFSET ?",
                (*params, value["limit"], value["offset"]),
            ).fetchall()
            return self._page("core-workspace-page/v1", rows, value["offset"], value["limit"], total, self._workspace)

        if route_id == "workspace.get":
            return self._workspace(self._require_workspace(connection, value["workspace_id"]))

        if route_id == "document.list":
            self._require_workspace(connection, value["workspace_id"])
            clauses = ["workspace_id=?"]
            params = [value["workspace_id"]]
            if value["document_id"] is not None:
                clauses.append("document_id=?")
                params.append(value["document_id"])
            where = " AND ".join(clauses)
            total = connection.execute(f"SELECT count(*) FROM document WHERE {where}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM document WHERE {where} ORDER BY document_id LIMIT ? OFFSET ?",
                (*params, value["limit"], value["offset"]),
            ).fetchall()
            return self._page("core-document-page/v1", rows, value["offset"], value["limit"], total, self._document)

        if route_id == "document.get":
            row = connection.execute("SELECT * FROM document WHERE document_id=?", (value["document_id"],)).fetchone()
            return self._document(self._require_owned(row, value["workspace_id"], "document"))

        if route_id == "node.list":
            self._require_workspace(connection, value["workspace_id"])
            clauses = ["workspace_id=?"]
            params = [value["workspace_id"]]
            for name in ("node_id", "document_id", "parent_node_id"):
                if value[name] is not None:
                    clauses.append(f"{name}=?")
                    params.append(value[name])
            where = " AND ".join(clauses)
            total = connection.execute(f"SELECT count(*) FROM node WHERE {where}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM node WHERE {where} ORDER BY position,node_id LIMIT ? OFFSET ?",
                (*params, value["limit"], value["offset"]),
            ).fetchall()
            return self._page("core-node-page/v1", rows, value["offset"], value["limit"], total, self._node)

        if route_id == "node.get":
            row = connection.execute("SELECT * FROM node WHERE node_id=?", (value["node_id"],)).fetchone()
            return self._node(self._require_owned(row, value["workspace_id"], "node"))

        if route_id == "relation.list":
            self._require_workspace(connection, value["workspace_id"])
            clauses = ["workspace_id=?"]
            params = [value["workspace_id"]]
            for name in ("relation_id", "source_id", "target_id", "relation_type"):
                if value[name] is not None:
                    clauses.append(f"{name}=?")
                    params.append(value[name])
            where = " AND ".join(clauses)
            total = connection.execute(f"SELECT count(*) FROM relation WHERE {where}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM relation WHERE {where} ORDER BY relation_id LIMIT ? OFFSET ?",
                (*params, value["limit"], value["offset"]),
            ).fetchall()
            return self._page("core-relation-page/v1", rows, value["offset"], value["limit"], total, self._relation)

        if route_id in {"document.revision.list", "node.revision.list"}:
            target_key = "document_id" if route_id.startswith("document") else "node_id"
            target_table = "document" if target_key == "document_id" else "node"
            target = connection.execute(
                f"SELECT * FROM {target_table} WHERE {target_key}=?", (value[target_key],)
            ).fetchone()
            self._require_owned(target, value["workspace_id"], target_table)
            clauses = ["workspace_id=?", f"{target_key}=?"]
            params = [value["workspace_id"], value[target_key]]
            if value["revision_id"] is not None:
                clauses.append("revision_id=?")
                params.append(value["revision_id"])
            where = " AND ".join(clauses)
            total = connection.execute(f"SELECT count(*) FROM revision WHERE {where}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM revision WHERE {where} ORDER BY revision_number,revision_id LIMIT ? OFFSET ?",
                (*params, value["limit"], value["offset"]),
            ).fetchall()
            return self._page("core-revision-page/v1", rows, value["offset"], value["limit"], total, self._revision)

        if route_id == "revision.get":
            row = connection.execute("SELECT * FROM revision WHERE revision_id=?", (value["revision_id"],)).fetchone()
            return self._revision(self._require_owned(row, value["workspace_id"], "revision"))

        if route_id == "revision.content":
            row = connection.execute("SELECT * FROM revision WHERE revision_id=?", (value["revision_id"],)).fetchone()
            row = self._require_owned(row, value["workspace_id"], "revision")
            text = row["content"]
            offset, limit = value["offset"], value["length"]
            page = text[offset : offset + limit]
            end = offset + len(page)
            return {
                "schema": "core-revision-content-page/v1",
                "revision_id": row["revision_id"],
                "offset": offset,
                "length": len(page),
                "total_length": len(text),
                "text": page,
                "next_offset": end if end < len(text) else None,
            }

        raise ValueError(f"unsupported authority query route: {route_id}")

    def _mutate(self, connection: sqlite3.Connection, route_id: str, value: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        workspace_id = value["workspace_id"]

        if route_id == "workspace.create":
            if connection.execute("SELECT 1 FROM workspace WHERE workspace_id=?", (workspace_id,)).fetchone():
                raise StaleCasError("workspace identity already exists")
            connection.execute(
                "INSERT INTO workspace VALUES(?,?,?,?,?,?,?,?,?)",
                (workspace_id, value["workspace_kind"], value["title"], "active", None, "{}", now, now, 0),
            )
            return self._workspace(connection.execute("SELECT * FROM workspace WHERE workspace_id=?", (workspace_id,)).fetchone())

        if route_id == "workspace.update":
            row = self._require_workspace(connection, workspace_id)
            if row["revision"] != value["expected_revision"]:
                raise StaleCasError("workspace revision changed")
            title = value["title"] if value["title"] is not None else row["title"]
            status = value["status"] if value["status"] is not None else row["status"]
            changed = connection.execute(
                "UPDATE workspace SET title=?,status=?,updated_at=?,revision=revision+1 WHERE workspace_id=? AND revision=?",
                (title, status, now, workspace_id, value["expected_revision"]),
            ).rowcount
            if changed != 1:
                raise StaleCasError("workspace revision changed")
            return self._workspace(connection.execute("SELECT * FROM workspace WHERE workspace_id=?", (workspace_id,)).fetchone())

        if route_id == "workspace.delete":
            row = self._require_workspace(connection, workspace_id)
            if row["revision"] != value["expected_revision"]:
                raise StaleCasError("workspace revision changed")
            for table in ("document", "node", "relation", "candidate", "execution_job"):
                if table == "candidate":
                    dependent = connection.execute(
                        "SELECT 1 FROM candidate WHERE json_extract(item_json,'$.target.workspace_id')=? LIMIT 1",
                        (workspace_id,),
                    ).fetchone()
                else:
                    dependent = connection.execute(
                        f"SELECT 1 FROM {table} WHERE workspace_id=? LIMIT 1", (workspace_id,)
                    ).fetchone()
                if dependent is not None:
                    raise StaleCasError("workspace has dependent authority")
            connection.execute("DELETE FROM workspace WHERE workspace_id=?", (workspace_id,))
            return self._delete_result(value, "workspace", workspace_id, row["revision"])

        if route_id == "document.create":
            self._require_workspace(connection, workspace_id)
            if connection.execute("SELECT 1 FROM document WHERE document_id=?", (value["document_id"],)).fetchone():
                raise StaleCasError("document identity already exists")
            connection.execute(
                "INSERT INTO document VALUES(?,?,?,?,?,?,?,?,?)",
                (value["document_id"], workspace_id, value["document_type"], value["title"], None, "{}", now, now, 0),
            )
            return self._document(connection.execute("SELECT * FROM document WHERE document_id=?", (value["document_id"],)).fetchone())

        if route_id == "document.update":
            row = connection.execute("SELECT * FROM document WHERE document_id=?", (value["document_id"],)).fetchone()
            row = self._require_owned(row, workspace_id, "document")
            if row["revision"] != value["expected_revision"]:
                raise StaleCasError("document revision changed")
            changed = connection.execute(
                "UPDATE document SET title=?,updated_at=?,revision=revision+1 WHERE document_id=? AND revision=?",
                (value["title"], now, value["document_id"], value["expected_revision"]),
            ).rowcount
            if changed != 1:
                raise StaleCasError("document revision changed")
            return self._document(connection.execute("SELECT * FROM document WHERE document_id=?", (value["document_id"],)).fetchone())

        if route_id == "node.create":
            self._require_workspace(connection, workspace_id)
            if connection.execute("SELECT 1 FROM node WHERE node_id=?", (value["node_id"],)).fetchone():
                raise StaleCasError("node identity already exists")
            self._validate_node_links(connection, workspace_id, value["node_id"], value["document_id"], value["parent_node_id"])
            connection.execute(
                "INSERT INTO node VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value["node_id"], workspace_id, value["document_id"], value["node_type"], value["title"],
                    value["parent_node_id"], value["position"], "{}", None, now, now, 0,
                ),
            )
            return self._node(connection.execute("SELECT * FROM node WHERE node_id=?", (value["node_id"],)).fetchone())

        if route_id == "node.update":
            row = connection.execute("SELECT * FROM node WHERE node_id=?", (value["node_id"],)).fetchone()
            row = self._require_owned(row, workspace_id, "node")
            if row["revision"] != value["expected_revision"]:
                raise StaleCasError("node revision changed")
            self._validate_node_links(connection, workspace_id, value["node_id"], row["document_id"], value["parent_node_id"])
            changed = connection.execute(
                "UPDATE node SET title=?,parent_node_id=?,position=?,updated_at=?,revision=revision+1 WHERE node_id=? AND revision=?",
                (value["title"], value["parent_node_id"], value["position"], now, value["node_id"], value["expected_revision"]),
            ).rowcount
            if changed != 1:
                raise StaleCasError("node revision changed")
            return self._node(connection.execute("SELECT * FROM node WHERE node_id=?", (value["node_id"],)).fetchone())

        if route_id == "node.delete":
            row = connection.execute("SELECT * FROM node WHERE node_id=?", (value["node_id"],)).fetchone()
            row = self._require_owned(row, workspace_id, "node")
            if row["revision"] != value["expected_revision"]:
                raise StaleCasError("node revision changed")
            if connection.execute("SELECT 1 FROM node WHERE parent_node_id=? LIMIT 1", (value["node_id"],)).fetchone():
                raise StaleCasError("node has dependent children")
            if connection.execute(
                "SELECT 1 FROM relation WHERE source_id=? OR target_id=? LIMIT 1", (value["node_id"], value["node_id"])
            ).fetchone():
                raise StaleCasError("node has dependent relations")
            if connection.execute("SELECT 1 FROM revision WHERE node_id=? LIMIT 1", (value["node_id"],)).fetchone():
                raise StaleCasError("node has dependent revisions")
            connection.execute("DELETE FROM node WHERE node_id=?", (value["node_id"],))
            return self._delete_result(value, "node", value["node_id"], row["revision"])

        if route_id == "relation.create":
            self._require_workspace(connection, workspace_id)
            if connection.execute("SELECT 1 FROM relation WHERE relation_id=?", (value["relation_id"],)).fetchone():
                raise StaleCasError("relation identity already exists")
            for endpoint in (value["source_id"], value["target_id"]):
                owner = connection.execute(
                    "SELECT workspace_id FROM document WHERE document_id=? UNION ALL SELECT workspace_id FROM node WHERE node_id=?",
                    (endpoint, endpoint),
                ).fetchone()
                if owner is None:
                    raise UnknownReferenceError("relation endpoint does not exist")
                if owner["workspace_id"] != workspace_id:
                    raise CrossWorkspaceError("relation endpoint is outside workspace")
            if value["revision_id"] is not None:
                revision = connection.execute("SELECT workspace_id FROM revision WHERE revision_id=?", (value["revision_id"],)).fetchone()
                if revision is None:
                    raise UnknownReferenceError("relation revision does not exist")
                if revision["workspace_id"] != workspace_id:
                    raise CrossWorkspaceError("relation revision is outside workspace")
            connection.execute(
                "INSERT INTO relation VALUES(?,?,?,?,?,?,?,?)",
                (value["relation_id"], workspace_id, value["relation_type"], value["source_id"], value["target_id"], "{}", value["revision_id"], now),
            )
            return self._relation(connection.execute("SELECT * FROM relation WHERE relation_id=?", (value["relation_id"],)).fetchone())

        if route_id == "relation.delete":
            row = connection.execute("SELECT * FROM relation WHERE relation_id=?", (value["relation_id"],)).fetchone()
            row = self._require_owned(row, workspace_id, "relation")
            if row["revision_id"] != value["expected_revision_id"]:
                raise StaleCasError("relation revision changed")
            connection.execute("DELETE FROM relation WHERE relation_id=?", (value["relation_id"],))
            return self._delete_result(value, "relation", value["relation_id"], None)

        if route_id in {"document.revision.create", "node.revision.create"}:
            target_kind = "document" if route_id.startswith("document") else "node"
            target_key = f"{target_kind}_id"
            target_id = value[target_key]
            target = connection.execute(f"SELECT * FROM {target_kind} WHERE {target_key}=?", (target_id,)).fetchone()
            target = self._require_owned(target, workspace_id, target_kind)
            if target["current_revision_id"] != value["base_revision_id"]:
                raise StaleCasError("revision base changed")
            if connection.execute("SELECT 1 FROM revision WHERE revision_id=?", (value["revision_id"],)).fetchone():
                raise StaleCasError("revision identity already exists")
            self._verify_source_candidate(connection, value, target_kind, target_id)
            number = connection.execute(
                f"SELECT count(*) FROM revision WHERE {target_key}=?", (target_id,)
            ).fetchone()[0] + 1
            content_hash = hashlib.sha256(value["content"].encode("utf-8")).hexdigest()
            connection.execute(
                "INSERT INTO revision VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value["revision_id"], workspace_id,
                    target_id if target_kind == "document" else None,
                    target_id if target_kind == "node" else None,
                    value["base_revision_id"], value["content"], content_hash, value["created_by"],
                    value["source_candidate_id"], now, number, value["payload_schema"],
                ),
            )
            changed = connection.execute(
                f"UPDATE {target_kind} SET current_revision_id=?,updated_at=?,revision=revision+1 "
                f"WHERE {target_key}=? AND current_revision_id IS ?",
                (value["revision_id"], now, target_id, value["base_revision_id"]),
            ).rowcount
            if changed != 1:
                raise StaleCasError("revision base changed")
            return self._revision(connection.execute("SELECT * FROM revision WHERE revision_id=?", (value["revision_id"],)).fetchone())

        raise ValueError(f"unsupported authority command route: {route_id}")

    @staticmethod
    def _delete_result(value: dict[str, Any], entity_kind: str, entity_id: str, previous_revision: int | None) -> dict[str, Any]:
        return {
            "schema": "core-delete-result/v1",
            "operation_key": value["operation_key"],
            "workspace_id": value["workspace_id"],
            "entity_kind": entity_kind,
            "entity_id": entity_id,
            "previous_revision": previous_revision,
            "deleted": True,
            "idempotent": False,
        }

    def _validate_node_links(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        node_id: str,
        document_id: str | None,
        parent_node_id: str | None,
    ) -> None:
        if document_id is not None:
            document = connection.execute("SELECT workspace_id FROM document WHERE document_id=?", (document_id,)).fetchone()
            if document is None:
                raise UnknownReferenceError("node document does not exist")
            if document["workspace_id"] != workspace_id:
                raise CrossWorkspaceError("node document is outside workspace")
        visited = {node_id}
        current = parent_node_id
        while current is not None:
            if current in visited:
                raise StaleCasError("node parent graph contains a cycle")
            visited.add(current)
            parent = connection.execute(
                "SELECT workspace_id,parent_node_id FROM node WHERE node_id=?", (current,)
            ).fetchone()
            if parent is None:
                raise UnknownReferenceError("node parent does not exist")
            if parent["workspace_id"] != workspace_id:
                raise CrossWorkspaceError("node parent is outside workspace")
            current = parent["parent_node_id"]

    def _verify_source_candidate(
        self,
        connection: sqlite3.Connection,
        command: dict[str, Any],
        target_kind: str,
        target_id: str,
    ) -> None:
        candidate_id = command["source_candidate_id"]
        if candidate_id is None:
            return
        if self.publication_service is None:
            raise IncompletePublicationError("source Candidate closure verifier is not configured")

        # Reuse the Publication service's complete parent/source/Asset/base
        # closure verifier on this exact transaction snapshot.  The remaining
        # checks bind that verified authority to this Revision command only.
        preflight = self.publication_service.verify_candidate_closure(
            connection,
            candidate_id,
            expected_workspace_id=command["workspace_id"],
        )
        item = preflight.item
        target = item["target"]
        base = item["base"]
        if (
            preflight.root_status != "staged"
            or item["status"] != "complete"
            or item["item_kind"] != target_kind
            or target != {
                "workspace_id": command["workspace_id"],
                "entity_kind": target_kind,
                "entity_id": target_id,
            }
            or base["revision_id"] != command["base_revision_id"]
            or item["mutation"]["payload_schema"] != command["payload_schema"]
            or item["mutation"]["mode"] not in {"replace", "append_text"}
        ):
            raise StaleCasError("source Candidate is not bound to the Revision command")
        if connection.execute("SELECT 1 FROM revision WHERE source_candidate_id=?", (candidate_id,)).fetchone():
            raise StaleCasError("source Candidate already created a Revision")
        if command["base_revision_id"] is None:
            base_content = ""
        else:
            base_row = connection.execute(
                "SELECT workspace_id,document_id,node_id,content,content_hash FROM revision WHERE revision_id=?",
                (command["base_revision_id"],),
            ).fetchone()
            if (
                base_row is None
                or base_row["workspace_id"] != command["workspace_id"]
                or base_row[f"{target_kind}_id"] != target_id
                or base_row["content_hash"] != base["content_hash"]
                or not isinstance(base_row["content"], str)
                or hashlib.sha256(base_row["content"].encode("utf-8")).hexdigest()
                != base_row["content_hash"]
            ):
                raise StaleCasError("source Candidate base drifted")
            base_content = base_row["content"]
        expected_content = (
            preflight.payload_text
            if item["mutation"]["mode"] == "replace"
            else base_content + preflight.payload_text
        )
        if command["content"] != expected_content:
            raise StaleCasError("source Candidate mutation drifted")


__all__ = ["CoreAuthorityApplication"]
