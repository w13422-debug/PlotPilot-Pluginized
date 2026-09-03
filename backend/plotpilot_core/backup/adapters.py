from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import zipfile
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path

from backend import plotpilot_plugin_sdk as _sdk_package
from backend.plotpilot_plugin_sdk.canonical import (
    canonical_bytes,
    hash_jcs,
    parse_json_bytes,
)
from backend.plotpilot_plugin_sdk.context_identity import (
    derive_operation_context_identity,
)
from backend.plotpilot_plugin_sdk.framing import decode_frame
from backend.plotpilot_plugin_sdk.verifier import (
    assert_valid,
    hash_without_field,
    verify_checkpoint,
    verify_snapshot,
)

from ..broker.service import (
    BrokerChildRecord,
    ChildCreationResult,
    verify_child_snapshot_binding,
)
from ..candidates.service import CandidateService
from ..repositories.authority import CoreAuthorityRepository
from ..repositories.execution import _broker_context_identity, _dependency_ids

# The repository supports both ``backend.*`` source-tree imports and the
# installed top-level SDK package.  Core's accepted package authority uses the
# installed spelling internally; provide that spelling when running directly
# from the source tree without changing either authority module.
sys.modules.setdefault("plotpilot_plugin_sdk", _sdk_package)

from ..assets import AssetStore
from ..plugins.package import VerifiedPackage, verify_package
from ..plugins.store import PackageStore
from .models import BackupBarrier, BackupMode, CoreSnapshotCapture, PluginBackupFile


def _json_canonical(value: Mapping[str, object]) -> str:
    """Return the exact JSON text accepted for projected Core rows."""

    return canonical_bytes(dict(value)).decode("utf-8")


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\n".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:48]}"


class CoreSnapshotAdapterError(RuntimeError):
    """The frozen Core authority cannot be represented as core-snapshot/v1."""


class WorkspaceProjectionError(CoreSnapshotAdapterError):
    """The frozen Core database cannot be safely scoped to one workspace."""


_CORE_TABLE_ORDER = (
    "schema_migration",
    "workspace",
    "execution_orchestration_owner",
    "document",
    "node",
    "revision",
    "relation",
    "candidate",
    "candidate_review",
    "candidate_batch_operation",
    "chapter_candidate_authority",
    "chapter_writer_fence",
    "publication_receipt",
    "execution_job",
    "execution_step",
    "execution_attempt",
    "execution_receipt",
    "execution_job_event",
    "execution_core_event",
    "execution_checkpoint",
    "execution_checkpoint_operation",
    "execution_control_operation",
    "execution_outcome",
    "execution_candidate_binding",
    "execution_publication_binding",
    "execution_child_creation",
    "p3_broker_child_record",
    "p3_broker_operation",
    "p3_host_operation_ledger",
)


def _sqlite_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro&immutable=1"


def _rows(
    connection: sqlite3.Connection,
    table: str,
    *,
    where: str = "",
    parameters: tuple[object, ...] = (),
) -> list[dict[str, object]]:
    columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
    if not columns:
        raise CoreSnapshotAdapterError(f"required Core authority table is missing: {table}")
    order = ",".join(f'CAST("{column}" AS BLOB)' for column in columns)
    query = f'SELECT * FROM "{table}"'
    if where:
        query += f" WHERE {where}"
    query += f" ORDER BY {order}"
    return [dict(zip(columns, row, strict=True)) for row in connection.execute(query, parameters)]


def _candidate_workspace(row: dict[str, object]) -> tuple[str, dict[str, object]]:
    raw = row.get("item_json")
    if not isinstance(raw, str):
        raise WorkspaceProjectionError("candidate item_json must be text")
    try:
        item = parse_json_bytes(raw.encode("utf-8"))
    except Exception as exc:
        raise WorkspaceProjectionError("candidate item_json is invalid") from exc
    if not isinstance(item, dict):
        raise WorkspaceProjectionError("candidate item_json must be an object")
    try:
        canonical, item_hash = CandidateService._canonical(item)
    except Exception as exc:
        raise WorkspaceProjectionError("candidate item_json violates Core authority") from exc
    if (
        item.get("schema") != "candidate-item/v1"
        or item.get("item_id") != row.get("item_id")
        or canonical != raw
        or item_hash != row.get("item_hash")
    ):
        raise WorkspaceProjectionError("candidate row identity does not bind item_json")
    target = item.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("workspace_id"), str):
        raise WorkspaceProjectionError("candidate target lacks a workspace identity")
    return str(target["workspace_id"]), item


def _migration_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    columns = [
        str(row[1]) for row in connection.execute('PRAGMA table_info("schema_migration")')
    ]
    if not columns:
        raise CoreSnapshotAdapterError("required Core migration ledger is missing")
    return [
        dict(zip(columns, row, strict=True))
        for row in connection.execute("SELECT * FROM schema_migration ORDER BY rowid")
    ]


class SqliteWorkspaceDatabaseProjector:
    """Create a one-workspace Core SQLite image from an immutable frozen image.

    Only the accepted Core authority tables have classification rules.  Any
    additional table, view, or trigger fails closed instead of leaking an
    unclassified global row into a workspace backup.
    """

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> tuple[dict[str, str], list[str]]:
        query = (
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
        canonical = CoreAuthorityRepository(":memory:")
        try:
            with canonical.read_connection() as accepted:
                expected_objects = [tuple(row) for row in accepted.execute(query)]
                expected_migrations = {
                    str(migration_id): str(sha256)
                    for migration_id, sha256 in accepted.execute(
                        "SELECT migration_id,sha256 FROM schema_migration"
                    )
                }
            actual_objects = [tuple(row) for row in connection.execute(query)]
            if actual_objects != expected_objects:
                raise WorkspaceProjectionError(
                    "Core authority schema differs from CoreAuthorityRepository migrations"
                )
            actual_migrations = {
                str(migration_id): str(sha256)
                for migration_id, sha256 in connection.execute(
                    "SELECT migration_id,sha256 FROM schema_migration"
                )
            }
            if actual_migrations != expected_migrations:
                raise WorkspaceProjectionError(
                    "Core migration ledger differs from CoreAuthorityRepository authority"
                )
            table_sql = {
                str(name): str(sql)
                for object_type, name, _table_name, sql in expected_objects
                if object_type == "table"
            }
            if set(table_sql) != set(_CORE_TABLE_ORDER):
                raise WorkspaceProjectionError(
                    "integrated Core table classification is incomplete"
                )
            index_sql = [
                str(sql)
                for object_type, _name, _table_name, sql in expected_objects
                if object_type == "index" and sql is not None
            ]
            return table_sql, index_sql
        finally:
            canonical.close()

    @staticmethod
    def _selected_rows(
        connection: sqlite3.Connection, workspace_id: str
    ) -> dict[str, list[dict[str, object]]]:
        workspace = _rows(
            connection,
            "workspace",
            where='"workspace_id"=?',
            parameters=(workspace_id,),
        )
        if len(workspace) != 1:
            raise WorkspaceProjectionError("selected workspace is absent from frozen Core authority")
        selected: dict[str, list[dict[str, object]]] = {
            "schema_migration": _migration_rows(connection),
            "workspace": workspace,
        }
        selected["execution_orchestration_owner"] = [
            row
            for row in _rows(connection, "execution_orchestration_owner")
            if row.get("workspace_id") == workspace_id
        ]
        for table in ("document", "node", "revision", "relation"):
            selected[table] = _rows(
                connection,
                table,
                where='"workspace_id"=?',
                parameters=(workspace_id,),
            )
        candidates: list[dict[str, object]] = []
        candidate_items: dict[str, dict[str, object]] = {}
        for row in _rows(connection, "candidate"):
            owner, item = _candidate_workspace(row)
            if owner == workspace_id:
                candidate_id = row.get("candidate_id")
                if not isinstance(candidate_id, str):
                    raise WorkspaceProjectionError("candidate ID is invalid")
                candidates.append(row)
                candidate_items[candidate_id] = item
        selected["candidate"] = candidates
        selected["candidate_review"] = [
            row
            for row in _rows(connection, "candidate_review")
            if row.get("candidate_id") in candidate_items
        ]
        selected["candidate_batch_operation"] = _rows(
            connection,
            "candidate_batch_operation",
            where='"workspace_id"=?',
            parameters=(workspace_id,),
        )
        selected["chapter_candidate_authority"] = [
            row
            for row in _rows(connection, "chapter_candidate_authority")
            if row.get("candidate_id") in candidate_items
        ]
        selected["chapter_writer_fence"] = _rows(
            connection,
            "chapter_writer_fence",
            where='"workspace_id"=?',
            parameters=(workspace_id,),
        )
        selected["publication_receipt"] = _rows(
            connection,
            "publication_receipt",
            where=(
                '"revision_id" IN (SELECT "revision_id" FROM "revision" '
                'WHERE "workspace_id"=?)'
            ),
            parameters=(workspace_id,),
        )
        all_jobs = _rows(connection, "execution_job")
        selected["execution_job"] = [
            row for row in all_jobs if row.get("workspace_id") == workspace_id
        ]
        job_ids = {str(row["job_id"]) for row in selected["execution_job"]}
        selected["execution_step"] = [
            row for row in _rows(connection, "execution_step") if row.get("job_id") in job_ids
        ]
        selected["execution_attempt"] = [
            row for row in _rows(connection, "execution_attempt") if row.get("job_id") in job_ids
        ]
        for table in (
            "execution_receipt",
            "execution_job_event",
            "execution_checkpoint",
            "execution_checkpoint_operation",
            "execution_control_operation",
            "execution_outcome",
            "execution_candidate_binding",
            "execution_publication_binding",
        ):
            selected[table] = [
                row for row in _rows(connection, table) if row.get("job_id") in job_ids
            ]
        selected["execution_core_event"] = [
            row
            for row in _rows(connection, "execution_core_event")
            if row.get("workspace_id") == workspace_id
        ]
        for table in ("execution_child_creation", "p3_broker_child_record"):
            selected[table] = [
                row for row in _rows(connection, table) if row.get("child_job_id") in job_ids
            ]

        job_owner = {str(row["job_id"]): str(row["workspace_id"]) for row in all_jobs}
        broker_context_owner: dict[str, str] = {}
        for attempt in _rows(connection, "execution_attempt"):
            job_id = str(attempt["job_id"])
            owner = job_owner.get(job_id)
            if owner is None:
                raise WorkspaceProjectionError("execution attempt has no Workspace-owned job")
            context = _broker_context_identity(
                job_id, str(attempt["step_id"]), str(attempt["attempt_id"])
            )
            previous = broker_context_owner.setdefault(context, owner)
            if previous != owner:
                raise WorkspaceProjectionError("broker context maps to multiple Workspaces")
        selected["p3_broker_operation"] = []
        for row in _rows(connection, "p3_broker_operation"):
            owner = broker_context_owner.get(str(row.get("context_identity")))
            if owner is None:
                raise WorkspaceProjectionError("broker operation has no Workspace-owned attempt")
            if owner == workspace_id:
                selected["p3_broker_operation"].append(row)

        outcome_key_owners: dict[tuple[str, str], set[str]] = defaultdict(set)
        for outcome in _rows(connection, "execution_outcome"):
            owner = job_owner.get(str(outcome.get("job_id")))
            if owner is None:
                raise WorkspaceProjectionError("execution outcome has no Workspace-owned job")
            outcome_key_owners[
                (str(outcome.get("context_identity")), str(outcome.get("operation_key")))
            ].add(owner)
        selected["p3_host_operation_ledger"] = []
        for row in _rows(connection, "p3_host_operation_ledger"):
            owners = outcome_key_owners.get(
                (str(row.get("context_identity")), str(row.get("operation_key"))), set()
            )
            if len(owners) != 1:
                raise WorkspaceProjectionError(
                    "host operation ledger is orphaned or ambiguously scoped"
                )
            if workspace_id in owners:
                selected["p3_host_operation_ledger"].append(row)
        SqliteWorkspaceDatabaseProjector._validate_boundaries(
            connection=connection,
            workspace_id=workspace_id,
            selected=selected,
            candidate_items=candidate_items,
        )
        return selected

    @staticmethod
    def _validate_boundaries(
        *,
        connection: sqlite3.Connection,
        workspace_id: str,
        selected: dict[str, list[dict[str, object]]],
        candidate_items: dict[str, dict[str, object]],
    ) -> None:
        document_ids = {str(row["document_id"]) for row in selected["document"]}
        node_ids = {str(row["node_id"]) for row in selected["node"]}
        revision_ids = {str(row["revision_id"]) for row in selected["revision"]}
        candidate_ids = set(candidate_items)
        endpoint_ids = document_ids | node_ids
        workspace_plan = selected["workspace"][0].get("current_plan_revision_id")
        if workspace_plan is not None and workspace_plan not in revision_ids:
            raise WorkspaceProjectionError("workspace plan revision crosses the selected boundary")
        for document in selected["document"]:
            current_revision = document.get("current_revision_id")
            if current_revision is not None:
                revision = next(
                    (
                        item
                        for item in selected["revision"]
                        if item.get("revision_id") == current_revision
                    ),
                    None,
                )
                if revision is None or revision.get("document_id") != document.get("document_id"):
                    raise WorkspaceProjectionError(
                        "document current revision crosses the selected boundary"
                    )
        for node in selected["node"]:
            current_revision = node.get("current_revision_id")
            if current_revision is not None:
                revision = next(
                    (
                        item
                        for item in selected["revision"]
                        if item.get("revision_id") == current_revision
                    ),
                    None,
                )
                if revision is None or revision.get("node_id") != node.get("node_id"):
                    raise WorkspaceProjectionError(
                        "node current revision crosses the selected boundary"
                    )
        for relation in selected["relation"]:
            if relation.get("source_id") not in endpoint_ids or relation.get("target_id") not in endpoint_ids:
                raise WorkspaceProjectionError("relation endpoint crosses the selected boundary")
            if relation.get("revision_id") is not None and relation.get("revision_id") not in revision_ids:
                raise WorkspaceProjectionError("relation revision crosses the selected boundary")
        for revision in selected["revision"]:
            source_candidate = revision.get("source_candidate_id")
            if source_candidate is not None and source_candidate not in candidate_ids:
                raise WorkspaceProjectionError("revision source candidate crosses the selected boundary")
        for receipt in selected["publication_receipt"]:
            if receipt.get("candidate_id") not in candidate_ids:
                raise WorkspaceProjectionError("publication candidate crosses the selected boundary")
        for candidate_id, item in candidate_items.items():
            parents = item.get("parent_candidate_ids")
            write_set = item.get("write_set")
            source_refs = item.get("source_refs")
            if not isinstance(parents, list) or any(parent not in candidate_ids for parent in parents):
                raise WorkspaceProjectionError(
                    f"candidate parent crosses the selected boundary: {candidate_id}"
                )
            if not isinstance(write_set, list) or any(
                not isinstance(entry, dict) or entry.get("workspace_id") != workspace_id
                for entry in write_set
            ):
                raise WorkspaceProjectionError(
                    f"candidate write set crosses the selected boundary: {candidate_id}"
                )
            if not isinstance(source_refs, list) or any(
                not isinstance(entry, dict)
                or entry.get("workspace_id") not in {None, workspace_id}
                for entry in source_refs
            ):
                raise WorkspaceProjectionError(
                    f"candidate source reference crosses the selected boundary: {candidate_id}"
                )
        SqliteWorkspaceDatabaseProjector._validate_integrated_boundaries(connection)

    @staticmethod
    def _validate_integrated_boundaries(connection: sqlite3.Connection) -> None:
        violation = connection.execute("PRAGMA foreign_key_check").fetchone()
        if violation is not None:
            raise WorkspaceProjectionError(f"source Core authority violates a foreign key: {violation}")

        def mapping(table: str, identity: str, owner: str) -> dict[str, str]:
            result: dict[str, str] = {}
            for row in _rows(connection, table):
                key = row.get(identity)
                scope = row.get(owner)
                if not isinstance(key, str) or not isinstance(scope, str) or key in result:
                    raise WorkspaceProjectionError(f"{table} identity is ambiguous")
                result[key] = scope
            return result

        def require(values: dict[str, str], value: object, label: str) -> str:
            if not isinstance(value, str) or value not in values:
                raise WorkspaceProjectionError(f"{label} is orphaned")
            return values[value]

        workspace_ids = {
            str(row["workspace_id"]) for row in _rows(connection, "workspace")
        }
        workspace_owner = {workspace_id: workspace_id for workspace_id in workspace_ids}
        document_owner = mapping("document", "document_id", "workspace_id")
        node_owner = mapping("node", "node_id", "workspace_id")
        revision_owner = mapping("revision", "revision_id", "workspace_id")
        document_rows = {
            str(row["document_id"]): row for row in _rows(connection, "document")
        }
        node_rows = {str(row["node_id"]): row for row in _rows(connection, "node")}
        revision_rows = {
            str(row["revision_id"]): row for row in _rows(connection, "revision")
        }
        for owner in (*document_owner.values(), *node_owner.values(), *revision_owner.values()):
            require(workspace_owner, owner, "Core authority workspace")

        for row in _rows(connection, "workspace"):
            current = row.get("current_plan_revision_id")
            if current is not None and require(
                revision_owner, current, "workspace plan revision"
            ) != row.get("workspace_id"):
                raise WorkspaceProjectionError("workspace plan revision crosses Workspace")
        for document_id, row in document_rows.items():
            current = row.get("current_revision_id")
            if current is not None:
                revision = revision_rows.get(str(current))
                if (
                    revision is None
                    or revision.get("document_id") != document_id
                    or require(revision_owner, current, "document current revision")
                    != document_owner[document_id]
                ):
                    raise WorkspaceProjectionError(
                        "document current revision crosses Workspace"
                    )
        for node_id, row in node_rows.items():
            owner = node_owner[node_id]
            document_id = row.get("document_id")
            parent_id = row.get("parent_node_id")
            current = row.get("current_revision_id")
            if document_id is not None and require(
                document_owner, document_id, "node document"
            ) != owner:
                raise WorkspaceProjectionError("node document crosses Workspace")
            if parent_id is not None and require(
                node_owner, parent_id, "node parent"
            ) != owner:
                raise WorkspaceProjectionError("node parent crosses Workspace")
            if current is not None:
                revision = revision_rows.get(str(current))
                if (
                    revision is None
                    or revision.get("node_id") != node_id
                    or require(revision_owner, current, "node current revision") != owner
                ):
                    raise WorkspaceProjectionError("node current revision crosses Workspace")
        for revision_id, row in revision_rows.items():
            owner = revision_owner[revision_id]
            document_id = row.get("document_id")
            node_id = row.get("node_id")
            if (document_id is None) == (node_id is None):
                raise WorkspaceProjectionError("revision target authority is ambiguous")
            if document_id is not None and require(
                document_owner, document_id, "revision document"
            ) != owner:
                raise WorkspaceProjectionError("revision document crosses Workspace")
            if node_id is not None and require(node_owner, node_id, "revision node") != owner:
                raise WorkspaceProjectionError("revision node crosses Workspace")
            parent = row.get("parent_revision_id")
            if parent is not None and require(
                revision_owner, parent, "revision parent"
            ) != owner:
                raise WorkspaceProjectionError("revision parent crosses Workspace")
        endpoint_owner = {**document_owner, **node_owner}
        for row in _rows(connection, "relation"):
            owner = require(workspace_owner, row.get("workspace_id"), "relation workspace")
            if (
                require(endpoint_owner, row.get("source_id"), "relation source") != owner
                or require(endpoint_owner, row.get("target_id"), "relation target") != owner
            ):
                raise WorkspaceProjectionError("relation endpoint crosses Workspace")
            revision_id = row.get("revision_id")
            if revision_id is not None and require(
                revision_owner, revision_id, "relation revision"
            ) != owner:
                raise WorkspaceProjectionError("relation revision crosses Workspace")

        candidate_owner: dict[str, str] = {}
        candidate_rows: dict[str, dict[str, object]] = {}
        candidate_items: dict[str, dict[str, object]] = {}
        for row in _rows(connection, "candidate"):
            owner, item = _candidate_workspace(row)
            require(workspace_owner, owner, "candidate workspace")
            candidate_id = row.get("candidate_id")
            if not isinstance(candidate_id, str) or candidate_id in candidate_owner:
                raise WorkspaceProjectionError("candidate identity is ambiguous")
            candidate_owner[candidate_id] = owner
            candidate_rows[candidate_id] = row
            candidate_items[candidate_id] = item
        for candidate_id, item in candidate_items.items():
            owner = candidate_owner[candidate_id]
            target = item.get("target")
            base = item.get("base")
            if not isinstance(target, dict) or not isinstance(base, dict):
                raise WorkspaceProjectionError("candidate target/base authority is invalid")
            base_id = base.get("revision_id")
            base_row = revision_rows.get(str(base_id))
            if (
                target.get("workspace_id") != owner
                or target.get("entity_kind") != "document"
                or require(document_owner, target.get("entity_id"), "candidate target") != owner
                or require(revision_owner, base_id, "candidate base") != owner
                or base_row is None
                or base_row.get("content_hash") != base.get("content_hash")
            ):
                raise WorkspaceProjectionError("candidate target/base crosses Workspace")
            parents = item.get("parent_candidate_ids")
            if not isinstance(parents, list) or any(
                require(candidate_owner, parent, "candidate parent") != owner
                for parent in parents
            ):
                raise WorkspaceProjectionError("candidate parent crosses Workspace")
            write_set = item.get("write_set")
            if not isinstance(write_set, list) or not write_set or any(
                not isinstance(entry, dict)
                or entry.get("workspace_id") != owner
                or require(
                    document_owner, entry.get("entity_id"), "candidate write-set target"
                )
                != owner
                or require(
                    revision_owner,
                    entry.get("revision_id"),
                    "candidate write-set revision",
                )
                != owner
                for entry in write_set
            ):
                raise WorkspaceProjectionError("candidate write set crosses Workspace")
            source_refs = item.get("source_refs")
            if not isinstance(source_refs, list) or any(
                not isinstance(entry, dict)
                or (
                    entry.get("workspace_id") is not None
                    and require(
                        workspace_owner,
                        entry.get("workspace_id"),
                        "candidate source Workspace",
                    )
                    != entry.get("workspace_id")
                )
                for entry in source_refs
            ):
                raise WorkspaceProjectionError("candidate source reference is invalid")
            for source in source_refs:
                source_workspace = source.get("workspace_id")
                if source_workspace is None:
                    continue
                source_type = source.get("source_type")
                if source_type == "document":
                    source_owner = require(
                        document_owner, source.get("source_id"), "candidate source document"
                    )
                elif source_type == "revision":
                    source_owner = require(
                        revision_owner, source.get("source_id"), "candidate source revision"
                    )
                    source_revision = revision_rows[str(source["source_id"])]
                    if source.get("revision_or_hash") not in {
                        source_revision.get("revision_id"),
                        source_revision.get("content_hash"),
                    }:
                        raise WorkspaceProjectionError(
                            "candidate source revision identity is invalid"
                        )
                else:
                    raise WorkspaceProjectionError(
                        "candidate source reference type is unclassified"
                    )
                if source_owner != source_workspace:
                    raise WorkspaceProjectionError(
                        "candidate source reference crosses Workspace"
                    )

        for row in _rows(connection, "candidate_review"):
            require(candidate_owner, row.get("candidate_id"), "candidate review Candidate")

        candidates_by_operation: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in candidate_rows.values():
            operation_key = row.get("operation_key")
            if not isinstance(operation_key, str) or not operation_key:
                raise WorkspaceProjectionError("candidate operation identity is invalid")
            candidates_by_operation[operation_key].append(row)
        for row in _rows(connection, "candidate_batch_operation"):
            owner = require(
                workspace_owner,
                row.get("workspace_id"),
                "candidate batch Workspace",
            )
            operation_key = row.get("operation_key")
            raw_item_ids = row.get("item_ids_json")
            if not isinstance(operation_key, str) or not operation_key:
                raise WorkspaceProjectionError("candidate batch operation identity is invalid")
            if not isinstance(raw_item_ids, str):
                raise WorkspaceProjectionError("candidate batch item authority is invalid")
            try:
                item_ids = parse_json_bytes(raw_item_ids.encode("utf-8"))
            except Exception as exc:
                raise WorkspaceProjectionError(
                    "candidate batch item authority is invalid"
                ) from exc
            if (
                not isinstance(item_ids, list)
                or not item_ids
                or any(not isinstance(item_id, str) or not item_id for item_id in item_ids)
                or len(set(item_ids)) != len(item_ids)
            ):
                raise WorkspaceProjectionError("candidate batch item authority is invalid")
            item_id_set = set(item_ids)
            for candidate in candidates_by_operation.get(operation_key, []):
                candidate_id = str(candidate["candidate_id"])
                if (
                    candidate_owner[candidate_id] != owner
                    or candidate.get("item_id") not in item_id_set
                ):
                    raise WorkspaceProjectionError(
                        "candidate batch operation crosses Workspace or item closure"
                    )

        publication_owner: dict[str, str] = {}
        publication_rows: dict[str, dict[str, object]] = {}
        for row in _rows(connection, "publication_receipt"):
            candidate_id = row.get("candidate_id")
            revision_id = row.get("revision_id")
            owner = require(candidate_owner, candidate_id, "publication candidate")
            if require(revision_owner, revision_id, "publication revision") != owner:
                raise WorkspaceProjectionError("publication crosses Workspace")
            revision = revision_rows[str(revision_id)]
            if revision.get("source_candidate_id") != candidate_id:
                raise WorkspaceProjectionError("publication revision is not candidate-bound")
            publication_id = row.get("publication_id")
            if not isinstance(publication_id, str) or publication_id in publication_owner:
                raise WorkspaceProjectionError("publication identity is ambiguous")
            publication_owner[publication_id] = owner
            publication_rows[publication_id] = row

        job_owner = mapping("execution_job", "job_id", "workspace_id")
        job_rows = {str(row["job_id"]): row for row in _rows(connection, "execution_job")}
        job_snapshots: dict[str, dict[str, object]] = {}
        for job_id, row in job_rows.items():
            owner = require(workspace_owner, job_owner[job_id], "execution job workspace")
            snapshot = SqliteWorkspaceDatabaseProjector._json_mapping(
                row.get("run_snapshot_json"), "RunSnapshot authority"
            )
            job_snapshots[job_id] = snapshot
            if snapshot.get("schema") == "run-snapshot/v1":
                try:
                    verify_snapshot(snapshot)
                except Exception as exc:
                    raise WorkspaceProjectionError("RunSnapshot authority is invalid") from exc
                if (
                    snapshot.get("snapshot_hash") != row.get("run_snapshot_hash")
                    or snapshot.get("run_intent_id") != row.get("run_intent_id")
                    or snapshot.get("workspace_id") != owner
                ):
                    raise WorkspaceProjectionError("RunSnapshot identity crosses Workspace")
            elif snapshot.get("schema") == "broker-child-snapshot-binding/v1":
                if (
                    snapshot.get("child_job_id") != job_id
                    or hashlib.sha256(canonical_bytes(snapshot)).hexdigest()
                    != row.get("run_snapshot_hash")
                ):
                    raise WorkspaceProjectionError(
                        "Broker child Snapshot identity is invalid"
                    )
            else:
                raise WorkspaceProjectionError("execution Snapshot profile is unclassified")
        step_job = {
            str(row["step_id"]): str(row["job_id"])
            for row in _rows(connection, "execution_step")
        }
        step_owner = {
            step_id: require(job_owner, job_id, "execution step job")
            for step_id, job_id in step_job.items()
        }
        step_rows = {str(row["step_id"]): row for row in _rows(connection, "execution_step")}
        attempt_rows = {
            str(row["attempt_id"]): row for row in _rows(connection, "execution_attempt")
        }
        attempt_owner: dict[str, str] = {}
        for attempt_id, row in attempt_rows.items():
            owner = require(job_owner, row.get("job_id"), "execution attempt job")
            if (
                require(step_owner, row.get("step_id"), "execution attempt step") != owner
                or step_job[str(row["step_id"])] != row.get("job_id")
            ):
                raise WorkspaceProjectionError("execution attempt crosses Workspace")
            attempt_owner[attempt_id] = owner

        chapter_authority_rows: dict[str, dict[str, object]] = {}
        for row in _rows(connection, "chapter_candidate_authority"):
            candidate_id = row.get("candidate_id")
            owner = require(
                candidate_owner,
                candidate_id,
                "chapter Candidate authority Candidate",
            )
            if not isinstance(candidate_id, str) or candidate_id in chapter_authority_rows:
                raise WorkspaceProjectionError("chapter Candidate authority is ambiguous")
            source_job_id = row.get("source_job_id")
            source_attempt_id = row.get("source_attempt_id")
            writer_epoch = row.get("writer_epoch")
            source_values = (source_job_id, source_attempt_id, writer_epoch)
            if any(value is None for value in source_values) and not all(
                value is None for value in source_values
            ):
                raise WorkspaceProjectionError(
                    "chapter Candidate source authority is incomplete"
                )
            if source_job_id is not None:
                source_attempt = attempt_rows.get(str(source_attempt_id))
                if (
                    require(job_owner, source_job_id, "chapter Candidate source job")
                    != owner
                    or require(
                        attempt_owner,
                        source_attempt_id,
                        "chapter Candidate source attempt",
                    )
                    != owner
                    or source_attempt is None
                    or source_attempt.get("job_id") != source_job_id
                    or source_attempt.get("lease_epoch") != writer_epoch
                ):
                    raise WorkspaceProjectionError(
                        "chapter Candidate source crosses Workspace or Attempt"
                    )
            if (
                not isinstance(row.get("provenance_receipt_id"), str)
                or not row.get("provenance_receipt_id")
                or not isinstance(row.get("operation_key"), str)
                or not row.get("operation_key")
            ):
                raise WorkspaceProjectionError(
                    "chapter Candidate provenance authority is invalid"
                )
            chapter_authority_rows[candidate_id] = row

        for row in _rows(connection, "chapter_writer_fence"):
            owner = require(
                workspace_owner,
                row.get("workspace_id"),
                "chapter writer fence Workspace",
            )
            candidate_id = row.get("candidate_id")
            candidate_owner_id = require(
                candidate_owner,
                candidate_id,
                "chapter writer fence Candidate",
            )
            candidate_item = candidate_items[str(candidate_id)]
            target = candidate_item.get("target")
            job_id = row.get("job_id")
            step_id = row.get("step_id")
            attempt_id = row.get("attempt_id")
            attempt = attempt_rows.get(str(attempt_id))
            authority = chapter_authority_rows.get(str(candidate_id))
            if (
                candidate_owner_id != owner
                or not isinstance(target, dict)
                or row.get("entity_kind") != "document"
                or target.get("workspace_id") != owner
                or target.get("entity_kind") != row.get("entity_kind")
                or target.get("entity_id") != row.get("entity_id")
                or require(
                    document_owner,
                    row.get("entity_id"),
                    "chapter writer fence target",
                )
                != owner
                or require(job_owner, job_id, "chapter writer fence job") != owner
                or require(step_owner, step_id, "chapter writer fence step") != owner
                or require(
                    attempt_owner,
                    attempt_id,
                    "chapter writer fence attempt",
                )
                != owner
                or step_job[str(step_id)] != job_id
                or attempt is None
                or attempt.get("job_id") != job_id
                or attempt.get("step_id") != step_id
                or attempt.get("lease_epoch") != row.get("writer_epoch")
                or authority is None
                or authority.get("source_job_id") != job_id
                or authority.get("source_attempt_id") != attempt_id
                or authority.get("writer_epoch") != row.get("writer_epoch")
                or authority.get("operation_key") != row.get("operation_key")
            ):
                raise WorkspaceProjectionError(
                    "chapter writer fence crosses Workspace or authority closure"
                )

        owner_rows: dict[str, dict[str, object]] = {}
        for row in _rows(connection, "execution_orchestration_owner"):
            workspace_id = row.get("workspace_id")
            require(workspace_owner, workspace_id, "orchestration owner Workspace")
            if not isinstance(workspace_id, str) or workspace_id in owner_rows:
                raise WorkspaceProjectionError("orchestration owner identity is ambiguous")
            if (
                not isinstance(row.get("owner_instance_id"), str)
                or not isinstance(row.get("owner_token"), str)
                or not isinstance(row.get("lease_epoch"), int)
                or isinstance(row.get("lease_epoch"), bool)
                or int(row["lease_epoch"]) < 1
                or not isinstance(row.get("revision"), int)
                or isinstance(row.get("revision"), bool)
                or int(row["revision"]) < 1
                or not isinstance(row.get("lease_expires_at"), str)
            ):
                raise WorkspaceProjectionError("orchestration owner authority is invalid")
            owner_rows[workspace_id] = row

        checkpoint_rows: dict[str, dict[str, object]] = {}
        checkpoint_operation_keys: dict[tuple[str, str], dict[str, object]] = {}
        checkpoint_chain_totals: dict[tuple[str, str], object] = {}
        checkpoint_chain_sequences: dict[tuple[str, str], set[int]] = defaultdict(set)
        for row in _rows(connection, "execution_checkpoint"):
            checkpoint_id = row.get("checkpoint_id")
            job_id = row.get("job_id")
            step_id = row.get("step_id")
            attempt_id = row.get("source_attempt_id")
            owner = require(job_owner, job_id, "execution checkpoint job")
            step = step_rows.get(str(step_id))
            attempt = attempt_rows.get(str(attempt_id))
            if (
                not isinstance(checkpoint_id, str)
                or checkpoint_id in checkpoint_rows
                or step is None
                or attempt is None
                or step.get("job_id") != job_id
                or attempt.get("job_id") != job_id
                or attempt.get("step_id") != step_id
                or require(step_owner, step_id, "execution checkpoint step") != owner
                or require(attempt_owner, attempt_id, "execution checkpoint attempt") != owner
                or int(row.get("lease_epoch", 0)) != int(attempt.get("lease_epoch", 0))
                or row.get("run_snapshot_hash") != job_rows[str(job_id)].get("run_snapshot_hash")
            ):
                raise WorkspaceProjectionError("execution checkpoint crosses Workspace or Attempt")
            value = SqliteWorkspaceDatabaseProjector._json_mapping(
                row.get("checkpoint_json"), "execution checkpoint"
            )
            try:
                verify_checkpoint(
                    value,
                    expected_snapshot_hash=str(row.get("run_snapshot_hash")),
                )
            except Exception as exc:
                raise WorkspaceProjectionError("execution checkpoint contract is invalid") from exc
            if (
                _json_canonical(value) != row.get("checkpoint_json")
                or any(
                    value.get(field) != row.get(field)
                    for field in (
                        "checkpoint_id",
                        "job_id",
                        "step_id",
                        "source_attempt_id",
                        "checkpoint_seq",
                        "lease_epoch",
                        "run_snapshot_hash",
                        "replay_policy",
                        "completed_units",
                        "total_units",
                        "unit_set_hash",
                        "state_asset_id",
                        "checkpoint_hash",
                    )
                )
                or not isinstance(row.get("operation_key"), str)
                or not isinstance(row.get("payload_hash"), str)
                or len(str(row.get("payload_hash"))) != 64
            ):
                raise WorkspaceProjectionError("execution checkpoint row identity is invalid")
            chain = (str(job_id), str(step_id))
            sequence = int(row["checkpoint_seq"])
            if sequence in checkpoint_chain_sequences[chain]:
                raise WorkspaceProjectionError("execution checkpoint sequence is ambiguous")
            checkpoint_chain_sequences[chain].add(sequence)
            if chain not in checkpoint_chain_totals:
                checkpoint_chain_totals[chain] = row.get("total_units")
            elif checkpoint_chain_totals[chain] != row.get("total_units"):
                raise WorkspaceProjectionError("execution checkpoint total_units drifted")
            operation_key = (str(job_id), str(row["operation_key"]))
            if operation_key in checkpoint_operation_keys:
                raise WorkspaceProjectionError("execution checkpoint operation identity is ambiguous")
            checkpoint_rows[checkpoint_id] = row
            checkpoint_operation_keys[operation_key] = row

        receipt_owner: dict[str, str] = {}
        receipt_rows: dict[str, dict[str, object]] = {}
        receipt_values: dict[str, dict[str, object]] = {}
        for row in _rows(connection, "execution_receipt"):
            owner_set = {
                require(job_owner, row.get("job_id"), "execution receipt job"),
                require(step_owner, row.get("step_id"), "execution receipt step"),
                require(attempt_owner, row.get("attempt_id"), "execution receipt attempt"),
            }
            attempt = attempt_rows[str(row["attempt_id"])]
            if (
                len(owner_set) != 1
                or attempt.get("job_id") != row.get("job_id")
                or attempt.get("step_id") != row.get("step_id")
            ):
                raise WorkspaceProjectionError("execution receipt crosses Workspace")
            receipt = SqliteWorkspaceDatabaseProjector._json_mapping(
                row.get("receipt_json"), "execution receipt"
            )
            try:
                assert_valid("provenance-receipt/v1", receipt)
            except Exception as exc:
                raise WorkspaceProjectionError("execution receipt contract is invalid") from exc
            if any(
                receipt.get(field) != row.get(field)
                for field in ("receipt_id", "job_id", "step_id", "attempt_id", "receipt_hash")
            ) or receipt.get("receipt_hash") != hash_without_field(
                receipt, "receipt_hash", "provenance-receipt/v1"
            ):
                raise WorkspaceProjectionError("execution receipt identity is invalid")
            receipt_id = str(row["receipt_id"])
            receipt_owner[receipt_id] = next(iter(owner_set))
            receipt_rows[receipt_id] = row
            receipt_values[receipt_id] = receipt

        for receipt_id, receipt in receipt_values.items():
            owner = receipt_owner[receipt_id]
            for parent_receipt_id in receipt["parent_receipt_ids"]:  # type: ignore[index]
                if require(
                    receipt_owner,
                    parent_receipt_id,
                    "execution parent receipt",
                ) != owner:
                    raise WorkspaceProjectionError(
                        "execution parent receipt crosses Workspace"
                    )

        for job_id, row in job_rows.items():
            owner = job_owner[job_id]
            output_step = row.get("output_step_id")
            if output_step is not None and (
                require(step_owner, output_step, "execution output step") != owner
                or step_job[str(output_step)] != job_id
            ):
                raise WorkspaceProjectionError("execution output step crosses Workspace")
            provenance_receipt = row.get("provenance_receipt_id")
            if provenance_receipt is not None and require(
                receipt_owner, provenance_receipt, "execution job receipt"
            ) != owner:
                raise WorkspaceProjectionError("execution job receipt crosses Workspace")
        for step_id, row in step_rows.items():
            owner = step_owner[step_id]
            active_attempt = row.get("active_attempt_id")
            if active_attempt is not None:
                attempt = attempt_rows.get(str(active_attempt))
                if (
                    attempt is None
                    or require(
                        attempt_owner, active_attempt, "execution active attempt"
                    )
                    != owner
                    or attempt.get("step_id") != step_id
                ):
                    raise WorkspaceProjectionError(
                        "execution active attempt crosses Workspace"
                    )
            try:
                dependencies = _dependency_ids(str(row.get("dependency_step_ids_json")))
            except Exception as exc:
                raise WorkspaceProjectionError(
                    "execution Step dependency authority is invalid"
                ) from exc
            if any(
                require(step_owner, dependency, "execution dependency") != owner
                or step_job[dependency] != row.get("job_id")
                for dependency in dependencies
            ):
                raise WorkspaceProjectionError("execution dependency crosses Workspace")
            is_output = row.get("is_output") == 1
            if is_output != (job_rows[str(row["job_id"])].get("output_step_id") == step_id):
                raise WorkspaceProjectionError("execution output Step authority is inconsistent")
        for attempt_id, row in attempt_rows.items():
            preallocated = row.get("preallocated_receipt_id")
            if preallocated in receipt_owner and (
                receipt_owner[str(preallocated)] != attempt_owner[attempt_id]
                or receipt_rows[str(preallocated)].get("attempt_id") != attempt_id
            ):
                raise WorkspaceProjectionError(
                    "preallocated execution receipt crosses Workspace"
                )

        job_event_keys: set[tuple[str, int]] = set()
        job_event_rows: dict[tuple[str, int], dict[str, object]] = {}
        job_event_values: dict[tuple[str, int], dict[str, object]] = {}
        job_event_by_id: dict[str, dict[str, object]] = {}
        for row in _rows(connection, "execution_job_event"):
            owner = require(job_owner, row.get("job_id"), "execution Job Event job")
            attempt_id = row.get("attempt_id")
            attempt = attempt_rows.get(str(attempt_id))
            if (
                attempt is None
                or require(attempt_owner, attempt_id, "execution Job Event attempt")
                != owner
                or attempt.get("job_id") != row.get("job_id")
            ):
                raise WorkspaceProjectionError("execution Job Event crosses Workspace")
            event = SqliteWorkspaceDatabaseProjector._json_mapping(
                row.get("event_json"), "execution Job Event"
            )
            try:
                assert_valid("plugin-job-event/v1", event)
            except Exception as exc:
                raise WorkspaceProjectionError("execution Job Event is invalid") from exc
            expected = (
                row.get("event_id"),
                row.get("job_id"),
                attempt.get("step_id"),
                attempt_id,
                row.get("job_event_seq"),
                row.get("local_seq"),
            )
            actual = tuple(
                event.get(field)
                for field in (
                    "event_id",
                    "job_id",
                    "step_id",
                    "attempt_id",
                    "job_event_seq",
                    "local_seq",
                )
            )
            if actual != expected:
                raise WorkspaceProjectionError("execution Job Event identity is invalid")
            key = (str(row["job_id"]), int(row["job_event_seq"]))
            if key in job_event_keys or str(row["event_id"]) in job_event_by_id:
                raise WorkspaceProjectionError("execution Job Event identity is ambiguous")
            if int(row["job_event_seq"]) > int(job_rows[str(row["job_id"])] ["job_event_high_water"]):
                raise WorkspaceProjectionError("execution Job Event exceeds its durable high-water")
            job_event_keys.add(key)
            job_event_rows[key] = row
            job_event_values[key] = event
            job_event_by_id[str(row["event_id"])] = event

        checkpoint_operation_rows: dict[tuple[str, str], dict[str, object]] = {}
        for row in _rows(connection, "execution_checkpoint_operation"):
            job_id = row.get("job_id")
            operation_key = row.get("operation_key")
            key = (str(job_id), str(operation_key))
            checkpoint_row = checkpoint_operation_keys.get(key)
            if (
                checkpoint_row is None
                or key in checkpoint_operation_rows
                or require(job_owner, job_id, "checkpoint operation job")
                != job_owner.get(str(checkpoint_row.get("job_id")))
                or row.get("method") != "host.checkpoint.commit/v1"
                or row.get("checkpoint_id") != checkpoint_row.get("checkpoint_id")
                or int(row.get("job_event_seq", 0)) != int(checkpoint_row.get("job_event_seq", 0))
                or row.get("payload_hash") != checkpoint_row.get("payload_hash")
            ):
                raise WorkspaceProjectionError("execution checkpoint operation is orphaned or inconsistent")
            event_key = (str(job_id), int(row["job_event_seq"]))
            event = job_event_values.get(event_key)
            attempt = attempt_rows.get(str(checkpoint_row.get("source_attempt_id")))
            if event is None or attempt is None:
                raise WorkspaceProjectionError("execution checkpoint operation Event closure is missing")
            if (
                event.get("event_type") != f"plugin.{attempt.get('plugin_id')}.job.checkpoint"
                or event.get("attempt_id") != checkpoint_row.get("source_attempt_id")
                or event.get("step_id") != checkpoint_row.get("step_id")
                or (event.get("payload_asset_id"), event.get("payload_hash"))
                == (None, None)
                or not isinstance(event.get("payload_asset_id"), str)
                or not isinstance(event.get("payload_hash"), str)
                or len(str(event.get("payload_hash"))) != 64
                or any(char not in "0123456789abcdef" for char in str(event.get("payload_hash")))
                or event.get("payload_asset_id") != "asset-sha256-" + str(event.get("payload_hash"))
            ):
                raise WorkspaceProjectionError("execution checkpoint Event Asset closure is invalid")
            checkpoint_value = SqliteWorkspaceDatabaseProjector._json_mapping(
                checkpoint_row.get("checkpoint_json"), "execution checkpoint"
            )
            request = SqliteWorkspaceDatabaseProjector._json_mapping(
                row.get("request_json"), "execution checkpoint operation request"
            )
            response = SqliteWorkspaceDatabaseProjector._json_mapping(
                row.get("response_json"), "execution checkpoint operation response"
            )
            expected_request = {
                "method": "host.checkpoint.commit/v1",
                "operation_key": operation_key,
                "checkpoint": checkpoint_value,
                "checkpoint_asset_id": event["payload_asset_id"],
                "checkpoint_asset_hash": event["payload_hash"],
                "local_seq": request.get("local_seq"),
                "worker_run_id": attempt.get("worker_run_id"),
            }
            if request.get("local_seq") is not None and request.get("local_seq") != event.get("local_seq"):
                raise WorkspaceProjectionError("execution checkpoint operation local sequence drifted")
            if (
                _json_canonical(request) != row.get("request_json")
                or request != expected_request
                or hashlib.sha256(canonical_bytes(request)).hexdigest() != row.get("payload_hash")
                or response
                != {
                    "accepted": True,
                    "checkpoint_id": checkpoint_row.get("checkpoint_id"),
                    "completed_units": checkpoint_value.get("completed_units"),
                    "total_units": checkpoint_value.get("total_units"),
                    "job_event_seq": checkpoint_row.get("job_event_seq"),
                }
                or _json_canonical(response) != row.get("response_json")
            ):
                raise WorkspaceProjectionError("execution checkpoint operation closure is invalid")
            checkpoint_operation_rows[key] = row

        if set(checkpoint_operation_rows) != set(checkpoint_operation_keys):
            raise WorkspaceProjectionError("execution checkpoint operation closure is incomplete")
        checkpoint_by_job: dict[str, list[dict[str, object]]] = defaultdict(list)
        for row in checkpoint_rows.values():
            checkpoint_by_job[str(row["job_id"])].append(row)
            event = job_event_values.get((str(row["job_id"]), int(row["job_event_seq"])))
            if event is None:
                raise WorkspaceProjectionError("execution checkpoint Job Event is missing")
            if (
                event.get("event_type") != f"plugin.{attempt_rows[str(row['source_attempt_id'])].get('plugin_id')}.job.checkpoint"
                or event.get("attempt_id") != row.get("source_attempt_id")
                or event.get("step_id") != row.get("step_id")
            ):
                raise WorkspaceProjectionError("execution checkpoint Job Event identity is invalid")
        for job_id, rows in checkpoint_by_job.items():
            latest = max(rows, key=lambda item: (int(item["job_event_seq"]), int(item["checkpoint_seq"]), str(item["checkpoint_id"])))
            if job_rows[job_id].get("current_checkpoint_id") != latest.get("checkpoint_id"):
                raise WorkspaceProjectionError("execution Job current checkpoint authority is inconsistent")

        control_operation_rows: dict[tuple[str, str], dict[str, object]] = {}
        control_methods = {
            "pause": "job.pause",
            "resume": "job.resume",
            "cancel": "job.cancel",
            "await_user": "host.job.await_user/v1",
        }
        control_request_fields = {
            "pause": {"method", "operation", "job_id", "step_id", "attempt_id", "lease_epoch", "operation_key", "worker_run_id", "reason", "checkpoint", "checkpoint_asset_id", "checkpoint_asset_hash", "prompt_asset_id", "prompt_asset_hash"},
            "await_user": {"method", "operation", "job_id", "step_id", "attempt_id", "lease_epoch", "operation_key", "worker_run_id", "reason", "checkpoint", "checkpoint_asset_id", "checkpoint_asset_hash", "prompt_asset_id", "prompt_asset_hash"},
            "cancel": {"method", "operation", "job_id", "step_id", "attempt_id", "lease_epoch", "operation_key", "worker_run_id", "reason"},
            "resume": {"method", "operation", "job_id", "step_id", "operation_key", "attempt_id", "resume_of_attempt_id", "source_attempt_id", "lease_epoch", "worker_run_id", "new_attempt_id", "plugin_id", "release_id", "package_hash", "capability_id", "generation_id", "preallocated_receipt_id", "lease_expires_at", "checkpoint", "checkpoint_asset_id", "checkpoint_asset_hash", "resume_reason"},
        }
        for row in _rows(connection, "execution_control_operation"):
            job_id = row.get("job_id")
            operation_key = row.get("operation_key")
            key = (str(job_id), str(operation_key))
            operation = row.get("operation")
            if (
                not isinstance(operation, str)
                or operation not in control_methods
                or key in control_operation_rows
                or require(job_owner, job_id, "control operation job") is None
                or row.get("method") != control_methods.get(operation)
            ):
                raise WorkspaceProjectionError("execution control operation identity is invalid")
            request = SqliteWorkspaceDatabaseProjector._json_mapping(
                row.get("request_json"), "execution control operation request"
            )
            result = SqliteWorkspaceDatabaseProjector._json_mapping(
                row.get("response_json"), "execution control operation response"
            )
            if (
                set(request) != control_request_fields[operation]
                or request.get("method") != row.get("method")
                or request.get("operation") != operation
                or request.get("job_id") != job_id
                or request.get("operation_key") != operation_key
                or _json_canonical(request) != row.get("request_json")
                or hashlib.sha256(canonical_bytes(request)).hexdigest() != row.get("payload_hash")
                or _json_canonical(result) != row.get("response_json")
            ):
                raise WorkspaceProjectionError("execution control operation request closure is invalid")
            request_attempt_id = request.get("source_attempt_id") if operation == "resume" else request.get("attempt_id")
            request_attempt = attempt_rows.get(str(request_attempt_id))
            step = step_rows.get(str(request.get("step_id")))
            job = job_rows.get(str(job_id))
            if (
                request_attempt is None
                or step is None
                or job is None
                or request_attempt.get("job_id") != job_id
                or request_attempt.get("step_id") != request.get("step_id")
            ):
                raise WorkspaceProjectionError("execution control operation Attempt is orphaned")
            target_attempt = request_attempt
            if operation == "resume":
                if (
                    request.get("lease_epoch") is not None
                    and request.get("lease_epoch") != request_attempt.get("lease_epoch")
                ):
                    raise WorkspaceProjectionError("execution resume source epoch is invalid")
                target_attempt = attempt_rows.get(str(request.get("new_attempt_id")))
            elif (
                request.get("lease_epoch") != request_attempt.get("lease_epoch")
                or request.get("worker_run_id") != request_attempt.get("worker_run_id")
            ):
                raise WorkspaceProjectionError("execution control Attempt fence is invalid")
            if target_attempt is None:
                raise WorkspaceProjectionError("execution control target Attempt is orphaned")
            if (
                row.get("attempt_id") != target_attempt.get("attempt_id")
                or row.get("lease_epoch") is None
                or int(row["lease_epoch"]) < 1
                or row.get("lease_epoch") != target_attempt.get("lease_epoch")
            ):
                raise WorkspaceProjectionError("execution control operation fence is invalid")
            if operation == "resume":
                source_attempt = attempt_rows.get(str(request.get("source_attempt_id")))
                checkpoint_value = request.get("checkpoint")
                checkpoint_row = None if not isinstance(checkpoint_value, Mapping) else checkpoint_rows.get(str(checkpoint_value.get("checkpoint_id")))
                if (
                    target_attempt is None
                    or source_attempt is None
                    or checkpoint_row is None
                    or target_attempt.get("job_id") != job_id
                    or target_attempt.get("step_id") != request.get("step_id")
                    or target_attempt.get("resume_of_attempt_id") != request.get("source_attempt_id")
                    or target_attempt.get("resume_checkpoint_id") != checkpoint_row.get("checkpoint_id")
                    or row.get("lease_epoch") != target_attempt.get("lease_epoch")
                    or target_attempt.get("worker_run_id") != request.get("worker_run_id")
                ):
                    raise WorkspaceProjectionError("execution resume operation closure is invalid")
                expected_result = {
                    "accepted": True,
                    "worker_run_id": target_attempt.get("worker_run_id"),
                    "provenance_receipt_id": target_attempt.get("preallocated_receipt_id"),
                    "output_streams": [],
                }
            elif operation in {"pause", "await_user"}:
                checkpoint_value = request.get("checkpoint")
                checkpoint_row = None if not isinstance(checkpoint_value, Mapping) else checkpoint_rows.get(str(checkpoint_value.get("checkpoint_id")))
                if (
                    checkpoint_row is None
                    or checkpoint_row.get("operation_key") != operation_key
                    or request.get("checkpoint_asset_id") is None
                    or request.get("checkpoint_asset_hash") is None
                ):
                    raise WorkspaceProjectionError("execution control checkpoint closure is invalid")
                if operation == "await_user":
                    if request.get("reason") not in {"user_input", "external_confirmation"}:
                        raise WorkspaceProjectionError("execution await_user reason is not frozen")
                    if (
                        not isinstance(request.get("prompt_asset_id"), str)
                        or not isinstance(request.get("prompt_asset_hash"), str)
                        or request.get("prompt_asset_id")
                        != "asset-sha256-" + str(request.get("prompt_asset_hash"))
                    ):
                        raise WorkspaceProjectionError("execution await_user prompt Asset closure is invalid")
                expected_result = (
                    {"accepted": True, "checkpoint_asset_id": request.get("checkpoint_asset_id")}
                    if operation == "pause"
                    else {"accepted": True, "attempt_state": "suspended", "step_state": "waiting_user", "job_state": "waiting_user", "job_event_seq": None}
                )
                if operation == "await_user":
                    event = job_event_values.get((str(checkpoint_row["job_id"]), int(checkpoint_row["job_event_seq"])))
                    control_event = job_event_by_id.get(_stable_id("job-event", job_id, operation, operation_key))
                    if not isinstance(control_event, Mapping):
                        raise WorkspaceProjectionError("execution await_user Job Event closure is missing")
                    expected_result["job_event_seq"] = control_event.get("job_event_seq")
            else:
                terminal_known = result.get("terminal_known") is True
                if terminal_known:
                    if result.get("accepted") is not True or not isinstance(result.get("attempt_state"), str):
                        raise WorkspaceProjectionError("terminal cancel result is invalid")
                    expected_result = result
                else:
                    expected_result = {"accepted": True, "terminal_known": False, "attempt_state": "cancelling"}
            if operation != "cancel" or result.get("terminal_known") is not True:
                if operation == "resume":
                    checkpoint_value = request.get("checkpoint")
                    checkpoint_row = checkpoint_rows.get(str(checkpoint_value.get("checkpoint_id"))) if isinstance(checkpoint_value, Mapping) else None
                elif operation in {"pause", "await_user"}:
                    checkpoint_value = request.get("checkpoint")
                control_event = job_event_by_id.get(_stable_id("job-event", job_id, operation, operation_key))
                if not isinstance(control_event, Mapping):
                    raise WorkspaceProjectionError("execution control Job Event closure is missing")
                control_event_key = (str(job_id), int(control_event.get("job_event_seq", 0)))
                if (
                    control_event_key not in job_event_rows
                    or control_event.get("event_type") != f"plugin.{request_attempt.get('plugin_id')}.job.{operation}"
                    or control_event.get("attempt_id") != (request.get("new_attempt_id") if operation == "resume" else request.get("attempt_id"))
                    or int(control_event.get("job_event_seq", 0)) > int(job_rows[str(job_id)].get("job_event_high_water", 0))
                ):
                    raise WorkspaceProjectionError("execution control Job Event identity is invalid")
                if checkpoint_value is not None:
                    cp_event = job_event_values.get((str(job_id), int(checkpoint_row["job_event_seq"]))) if checkpoint_row is not None else None
                    if cp_event is None or (
                        control_event.get("payload_asset_id"), control_event.get("payload_hash")
                    ) != (cp_event.get("payload_asset_id"), cp_event.get("payload_hash")):
                        raise WorkspaceProjectionError("execution control checkpoint Event binding is invalid")
                elif (control_event.get("payload_asset_id"), control_event.get("payload_hash")) != (None, None):
                    raise WorkspaceProjectionError("execution control Job Event payload is invalid")
                if isinstance(result, dict) and expected_result != result:
                    raise WorkspaceProjectionError("execution control result closure is invalid")
                if operation == "await_user" and isinstance(result.get("job_event_seq"), int) and result.get("job_event_seq") != control_event.get("job_event_seq"):
                    raise WorkspaceProjectionError("execution await_user result cursor is invalid")
                core_event = connection.execute(
                    "SELECT core_event_seq,aggregate_revision,event_json "
                    "FROM execution_core_event WHERE event_id=?",
                    (_stable_id("core-event", job_id, operation_key, "job.state.changed"),),
                ).fetchone()
                if core_event is None:
                    raise WorkspaceProjectionError("execution control Core Event closure is missing")
                core_event = {
                    "core_event_seq": core_event[0],
                    "aggregate_revision": core_event[1],
                    "event_json": core_event[2],
                }
                core_value = SqliteWorkspaceDatabaseProjector._json_mapping(
                    core_event.get("event_json"), "execution control Core Event"
                )
                if (
                    core_value.get("event_type") != "job.state.changed"
                    or core_value.get("aggregate_id") != job_id
                    or core_value.get("workspace_id") != job_rows[str(job_id)].get("workspace_id")
                    or core_value.get("correlation_id") != operation_key
                    or core_value.get("causation_id") != (request.get("new_attempt_id") if operation == "resume" else request.get("attempt_id"))
                    or core_value.get("core_event_seq") != core_event.get("core_event_seq")
                    or int(core_event.get("core_event_seq", 0)) > int(job_rows[str(job_id)].get("core_event_high_water", 0))
                    or int(core_event.get("aggregate_revision", 0)) > int(job_rows[str(job_id)].get("job_revision", 0))
                ):
                    raise WorkspaceProjectionError("execution control Core Event identity is invalid")
            control_operation_rows[key] = row

        core_event_seqs: set[int] = set()
        for row in _rows(connection, "execution_core_event"):
            owner = require(
                workspace_owner, row.get("workspace_id"), "execution Core Event workspace"
            )
            event = SqliteWorkspaceDatabaseProjector._json_mapping(
                row.get("event_json"), "execution Core Event"
            )
            try:
                assert_valid("core-event-v1", event)
            except Exception as exc:
                raise WorkspaceProjectionError("execution Core Event is invalid") from exc
            expected = tuple(
                row.get(field)
                for field in (
                    "event_id",
                    "workspace_id",
                    "core_event_seq",
                    "aggregate_id",
                    "aggregate_revision",
                )
            )
            actual = tuple(
                event.get(field)
                for field in (
                    "event_id",
                    "workspace_id",
                    "core_event_seq",
                    "aggregate_id",
                    "aggregate_revision",
                )
            )
            if actual != expected:
                raise WorkspaceProjectionError("execution Core Event identity is invalid")
            event_type = event.get("event_type")
            if event_type in {"job.state.changed", "job.terminal"}:
                aggregate_owner = require(
                    job_owner, row.get("aggregate_id"), "Core Job Event aggregate"
                )
                causation_owner = require(
                    attempt_owner, event.get("causation_id"), "Core Job Event causation"
                )
            elif event_type == "revision.published":
                aggregate_owner = require(
                    document_owner,
                    row.get("aggregate_id"),
                    "publication Core Event aggregate",
                )
                causation_owner = require(
                    candidate_owner,
                    event.get("causation_id"),
                    "publication Core Event causation",
                )
            else:
                raise WorkspaceProjectionError("execution Core Event type is unclassified")
            if aggregate_owner != owner or causation_owner != owner:
                raise WorkspaceProjectionError("execution Core Event crosses Workspace")
            core_event_seqs.add(int(row["core_event_seq"]))

        host_context_owner: dict[str, str] = {}
        host_context_attempt: dict[str, str] = {}
        for attempt_id, row in attempt_rows.items():
            lease_epoch = row.get("lease_epoch")
            meta = {
                "protocol_version": "1",
                "context": "attempt",
                "operation_id": "backup-closure-validation",
                "deadline_at": "9999-12-31T23:59:59Z",
                "job_id": row.get("job_id"),
                "step_id": row.get("step_id"),
                "attempt_id": attempt_id,
                "lease_epoch": lease_epoch,
                "generation_id": row.get("generation_id"),
                "plugin_release_id": row.get("release_id"),
            }
            try:
                context_identity = derive_operation_context_identity(
                    meta, expected_lease_epoch=lease_epoch  # type: ignore[arg-type]
                )
            except Exception as exc:
                raise WorkspaceProjectionError("execution Attempt context is invalid") from exc
            if context_identity in host_context_attempt:
                raise WorkspaceProjectionError("execution Attempt context is ambiguous")
            host_context_attempt[context_identity] = attempt_id
            host_context_owner[context_identity] = attempt_owner[attempt_id]

        outcome_by_attempt: dict[str, dict[str, object]] = {}
        outcome_by_key: dict[tuple[str, str], dict[str, object]] = {}
        for row in _rows(connection, "execution_outcome"):
            attempt_id = row.get("attempt_id")
            attempt = attempt_rows.get(str(attempt_id))
            owner_set = {
                require(job_owner, row.get("job_id"), "execution outcome job"),
                require(step_owner, row.get("step_id"), "execution outcome step"),
                require(attempt_owner, attempt_id, "execution outcome attempt"),
                require(
                    host_context_owner,
                    row.get("context_identity"),
                    "execution outcome context",
                ),
            }
            if (
                len(owner_set) != 1
                or attempt is None
                or attempt.get("job_id") != row.get("job_id")
                or attempt.get("step_id") != row.get("step_id")
                or host_context_attempt[str(row["context_identity"])] != attempt_id
            ):
                raise WorkspaceProjectionError("execution outcome crosses Workspace")
            receipt_id = row.get("provenance_receipt_id")
            if receipt_id is not None and (
                require(receipt_owner, receipt_id, "execution outcome receipt")
                != next(iter(owner_set))
                or receipt_rows[str(receipt_id)].get("attempt_id") != attempt_id
            ):
                raise WorkspaceProjectionError("execution outcome receipt crosses Workspace")
            response = SqliteWorkspaceDatabaseProjector._json_mapping(
                row.get("response_json"), "execution outcome response"
            )
            if response.get("provenance_receipt_id") != receipt_id:
                raise WorkspaceProjectionError("execution outcome response identity is invalid")
            key = (str(row["context_identity"]), str(row["operation_key"]))
            if key in outcome_by_key or str(attempt_id) in outcome_by_attempt:
                raise WorkspaceProjectionError("execution outcome identity is ambiguous")
            outcome_by_key[key] = row
            outcome_by_attempt[str(attempt_id)] = row

        candidate_binding_by_id: dict[str, dict[str, object]] = {}
        candidate_binding_by_identity: dict[
            tuple[str, str, str, str], dict[str, object]
        ] = {}
        for row in _rows(connection, "execution_candidate_binding"):
            job_id = row.get("job_id")
            attempt_id = row.get("attempt_id")
            candidate_id = row.get("candidate_id")
            owner = require(job_owner, job_id, "execution Candidate binding job")
            if (
                require(attempt_owner, attempt_id, "execution Candidate binding attempt")
                != owner
                or attempt_rows[str(attempt_id)].get("job_id") != job_id
                or require(candidate_owner, candidate_id, "execution Candidate binding candidate")
                != owner
                or candidate_rows[str(candidate_id)].get("item_id") != row.get("item_id")
            ):
                raise WorkspaceProjectionError("execution Candidate binding crosses Workspace")
            outcome = outcome_by_attempt.get(str(attempt_id))
            if outcome is None or outcome.get("job_id") != job_id:
                raise WorkspaceProjectionError(
                    "execution Candidate binding has no terminal outcome"
                )
            receipt_id = outcome.get("provenance_receipt_id")
            receipt = SqliteWorkspaceDatabaseProjector._json_mapping(
                receipt_rows.get(str(receipt_id), {}).get("receipt_json"),
                "execution Candidate receipt",
            )
            if (
                receipt.get("bundle_id") != row.get("bundle_id")
                or row.get("item_id") not in receipt.get("staged_items", ())
            ):
                raise WorkspaceProjectionError(
                    "execution Candidate binding receipt is incomplete"
                )
            scoped_operation_key = hashlib.sha256(
                (
                    "candidate-operation/v1\n"
                    f"{outcome['context_identity']}\nhost.job.complete/v1\n"
                    f"{row['stage_operation_key']}"
                ).encode()
            ).hexdigest()
            if candidate_rows[str(candidate_id)].get("operation_key") != scoped_operation_key:
                raise WorkspaceProjectionError(
                    "execution Candidate binding operation identity is invalid"
                )
            identity = (
                str(job_id),
                str(attempt_id),
                str(row["item_id"]),
                str(candidate_id),
            )
            if str(candidate_id) in candidate_binding_by_id or identity in candidate_binding_by_identity:
                raise WorkspaceProjectionError("execution Candidate binding is ambiguous")
            candidate_binding_by_id[str(candidate_id)] = row
            candidate_binding_by_identity[identity] = row

        for row in _rows(connection, "execution_publication_binding"):
            identity = (
                str(row.get("job_id")),
                str(row.get("attempt_id")),
                str(row.get("item_id")),
                str(row.get("candidate_id")),
            )
            candidate_binding = candidate_binding_by_identity.get(identity)
            publication_id = row.get("publication_id")
            publication = publication_rows.get(str(publication_id))
            if (
                candidate_binding is None
                or publication is None
                or publication.get("candidate_id") != row.get("candidate_id")
                or require(
                    publication_owner,
                    publication_id,
                    "execution publication binding",
                )
                != require(
                    job_owner, row.get("job_id"), "execution publication binding job"
                )
            ):
                raise WorkspaceProjectionError(
                    "execution publication binding crosses Workspace"
                )

        broker_context_owner: dict[str, str] = {}
        broker_context_attempt: dict[str, str] = {}
        for attempt_id, row in attempt_rows.items():
            context = _broker_context_identity(
                str(row["job_id"]), str(row["step_id"]), attempt_id
            )
            if context in broker_context_attempt:
                raise WorkspaceProjectionError("Broker attempt context is ambiguous")
            broker_context_attempt[context] = attempt_id
            broker_context_owner[context] = attempt_owner[attempt_id]

        creation_by_key: dict[tuple[str, str], dict[str, object]] = {}
        for row in _rows(connection, "execution_child_creation"):
            context = str(row.get("context_identity"))
            key = (context, str(row.get("operation_key")))
            parent_owner = require(
                broker_context_owner, context, "Broker child parent context"
            )
            child_owner = require(
                job_owner, row.get("child_job_id"), "Broker child job"
            )
            result = SqliteWorkspaceDatabaseProjector._json_mapping(
                row.get("result_json"), "Broker child creation"
            )
            try:
                creation = ChildCreationResult.from_mapping(result)
            except Exception as exc:
                raise WorkspaceProjectionError("Broker child creation is invalid") from exc
            if (
                parent_owner != child_owner
                or creation.child_job_id != row.get("child_job_id")
                or require(step_owner, creation.child_step_id, "Broker child Step")
                != child_owner
                or require(attempt_owner, creation.child_attempt_id, "Broker child Attempt")
                != child_owner
                or attempt_rows[creation.child_attempt_id].get("job_id")
                != creation.child_job_id
            ):
                raise WorkspaceProjectionError("Broker child creation crosses Workspace")
            if key in creation_by_key:
                raise WorkspaceProjectionError("Broker child creation is ambiguous")
            creation_by_key[key] = row

        child_record_by_key: dict[tuple[str, str], dict[str, object]] = {}
        for row in _rows(connection, "p3_broker_child_record"):
            context = str(row.get("context_identity"))
            key = (context, str(row.get("operation_key")))
            parent_attempt_id = broker_context_attempt.get(context)
            if parent_attempt_id is None:
                raise WorkspaceProjectionError("Broker child record is orphaned")
            record_value = SqliteWorkspaceDatabaseProjector._json_mapping(
                row.get("record_json"), "Broker child record"
            )
            try:
                record = BrokerChildRecord.from_mapping(record_value)
            except Exception as exc:
                raise WorkspaceProjectionError("Broker child record is invalid") from exc
            parent_attempt = attempt_rows[parent_attempt_id]
            parent_owner = broker_context_owner[context]
            creation_row = creation_by_key.get(key)
            if creation_row is None:
                raise WorkspaceProjectionError("Broker child record is orphaned")
            creation = ChildCreationResult.from_mapping(
                SqliteWorkspaceDatabaseProjector._json_mapping(
                    creation_row.get("result_json"), "Broker child creation"
                )
            )
            child_snapshot = job_snapshots.get(record.child_job_id)
            if child_snapshot is None:
                raise WorkspaceProjectionError("Broker child Snapshot is orphaned")
            try:
                verify_child_snapshot_binding(
                    child_snapshot,
                    child_job_id=record.child_job_id,
                    child_step_id=creation.child_step_id,
                    child_attempt_id=creation.child_attempt_id,
                    envelope_asset_id=record.broker_invocation_asset_id,
                    envelope_hash=record.broker_invocation_hash,
                    input_asset_id=str(child_snapshot.get("input_asset_id")),
                    input_hash=str(child_snapshot.get("input_hash")),
                    parameters_asset_id=child_snapshot.get(
                        "source_parameters_asset_id"
                    ),  # type: ignore[arg-type]
                    parameters_hash=child_snapshot.get(
                        "source_parameters_hash"
                    ),  # type: ignore[arg-type]
                    child_run_snapshot_asset_id=record.child_run_snapshot_asset_id,
                    child_run_snapshot_hash=record.child_run_snapshot_hash,
                )
            except Exception as exc:
                raise WorkspaceProjectionError("Broker child Snapshot is invalid") from exc
            if (
                require(job_owner, row.get("child_job_id"), "Broker child record job")
                != parent_owner
                or record.child_job_id != row.get("child_job_id")
                or record.parent_job_id != parent_attempt.get("job_id")
                or record.parent_step_id != parent_attempt.get("step_id")
                or record.parent_attempt_id != parent_attempt_id
                or record.invoke_operation_key != row.get("operation_key")
            ):
                raise WorkspaceProjectionError("Broker child record crosses Workspace")
            if key in child_record_by_key:
                raise WorkspaceProjectionError("Broker child record is ambiguous")
            child_record_by_key[key] = row

        broker_operations: dict[tuple[str, str], dict[str, object]] = {}
        for row in _rows(connection, "p3_broker_operation"):
            context = str(row.get("context_identity"))
            key = (context, str(row.get("operation_key")))
            require(broker_context_owner, context, "Broker operation context")
            if row.get("method") != "host.capability.invoke/v1":
                raise WorkspaceProjectionError("Broker operation method is unclassified")
            child_json = row.get("child_creation_json")
            response = row.get("response")
            if child_json is None:
                if response is not None or key in creation_by_key or key in child_record_by_key:
                    raise WorkspaceProjectionError("Broker child operation closure is incomplete")
            else:
                creation = creation_by_key.get(key)
                record = child_record_by_key.get(key)
                if (
                    creation is None
                    or record is None
                    or creation.get("result_json") != child_json
                    or response is None
                ):
                    raise WorkspaceProjectionError("Broker child operation closure is incomplete")
                try:
                    result = parse_json_bytes(bytes(response))
                except Exception as exc:
                    raise WorkspaceProjectionError(
                        "Broker child response is invalid"
                    ) from exc
                if not isinstance(result, Mapping):
                    raise WorkspaceProjectionError("Broker child response is invalid")
                child_result = ChildCreationResult.from_mapping(
                    SqliteWorkspaceDatabaseProjector._json_mapping(
                        child_json, "Broker child operation"
                    )
                )
                if result.get("child_job_id") != child_result.child_job_id:
                    raise WorkspaceProjectionError("Broker child response identity is invalid")
            if key in broker_operations:
                raise WorkspaceProjectionError("Broker operation identity is ambiguous")
            broker_operations[key] = row
        if set(creation_by_key) != {
            key for key, row in broker_operations.items() if row.get("child_creation_json") is not None
        } or set(child_record_by_key) != set(creation_by_key):
            raise WorkspaceProjectionError("Broker child authority is orphaned")

        ledger_keys: set[tuple[str, str]] = set()
        for row in _rows(connection, "p3_host_operation_ledger"):
            if row.get("method") != "host.job.complete/v1":
                raise WorkspaceProjectionError("host operation ledger method is unclassified")
            key = (str(row.get("context_identity")), str(row.get("operation_key")))
            outcome = outcome_by_key.get(key)
            if outcome is None or outcome.get("payload_hash") != row.get("payload_hash"):
                raise WorkspaceProjectionError("host operation ledger is orphaned")
            frame = decode_frame(bytes(row["response_frame"]))
            stored_response = SqliteWorkspaceDatabaseProjector._json_mapping(
                outcome.get("response_json"), "execution outcome response"
            )
            if frame.get("result") != stored_response:
                raise WorkspaceProjectionError("host operation response identity is invalid")
            ledger_keys.add(key)
        if ledger_keys != set(outcome_by_key):
            raise WorkspaceProjectionError("execution outcome lacks a host operation ledger")

    @staticmethod
    def _json_mapping(raw: object, label: str) -> dict[str, object]:
        if not isinstance(raw, str):
            raise WorkspaceProjectionError(f"{label} must be canonical JSON text")
        try:
            value = parse_json_bytes(raw.encode("utf-8"))
        except Exception as exc:
            raise WorkspaceProjectionError(f"{label} is invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise WorkspaceProjectionError(f"{label} must be a JSON object")
        return dict(value)

    @staticmethod
    def _insert_rows(
        connection: sqlite3.Connection,
        table: str,
        rows: list[dict[str, object]],
    ) -> None:
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        if not columns:
            raise WorkspaceProjectionError(f"projected table is missing: {table}")
        quoted = ",".join(f'"{column}"' for column in columns)
        placeholders = ",".join("?" for _ in columns)
        for row in rows:
            connection.execute(
                f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
                tuple(row[column] for column in columns),
            )

    def project(
        self,
        *,
        frozen_database: str | Path,
        destination: str | Path,
        workspace_id: str,
    ) -> None:
        source_path = Path(frozen_database)
        destination_path = Path(destination)
        if destination_path.exists():
            raise WorkspaceProjectionError("workspace projection destination already exists")
        source = sqlite3.connect(_sqlite_uri(source_path), uri=True)
        target: sqlite3.Connection | None = None
        try:
            table_sql, index_sql = self._validate_schema(source)
            selected = self._selected_rows(source, workspace_id)
            page_size = int(source.execute("PRAGMA page_size").fetchone()[0])
            user_version = int(source.execute("PRAGMA user_version").fetchone()[0])
            application_id = int(source.execute("PRAGMA application_id").fetchone()[0])
            sequence_row = source.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='execution_core_event'"
            ).fetchone()
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            target = sqlite3.connect(destination_path)
            target.execute(f"PRAGMA page_size={page_size}")
            target.execute("PRAGMA foreign_keys=OFF")
            for table in _CORE_TABLE_ORDER:
                target.execute(table_sql[table])
            for table in _CORE_TABLE_ORDER:
                self._insert_rows(target, table, selected[table])
            if sequence_row is not None:
                target.execute(
                    "DELETE FROM sqlite_sequence WHERE name='execution_core_event'"
                )
                target.execute(
                    "INSERT INTO sqlite_sequence(name,seq) VALUES('execution_core_event',?)",
                    (int(sequence_row[0]),),
                )
            for sql in index_sql:
                target.execute(sql)
            target.execute(f"PRAGMA user_version={user_version}")
            target.execute(f"PRAGMA application_id={application_id}")
            target.commit()
            target.execute("PRAGMA foreign_keys=ON")
            self._validate_schema(target)
            self._selected_rows(target, workspace_id)
            violation = target.execute("PRAGMA foreign_key_check").fetchone()
            if violation is not None:
                raise WorkspaceProjectionError(
                    f"projected Core authority has a foreign-key violation: {violation}"
                )
            integrity = target.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise WorkspaceProjectionError("projected Core authority failed integrity_check")
            target.close()
            target = None
            with destination_path.open("r+b") as stream:
                os.fsync(stream.fileno())
        except BaseException:
            if target is not None:
                target.close()
            destination_path.unlink(missing_ok=True)
            raise
        finally:
            source.close()


class SqliteCoreSnapshotAdapter:
    """Production CoreSnapshot adapter over the accepted SQLite/Asset authorities.

    The durable high-water is supplied by the injected backup barrier.  This
    adapter never allocates or guesses it.  It reads only the already-frozen
    SQLite image and stores deterministic aggregate state through AssetStore.
    """

    def __init__(self, asset_root: str | Path) -> None:
        self.asset_store = AssetStore(asset_root)

    @staticmethod
    def _state_row(row: dict[str, object]) -> dict[str, object]:
        return {
            key: ({"sqlite_blob_hex": bytes(value).hex()} if isinstance(value, bytes) else value)
            for key, value in row.items()
        }

    @staticmethod
    def _workspace_state(
        connection: sqlite3.Connection,
        workspace_id: str,
        *,
        include_integrated: bool = False,
    ) -> tuple[dict[str, object], int]:
        if include_integrated:
            selected = SqliteWorkspaceDatabaseProjector._selected_rows(
                connection, workspace_id
            )
            tables: dict[str, object] = {
                table: [
                    SqliteCoreSnapshotAdapter._state_row(row)
                    for row in selected[table]
                ]
                for table in _CORE_TABLE_ORDER
                if table != "schema_migration"
            }
        else:
            tables = {}
        workspace = _rows(
            connection,
            "workspace",
            where='"workspace_id"=?',
            parameters=(workspace_id,),
        )
        if len(workspace) != 1:
            raise CoreSnapshotAdapterError(
                f"workspace scope is absent or ambiguous in frozen Core authority: {workspace_id}"
            )
        if not include_integrated:
            tables["workspace"] = workspace
            for table in ("document", "node", "revision", "relation"):
                tables[table] = _rows(
                    connection,
                    table,
                    where='"workspace_id"=?',
                    parameters=(workspace_id,),
                )
            tables["candidate"] = [
                row
                for row in _rows(connection, "candidate")
                if _candidate_workspace(row)[0] == workspace_id
            ]
            tables["publication_receipt"] = _rows(
                connection,
                "publication_receipt",
                where=(
                    '"revision_id" IN (SELECT "revision_id" FROM "revision" '
                    'WHERE "workspace_id"=?)'
                ),
                parameters=(workspace_id,),
            )
        revision_values = [
            row.get("revision")
            for table in ("workspace", "document", "node")
            for row in tables[table]  # type: ignore[union-attr]
        ]
        revision_numbers = [
            row.get("revision_number") for row in tables["revision"]  # type: ignore[union-attr]
        ]
        numeric = [
            value
            for value in (*revision_values, *revision_numbers)
            if type(value) is int and value >= 0
        ]
        aggregate_revision = max((value + 1 for value in numeric), default=1)
        return (
            {
                "schema": "plotpilot-core-aggregate-state/v1",
                "aggregate_type": "workspace",
                "aggregate_id": workspace_id,
                "tables": tables,
            },
            aggregate_revision,
        )

    def capture_for_backup(
        self,
        *,
        barrier: BackupBarrier,
        core_database: Path,
        database_sha256: str,
        database_asset_ids: tuple[str, ...],
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
    ) -> CoreSnapshotCapture:
        high_water = barrier.core_event_high_water
        if type(high_water) is not int or high_water < 0:
            raise CoreSnapshotAdapterError("backup barrier lacks a durable Core event high-water")
        connection = sqlite3.connect(_sqlite_uri(core_database), uri=True)
        try:
            actual_workspaces = tuple(
                row[0]
                for row in connection.execute(
                    'SELECT "workspace_id" FROM "workspace" ORDER BY CAST("workspace_id" AS BLOB)'
                )
            )
            if actual_workspaces != workspace_ids:
                raise CoreSnapshotAdapterError(
                    "requested workspace set differs from frozen Core authority"
                )
            if mode == "workspace" and len(workspace_ids) != 1:
                raise CoreSnapshotAdapterError("workspace backup requires exactly one workspace")
            covered: list[dict[str, object]] = []
            required_assets: list[str] = []
            for workspace_id in workspace_ids:
                state, revision = self._workspace_state(
                    connection,
                    workspace_id,
                    include_integrated=mode == "workspace",
                )
                state_raw = canonical_bytes(state)
                metadata = self.asset_store.put(
                    state_raw,
                    mime="application/vnd.plotpilot.core-aggregate-state+json",
                    logical_role="core_snapshot_state",
                    provenance="sqlite-core-snapshot-adapter/v1",
                    rebuildable=False,
                )
                required_assets.append(metadata.asset_id)
                covered.append(
                    {
                        "aggregate_type": "workspace",
                        "aggregate_id": workspace_id,
                        "aggregate_revision": revision,
                        "state_asset_id": metadata.asset_id,
                        "state_hash": metadata.sha256,
                    }
                )
        finally:
            connection.close()
        covered.sort(
            key=lambda item: (
                str(item["aggregate_type"]).encode("utf-8"),
                str(item["aggregate_id"]).encode("utf-8"),
            )
        )
        scope = workspace_ids[0] if mode == "workspace" else None
        snapshot: dict[str, object] = {
            "schema": "core-snapshot/v1",
            "snapshot_id": "core-snapshot-"
            + hash_jcs(
                "plotpilot-core-snapshot-id/v1",
                {
                    "database_sha256": database_sha256,
                    "core_event_high_water": high_water,
                    "workspace_id": scope,
                    "state_asset_ids": required_assets,
                },
            ),
            "subscription_scope": {"workspace_id": scope, "event_types": []},
            "core_snapshot_revision": max(
                (int(item["aggregate_revision"]) for item in covered), default=1
            ),
            "core_event_high_water": high_water,
            "coverage_complete": True,
            "covered_aggregates": covered,
            "created_at": barrier.created_at,
        }
        snapshot["snapshot_hash"] = hash_jcs("core-snapshot/v1", snapshot)
        return CoreSnapshotCapture(
            barrier_token=barrier.token,
            bound_database_sha256=database_sha256,
            bound_core_event_high_water=high_water,
            bound_asset_ids=database_asset_ids,
            bound_workspace_ids=workspace_ids,
            core_contract_version="1.2.0",
            workspace_snapshot_hash=(
                str(snapshot["snapshot_hash"]) if mode == "workspace" else None
            ),
            required_asset_ids=tuple(sorted(required_assets, key=lambda value: value.encode("utf-8"))),
            compatible=True,
            snapshot=snapshot,
        )


def deterministic_package_archive(package: VerifiedPackage) -> bytes:
    """Serialize a verified PackageStore value with a fixed ZIP profile."""

    import io

    verified = verify_package(package)
    members = dict(verified.files)
    members["files.sha256"] = verified.files_sha256
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as archive:
        for name in sorted(members, key=lambda value: value.encode("utf-8")):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (0o100444 & 0xFFFF) << 16
            info.flag_bits = 0x800
            archive.writestr(info, members[name])
    return output.getvalue()


class PackageStoreArchiveAdapter:
    """Thin, deterministic export adapter over PackageStore authority."""

    def __init__(self, store: PackageStore) -> None:
        self.store = store

    def backup_file(
        self,
        *,
        plugin_id: str,
        version: str,
        destination: str | Path,
        bundle_path: str,
        package_hash: str | None = None,
    ) -> PluginBackupFile:
        package = self.store.get(plugin_id, version, package_hash)
        raw = deterministic_package_archive(package)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if target.read_bytes() != raw:
                raise
        return PluginBackupFile(
            path=bundle_path,
            role="package",
            source_path=target,
            sha256=hashlib.sha256(raw).hexdigest(),
            size=len(raw),
            release_id=package.release_id,
            package_hash=package.package_hash,
        )


__all__ = [
    "CoreSnapshotAdapterError",
    "PackageStoreArchiveAdapter",
    "SqliteCoreSnapshotAdapter",
    "SqliteWorkspaceDatabaseProjector",
    "WorkspaceProjectionError",
    "deterministic_package_archive",
]
