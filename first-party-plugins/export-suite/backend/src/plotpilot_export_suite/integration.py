"""Strict, injection-based Export input and output integration.

This module is intentionally an adapter, not a runtime.  It consumes the
Core-created ``export-current-revisions/v1`` JSON Asset named by a verified
RunSnapshot, reads each immutable chapter Asset through an injected P1 port,
and delegates rendering to the existing :func:`build_export` implementation.
The only durable effect is one call to an injected immutable Asset creator.

P2/P3 worker, Job, Candidate, Publication, HTTP, and SQL behavior is outside
this module and is not emulated here.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, NoReturn

from .domain import ChapterRevision, ExportDocument, ExportFormat, ExportPayload, build_export
from .ports import AssetReadResult


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ASSET_MIME_RE = re.compile(r"^[^\s/]+/[^\s/]+$")
_UTF8_BOM = b"\xef\xbb\xbf"
_MISSING = object()


class ExportPortError(ValueError):
    """A fail-closed port, binding, metadata, or output identity failure."""


@dataclass(frozen=True, slots=True)
class ExportAssetMetadata:
    """Immutable metadata calculated from the bytes handed to the creator."""

    asset_id: str
    sha256: str
    mime: str
    size: int
    logical_role: str = "export_output"
    provenance: str = "plotpilot:export-suite"
    rebuildable: bool = True
    schema: str = field(init=False, default="asset-metadata/v1")

    @property
    def media_type(self) -> str:
        return self.mime

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "asset_id": self.asset_id,
            "sha256": self.sha256,
            "mime": self.mime,
            "size": self.size,
            "logical_role": self.logical_role,
            "provenance": self.provenance,
            "rebuildable": self.rebuildable,
        }


@dataclass(frozen=True, slots=True)
class PreparedExport:
    """Verified, rendered export ready for the single Asset create call."""

    document: ExportDocument
    payload: ExportPayload
    export_format: ExportFormat
    manifest_asset_id: str
    manifest_sha256: str
    run_snapshot_id: str
    run_snapshot_hash: str
    source_assets: tuple[tuple[str, str], ...]

    @property
    def content(self) -> bytes:
        return self.payload.content

    @property
    def filename(self) -> str:
        return self.payload.filename

    @property
    def mime(self) -> str:
        return self.payload.media_type

    @property
    def media_type(self) -> str:
        return self.payload.media_type

    @property
    def sha256(self) -> str:
        return self.payload.sha256

    @property
    def source_revisions(self) -> tuple[tuple[str, str, str], ...]:
        return self.payload.source_revisions


@dataclass(frozen=True, slots=True)
class ExportReceipt:
    """Deterministic provenance receipt for the created immutable Asset."""

    receipt_id: str
    workspace_id: str
    novel_id: str
    export_format: ExportFormat
    filename: str
    asset_id: str
    sha256: str
    mime: str
    size: int
    manifest_asset_id: str
    manifest_sha256: str
    run_snapshot_id: str
    run_snapshot_hash: str
    source_revisions: tuple[tuple[str, str, str], ...]
    source_assets: tuple[tuple[str, str], ...]
    receipt_hash: str
    schema: str = field(init=False, default="export-receipt/v1")

    @property
    def output_asset_id(self) -> str:
        return self.asset_id

    @property
    def output_hash(self) -> str:
        return self.sha256

    @property
    def media_type(self) -> str:
        return self.mime

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "receipt_id": self.receipt_id,
            "workspace_id": self.workspace_id,
            "novel_id": self.novel_id,
            "format": self.export_format.value,
            "filename": self.filename,
            "asset_id": self.asset_id,
            "sha256": self.sha256,
            "mime": self.mime,
            "size": self.size,
            "manifest_asset_id": self.manifest_asset_id,
            "manifest_sha256": self.manifest_sha256,
            "run_snapshot_id": self.run_snapshot_id,
            "run_snapshot_hash": self.run_snapshot_hash,
            "source_revisions": [list(value) for value in self.source_revisions],
            "source_assets": [list(value) for value in self.source_assets],
            "receipt_hash": self.receipt_hash,
        }


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Result of rendering and creating one immutable output Asset."""

    prepared: PreparedExport
    asset_metadata: ExportAssetMetadata
    receipt: ExportReceipt

    @property
    def document(self) -> ExportDocument:
        return self.prepared.document

    @property
    def payload(self) -> ExportPayload:
        return self.prepared.payload

    @property
    def output_asset(self) -> ExportAssetMetadata:
        return self.asset_metadata

    @property
    def output_asset_metadata(self) -> ExportAssetMetadata:
        return self.asset_metadata

    @property
    def content(self) -> bytes:
        return self.payload.content

    @property
    def filename(self) -> str:
        return self.payload.filename

    @property
    def media_type(self) -> str:
        return self.payload.media_type

    @property
    def sha256(self) -> str:
        return self.payload.sha256

    @property
    def asset_id(self) -> str:
        return self.asset_metadata.asset_id

    @property
    def source_revisions(self) -> tuple[tuple[str, str, str], ...]:
        return self.prepared.source_revisions

    @property
    def output_hash(self) -> str:
        return self.asset_metadata.sha256

    @property
    def mime(self) -> str:
        return self.asset_metadata.mime

    @property
    def asset_mime(self) -> str:
        """MIME stored in the Core Asset metadata contract."""

        return self.asset_metadata.mime

    @property
    def download_media_type(self) -> str:
        """Legacy renderer media type retained for the eventual download route."""

        return self.payload.media_type

    def as_dict(self) -> dict[str, Any]:
        return {
            "payload": {
                "media_type": self.payload.media_type,
                "filename": self.payload.filename,
                "sha256": self.payload.sha256,
                "size": len(self.payload.content),
                "source_revisions": [list(value) for value in self.payload.source_revisions],
            },
            "asset_metadata": self.asset_metadata.as_dict(),
            "receipt": self.receipt.as_dict(),
        }


# Descriptive alias for callers that prefer a named output result.
ExportToAssetResult = ExportResult


def _fail(message: str) -> NoReturn:
    raise ExportPortError(message)


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be a mapping")
    return value


def _require_text(value: object, label: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        suffix = " must be a non-empty string" if nonempty else " must be a string"
        _fail(label + suffix)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ExportPortError(f"{label} must be strict UTF-8 text") from exc
    return value


def _require_id(value: object, label: str) -> str:
    result = _require_text(value, label, nonempty=True)
    if _ID_RE.fullmatch(result) is None:
        _fail(f"{label} is not a valid Asset/contract identity")
    return result


def _require_hash(value: object, label: str) -> str:
    result = _require_text(value, label, nonempty=True)
    if _HASH_RE.fullmatch(result) is None:
        _fail(f"{label} is not a lowercase SHA-256")
    return result


def _normalise_asset_mime(value: object, label: str = "Asset MIME") -> str:
    """Return a contract-valid MIME while preserving the payload bytes.

    The legacy Markdown renderer intentionally exposes
    ``text/markdown; charset=utf-8`` as its download media type.  ``asset-metadata/v1`` uses a
    token-like MIME field with no whitespace, so the Asset boundary stores the
    equivalent ``text/markdown;charset=utf-8`` spelling.  This keeps the
    renderer/download contract and the Core Asset metadata contract separate.
    """

    raw = _require_text(value, label, nonempty=True)
    normalised = ";".join(part.strip() for part in raw.split(";"))
    if _ASSET_MIME_RE.fullmatch(normalised) is None:
        _fail(f"{label} is not valid asset-metadata/v1 MIME")
    return normalised


def _stable_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExportPortError(f"value cannot be made deterministic: {exc}") from exc


def _load_sdk_verifiers() -> tuple[Any, Any]:
    """Load P0 validators without making pure domain imports SDK-dependent."""

    try:
        from plotpilot_plugin_sdk.core_api import verify_export_current_revisions_asset
        from plotpilot_plugin_sdk.verifier import verify_snapshot
    except ModuleNotFoundError:
        try:
            from backend.plotpilot_plugin_sdk.core_api import verify_export_current_revisions_asset
            from backend.plotpilot_plugin_sdk.verifier import verify_snapshot
        except ModuleNotFoundError as exc:
            raise RuntimeError("P0 Plugin SDK validators are unavailable; Export integration is not runnable") from exc
    return verify_snapshot, verify_export_current_revisions_asset


def _verified_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        snapshot = copy.deepcopy(dict(value))
    except Exception as exc:  # pragma: no cover - hostile mapping implementation
        raise ExportPortError(f"RunSnapshot cannot be copied safely: {exc}") from exc
    verify_snapshot, _ = _load_sdk_verifiers()
    try:
        verify_snapshot(snapshot)
    except ExportPortError:
        raise
    except Exception as exc:
        raise ExportPortError(f"RunSnapshot verification failed: {exc}") from exc
    return snapshot


def _snapshot_manifest_binding(snapshot: Mapping[str, Any], manifest_asset_id: str | None) -> tuple[str, str]:
    expected_id = _require_id(snapshot.get("parameters_asset_id"), "RunSnapshot.parameters_asset_id")
    expected_hash: str | None = None
    for entry in snapshot["asset_hashes"]:
        if entry["asset_id"] == expected_id:
            expected_hash = _require_hash(entry["sha256"], "RunSnapshot export Asset hash")
            break
    if expected_hash is None:
        _fail("RunSnapshot export Asset hash is absent from asset_hashes")
    if manifest_asset_id is not None and _require_id(manifest_asset_id, "manifest_asset_id") != expected_id:
        _fail("manifest_asset_id does not match RunSnapshot.parameters_asset_id")
    return expected_id, expected_hash


def _read_asset(port: object, asset_id: str) -> tuple[bytes, object | None]:
    reader = getattr(port, "read_asset", None)
    if not callable(reader):
        _fail("injected Asset read port must expose read_asset(asset_id)")
    try:
        value = reader(asset_id)
    except Exception as exc:
        raise ExportPortError(f"Asset read failed for {asset_id}: {exc}") from exc
    metadata: object | None = None
    if isinstance(value, AssetReadResult):
        raw: object = value.content
        metadata = value.metadata
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raw = value
    elif isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], (bytes, bytearray, memoryview)):
        raw = value[0]
        metadata = value[1]
    else:
        _fail(f"Asset read for {asset_id} did not return raw bytes")
    return bytes(raw), metadata


def _describe_asset(port: object, asset_id: str) -> object | None:
    describer = getattr(port, "describe_asset", None)
    if not callable(describer):
        return None
    try:
        return describer(asset_id)
    except Exception as exc:
        raise ExportPortError(f"Asset metadata read failed for {asset_id}: {exc}") from exc


def _metadata_value(metadata: object, *names: str) -> object:
    if metadata is None:
        return _MISSING
    if isinstance(metadata, Mapping):
        for name in names:
            if name in metadata:
                return metadata[name]
        return _MISSING
    for name in names:
        try:
            return getattr(metadata, name)
        except AttributeError:
            continue
        except Exception as exc:  # pragma: no cover - hostile duck-typed metadata
            raise ExportPortError(f"Asset metadata field {name} could not be read: {exc}") from exc
    return _MISSING


def _metadata_values(metadata: object, *names: str) -> tuple[object, ...]:
    if metadata is None:
        return ()
    values: list[object] = []
    if isinstance(metadata, Mapping):
        for name in names:
            if name in metadata:
                values.append(metadata[name])
        return tuple(values)
    for name in names:
        try:
            values.append(getattr(metadata, name))
        except AttributeError:
            continue
        except Exception as exc:  # pragma: no cover - hostile duck-typed metadata
            raise ExportPortError(f"Asset metadata field {name} could not be read: {exc}") from exc
    return tuple(values)


def _validate_asset_metadata(
    metadata: object | None,
    *,
    asset_id: str,
    expected_hash: str,
    expected_size: int,
    expected_mime: str,
    workspace_id: str,
    expected_encoding: str = "utf-8",
) -> None:
    if metadata is None:
        return
    schema = _metadata_value(metadata, "schema")
    if schema is not _MISSING and schema != "asset-metadata/v1":
        _fail(f"Asset {asset_id} metadata schema is invalid")
    value = _metadata_value(metadata, "asset_id")
    if value is not _MISSING and value != asset_id:
        _fail(f"Asset {asset_id} metadata identity mismatch")
    value = _metadata_value(metadata, "sha256", "content_hash")
    if value is not _MISSING and value != expected_hash:
        _fail(f"Asset {asset_id} metadata hash mismatch")
    value = _metadata_value(metadata, "size", "total_size")
    if value is not _MISSING and (isinstance(value, bool) or not isinstance(value, int) or value != expected_size):
        _fail(f"Asset {asset_id} metadata size mismatch")
    value = _metadata_value(metadata, "mime", "media_type", "content_type")
    if value is not _MISSING and value != expected_mime:
        _fail(f"Asset {asset_id} metadata MIME mismatch")
    value = _metadata_value(metadata, "encoding", "charset")
    if value is not _MISSING and value != expected_encoding:
        _fail(f"Asset {asset_id} metadata encoding mismatch")
    value = _metadata_value(metadata, "workspace_id", "workspace")
    if value is not _MISSING and value != workspace_id:
        _fail(f"Asset {asset_id} metadata workspace mismatch")


def _read_verified_body(port: object, item: Mapping[str, Any], workspace_id: str) -> str:
    asset_id = _require_id(item.get("content_asset_id"), "export item content_asset_id")
    expected_hash = _require_hash(item.get("content_hash"), f"export item {asset_id} content_hash")
    if item.get("mime") != "text/plain":
        _fail(f"export item {asset_id} MIME must be text/plain")
    if item.get("encoding") != "utf-8":
        _fail(f"export item {asset_id} encoding must be utf-8")
    raw, attached_metadata = _read_asset(port, asset_id)
    described_metadata = _describe_asset(port, asset_id) if attached_metadata is None else None
    _validate_asset_metadata(
        attached_metadata,
        asset_id=asset_id,
        expected_hash=expected_hash,
        expected_size=len(raw),
        expected_mime="text/plain",
        workspace_id=workspace_id,
    )
    _validate_asset_metadata(
        described_metadata,
        asset_id=asset_id,
        expected_hash=expected_hash,
        expected_size=len(raw),
        expected_mime="text/plain",
        workspace_id=workspace_id,
    )
    actual_hash = sha256(raw).hexdigest()
    if actual_hash != expected_hash:
        _fail(f"Asset {asset_id} bytes do not match export item content_hash")
    if raw.startswith(_UTF8_BOM):
        _fail(f"Asset {asset_id} contains a UTF-8 BOM despite encoding=utf-8")
    try:
        content = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExportPortError(f"Asset {asset_id} is not valid strict UTF-8") from exc
    if content.encode("utf-8") != raw:
        _fail(f"Asset {asset_id} is not a stable UTF-8 byte sequence")
    return content


def _validate_manifest_metadata(
    metadata: object | None,
    *,
    asset_id: str,
    expected_hash: str,
    expected_size: int,
    workspace_id: str,
) -> None:
    _validate_asset_metadata(
        metadata,
        asset_id=asset_id,
        expected_hash=expected_hash,
        expected_size=expected_size,
        expected_mime="application/json",
        workspace_id=workspace_id,
    )


def _normalise_format(value: ExportFormat | str) -> ExportFormat:
    if isinstance(value, ExportFormat):
        return value
    try:
        return ExportFormat(value)
    except (TypeError, ValueError) as exc:
        raise ExportPortError(f"unsupported export format: {value!r}") from exc


def _build_prepared_export(
    *,
    snapshot: Mapping[str, Any],
    manifest_asset_id: str,
    manifest_sha256: str,
    manifest: Mapping[str, Any],
    asset_read_port: object,
    novel_id: str,
    title: str,
    author: str,
    premise: str,
    export_format: ExportFormat,
    document_id: str | None,
) -> PreparedExport:
    workspace_id = _require_id(snapshot.get("workspace_id"), "RunSnapshot.workspace_id")
    novel_id = _require_id(novel_id, "novel_id")
    title = _require_text(title, "title")
    author = _require_text(author, "author")
    premise = _require_text(premise, "premise")
    if document_id is not None:
        document_id = _require_id(document_id, "document_id")

    items = manifest.get("ordered_revisions")
    if not isinstance(items, list) or not items:
        _fail("export manifest ordered_revisions must be a non-empty list")
    chapters: list[ChapterRevision] = []
    source_by_key: dict[tuple[str, str, str], tuple[str, str]] = {}
    for index, raw_item in enumerate(items):
        item = _require_mapping(raw_item, f"ordered_revisions[{index}]")
        if item.get("ordinal") != index:
            _fail("export manifest ordinals must preserve contiguous order")
        item_document_id = _require_id(item.get("document_id"), f"ordered_revisions[{index}].document_id")
        revision_id = _require_id(item.get("revision_id"), f"ordered_revisions[{index}].revision_id")
        item_title = _require_text(item.get("title"), f"ordered_revisions[{index}].title", nonempty=True)
        content_asset_id = _require_id(item.get("content_asset_id"), f"ordered_revisions[{index}].content_asset_id")
        content_hash = _require_hash(item.get("content_hash"), f"ordered_revisions[{index}].content_hash")
        content = _read_verified_body(asset_read_port, item, workspace_id)
        try:
            chapter = ChapterRevision(item_document_id, revision_id, index + 1, item_title, content, content_hash)
        except Exception as exc:
            raise ExportPortError(f"export chapter domain validation failed: {exc}") from exc
        chapters.append(chapter)
        source_by_key[(item_document_id, revision_id, content_hash)] = (content_asset_id, content_hash)

    try:
        document = ExportDocument(workspace_id, novel_id, title, author, premise, tuple(chapters))
        payload = build_export(document, export_format, document_id=document_id)
    except Exception as exc:
        raise ExportPortError(f"export domain/render failed: {exc}") from exc
    source_assets: list[tuple[str, str]] = []
    for source in payload.source_revisions:
        try:
            source_assets.append(source_by_key[source])
        except KeyError as exc:  # pragma: no cover - build_export only returns input chapters
            raise ExportPortError("rendered output source is not backed by the accepted manifest") from exc
    return PreparedExport(
        document=document,
        payload=payload,
        export_format=export_format,
        manifest_asset_id=manifest_asset_id,
        manifest_sha256=manifest_sha256,
        run_snapshot_id=_require_id(snapshot.get("snapshot_id"), "RunSnapshot.snapshot_id"),
        run_snapshot_hash=_require_hash(snapshot.get("snapshot_hash"), "RunSnapshot.snapshot_hash"),
        source_assets=tuple(source_assets),
    )


def build_export_from_snapshot(
    run_snapshot: Mapping[str, Any],
    asset_read_port: object | None = None,
    *,
    asset_port: object | None = None,
    novel_id: str = "novel",
    title: str = "",
    author: str = "",
    premise: str = "",
    export_format: ExportFormat | str = ExportFormat.MARKDOWN,
    document_id: str | None = None,
    manifest_asset_id: str | None = None,
) -> PreparedExport:
    """Verify a RunSnapshot/manifest binding and render without creating output.

    The port must expose SDK-style ``read_asset(asset_id)``.  A composite
    ``asset_port`` may be used as a named alias for the same reader.  The
    manifest Asset ID is never caller-authoritative: it must equal the
    verified RunSnapshot ``parameters_asset_id``.
    """

    if asset_read_port is not None and asset_port is not None and asset_read_port is not asset_port:
        _fail("asset_read_port and asset_port disagree")
    reader_port = asset_read_port or asset_port
    if reader_port is None:
        _fail("an injected Asset read port is required")
    snapshot_value = _require_mapping(run_snapshot, "run_snapshot")
    snapshot = _verified_snapshot(snapshot_value)
    expected_manifest_id, expected_manifest_hash = _snapshot_manifest_binding(snapshot, manifest_asset_id)
    raw_manifest, attached_metadata = _read_asset(reader_port, expected_manifest_id)
    described_metadata = _describe_asset(reader_port, expected_manifest_id) if attached_metadata is None else None
    _validate_manifest_metadata(
        attached_metadata,
        asset_id=expected_manifest_id,
        expected_hash=expected_manifest_hash,
        expected_size=len(raw_manifest),
        workspace_id=snapshot["workspace_id"],
    )
    _validate_manifest_metadata(
        described_metadata,
        asset_id=expected_manifest_id,
        expected_hash=expected_manifest_hash,
        expected_size=len(raw_manifest),
        workspace_id=snapshot["workspace_id"],
    )
    _, verify_export_current_revisions_asset = _load_sdk_verifiers()
    try:
        manifest = verify_export_current_revisions_asset(raw_manifest, snapshot, asset_id=expected_manifest_id)
    except ExportPortError:
        raise
    except Exception as exc:
        raise ExportPortError(f"export-current-revisions/v1 verification failed: {exc}") from exc
    return _build_prepared_export(
        snapshot=snapshot,
        manifest_asset_id=expected_manifest_id,
        manifest_sha256=expected_manifest_hash,
        manifest=manifest,
        asset_read_port=reader_port,
        novel_id=novel_id,
        title=title,
        author=author,
        premise=premise,
        export_format=_normalise_format(export_format),
        document_id=document_id,
    )


def _create_asset(port: object, prepared: PreparedExport) -> ExportAssetMetadata:
    creator = getattr(port, "create_asset", None)
    if not callable(creator):
        _fail("injected Asset create port must expose create_asset(content, *, mime)")
    asset_mime = _normalise_asset_mime(prepared.payload.media_type)
    try:
        returned = creator(prepared.payload.content, mime=asset_mime)
    except Exception as exc:
        raise ExportPortError(f"output Asset create failed: {exc}") from exc

    if isinstance(returned, str):
        asset_id = returned
        supplied: object = None
    else:
        supplied = returned
        asset_ids = _metadata_values(returned, "asset_id")
        if not asset_ids:
            _fail("Asset create result has no asset_id")
        if len(asset_ids) != 1:
            _fail("Asset create result has ambiguous asset_id")
        asset_id = asset_ids[0]
        schemas = _metadata_values(returned, "schema")
        if any(not isinstance(value, str) or value != "asset-metadata/v1" for value in schemas):
            _fail("created output Asset schema must be asset-metadata/v1")
    asset_id = _require_id(asset_id, "created output asset_id")
    output_hash = prepared.payload.sha256
    output_size = len(prepared.payload.content)
    if supplied is not None:
        hashes = _metadata_values(supplied, "sha256", "content_hash")
        if not hashes:
            _fail("created output Asset has no sha256")
        if any(not isinstance(value, str) or _HASH_RE.fullmatch(value) is None or value != output_hash for value in hashes):
            _fail("created output Asset sha256 does not match rendered bytes")
        mimes = _metadata_values(supplied, "mime", "media_type", "content_type")
        if not mimes:
            _fail("created output Asset has no MIME")
        if any(not isinstance(value, str) or value != asset_mime for value in mimes):
            _fail("created output Asset MIME does not match rendered bytes")
        sizes = _metadata_values(supplied, "size", "total_size")
        if not sizes:
            _fail("created output Asset has no size")
        if any(isinstance(value, bool) or not isinstance(value, int) or value != output_size for value in sizes):
            _fail("created output Asset size does not match rendered bytes")
    logical_role = _metadata_value(supplied, "logical_role")
    if logical_role is _MISSING:
        logical_role = "export_output"
    logical_role = _require_id(logical_role, "created output logical_role")
    provenance = _metadata_value(supplied, "provenance")
    if provenance is _MISSING:
        provenance = "plotpilot:export-suite"
    provenance = _require_text(provenance, "created output provenance", nonempty=True)
    rebuildable = _metadata_value(supplied, "rebuildable")
    if rebuildable is _MISSING:
        rebuildable = True
    if not isinstance(rebuildable, bool):
        _fail("created output rebuildable must be boolean")
    return ExportAssetMetadata(
        asset_id=asset_id,
        sha256=output_hash,
        mime=asset_mime,
        size=output_size,
        logical_role=logical_role,
        provenance=provenance,
        rebuildable=rebuildable,
    )


def _make_receipt(prepared: PreparedExport, metadata: ExportAssetMetadata) -> ExportReceipt:
    projection = {
        "schema": "export-receipt/v1",
        "workspace_id": prepared.document.workspace_id,
        "novel_id": prepared.document.novel_id,
        "format": prepared.export_format.value,
        "filename": prepared.payload.filename,
        "asset_id": metadata.asset_id,
        "sha256": metadata.sha256,
        "mime": metadata.mime,
        "size": metadata.size,
        "manifest_asset_id": prepared.manifest_asset_id,
        "manifest_sha256": prepared.manifest_sha256,
        "run_snapshot_id": prepared.run_snapshot_id,
        "run_snapshot_hash": prepared.run_snapshot_hash,
        "source_revisions": [list(value) for value in prepared.payload.source_revisions],
        "source_assets": [list(value) for value in prepared.source_assets],
    }
    receipt_hash = sha256(_stable_json_bytes(projection)).hexdigest()
    return ExportReceipt(
        receipt_id=f"export-receipt-{receipt_hash}",
        workspace_id=prepared.document.workspace_id,
        novel_id=prepared.document.novel_id,
        export_format=prepared.export_format,
        filename=prepared.payload.filename,
        asset_id=metadata.asset_id,
        sha256=metadata.sha256,
        mime=metadata.mime,
        size=metadata.size,
        manifest_asset_id=prepared.manifest_asset_id,
        manifest_sha256=prepared.manifest_sha256,
        run_snapshot_id=prepared.run_snapshot_id,
        run_snapshot_hash=prepared.run_snapshot_hash,
        source_revisions=prepared.payload.source_revisions,
        source_assets=prepared.source_assets,
        receipt_hash=receipt_hash,
    )


def export_to_asset(
    prepared_or_snapshot: PreparedExport | Mapping[str, Any],
    asset_create_port: object | None = None,
    *,
    create_port: object | None = None,
    asset_read_port: object | None = None,
    asset_port: object | None = None,
    novel_id: str = "novel",
    title: str = "",
    author: str = "",
    premise: str = "",
    export_format: ExportFormat | str = ExportFormat.MARKDOWN,
    document_id: str | None = None,
    manifest_asset_id: str | None = None,
) -> ExportResult:
    """Create one immutable output Asset from a prepared export.

    For convenience this also accepts a verified-snapshot input and will call
    :func:`build_export_from_snapshot` before the create call.  In that form
    ``asset_read_port`` (or a composite ``asset_port``) is required.  The
    create side always uses only SDK-style ``create_asset(content, *, mime)``.
    """

    if asset_create_port is not None and create_port is not None and asset_create_port is not create_port:
        _fail("asset_create_port and create_port disagree")
    creator_port = asset_create_port or create_port
    if creator_port is None:
        _fail("an injected Asset create port is required")
    if isinstance(prepared_or_snapshot, PreparedExport):
        prepared = prepared_or_snapshot
    else:
        reader_port = asset_read_port or asset_port
        if reader_port is None and callable(getattr(creator_port, "read_asset", None)):
            reader_port = creator_port
        prepared = build_export_from_snapshot(
            prepared_or_snapshot,
            reader_port,
            novel_id=novel_id,
            title=title,
            author=author,
            premise=premise,
            export_format=export_format,
            document_id=document_id,
            manifest_asset_id=manifest_asset_id,
        )
    metadata = _create_asset(creator_port, prepared)
    receipt = _make_receipt(prepared, metadata)
    return ExportResult(prepared, metadata, receipt)


def export_from_ports(
    run_snapshot: Mapping[str, Any],
    asset_port: object | None = None,
    *,
    asset_read_port: object | None = None,
    asset_create_port: object | None = None,
    read_port: object | None = None,
    create_port: object | None = None,
    novel_id: str = "novel",
    title: str = "",
    author: str = "",
    premise: str = "",
    export_format: ExportFormat | str = ExportFormat.MARKDOWN,
    document_id: str | None = None,
    manifest_asset_id: str | None = None,
) -> ExportResult:
    """Convenience composition for separate or composite injected ports."""

    read_candidates = [value for value in (asset_read_port, read_port, asset_port) if value is not None]
    create_candidates = [value for value in (asset_create_port, create_port, asset_port) if value is not None]
    if len({id(value) for value in read_candidates}) > 1:
        _fail("multiple Asset read ports were supplied")
    if len({id(value) for value in create_candidates}) > 1:
        _fail("multiple Asset create ports were supplied")
    reader_port = read_candidates[0] if read_candidates else None
    creator_port = create_candidates[0] if create_candidates else None
    if reader_port is None or creator_port is None:
        _fail("both injected Asset read and create ports are required")
    prepared = build_export_from_snapshot(
        run_snapshot,
        reader_port,
        novel_id=novel_id,
        title=title,
        author=author,
        premise=premise,
        export_format=export_format,
        document_id=document_id,
        manifest_asset_id=manifest_asset_id,
    )
    return export_to_asset(prepared, creator_port)


# Public spelling aliases kept intentionally thin and behavior-identical.
build_export_from_assets = build_export_from_snapshot
export_asset = export_to_asset


__all__ = [
    "ExportAssetMetadata",
    "ExportPortError",
    "ExportReceipt",
    "ExportResult",
    "ExportToAssetResult",
    "PreparedExport",
    "build_export_from_assets",
    "build_export_from_snapshot",
    "export_asset",
    "export_from_ports",
    "export_to_asset",
]
