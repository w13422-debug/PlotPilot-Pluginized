from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from backend.plotpilot_plugin_sdk.canonical import (
    canonical_bytes,
    hash_jcs,
    parse_json_bytes,
)
from backend.plotpilot_plugin_sdk.package import normalize_relative_path
from backend.plotpilot_plugin_sdk.verifier import (
    verify_backup,
    verify_core_snapshot,
    verify_restore_report,
)

from ..assets import AssetMetadata, AssetStore
from ..plugins.package import verify_package
from .adapters import SqliteWorkspaceDatabaseProjector
from .models import (
    BackupBarrier,
    BackupMode,
    BackupRequest,
    BackupResult,
    CoreSnapshotCapture,
    GenerationSnapshot,
    PluginBackupFile,
    PluginDataSnapshot,
    RestoreRequest,
    RestoreResult,
)
from .ports import (
    BackupBarrierPort,
    CoreSnapshotPort,
    GenerationBackupPort,
    PluginDataBackupPort,
)


class BackupDataError(RuntimeError):
    pass


class BackupValidationError(BackupDataError):
    pass


class BackupConflictError(BackupDataError):
    pass


_ASSET_PREFIX = "asset-sha256-"
_BACKUP_MANIFEST = "backup.json"
_BACKUP_RECEIPT = "receipt.json"
_CORE_DATABASE = "core/core.db"
_CORE_SNAPSHOT = "core/snapshot.json"
_STAGE_MARKER = ".plotpilot-stage.json"
_RESTORE_REPORT = ".plotpilot/restore-report.json"
_CORE_CONTRACT_VERSION = "1.2.0"
_HEX = frozenset("0123456789abcdef")
_ASSET_METADATA_FIELDS = {
    "asset_id",
    "sha256",
    "mime",
    "size",
    "logical_role",
    "provenance",
    "rebuildable",
}
_RECEIPT_FIELDS = {
    "schema",
    "backup_id",
    "bundle_hash",
    "manifest_sha256",
    "core_snapshot_hash",
    "core_database_sha256",
    "asset_closure_root",
    "asset_ids",
    "asset_roots",
    "package_bindings",
    "compatibility_evidence",
    "verified_files",
    "created_at",
    "receipt_hash",
}
_COMPATIBILITY_FIELDS = {
    "core_contract_version",
    "core_compatible",
    "generation_compatible",
    "plugin_data_compatible",
    "workspace_snapshot_hash",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in _HEX for ch in value)


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return canonical_bytes(value) + b"\n"


def _read_canonical_json(path: Path) -> dict[str, Any]:
    _assert_no_reparse_components(path)
    if _is_reparse_point(path):
        raise BackupValidationError(f"JSON control file cannot be a reparse point: {path.name}")
    try:
        raw = path.read_bytes()
        value = parse_json_bytes(raw)
    except Exception as exc:
        raise BackupValidationError(f"invalid JSON file: {path.name}") from exc
    if not isinstance(value, dict) or raw != _canonical_json(value):
        raise BackupValidationError(f"JSON file is not deterministic canonical bytes: {path.name}")
    return value


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _lexical_absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _lexical_identity(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(_lexical_absolute(path))))


def _lexically_contained(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_lexical_identity(path), _lexical_identity(root))) == _lexical_identity(root)
    except ValueError:
        return False


def _assert_no_reparse_components(path: str | Path) -> None:
    """Reject every existing lexical component without resolving through it."""

    absolute = _lexical_absolute(path)
    anchor = Path(absolute.anchor)
    current = anchor
    relative_parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in relative_parts:
        current = current / part
        try:
            current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BackupValidationError(f"cannot inspect path boundary: {current}") from exc
        if _is_reparse_point(current):
            raise BackupValidationError(f"reparse point is forbidden in path boundary: {current}")


def _disjoint(left: Path, right: Path) -> bool:
    return not _contained(left, right) and not _contained(right, left)


def _safe_relative_path(value: str) -> str:
    try:
        normalized = normalize_relative_path(value)
    except Exception as exc:
        raise BackupValidationError(f"unsafe bundle path: {value!r}") from exc
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise BackupValidationError(f"unsafe bundle path: {value!r}")
    if pure.parts and (":" in pure.parts[0] or pure.parts[0].startswith("//")):
        raise BackupValidationError(f"unsafe bundle path: {value!r}")
    return normalized


def _join(root: Path, relative: str) -> Path:
    normalized = _safe_relative_path(relative)
    result = root.joinpath(*PurePosixPath(normalized).parts)
    if not _lexically_contained(result, root):
        raise BackupValidationError(f"bundle path escapes root: {relative!r}")
    _assert_no_reparse_components(result)
    return result


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise BackupConflictError(f"refusing to replace existing path: {path}") from exc


def _copy_expected(source: Path, destination: Path, *, digest: str, size: int) -> None:
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise BackupValidationError(f"required source file is unavailable: {source}") from exc
    if len(data) != size or _sha256(data) != digest:
        raise BackupValidationError(f"source file changed or failed hash validation: {source}")
    _write_new(destination, data)


def _sqlite_uri(path: Path, *, immutable: bool = True) -> str:
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    return path.resolve().as_uri() + suffix


def _verify_database(path: Path) -> None:
    try:
        connection = sqlite3.connect(_sqlite_uri(path), uri=True, isolation_level=None)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise BackupValidationError(f"SQLite integrity_check failed for {path.name}")
            connection.execute("PRAGMA foreign_keys=ON")
            violation = connection.execute("PRAGMA foreign_key_check").fetchone()
            if violation is not None:
                raise BackupValidationError(f"SQLite foreign_key_check failed for {path.name}")
        finally:
            connection.close()
    except BackupValidationError:
        raise
    except sqlite3.Error as exc:
        raise BackupValidationError(f"invalid SQLite snapshot: {path.name}") from exc


def _asset_digest(asset_id: str) -> str:
    if not isinstance(asset_id, str) or not asset_id.startswith(_ASSET_PREFIX):
        raise BackupValidationError(f"invalid Asset ID: {asset_id!r}")
    digest = asset_id[len(_ASSET_PREFIX) :]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise BackupValidationError(f"invalid Asset ID: {asset_id!r}")
    return digest


def _sorted_asset_ids(values: Iterable[str], *, label: str) -> tuple[str, ...]:
    original = tuple(values)
    result = tuple(sorted(set(original), key=lambda value: value.encode("utf-8")))
    for asset_id in result:
        _asset_digest(asset_id)
    if original != result:
        raise BackupValidationError(f"{label} Asset IDs must be a sorted unique tuple")
    return result


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & 0x400)


def _walk_asset_values(value: object) -> Iterable[str]:
    if isinstance(value, str):
        if value.startswith(_ASSET_PREFIX):
            _asset_digest(value)
            yield value
        return
    if isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: str(item).encode("utf-8")):
            yield from _walk_asset_values(key)
            yield from _walk_asset_values(value[key])
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_asset_values(item)


class SqliteAssetReferenceScanner:
    """Find Asset IDs only in declared Asset columns and structured JSON."""

    def scan(self, database: Path) -> tuple[str, ...]:
        found: set[str] = set()
        connection = sqlite3.connect(_sqlite_uri(database), uri=True)
        try:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            for table in tables:
                quoted_table = '"' + table.replace('"', '""') + '"'
                columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})")]
                candidates = [
                    column
                    for column in columns
                    if column.lower() == "asset_id"
                    or column.lower().endswith("_asset_id")
                    or column.lower().endswith("_json")
                ]
                if not candidates:
                    continue
                selection = ",".join('"' + column.replace('"', '""') + '"' for column in candidates)
                for row in connection.execute(f"SELECT {selection} FROM {quoted_table}"):
                    for column, raw in zip(candidates, row):
                        if raw is None:
                            continue
                        if not isinstance(raw, str):
                            raise BackupValidationError(f"Asset-bearing column {table}.{column} is not text")
                        if column.lower().endswith("_json"):
                            try:
                                decoded = parse_json_bytes(raw.encode("utf-8"))
                            except Exception as exc:
                                raise BackupValidationError(f"invalid structured JSON in {table}.{column}") from exc
                            found.update(_walk_asset_values(decoded))
                        else:
                            _asset_digest(raw)
                            found.add(raw)
            return tuple(sorted(found, key=lambda value: value.encode("utf-8")))
        finally:
            connection.close()


def _parse_asset_metadata(
    raw: bytes,
    *,
    asset_id: str,
    object_size: int,
) -> AssetMetadata:
    try:
        value = parse_json_bytes(raw)
    except Exception as exc:
        raise BackupValidationError(f"invalid Asset metadata for {asset_id}") from exc
    if not isinstance(value, Mapping) or set(value) != _ASSET_METADATA_FIELDS:
        raise BackupValidationError(f"Asset metadata schema is not closed for {asset_id}")
    if (
        any(type(value[name]) is not str for name in ("asset_id", "sha256", "mime", "logical_role", "provenance"))
        or type(value["size"]) is not int
        or type(value["rebuildable"]) is not bool
    ):
        raise BackupValidationError(f"Asset metadata field type is invalid for {asset_id}")
    try:
        metadata = AssetMetadata(**dict(value))
    except (TypeError, ValueError) as exc:
        raise BackupValidationError(f"Asset metadata shape is invalid for {asset_id}") from exc
    digest = _asset_digest(asset_id)
    if (
        metadata.asset_id != asset_id
        or metadata.sha256 != digest
        or metadata.size != object_size
    ):
        raise BackupValidationError(f"Asset metadata identity mismatch for {asset_id}")
    return metadata


class BackupDataPlane:
    """Single-writer backup and restore-to-new-root staging data plane."""

    def __init__(
        self,
        *,
        source_root: str | Path,
        core_database: str | Path,
        asset_root: str | Path,
        barrier_port: BackupBarrierPort,
        core_snapshot_port: CoreSnapshotPort,
        generation_port: GenerationBackupPort,
        plugin_data_port: PluginDataBackupPort,
        asset_scanner: SqliteAssetReferenceScanner | None = None,
        backup_pages: int = 128,
        backup_sleep: float = 0.01,
        on_backup_progress: Callable[[int, int, int], None] | None = None,
        on_core_snapshot_copied: Callable[[Path], None] | None = None,
        on_restore_stage: Callable[[str, Path], None] | None = None,
    ) -> None:
        _assert_no_reparse_components(source_root)
        _assert_no_reparse_components(core_database)
        _assert_no_reparse_components(asset_root)
        self.source_root = Path(source_root).resolve()
        self.core_database = Path(core_database).resolve()
        self.asset_root = Path(asset_root).resolve()
        if not self.source_root.is_dir():
            raise BackupValidationError("source root does not exist")
        if not self.core_database.is_file() or not _contained(self.core_database, self.source_root):
            raise BackupValidationError("Core database must be an existing file inside source root")
        if not self.asset_root.is_dir() or not _contained(self.asset_root, self.source_root):
            raise BackupValidationError("Asset root must be an existing directory inside source root")
        if (
            barrier_port is None
            or core_snapshot_port is None
            or generation_port is None
            or plugin_data_port is None
        ):
            raise BackupValidationError("barrier, Core snapshot, P2, and P3 ports must be injected")
        if backup_pages < 1 or backup_sleep < 0:
            raise ValueError("invalid SQLite backup tuning")
        self.barrier_port = barrier_port
        self.core_snapshot_port = core_snapshot_port
        self.generation_port = generation_port
        self.plugin_data_port = plugin_data_port
        self.asset_scanner = asset_scanner or SqliteAssetReferenceScanner()
        self.backup_pages = backup_pages
        self.backup_sleep = backup_sleep
        self.on_backup_progress = on_backup_progress
        self.on_core_snapshot_copied = on_core_snapshot_copied
        self.on_restore_stage = on_restore_stage

    @staticmethod
    def _new_stage_path(target: Path, operation: str) -> tuple[Path, str]:
        generation_id = uuid.uuid4().hex
        return (
            target.with_name(f".{operation[:1]}-{generation_id}.stage"),
            generation_id,
        )

    def _online_backup(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_connection = sqlite3.connect(
            _sqlite_uri(source, immutable=False), uri=True, isolation_level=None
        )
        destination_connection = sqlite3.connect(destination, isolation_level=None)
        try:
            source_connection.backup(
                destination_connection,
                pages=self.backup_pages,
                progress=self.on_backup_progress,
                sleep=self.backup_sleep,
            )
        except sqlite3.Error as exc:
            raise BackupValidationError(f"SQLite online backup failed: {source.name}") from exc
        finally:
            destination_connection.close()
            source_connection.close()
        with destination.open("r+b") as stream:
            os.fsync(stream.fileno())
        _verify_database(destination)

    def _stage_marker(
        self,
        *,
        operation: str,
        operation_id: str,
        generation_id: str,
        target: Path,
        binding: str,
        state: str,
    ) -> dict[str, object]:
        if state not in {"staging", "published"}:
            raise ValueError("invalid stage marker state")
        marker: dict[str, object] = {
            "schema": "plotpilot-owned-stage/v2",
            "operation": operation,
            "state": state,
            "operation_id": operation_id,
            "generation_id": generation_id,
            "target_name": target.name,
            "target_parent_id": _lexical_identity(target.parent),
            "binding": binding,
        }
        marker["marker_hash"] = hash_jcs("plotpilot-owned-stage/v2", marker)
        return marker

    def _marker_matches(self, stage: Path, expected: Mapping[str, object]) -> bool:
        try:
            return _read_canonical_json(stage / _STAGE_MARKER) == expected
        except BackupDataError:
            return False

    def _remove_owned_stage(self, stage: Path, expected: Mapping[str, object]) -> None:
        stage = _lexical_absolute(stage)
        if not stage.exists() and not stage.is_symlink():
            return
        target = Path(str(expected.get("target_parent_id", ""))) / str(
            expected.get("target_name", "")
        )
        operation = str(expected.get("operation", ""))
        generation_id = str(expected.get("generation_id", ""))
        expected_stage = target.with_name(f".{operation[:1]}-{generation_id}.stage")
        if (
            _lexical_identity(stage) != _lexical_identity(expected_stage)
            or _lexical_identity(stage) == _lexical_identity(target)
            or _is_reparse_point(stage)
            or _lexical_identity(stage.parent) != str(expected.get("target_parent_id", ""))
            or not self._marker_matches(stage, expected)
        ):
            raise BackupConflictError(f"refusing to remove unowned or altered staging directory: {stage}")
        for entry in stage.rglob("*"):
            if _is_reparse_point(entry):
                raise BackupConflictError(
                    f"refusing to remove staging containing a reparse point: {stage}"
                )
        shutil.rmtree(stage)

    def _prepare_stage(self, stage: Path, marker: Mapping[str, object]) -> None:
        _assert_no_reparse_components(stage)
        if stage.exists():
            raise BackupConflictError(f"unique staging generation already exists: {stage}")
        stage.mkdir(parents=False, exist_ok=False)
        _write_new(stage / _STAGE_MARKER, _canonical_json(marker))

    def _set_stage_state(
        self,
        stage: Path,
        current: Mapping[str, object],
        replacement: Mapping[str, object],
    ) -> None:
        if not self._marker_matches(stage, current):
            raise BackupConflictError("staging marker changed before publication")
        path = stage / _STAGE_MARKER
        temporary = stage / f".{_STAGE_MARKER}.{uuid.uuid4().hex}.tmp"
        _write_new(temporary, _canonical_json(replacement))
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        if not self._marker_matches(stage, replacement):
            raise BackupConflictError("could not bind staging marker for publication")

    def cleanup_restore_staging(
        self,
        stage_root: str | Path,
        *,
        target_root: str | Path,
        restore_id: str,
        bundle_hash: str,
    ) -> None:
        """Explicitly clean one interrupted generation after an exact marker check."""

        stage = _lexical_absolute(stage_root)
        target = _lexical_absolute(target_root)
        _assert_no_reparse_components(stage)
        _assert_no_reparse_components(target)
        marker = _read_canonical_json(stage / _STAGE_MARKER)
        expected = self._stage_marker(
            operation="restore",
            operation_id=restore_id,
            generation_id=str(marker.get("generation_id", "")),
            target=target,
            binding=bundle_hash,
            state=str(marker.get("state", "")),
        )
        self._remove_owned_stage(stage, expected)

    def cleanup_backup_staging(
        self,
        stage_root: str | Path,
        *,
        destination: str | Path,
        backup_id: str,
        backup_epoch: int,
    ) -> None:
        """Explicitly clean one interrupted backup generation after exact binding."""

        stage = _lexical_absolute(stage_root)
        target = _lexical_absolute(destination)
        _assert_no_reparse_components(stage)
        _assert_no_reparse_components(target)
        marker = _read_canonical_json(stage / _STAGE_MARKER)
        expected = self._stage_marker(
            operation="backup",
            operation_id=backup_id,
            generation_id=str(marker.get("generation_id", "")),
            target=target,
            binding=str(backup_epoch),
            state=str(marker.get("state", "")),
        )
        self._remove_owned_stage(stage, expected)

    def _require_publication_marker(
        self,
        root: Path,
        *,
        operation: str,
        operation_id: str,
        target: Path,
        binding: str,
    ) -> dict[str, Any]:
        marker = _read_canonical_json(root / _STAGE_MARKER)
        generation_id = marker.get("generation_id")
        if not isinstance(generation_id, str) or len(generation_id) != 32:
            raise BackupValidationError("publication marker generation is invalid")
        expected = self._stage_marker(
            operation=operation,
            operation_id=operation_id,
            generation_id=generation_id,
            target=target,
            binding=binding,
            state="published",
        )
        if marker != expected:
            raise BackupValidationError("publication marker binding mismatch")
        return marker

    def _copy_asset_closure(
        self, *, root_asset_ids: Iterable[str], stage: Path
    ) -> tuple[list[dict[str, object]], tuple[str, ...], str]:
        store = AssetStore(self.asset_root)
        pending = deque(sorted(set(root_asset_ids), key=lambda value: value.encode("utf-8")))
        seen: set[str] = set()
        files: list[dict[str, object]] = []
        closure: list[dict[str, object]] = []
        while pending:
            asset_id = pending.popleft()
            if asset_id in seen:
                continue
            seen.add(asset_id)
            digest = _asset_digest(asset_id)
            try:
                metadata_path = store.metadata / f"{digest}.json"
                metadata_bytes = metadata_path.read_bytes()
                metadata = _parse_asset_metadata(
                    metadata_bytes,
                    asset_id=asset_id,
                    object_size=len(store.read(asset_id)),
                )
                described = store.describe(asset_id)
                content = store.read(asset_id)
            except (OSError, TypeError, ValueError) as exc:
                raise BackupValidationError(f"required Asset is missing or invalid: {asset_id}") from exc
            if described != metadata or described.sha256 != digest or described.size != len(content):
                raise BackupValidationError(f"Asset metadata does not bind content: {asset_id}")
            object_relative = f"assets/objects/{digest[:2]}/{digest}"
            metadata_relative = f"assets/metadata/{digest}.json"
            _write_new(_join(stage, object_relative), content)
            _write_new(_join(stage, metadata_relative), metadata_bytes)
            metadata_digest = _sha256(metadata_bytes)
            files.extend(
                [
                    {"path": object_relative, "size": len(content), "sha256": digest, "role": "asset"},
                    {
                        "path": metadata_relative,
                        "size": len(metadata_bytes),
                        "sha256": metadata_digest,
                        "role": "asset",
                    },
                ]
            )
            closure.append(
                {
                    "asset_id": asset_id,
                    "sha256": digest,
                    "size": len(content),
                    "metadata_sha256": metadata_digest,
                }
            )
        closure.sort(key=lambda item: str(item["asset_id"]).encode("utf-8"))
        return files, tuple(str(item["asset_id"]) for item in closure), hash_jcs("plotpilot-asset-closure/v1", closure)

    @staticmethod
    def _workspace_rows(database: Path) -> tuple[str, ...]:
        connection = sqlite3.connect(_sqlite_uri(database), uri=True)
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='workspace'"
            ).fetchone()
            if not exists:
                return ()
            return tuple(
                row[0]
                for row in connection.execute(
                    "SELECT workspace_id FROM workspace ORDER BY CAST(workspace_id AS BLOB)"
                )
            )
        finally:
            connection.close()

    @staticmethod
    def _validate_state_bindings(snapshot: Mapping[str, object]) -> tuple[str, ...]:
        covered = snapshot.get("covered_aggregates")
        if not isinstance(covered, list):
            raise BackupValidationError("CoreSnapshot covered aggregates are invalid")
        result: list[str] = []
        for item in covered:
            if not isinstance(item, Mapping):
                raise BackupValidationError("CoreSnapshot aggregate entry is invalid")
            asset_id = item.get("state_asset_id")
            state_hash = item.get("state_hash")
            if not isinstance(asset_id, str) or state_hash != _asset_digest(asset_id):
                raise BackupValidationError(
                    "CoreSnapshot state hash is not bound to its state Asset ID"
                )
            result.append(asset_id)
        return tuple(sorted(set(result), key=lambda value: value.encode("utf-8")))

    @classmethod
    def _validate_snapshot_scope(
        cls,
        *,
        database: Path,
        snapshot: Mapping[str, object],
        mode: object,
        workspace_ids: object,
        workspace_snapshot_hash: object,
    ) -> tuple[str, ...]:
        if not isinstance(workspace_ids, (list, tuple)) or not all(
            isinstance(item, str) for item in workspace_ids
        ):
            raise BackupValidationError("backup workspace IDs are invalid")
        workspace_tuple = tuple(workspace_ids)
        if workspace_tuple != tuple(
            sorted(set(workspace_tuple), key=lambda value: value.encode("utf-8"))
        ):
            raise BackupValidationError("backup workspace IDs are not a sorted set")
        database_workspaces = cls._workspace_rows(database)
        if database_workspaces != workspace_tuple:
            raise BackupValidationError(
                "manifest workspace IDs differ from frozen Core database authority"
            )
        scope = snapshot.get("subscription_scope")
        if not isinstance(scope, Mapping):
            raise BackupValidationError("CoreSnapshot subscription scope is invalid")
        scoped_workspace = scope.get("workspace_id")
        snapshot_hash = snapshot.get("snapshot_hash")
        if mode == "workspace":
            if (
                len(database_workspaces) != 1
                or scoped_workspace != database_workspaces[0]
                or workspace_snapshot_hash != snapshot_hash
            ):
                raise BackupValidationError(
                    "workspace backup scope does not match its frozen Core database"
                )
        elif mode in {"full", "data"}:
            if scoped_workspace is not None or workspace_snapshot_hash is not None:
                raise BackupValidationError("unscoped backup claims a workspace CoreSnapshot")
        else:
            raise BackupValidationError("backup mode is invalid")
        return database_workspaces

    def _capture_core_snapshot(
        self,
        *,
        barrier: BackupBarrier,
        stage: Path,
        database_digest: str,
        mode: BackupMode,
        workspace_ids: tuple[str, ...],
        database_asset_ids: tuple[str, ...],
    ) -> tuple[CoreSnapshotCapture, dict[str, object], bytes]:
        try:
            capture = self.core_snapshot_port.capture_for_backup(
                barrier=barrier,
                core_database=_join(stage, _CORE_DATABASE),
                database_sha256=database_digest,
                database_asset_ids=database_asset_ids,
                mode=mode,
                workspace_ids=workspace_ids,
            )
        except BackupDataError:
            raise
        except Exception as exc:
            raise BackupValidationError(
                "Core snapshot authority could not capture the frozen database"
            ) from exc
        if (
            not isinstance(capture, CoreSnapshotCapture)
            or capture.barrier_token != barrier.token
            or capture.bound_database_sha256 != database_digest
            or capture.bound_core_event_high_water != barrier.core_event_high_water
            or capture.bound_asset_ids != database_asset_ids
            or capture.bound_workspace_ids != workspace_ids
            or capture.core_contract_version != _CORE_CONTRACT_VERSION
            or capture.compatible is not True
        ):
            raise BackupValidationError(
                "Core snapshot is not bound to the barrier, database, workspaces, and compatibility authority"
            )
        _sorted_asset_ids(capture.required_asset_ids, label="CoreSnapshotPort required")
        snapshot = dict(capture.snapshot)
        try:
            verify_core_snapshot(snapshot)
        except Exception as exc:
            raise BackupValidationError("CoreSnapshotPort returned an invalid core-snapshot/v1") from exc
        if snapshot.get("core_event_high_water") != barrier.core_event_high_water:
            raise BackupValidationError("Core snapshot high-water differs from the durable barrier")
        self._validate_state_bindings(snapshot)
        self._validate_snapshot_scope(
            database=_join(stage, _CORE_DATABASE),
            snapshot=snapshot,
            mode=mode,
            workspace_ids=workspace_ids,
            workspace_snapshot_hash=capture.workspace_snapshot_hash,
        )
        if snapshot["created_at"] != barrier.created_at:
            raise BackupValidationError("Core snapshot timestamp is not bound to the backup barrier")
        raw = _canonical_json(snapshot)
        _write_new(_join(stage, _CORE_SNAPSHOT), raw)
        return capture, snapshot, raw

    @staticmethod
    def _bound_generation(
        value: GenerationSnapshot, barrier: BackupBarrier, core_hash: str
    ) -> GenerationSnapshot:
        if (
            not isinstance(value, GenerationSnapshot)
            or value.barrier_token != barrier.token
            or value.bound_core_snapshot_hash != core_hash
            or value.compatible is not True
        ):
            raise BackupValidationError(
                "P2 generation snapshot is not bound to the Core snapshot or is incompatible"
            )
        _sorted_asset_ids(value.asset_ids, label="P2 declared")
        return value

    @staticmethod
    def _bound_plugin_data(
        value: PluginDataSnapshot, barrier: BackupBarrier, core_hash: str
    ) -> PluginDataSnapshot:
        if (
            not isinstance(value, PluginDataSnapshot)
            or value.barrier_token != barrier.token
            or value.bound_core_snapshot_hash != core_hash
            or value.compatible is not True
        ):
            raise BackupValidationError(
                "P3 plugin-data snapshot is not bound to the Core snapshot or is incompatible"
            )
        _sorted_asset_ids(value.asset_ids, label="P3 declared")
        return value

    def _copy_contributor_files(
        self,
        files: Iterable[PluginBackupFile],
        stage: Path,
        occupied: set[str],
        *,
        contributor: str,
        mode: BackupMode,
    ) -> tuple[list[dict[str, object]], list[dict[str, str]], tuple[Path, ...]]:
        result: list[dict[str, object]] = []
        package_bindings: list[dict[str, str]] = []
        plugin_databases: list[Path] = []
        for item in files:
            if not isinstance(item, PluginBackupFile):
                raise BackupValidationError(f"{contributor} returned an invalid file descriptor")
            relative = _safe_relative_path(item.path)
            if relative in occupied or relative in {_BACKUP_MANIFEST, _BACKUP_RECEIPT, _STAGE_MARKER, _RESTORE_REPORT}:
                raise BackupValidationError(f"duplicate or reserved backup path: {relative}")
            if contributor == "P2" and item.role != "package":
                raise BackupValidationError("P2 may contribute only package files")
            if contributor == "P3" and item.role not in {"plugin_db", "metadata"}:
                raise BackupValidationError("P3 may contribute only plugin_db or metadata files")
            if mode == "workspace" and item.role in {"package", "plugin_db"}:
                raise BackupValidationError("workspace backup cannot contain package or plugin DB files")
            if mode == "data" and item.role == "package":
                raise BackupValidationError("data backup cannot contain package files")
            if item.role == "package":
                if (
                    item.release_id is None
                    or item.package_hash is None
                ):
                    raise BackupValidationError(
                        "package descriptor must bind release_id and semantic package_hash"
                    )
                destination = _join(stage, relative)
                _copy_expected(
                    Path(item.source_path), destination, digest=item.sha256, size=item.size
                )
                try:
                    verified = verify_package(
                        destination,
                        expected_package_hash=item.package_hash,
                        expected_release_id=item.release_id,
                    )
                except Exception as exc:
                    raise BackupValidationError(
                        "package archive does not match PackageStore/SDK identity"
                    ) from exc
                package_bindings.append(
                    {
                        "plugin_id": verified.plugin_id,
                        "release_id": item.release_id,
                        "package_hash": item.package_hash,
                        "path": relative,
                        "archive_sha256": item.sha256,
                    }
                )
            elif item.release_id is not None or item.package_hash is not None:
                raise BackupValidationError("non-package file cannot claim a package identity")
            occupied.add(relative)
            if item.role != "package":
                _copy_expected(
                    Path(item.source_path),
                    _join(stage, relative),
                    digest=item.sha256,
                    size=item.size,
                )
            if item.role == "plugin_db":
                _verify_database(_join(stage, relative))
                plugin_databases.append(_join(stage, relative))
            result.append({"path": relative, "size": item.size, "sha256": item.sha256, "role": item.role})
        package_bindings.sort(
            key=lambda item: (
                item["plugin_id"].encode("utf-8"), item["release_id"].encode("utf-8")
            )
        )
        return result, package_bindings, tuple(plugin_databases)

    @staticmethod
    def _validate_release_packages(
        *,
        mode: BackupMode,
        plugin_releases: list[dict[str, object]],
        package_bindings: list[dict[str, str]],
    ) -> None:
        releases: dict[str, dict[str, object]] = {}
        for release in plugin_releases:
            release_id = release.get("release_id")
            if not isinstance(release_id, str) or release_id in releases:
                raise BackupValidationError("plugin releases must have unique release IDs")
            releases[release_id] = release
        bindings: dict[str, dict[str, str]] = {}
        for binding in package_bindings:
            release_id = binding["release_id"]
            if release_id in bindings:
                raise BackupValidationError("a release cannot have multiple package files")
            bindings[release_id] = binding
        if mode != "full" and bindings:
            raise BackupValidationError("only full backups may contain package files")
        for release_id, release in releases.items():
            present = release.get("package_present")
            if not isinstance(present, bool):
                raise BackupValidationError("plugin release package_present must be boolean")
            binding = bindings.get(release_id)
            if mode != "full" and present:
                raise BackupValidationError("non-full backup cannot claim a present package")
            if present:
                if (
                    binding is None
                    or binding["package_hash"] != release.get("package_hash")
                    or binding["plugin_id"] != release.get("plugin_id")
                ):
                    raise BackupValidationError(
                        f"release package is missing or has the wrong hash: {release_id}"
                    )
            elif binding is not None:
                raise BackupValidationError(f"package file is orphaned from release: {release_id}")
        orphaned = set(bindings) - set(releases)
        if orphaned:
            raise BackupValidationError("package file references an unknown release")

    def _populate_backup_stage(
        self,
        *,
        stage: Path,
        request: BackupRequest,
        workspace_ids: tuple[str, ...],
        barrier: BackupBarrier,
    ) -> tuple[dict[str, object], dict[str, object]]:
        if (
            not isinstance(barrier, BackupBarrier)
            or barrier.backup_epoch != request.backup_epoch
            or type(barrier.core_event_high_water) is not int
            or barrier.core_event_high_water < 0
            or barrier.created_at != request.created_at
        ):
            raise BackupValidationError("backup barrier did not bind the requested durable epoch")
        database_target = _join(stage, _CORE_DATABASE)
        if request.mode == "workspace":
            if len(workspace_ids) != 1:
                raise BackupValidationError("workspace backup requires exactly one selected workspace")
            frozen_database = _join(stage, "core/.workspace-source.db")
            self._online_backup(self.core_database, frozen_database)
            if self.on_core_snapshot_copied is not None:
                self.on_core_snapshot_copied(frozen_database)
            try:
                SqliteWorkspaceDatabaseProjector().project(
                    frozen_database=frozen_database,
                    destination=database_target,
                    workspace_id=workspace_ids[0],
                )
            except Exception as exc:
                raise BackupValidationError(
                    "frozen Core authority cannot be safely scoped to the selected workspace"
                ) from exc
            finally:
                frozen_database.unlink(missing_ok=True)
            _verify_database(database_target)
        else:
            self._online_backup(self.core_database, database_target)
            if self.on_core_snapshot_copied is not None:
                self.on_core_snapshot_copied(database_target)
        database_bytes = database_target.read_bytes()
        database_digest = _sha256(database_bytes)
        database_workspace_ids = self._workspace_rows(database_target)
        if database_workspace_ids != workspace_ids:
            raise BackupValidationError(
                "workspace IDs must exactly describe the workspaces present in the Core snapshot"
            )
        database_asset_ids = self.asset_scanner.scan(database_target)
        core_capture, core_snapshot, core_snapshot_raw = self._capture_core_snapshot(
            barrier=barrier,
            stage=stage,
            database_digest=database_digest,
            mode=request.mode,
            workspace_ids=workspace_ids,
            database_asset_ids=database_asset_ids,
        )
        core_hash = str(core_snapshot["snapshot_hash"])
        generation = self._bound_generation(
            self.generation_port.capture_for_backup(
                barrier=barrier,
                core_database=database_target,
                core_snapshot_hash=core_hash,
                mode=request.mode,
                workspace_ids=workspace_ids,
            ),
            barrier,
            core_hash,
        )
        plugin_data = self._bound_plugin_data(
            self.plugin_data_port.capture_for_backup(
                barrier=barrier,
                core_database=database_target,
                core_snapshot_hash=core_hash,
                mode=request.mode,
                workspace_ids=workspace_ids,
            ),
            barrier,
            core_hash,
        )
        if request.mode == "workspace" and (
            generation.asset_ids
            or generation.files
            or plugin_data.asset_ids
            or plugin_data.files
        ):
            raise BackupValidationError(
                "workspace backup cannot contain P2/P3 files or declared Asset roots"
            )
        plugin_releases = sorted(
            (dict(item) for item in generation.plugin_releases),
            key=lambda item: (
                str(item.get("plugin_id", "")).encode("utf-8"),
                str(item.get("release_id", "")).encode("utf-8"),
            ),
        )
        projections = sorted(
            (dict(item) for item in generation.projection_rebuild_required),
            key=lambda item: (
                str(item.get("plugin_id", "")).encode("utf-8"),
                str(item.get("release_id", "")).encode("utf-8"),
            ),
        )
        release_keys = {
            (str(item.get("plugin_id", "")), str(item.get("release_id", "")))
            for item in plugin_releases
        }
        projection_keys = [
            (str(item.get("plugin_id", "")), str(item.get("release_id", "")))
            for item in projections
        ]
        if len(set(projection_keys)) != len(projection_keys) or any(
            key not in release_keys for key in projection_keys
        ):
            raise BackupValidationError(
                "projection rebuild declarations must uniquely reference captured plugin releases"
            )
        files: list[dict[str, object]] = [
            {
                "path": _CORE_DATABASE,
                "size": len(database_bytes),
                "sha256": database_digest,
                "role": "core_db",
            },
            {
                "path": _CORE_SNAPSHOT,
                "size": len(core_snapshot_raw),
                "sha256": _sha256(core_snapshot_raw),
                "role": "metadata",
            },
        ]
        occupied = {str(item["path"]) for item in files}
        p2_files, package_bindings, p2_databases = self._copy_contributor_files(
            generation.files,
            stage,
            occupied,
            contributor="P2",
            mode=request.mode,
        )
        if p2_databases:
            raise BackupValidationError("P2 package authority cannot contribute plugin databases")
        p3_files, p3_package_bindings, plugin_databases = self._copy_contributor_files(
            plugin_data.files,
            stage,
            occupied,
            contributor="P3",
            mode=request.mode,
        )
        if p3_package_bindings:
            raise BackupValidationError("P3 cannot contribute package bindings")
        self._validate_release_packages(
            mode=request.mode,
            plugin_releases=plugin_releases,
            package_bindings=package_bindings,
        )
        plugin_database_asset_ids = tuple(
            sorted(
                {
                    asset_id
                    for plugin_database in plugin_databases
                    for asset_id in self.asset_scanner.scan(plugin_database)
                },
                key=lambda value: value.encode("utf-8"),
            )
        )
        core_snapshot_asset_ids = tuple(
            sorted(
                {
                    str(item["state_asset_id"])
                    for item in core_snapshot["covered_aggregates"]  # type: ignore[index]
                },
                key=lambda value: value.encode("utf-8"),
            )
        )
        for asset_id in core_snapshot_asset_ids:
            _asset_digest(asset_id)
        asset_roots = {
            "core_database": list(database_asset_ids),
            "core_snapshot": list(core_snapshot_asset_ids),
            "core_declared": list(core_capture.required_asset_ids),
            "generation_declared": list(generation.asset_ids),
            "plugin_declared": list(plugin_data.asset_ids),
            "plugin_databases": list(plugin_database_asset_ids),
        }
        root_asset_ids = {
            asset_id
            for values in asset_roots.values()
            for asset_id in values
        }
        asset_files, asset_ids, asset_root = self._copy_asset_closure(
            root_asset_ids=root_asset_ids,
            stage=stage,
        )
        files.extend((*p2_files, *p3_files, *asset_files))
        files.sort(key=lambda item: str(item["path"]).encode("utf-8"))
        workspace_hash = core_capture.workspace_snapshot_hash
        manifest: dict[str, object] = {
            "schema": "backup-bundle/v1",
            "backup_id": request.backup_id,
            "library_root_id": request.library_root_id,
            "backup_epoch": request.backup_epoch,
            "mode": request.mode,
            "workspace_ids": list(workspace_ids),
            "core_contract_version": request.core_contract_version,
            "core_snapshot_hash": core_hash,
            "workspace_snapshot_hash": workspace_hash,
            "current_generation_id": generation.current_generation_id,
            "lkg_generation_id": generation.lkg_generation_id,
            "asset_closure_root": asset_root,
            "plugin_releases": plugin_releases,
            "projection_rebuild_required": projections,
            "files": files,
            "created_at": request.created_at,
            "verification": {
                "databases_valid": True,
                "assets_valid": True,
                "files_valid": True,
                "compatible": (
                    core_capture.compatible
                    and generation.compatible
                    and plugin_data.compatible
                    and request.core_contract_version == _CORE_CONTRACT_VERSION
                ),
                "verified_at": request.verified_at,
            },
        }
        manifest["bundle_hash"] = hash_jcs("plotpilot-backup/v1", manifest)
        try:
            verify_backup(manifest)
        except Exception as exc:
            raise BackupValidationError(
                "constructed backup manifest violates the accepted contract"
            ) from exc
        manifest_raw = _canonical_json(manifest)
        _write_new(stage / _BACKUP_MANIFEST, manifest_raw)
        receipt: dict[str, object] = {
            "schema": "plotpilot-backup-receipt/v1",
            "backup_id": request.backup_id,
            "bundle_hash": manifest["bundle_hash"],
            "manifest_sha256": _sha256(manifest_raw),
            "core_snapshot_hash": core_hash,
            "core_database_sha256": database_digest,
            "asset_closure_root": asset_root,
            "asset_ids": list(asset_ids),
            "asset_roots": asset_roots,
            "package_bindings": package_bindings,
            "compatibility_evidence": {
                "core_contract_version": core_capture.core_contract_version,
                "core_compatible": core_capture.compatible,
                "generation_compatible": generation.compatible,
                "plugin_data_compatible": plugin_data.compatible,
                "workspace_snapshot_hash": workspace_hash,
            },
            "verified_files": [item["path"] for item in files],
            "created_at": request.created_at,
        }
        receipt["receipt_hash"] = hash_jcs("plotpilot-backup-receipt/v1", receipt)
        _write_new(stage / _BACKUP_RECEIPT, _canonical_json(receipt))
        return manifest, receipt

    def create_backup(self, destination: str | Path, request: BackupRequest) -> BackupResult:
        if not isinstance(request, BackupRequest):
            raise TypeError("request must be BackupRequest")
        if request.core_contract_version != _CORE_CONTRACT_VERSION:
            raise BackupValidationError(
                f"unsupported Core contract version: {request.core_contract_version}"
            )
        _assert_no_reparse_components(destination)
        destination = _lexical_absolute(destination)
        if not _disjoint(destination, self.source_root):
            raise BackupValidationError("backup destination must be disjoint from the live source root")
        workspace_ids = tuple(sorted(set(request.workspace_ids), key=lambda value: value.encode("utf-8")))
        if workspace_ids != request.workspace_ids:
            raise BackupValidationError("workspace IDs must be a sorted unique tuple")
        if destination.exists():
            try:
                existing = self.verify_backup(destination)
            except BackupDataError as exc:
                raise BackupConflictError("backup destination already exists and is not reusable") from exc
            manifest = existing.manifest
            verification = manifest.get("verification")
            expected = (
                manifest.get("backup_id") == request.backup_id
                and manifest.get("library_root_id") == request.library_root_id
                and manifest.get("backup_epoch") == request.backup_epoch
                and manifest.get("mode") == request.mode
                and manifest.get("workspace_ids") == list(request.workspace_ids)
                and manifest.get("core_contract_version") == request.core_contract_version
                and manifest.get("created_at") == request.created_at
                and isinstance(verification, Mapping)
                and verification.get("verified_at") == request.verified_at
            )
            if not expected:
                raise BackupConflictError("backup destination belongs to a different request")
            return existing
        stage, generation_id = self._new_stage_path(destination, "backup")
        marker = self._stage_marker(
            operation="backup",
            operation_id=request.backup_id,
            generation_id=generation_id,
            target=destination,
            binding=str(request.backup_epoch),
            state="staging",
        )
        self._prepare_stage(stage, marker)
        try:
            with self.barrier_port.hold_for_backup(
                backup_epoch=request.backup_epoch,
                created_at=request.created_at,
                mode=request.mode,
                workspace_ids=workspace_ids,
            ) as barrier:
                manifest, receipt = self._populate_backup_stage(
                    stage=stage,
                    request=request,
                    workspace_ids=workspace_ids,
                    barrier=barrier,
                )
            self._verify_bundle(stage, allowed_extras={_STAGE_MARKER})
            published_marker = self._stage_marker(
                operation="backup",
                operation_id=request.backup_id,
                generation_id=generation_id,
                target=destination,
                binding=str(request.backup_epoch),
                state="published",
            )
            self._set_stage_state(stage, marker, published_marker)
            marker = published_marker
            self._require_publication_marker(
                stage,
                operation="backup",
                operation_id=request.backup_id,
                target=destination,
                binding=str(request.backup_epoch),
            )
            _assert_no_reparse_components(destination)
            try:
                stage.rename(destination)
            except OSError as exc:
                raise BackupConflictError("could not publish backup without replacement") from exc
            self._require_publication_marker(
                destination,
                operation="backup",
                operation_id=request.backup_id,
                target=destination,
                binding=str(request.backup_epoch),
            )
            return BackupResult(destination, manifest, receipt)
        except Exception:
            self._remove_owned_stage(stage, marker)
            raise

    @staticmethod
    def _verify_receipt(receipt: Mapping[str, object], manifest: Mapping[str, object], manifest_raw: bytes) -> None:
        if set(receipt) != _RECEIPT_FIELDS:
            raise BackupValidationError("backup receipt schema is not closed")
        evidence = receipt.get("compatibility_evidence")
        if not isinstance(evidence, Mapping) or set(evidence) != _COMPATIBILITY_FIELDS:
            raise BackupValidationError("backup compatibility evidence schema is not closed")
        if (
            not all(
                _is_hex64(receipt.get(name))
                for name in (
                    "bundle_hash",
                    "manifest_sha256",
                    "core_snapshot_hash",
                    "core_database_sha256",
                    "asset_closure_root",
                    "receipt_hash",
                )
            )
            or not isinstance(receipt.get("backup_id"), str)
            or not isinstance(receipt.get("asset_ids"), list)
            or not all(isinstance(item, str) for item in receipt.get("asset_ids", []))  # type: ignore[union-attr]
            or not isinstance(receipt.get("asset_roots"), Mapping)
            or not isinstance(receipt.get("package_bindings"), list)
            or not isinstance(receipt.get("verified_files"), list)
            or not all(isinstance(item, str) for item in receipt.get("verified_files", []))  # type: ignore[union-attr]
            or not isinstance(receipt.get("created_at"), str)
        ):
            raise BackupValidationError("backup receipt field type is invalid")
        unsigned = dict(receipt)
        receipt_hash = unsigned.pop("receipt_hash", None)
        if receipt_hash != hash_jcs("plotpilot-backup-receipt/v1", unsigned):
            raise BackupValidationError("backup receipt hash mismatch")
        if (
            receipt.get("schema") != "plotpilot-backup-receipt/v1"
            or receipt.get("backup_id") != manifest.get("backup_id")
            or receipt.get("bundle_hash") != manifest.get("bundle_hash")
            or receipt.get("manifest_sha256") != _sha256(manifest_raw)
            or receipt.get("core_snapshot_hash") != manifest.get("core_snapshot_hash")
            or receipt.get("asset_closure_root") != manifest.get("asset_closure_root")
            or receipt.get("created_at") != manifest.get("created_at")
            or receipt.get("verified_files")
            != [item["path"] for item in manifest.get("files", [])]  # type: ignore[index]
        ):
            raise BackupValidationError("backup receipt is not bound to manifest")
        if (
            evidence.get("core_contract_version") != _CORE_CONTRACT_VERSION
            or evidence.get("core_contract_version") != manifest.get("core_contract_version")
            or evidence.get("workspace_snapshot_hash") != manifest.get("workspace_snapshot_hash")
            or evidence.get("core_compatible") is not True
            or evidence.get("generation_compatible") is not True
            or evidence.get("plugin_data_compatible") is not True
            or manifest.get("verification", {}).get("compatible") is not True  # type: ignore[union-attr]
        ):
            raise BackupValidationError("backup compatibility evidence is missing or inconsistent")

    @classmethod
    def _verify_package_bindings(
        cls, root: Path, manifest: Mapping[str, object], receipt: Mapping[str, object]
    ) -> None:
        releases = [dict(item) for item in manifest.get("plugin_releases", [])]  # type: ignore[arg-type]
        raw_bindings = receipt.get("package_bindings")
        if not isinstance(raw_bindings, list):
            raise BackupValidationError("backup receipt package bindings are invalid")
        bindings: list[dict[str, str]] = []
        file_map = {str(item["path"]): item for item in manifest.get("files", [])}  # type: ignore[index]
        for raw in raw_bindings:
            fields = {
                "plugin_id",
                "release_id",
                "package_hash",
                "path",
                "archive_sha256",
            }
            if not isinstance(raw, Mapping) or set(raw) != fields:
                raise BackupValidationError("backup receipt package binding is invalid")
            if not all(isinstance(raw[key], str) for key in fields):
                raise BackupValidationError("backup receipt package binding type is invalid")
            binding = {key: str(raw[key]) for key in fields}
            item = file_map.get(binding["path"])
            if (
                item is None
                or item.get("role") != "package"
                or item.get("sha256") != binding["archive_sha256"]
                or not _is_hex64(binding["package_hash"])
                or not _is_hex64(binding["release_id"])
            ):
                raise BackupValidationError("package binding does not match a package file")
            try:
                verified = verify_package(
                    _join(root, binding["path"]),
                    expected_package_hash=binding["package_hash"],
                    expected_release_id=binding["release_id"],
                )
            except Exception as exc:
                raise BackupValidationError("packaged archive failed semantic verification") from exc
            if verified.plugin_id != binding["plugin_id"]:
                raise BackupValidationError("package plugin identity does not match receipt")
            bindings.append(binding)
        expected_order = sorted(
            bindings,
            key=lambda item: (
                item["plugin_id"].encode("utf-8"), item["release_id"].encode("utf-8")
            ),
        )
        if bindings != expected_order:
            raise BackupValidationError("package bindings are not deterministically sorted")
        cls._validate_release_packages(
            mode=str(manifest.get("mode")),  # type: ignore[arg-type]
            plugin_releases=releases,
            package_bindings=bindings,
        )
        bound_paths = {item["path"] for item in bindings}
        package_paths = {
            str(item["path"])
            for item in manifest.get("files", [])  # type: ignore[union-attr]
            if item.get("role") == "package"
        }
        if bound_paths != package_paths:
            raise BackupValidationError("package files and release bindings are not a bijection")

    def _verify_asset_closure(
        self, root: Path, manifest: Mapping[str, object], receipt: Mapping[str, object]
    ) -> None:
        file_map = {str(item["path"]): item for item in manifest["files"]}  # type: ignore[index]
        packaged: dict[str, dict[str, object]] = {}
        for path, item in file_map.items():
            if item["role"] != "asset":
                continue
            pure = PurePosixPath(path)
            if len(pure.parts) == 4 and pure.parts[:2] == ("assets", "objects"):
                digest = pure.parts[-1]
                if pure.parts[-2] != digest[:2]:
                    raise BackupValidationError("Asset object path does not match digest")
                packaged.setdefault(digest, {})["object"] = item
            elif len(pure.parts) == 3 and pure.parts[:2] == ("assets", "metadata") and pure.name.endswith(".json"):
                packaged.setdefault(pure.name[:-5], {})["metadata"] = item
            else:
                raise BackupValidationError(f"unexpected Asset closure path: {path}")
        raw_roots = receipt.get("asset_roots")
        root_names = {
            "core_database",
            "core_snapshot",
            "core_declared",
            "generation_declared",
            "plugin_declared",
            "plugin_databases",
        }
        if not isinstance(raw_roots, Mapping) or set(raw_roots) != root_names:
            raise BackupValidationError("backup receipt Asset roots are incomplete")
        asset_roots: dict[str, tuple[str, ...]] = {}
        for name in sorted(root_names):
            value = raw_roots[name]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise BackupValidationError(f"backup receipt {name} Asset roots are invalid")
            asset_roots[name] = _sorted_asset_ids(value, label=f"receipt {name}")
        if asset_roots["core_database"] != self.asset_scanner.scan(_join(root, _CORE_DATABASE)):
            raise BackupValidationError("Core database Asset roots do not match the frozen database")
        snapshot = _read_canonical_json(_join(root, _CORE_SNAPSHOT))
        snapshot_assets = tuple(
            sorted(
                {
                    str(item["state_asset_id"])
                    for item in snapshot["covered_aggregates"]
                },
                key=lambda value: value.encode("utf-8"),
            )
        )
        for asset_id in snapshot_assets:
            _asset_digest(asset_id)
        if asset_roots["core_snapshot"] != snapshot_assets:
            raise BackupValidationError("CoreSnapshot state Assets do not match receipt roots")
        plugin_database_assets = tuple(
            sorted(
                {
                    asset_id
                    for item in manifest["files"]  # type: ignore[index]
                    if item["role"] == "plugin_db"
                    for asset_id in self.asset_scanner.scan(_join(root, str(item["path"])))
                },
                key=lambda value: value.encode("utf-8"),
            )
        )
        if asset_roots["plugin_databases"] != plugin_database_assets:
            raise BackupValidationError("plugin database Asset roots do not match frozen files")
        reachable = {
            asset_id for values in asset_roots.values() for asset_id in values
        }
        pending = deque(sorted(reachable))
        closure: list[dict[str, object]] = []
        visited: set[str] = set()
        while pending:
            asset_id = pending.popleft()
            if asset_id in visited:
                continue
            visited.add(asset_id)
            digest = _asset_digest(asset_id)
            pair = packaged.get(digest)
            if pair is None or set(pair) != {"object", "metadata"}:
                raise BackupValidationError(f"Asset closure is missing files for {asset_id}")
            object_item = pair["object"]
            metadata_item = pair["metadata"]
            if object_item["sha256"] != digest:
                raise BackupValidationError(
                    f"Asset object hash is not bound to its content-addressed ID: {asset_id}"
                )
            metadata_path = _join(root, str(metadata_item["path"]))
            try:
                metadata_raw = metadata_path.read_bytes()
                metadata = _parse_asset_metadata(
                    metadata_raw,
                    asset_id=asset_id,
                    object_size=int(object_item["size"]),
                )
            except (OSError, TypeError, ValueError) as exc:
                raise BackupValidationError(f"invalid Asset metadata for {asset_id}") from exc
            if (
                metadata.asset_id != asset_id
                or metadata.sha256 != digest
                or metadata.size != object_item["size"]
            ):
                raise BackupValidationError(f"Asset metadata identity mismatch for {asset_id}")
            closure.append(
                {
                    "asset_id": asset_id,
                    "sha256": digest,
                    "size": object_item["size"],
                    "metadata_sha256": metadata_item["sha256"],
                }
            )
        if set(packaged) != {_asset_digest(asset_id) for asset_id in visited}:
            raise BackupValidationError("backup contains Assets outside the exact reachable closure")
        closure.sort(key=lambda item: str(item["asset_id"]).encode("utf-8"))
        if hash_jcs("plotpilot-asset-closure/v1", closure) != manifest["asset_closure_root"]:
            raise BackupValidationError("Asset closure root mismatch")
        if list(receipt.get("asset_ids", [])) != [item["asset_id"] for item in closure]:
            raise BackupValidationError("backup receipt Asset IDs do not match closure")

    def _verify_bundle(
        self, root: Path, *, allowed_extras: set[str] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        _assert_no_reparse_components(root)
        if _is_reparse_point(root) or not root.is_dir():
            raise BackupValidationError("backup root does not exist")
        manifest_path = root / _BACKUP_MANIFEST
        receipt_path = root / _BACKUP_RECEIPT
        manifest = _read_canonical_json(manifest_path)
        manifest_raw = _canonical_json(manifest)
        receipt = _read_canonical_json(receipt_path)
        try:
            verify_backup(manifest)
        except Exception as exc:
            raise BackupValidationError("backup manifest verification failed") from exc
        self._verify_receipt(receipt, manifest, manifest_raw)
        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise BackupValidationError("backup manifest files are invalid")
        expected = {_BACKUP_MANIFEST, _BACKUP_RECEIPT, *(str(item["path"]) for item in entries)}
        expected.update({_STAGE_MARKER, *(allowed_extras or set())})
        walked = list(root.rglob("*"))
        for path in walked:
            if _is_reparse_point(path):
                raise BackupValidationError(
                    f"backup tree cannot contain a reparse point: {path.relative_to(root)}"
                )
        actual = {path.relative_to(root).as_posix() for path in walked if path.is_file()}
        if actual != expected:
            raise BackupValidationError("backup file set differs from manifest closure")
        seen: set[str] = set()
        for item in entries:
            relative = _safe_relative_path(str(item["path"]))
            if relative in seen:
                raise BackupValidationError("duplicate backup file path")
            seen.add(relative)
            path = _join(root, relative)
            if _is_reparse_point(path):
                raise BackupValidationError(f"backup file cannot be a reparse point: {relative}")
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise BackupValidationError(f"backup file is missing: {relative}") from exc
            if len(raw) != item["size"] or _sha256(raw) != item["sha256"]:
                raise BackupValidationError(f"backup file hash mismatch: {relative}")
            if item["role"] in {"core_db", "plugin_db"}:
                _verify_database(path)
        snapshot = _read_canonical_json(_join(root, _CORE_SNAPSHOT))
        try:
            verify_core_snapshot(snapshot)
        except Exception as exc:
            raise BackupValidationError("Core snapshot verification failed") from exc
        snapshot_hash = snapshot["snapshot_hash"]
        if snapshot_hash != manifest["core_snapshot_hash"]:
            raise BackupValidationError("manifest is not bound to Core snapshot")
        if manifest.get("core_contract_version") != _CORE_CONTRACT_VERSION:
            raise BackupValidationError("backup uses an unsupported Core contract version")
        self._validate_state_bindings(snapshot)
        self._validate_snapshot_scope(
            database=_join(root, _CORE_DATABASE),
            snapshot=snapshot,
            mode=manifest.get("mode"),
            workspace_ids=manifest.get("workspace_ids"),
            workspace_snapshot_hash=manifest.get("workspace_snapshot_hash"),
        )
        core_database_entries = [item for item in entries if item.get("role") == "core_db"]
        if len(core_database_entries) != 1 or core_database_entries[0].get("path") != _CORE_DATABASE:
            raise BackupValidationError("backup must contain exactly one authoritative Core database")
        database_item = next((item for item in entries if item["path"] == _CORE_DATABASE), None)
        if (
            database_item is None
            or receipt.get("core_database_sha256") != database_item["sha256"]
        ):
            raise BackupValidationError("Core snapshot receipt database binding mismatch")
        self._verify_package_bindings(root, manifest, receipt)
        self._verify_asset_closure(root, manifest, receipt)
        return manifest, receipt

    def verify_backup(self, bundle_root: str | Path) -> BackupResult:
        _assert_no_reparse_components(bundle_root)
        root = _lexical_absolute(bundle_root)
        manifest, receipt = self._verify_bundle(root)
        self._require_publication_marker(
            root,
            operation="backup",
            operation_id=str(manifest["backup_id"]),
            target=root,
            binding=str(manifest["backup_epoch"]),
        )
        return BackupResult(root, manifest, receipt)

    @staticmethod
    def _restore_report_for(
        manifest: Mapping[str, object], request: RestoreRequest
    ) -> dict[str, object]:
        return {
            "schema": "restore-report/v1",
            "restore_id": request.restore_id,
            "backup_id": manifest["backup_id"],
            "source_root_id": manifest["library_root_id"],
            "target_root_id": request.target_root_id,
            "state": "restore_ready",
            "verified_files": [item["path"] for item in manifest["files"]],  # type: ignore[index]
            "missing_release_ids": sorted(
                str(item["release_id"])
                for item in manifest["plugin_releases"]  # type: ignore[index]
                if item.get("package_present") is False
            ),
            "projection_rebuild_required": manifest["projection_rebuild_required"],
            "errors": [],
            "created_at": request.created_at,
            "completed_at": request.completed_at,
        }

    def stage_restore(
        self, bundle_root: str | Path, target_root: str | Path, request: RestoreRequest
    ) -> RestoreResult:
        if not isinstance(request, RestoreRequest):
            raise TypeError("request must be RestoreRequest")
        if request.source_root_id == request.target_root_id:
            raise BackupValidationError("restore source and target root IDs must differ")
        _assert_no_reparse_components(bundle_root)
        _assert_no_reparse_components(target_root)
        bundle_root = _lexical_absolute(bundle_root)
        target_root = _lexical_absolute(target_root)
        if not _disjoint(target_root, self.source_root) or not _disjoint(target_root, bundle_root):
            raise BackupValidationError("restore target must be a new root disjoint from source and backup")
        source = self.verify_backup(bundle_root)
        manifest = source.manifest
        source_receipt = source.receipt
        if request.source_root_id != manifest.get("library_root_id"):
            raise BackupValidationError("restore source root ID differs from backup authority")
        expected_report = self._restore_report_for(manifest, request)
        if target_root.exists():
            report_path = _join(target_root, _RESTORE_REPORT)
            report = _read_canonical_json(report_path)
            try:
                verify_restore_report(report)
            except Exception as exc:
                raise BackupConflictError("existing restore report is invalid") from exc
            target_manifest, target_receipt = self._verify_bundle(
                target_root, allowed_extras={_RESTORE_REPORT}
            )
            self._require_publication_marker(
                target_root,
                operation="restore",
                operation_id=request.restore_id,
                target=target_root,
                binding=str(target_manifest["bundle_hash"]),
            )
            if (
                report != expected_report
                or target_manifest.get("bundle_hash") != manifest.get("bundle_hash")
                or target_receipt != source_receipt
            ):
                raise BackupConflictError(
                    "restore target already exists with a different bundle, receipt, or report"
                )
            return RestoreResult(target_root, report, True)
        stage, generation_id = self._new_stage_path(target_root, "restore")
        marker = self._stage_marker(
            operation="restore",
            operation_id=request.restore_id,
            generation_id=generation_id,
            target=target_root,
            binding=str(manifest["bundle_hash"]),
            state="staging",
        )
        self._prepare_stage(stage, marker)
        try:
            if self.on_restore_stage is not None:
                self.on_restore_stage("prepared", stage)
            copy_paths = [_BACKUP_MANIFEST, _BACKUP_RECEIPT, *(str(item["path"]) for item in manifest["files"])]
            for relative in copy_paths:
                source = _join(bundle_root, relative)
                raw = source.read_bytes()
                _write_new(_join(stage, relative), raw)
                if self.on_restore_stage is not None:
                    self.on_restore_stage(relative, stage)
            report = expected_report
            try:
                verify_restore_report(report)
            except Exception as exc:
                raise BackupValidationError("restore report violates the accepted contract") from exc
            _write_new(_join(stage, _RESTORE_REPORT), _canonical_json(report))
            if self.on_restore_stage is not None:
                self.on_restore_stage("verified", stage)
            self._verify_bundle(stage, allowed_extras={_STAGE_MARKER, _RESTORE_REPORT})
            stored_report = _read_canonical_json(_join(stage, _RESTORE_REPORT))
            try:
                verify_restore_report(stored_report)
            except Exception as exc:
                raise BackupValidationError("staged restore report failed final verification") from exc
            if stored_report != report:
                raise BackupValidationError("staged restore report changed before publication")
            published_marker = self._stage_marker(
                operation="restore",
                operation_id=request.restore_id,
                generation_id=generation_id,
                target=target_root,
                binding=str(manifest["bundle_hash"]),
                state="published",
            )
            self._set_stage_state(stage, marker, published_marker)
            marker = published_marker
            self._require_publication_marker(
                stage,
                operation="restore",
                operation_id=request.restore_id,
                target=target_root,
                binding=str(manifest["bundle_hash"]),
            )
            _assert_no_reparse_components(target_root)
            try:
                stage.rename(target_root)
            except OSError as exc:
                raise BackupConflictError("could not publish restore root without replacement") from exc
            self._require_publication_marker(
                target_root,
                operation="restore",
                operation_id=request.restore_id,
                target=target_root,
                binding=str(manifest["bundle_hash"]),
            )
            if self.on_restore_stage is not None:
                self.on_restore_stage("published", target_root)
            final_manifest, final_receipt = self._verify_bundle(
                target_root, allowed_extras={_RESTORE_REPORT}
            )
            final_report = _read_canonical_json(_join(target_root, _RESTORE_REPORT))
            if (
                final_manifest.get("bundle_hash") != manifest.get("bundle_hash")
                or final_receipt != source_receipt
                or final_report != report
            ):
                raise BackupValidationError("published restore root failed final verification")
            return RestoreResult(target_root, report, False)
        except Exception:
            self._remove_owned_stage(stage, marker)
            raise
