from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from backend.plotpilot_plugin_sdk.canonical import canonical_bytes
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash
from plotpilot_export_suite import (
    P1AssetStoreAdapter,
    ExportFormat,
    ExportPortError,
    export_from_ports,
)
import plotpilot_export_suite.integration as export_integration


class FakeAssetPort:
    def __init__(self, assets: dict[str, bytes]) -> None:
        self.assets = dict(assets)
        self.reads: list[str] = []
        self.created: list[tuple[bytes, str]] = []

    def read_asset(self, asset_id: str) -> bytes:
        self.reads.append(asset_id)
        return self.assets[asset_id]

    def create_asset(self, content: bytes, *, mime: str) -> str:
        raw = bytes(content)
        self.created.append((raw, mime))
        asset_id = f"asset-sha256-{sha256(raw).hexdigest()}"
        self.assets[asset_id] = raw
        return asset_id


class LegacyAssetStore:
    """Small P1-shaped store probe for the explicit adapter."""

    def __init__(self, assets: dict[str, bytes], manifest_id: str) -> None:
        self.assets = dict(assets)
        self.manifest_id = manifest_id
        self.reads: list[str] = []
        self.puts: list[dict[str, object]] = []

    def read(self, asset_id: str) -> bytes:
        self.reads.append(asset_id)
        return self.assets[asset_id]

    def describe(self, asset_id: str) -> dict[str, object]:
        content = self.assets[asset_id]
        return {
            "asset_id": asset_id,
            "sha256": sha256(content).hexdigest(),
            "mime": "application/json" if asset_id == self.manifest_id else "text/plain",
            "size": len(content),
        }

    def put(
        self,
        content: bytes,
        *,
        mime: str,
        logical_role: str,
        provenance: str,
        rebuildable: bool,
    ) -> dict[str, object]:
        raw = bytes(content)
        asset_id = f"asset-sha256-{sha256(raw).hexdigest()}"
        self.puts.append(
            {
                "content": raw,
                "mime": mime,
                "logical_role": logical_role,
                "provenance": provenance,
                "rebuildable": rebuildable,
            }
        )
        self.assets[asset_id] = raw
        return {
            "schema": "asset-metadata/v1",
            "asset_id": asset_id,
            "sha256": sha256(raw).hexdigest(),
            "mime": mime,
            "size": len(raw),
            "logical_role": logical_role,
            "provenance": provenance,
            "rebuildable": rebuildable,
        }


class RichMetadataPort(FakeAssetPort):
    def __init__(self, assets: dict[str, bytes], overrides: dict[str, object]) -> None:
        super().__init__(assets)
        self.overrides = dict(overrides)

    def create_asset(self, content: bytes, *, mime: str) -> dict[str, object]:
        asset_id = super().create_asset(content, mime=mime)
        raw = bytes(content)
        metadata: dict[str, object] = {
            "schema": "asset-metadata/v1",
            "asset_id": asset_id,
            "sha256": sha256(raw).hexdigest(),
            "mime": mime,
            "size": len(raw),
            "logical_role": "export_output",
            "provenance": "plotpilot:export-suite",
            "rebuildable": True,
        }
        metadata.update(self.overrides)
        return metadata


def _case() -> tuple[dict[str, object], FakeAssetPort, dict[str, bytes]]:
    first = "第一章正文\n下一行"
    second = "第二章正文"
    first_hash = sha256(first.encode("utf-8")).hexdigest()
    second_hash = sha256(second.encode("utf-8")).hexdigest()
    manifest = {
        "schema": "export-current-revisions/v1",
        "workspace_id": "ws-1",
        "core_snapshot_revision": 7,
        "generated_at": "2026-08-27T00:00:00Z",
        "ordered_revisions": [
            {
                "ordinal": 0,
                "document_id": "doc-a",
                "document_type": "core.chapter",
                "title": "开端",
                "revision_id": "rev-a",
                "content_asset_id": "asset-content-a",
                "content_hash": first_hash,
                "mime": "text/plain",
                "encoding": "utf-8",
            },
            {
                "ordinal": 1,
                "document_id": "doc-b",
                "document_type": "core.chapter",
                "title": "终章",
                "revision_id": "rev-b",
                "content_asset_id": "asset-content-b",
                "content_hash": second_hash,
                "mime": "text/plain",
                "encoding": "utf-8",
            },
        ],
    }
    manifest_raw = canonical_bytes(manifest)
    manifest_id = "asset-export-current-revisions"
    assets = {
        manifest_id: manifest_raw,
        "asset-content-a": first.encode("utf-8"),
        "asset-content-b": second.encode("utf-8"),
    }
    snapshot: dict[str, object] = {
        "schema": "run-snapshot/v1",
        "snapshot_id": "snapshot-export-1",
        "core_contract_version": "1.2.0",
        "workspace_id": "ws-1",
        "scope": {"document_id": None, "node_id": None, "operation": "writing.export/v1"},
        "input_revisions": [
            {"document_id": "doc-a", "revision_id": "rev-a", "content_hash": first_hash},
            {"document_id": "doc-b", "revision_id": "rev-b", "content_hash": second_hash},
        ],
        "plan_revision_id": "plan-export-r1",
        "plugin_releases": [],
        "plugin_settings_revisions": [],
        "data_bindings": [],
        "skill_releases": [],
        "model_profile_revision_id": None,
        "parameters_asset_id": manifest_id,
        "asset_hashes": [{"asset_id": manifest_id, "sha256": sha256(manifest_raw).hexdigest()}],
        "request_key": "",
        "run_intent_id": "export-intent-1",
        "created_at": "2026-08-27T00:00:00Z",
        "snapshot_hash": "",
    }
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    return snapshot, FakeAssetPort(assets), assets


def test_export_binds_snapshot_manifest_assets_and_preserves_order_without_body_write() -> None:
    snapshot, port, original = _case()

    result = export_from_ports(
        snapshot,
        port,
        novel_id="novel-1",
        title="星 河",
        author="作者",
        premise="简介",
        export_format=ExportFormat.MARKDOWN,
    )

    assert port.reads == ["asset-export-current-revisions", "asset-content-a", "asset-content-b"]
    assert len(port.created) == 1
    assert port.created[0][0] == result.content
    assert port.created[0][1] == "text/markdown;charset=utf-8"
    assert result.payload.media_type == "text/markdown; charset=utf-8"
    assert result.download_media_type == result.payload.media_type
    assert result.asset_mime == "text/markdown;charset=utf-8"
    markdown = result.content.decode("utf-8")
    assert markdown.index("## 开端") < markdown.index("## 终章")
    assert "第一章正文\n下一行" in markdown
    assert result.filename == "星_河.md"
    assert result.output_asset.sha256 == sha256(result.content).hexdigest()
    assert result.receipt.run_snapshot_hash == snapshot["snapshot_hash"]
    assert result.source_revisions == (
        ("doc-a", "rev-a", sha256(original["asset-content-a"]).hexdigest()),
        ("doc-b", "rev-b", sha256(original["asset-content-b"]).hexdigest()),
    )
    assert original["asset-content-a"] == port.assets["asset-content-a"]
    assert not hasattr(result, "publish")


def test_explicit_p1_asset_store_adapter_preserves_order_and_contract_mime() -> None:
    snapshot, sdk_port, assets = _case()
    store = LegacyAssetStore(assets, "asset-export-current-revisions")
    result = export_from_ports(
        snapshot,
        P1AssetStoreAdapter(store),
        novel_id="novel-1",
        title="星河",
        export_format=ExportFormat.MARKDOWN,
    )

    assert store.reads == ["asset-export-current-revisions", "asset-content-a", "asset-content-b"]
    assert len(store.puts) == 1
    assert store.puts[0]["content"] == result.content
    assert store.puts[0]["mime"] == "text/markdown;charset=utf-8"
    assert result.asset_id.startswith("asset-sha256-")
    assert sdk_port.created == []


def test_repeated_export_render_has_identical_payload_and_receipt_with_content_addressed_creator() -> None:
    snapshot, port, _ = _case()
    first = export_from_ports(snapshot, port, novel_id="novel-1", title="星河", export_format=ExportFormat.MARKDOWN)
    second = export_from_ports(snapshot, port, novel_id="novel-1", title="星河", export_format=ExportFormat.MARKDOWN)

    assert second.content == first.content
    assert second.sha256 == first.sha256
    assert second.asset_id == first.asset_id
    assert second.receipt.receipt_hash == first.receipt.receipt_hash


@pytest.mark.parametrize("missing", ["asset-export-current-revisions", "asset-content-a", "asset-content-b"])
def test_missing_manifest_or_content_asset_fails_closed(missing: str) -> None:
    snapshot, port, _ = _case()
    del port.assets[missing]

    with pytest.raises(ExportPortError, match="Asset read failed"):
        export_from_ports(snapshot, port, novel_id="novel-1", title="标题")
    assert port.created == []


def test_manifest_and_snapshot_binding_rejects_tampering_before_any_output_create() -> None:
    snapshot, port, _ = _case()
    tampered = deepcopy(snapshot)
    tampered["input_revisions"] = [
        {"document_id": "doc-a", "revision_id": "rev-other", "content_hash": "0" * 64},
        tampered["input_revisions"][1],
    ]
    tampered["request_key"] = request_key(tampered)
    tampered["snapshot_hash"] = snapshot_hash(tampered)

    with pytest.raises(ExportPortError):
        export_from_ports(tampered, port, novel_id="novel-1", title="标题")
    assert port.created == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "wrong-schema/v9"},
        {"size": 1.0},
        {"size": True},
        {"sha256": "0" * 64},
        {"mime": "application/octet-stream"},
        {"asset_id": 123},
    ],
)
def test_rich_create_metadata_is_strictly_validated(overrides: dict[str, object]) -> None:
    snapshot, base_port, assets = _case()
    port = RichMetadataPort(assets, overrides)

    with pytest.raises(ExportPortError):
        export_from_ports(snapshot, port, novel_id="novel-1", title="星河")
    assert len(port.created) == 1
    assert base_port.created == []


@pytest.mark.parametrize("field", ["title", "author", "premise"])
def test_output_strings_reject_surrogates_as_export_port_errors(field: str) -> None:
    snapshot, port, _ = _case()
    kwargs = {"novel_id": "novel-1", "title": "星河", "author": "作者", "premise": "前提"}
    kwargs[field] = "\ud800"

    with pytest.raises(ExportPortError, match="strict UTF-8") as captured:
        export_from_ports(snapshot, port, **kwargs)
    assert isinstance(captured.value.__cause__, UnicodeEncodeError)
    assert port.created == []


def test_missing_and_ambiguous_document_selection_are_wrapped_with_causes() -> None:
    snapshot, port, _ = _case()
    with pytest.raises(ExportPortError, match="domain/render") as missing:
        export_from_ports(snapshot, port, novel_id="novel-1", title="星河", document_id="doc-missing")
    assert isinstance(missing.value.__cause__, ValueError)
    assert port.created == []

    manifest = json.loads(port.assets["asset-export-current-revisions"].decode("utf-8"))
    manifest["ordered_revisions"][1]["document_id"] = "doc-a"
    with pytest.raises(ExportPortError, match="domain/render") as ambiguous:
        export_integration._build_prepared_export(
            snapshot=snapshot,
            manifest_asset_id="asset-export-current-revisions",
            manifest_sha256=sha256(port.assets["asset-export-current-revisions"]).hexdigest(),
            manifest=manifest,
            asset_read_port=port,
            novel_id="novel-1",
            title="星河",
            author="作者",
            premise="前提",
            export_format=ExportFormat.MARKDOWN,
            document_id="doc-a",
        )
    assert isinstance(ambiguous.value.__cause__, ValueError)
    assert port.created == []


def test_invalid_utf8_body_and_bom_manifest_are_rejected() -> None:
    snapshot, port, _ = _case()
    bad_body = b"\xff"
    port.assets["asset-content-a"] = bad_body
    manifest = canonical_bytes(
        {
            "schema": "export-current-revisions/v1",
            "workspace_id": "ws-1",
            "core_snapshot_revision": 7,
            "generated_at": "2026-08-27T00:00:00Z",
            "ordered_revisions": [
                {
                    "ordinal": 0,
                    "document_id": "doc-a",
                    "document_type": "core.chapter",
                    "title": "开端",
                    "revision_id": "rev-a",
                    "content_asset_id": "asset-content-a",
                    "content_hash": sha256(bad_body).hexdigest(),
                    "mime": "text/plain",
                    "encoding": "utf-8",
                },
                {
                    "ordinal": 1,
                    "document_id": "doc-b",
                    "document_type": "core.chapter",
                    "title": "终章",
                    "revision_id": "rev-b",
                    "content_asset_id": "asset-content-b",
                    "content_hash": sha256("第二章正文".encode()).hexdigest(),
                    "mime": "text/plain",
                    "encoding": "utf-8",
                },
            ],
        }
    )
    port.assets["asset-export-current-revisions"] = manifest
    snapshot["input_revisions"][0]["content_hash"] = sha256(bad_body).hexdigest()
    snapshot["asset_hashes"] = [{"asset_id": "asset-export-current-revisions", "sha256": sha256(manifest).hexdigest()}]
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)

    with pytest.raises(ExportPortError, match="strict UTF-8"):
        export_from_ports(snapshot, port, novel_id="novel-1", title="标题")

    bom = b"\xef\xbb\xbf" + port.assets["asset-export-current-revisions"]
    port.assets["asset-export-current-revisions"] = bom
    snapshot["asset_hashes"] = [{"asset_id": "asset-export-current-revisions", "sha256": sha256(bom).hexdigest()}]
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    with pytest.raises(ExportPortError):
        export_from_ports(snapshot, port, novel_id="novel-1", title="标题")
