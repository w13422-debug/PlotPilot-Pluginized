from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

from backend import plotpilot_plugin_sdk as _sdk_package
from backend.plotpilot_plugin_sdk.canonical import canonical_bytes, hash_jcs

# The repository supports both ``backend.*`` source-tree imports and the
# installed top-level SDK package.  Core's accepted package authority uses the
# installed spelling internally; provide that spelling when running directly
# from the source tree without changing either authority module.
sys.modules.setdefault("plotpilot_plugin_sdk", _sdk_package)

from ..assets import AssetStore
from ..plugins.package import VerifiedPackage, verify_package
from ..plugins.store import PackageStore
from .models import BackupBarrier, BackupMode, CoreSnapshotCapture, PluginBackupFile


class CoreSnapshotAdapterError(RuntimeError):
    """The frozen Core authority cannot be represented as core-snapshot/v1."""


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


class SqliteCoreSnapshotAdapter:
    """Production CoreSnapshot adapter over the accepted SQLite/Asset authorities.

    The durable high-water is supplied by the injected backup barrier.  This
    adapter never allocates or guesses it.  It reads only the already-frozen
    SQLite image and stores deterministic aggregate state through AssetStore.
    """

    def __init__(self, asset_root: str | Path) -> None:
        self.asset_store = AssetStore(asset_root)

    @staticmethod
    def _workspace_state(
        connection: sqlite3.Connection, workspace_id: str
    ) -> tuple[dict[str, object], int]:
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
        tables: dict[str, object] = {"workspace": workspace}
        for table in ("document", "node", "revision", "relation"):
            tables[table] = _rows(
                connection,
                table,
                where='"workspace_id"=?',
                parameters=(workspace_id,),
            )
        # Candidate/publication rows are included through their authoritative
        # revision relationship rather than copied as an unscoped global set.
        tables["candidate"] = _rows(
            connection,
            "candidate",
            where=(
                '"candidate_id" IN (SELECT "source_candidate_id" FROM "revision" '
                'WHERE "workspace_id"=? AND "source_candidate_id" IS NOT NULL)'
            ),
            parameters=(workspace_id,),
        )
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
                state, revision = self._workspace_state(connection, workspace_id)
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
    "deterministic_package_archive",
]
