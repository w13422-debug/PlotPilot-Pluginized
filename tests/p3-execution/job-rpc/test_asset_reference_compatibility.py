from __future__ import annotations

import hashlib
import json

import pytest

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.jobs.http_rpc.chapter_handlers import (
    DisposableAssetUploadBuffer,
)


def test_first_asset_upload_treats_typed_missing_reference_as_incomplete(
    tmp_path,
):
    assets = AssetStore(tmp_path / "assets")
    buffer = DisposableAssetUploadBuffer(assets)
    owner = ("job-1", "step-1", "attempt-1", 1)
    content = b"first upload"
    content_hash = hashlib.sha256(content).hexdigest()

    result, commit = buffer.prepare(
        owner=owner,
        operation_key="upload-op-first",
        upload_id="upload-first",
        offset=0,
        mime="text/plain",
        total_size=len(content),
        expected_hash=content_hash,
        chunk_hash=content_hash,
        chunk=content,
        final=True,
    )

    assert result == {
        "upload_id": "upload-first",
        "accepted_bytes": len(content),
        "completed": True,
        "asset_id": f"asset-sha256-{content_hash}",
    }
    assert commit() == result
    assert assets.read(f"asset-sha256-{content_hash}") == content


def _prepare_existing_asset(
    buffer: DisposableAssetUploadBuffer,
    *,
    upload_id: str,
    content: bytes,
    mime: str,
) -> None:
    content_hash = hashlib.sha256(content).hexdigest()
    buffer.prepare(
        owner=("job-1", "step-1", "attempt-1", 1),
        operation_key=f"operation-{upload_id}",
        upload_id=upload_id,
        offset=0,
        mime=mime,
        total_size=len(content),
        expected_hash=content_hash,
        chunk_hash=content_hash,
        chunk=content,
        final=True,
    )


def test_missing_object_is_not_reclassified_or_rewritten(tmp_path):
    assets = AssetStore(tmp_path / "assets")
    content = b"missing object"
    asset = assets.put(
        content,
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:missing-object",
    )
    object_path = assets.objects / asset.sha256[:2] / asset.sha256
    object_path.unlink()
    buffer = DisposableAssetUploadBuffer(assets)

    with pytest.raises(FileNotFoundError):
        _prepare_existing_asset(
            buffer,
            upload_id="upload-missing-object",
            content=content,
            mime="text/plain",
        )

    assert buffer._uploads == {}
    assert not object_path.exists()


def test_tampered_object_is_not_reclassified_or_rewritten(tmp_path):
    assets = AssetStore(tmp_path / "assets")
    content = b"tamper detection"
    asset = assets.put(
        content,
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:tampered-object",
    )
    object_path = assets.objects / asset.sha256[:2] / asset.sha256
    tampered = b"tampered object"
    object_path.write_bytes(tampered)
    buffer = DisposableAssetUploadBuffer(assets)

    with pytest.raises(OSError, match="content hash mismatch"):
        _prepare_existing_asset(
            buffer,
            upload_id="upload-tampered-object",
            content=content,
            mime="text/plain",
        )

    assert buffer._uploads == {}
    assert object_path.read_bytes() == tampered


def test_corrupt_metadata_is_not_reclassified_or_rewritten(tmp_path):
    assets = AssetStore(tmp_path / "assets")
    content = b"corrupt metadata"
    asset = assets.put(
        content,
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:corrupt-metadata",
    )
    metadata_path = assets.metadata / f"{asset.sha256}.json"
    corrupt_metadata = b"{not-json"
    metadata_path.write_bytes(corrupt_metadata)
    buffer = DisposableAssetUploadBuffer(assets)

    with pytest.raises(json.JSONDecodeError):
        _prepare_existing_asset(
            buffer,
            upload_id="upload-corrupt-metadata",
            content=content,
            mime="text/plain",
        )

    assert buffer._uploads == {}
    assert metadata_path.read_bytes() == corrupt_metadata


def test_mime_drift_is_not_reclassified_or_rewritten(tmp_path):
    assets = AssetStore(tmp_path / "assets")
    content = b"mime drift"
    asset = assets.put(
        content,
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:mime-drift",
    )
    object_path = assets.objects / asset.sha256[:2] / asset.sha256
    metadata_path = assets.metadata / f"{asset.sha256}.json"
    object_before = object_path.read_bytes()
    metadata_before = metadata_path.read_bytes()
    buffer = DisposableAssetUploadBuffer(assets)

    with pytest.raises(OSError, match="mime"):
        _prepare_existing_asset(
            buffer,
            upload_id="upload-mime-drift",
            content=content,
            mime="application/octet-stream",
        )

    assert buffer._uploads == {}
    assert object_path.read_bytes() == object_before
    assert metadata_path.read_bytes() == metadata_before


def test_hash_drift_is_not_reclassified_or_rewritten(tmp_path):
    assets = AssetStore(tmp_path / "assets")
    content = b"hash drift"
    asset = assets.put(
        content,
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:hash-drift",
    )
    object_path = assets.objects / asset.sha256[:2] / asset.sha256
    metadata_path = assets.metadata / f"{asset.sha256}.json"
    drifted_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    drifted_metadata["sha256"] = "0" * 64
    drifted_metadata_bytes = json.dumps(
        drifted_metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    metadata_path.write_bytes(drifted_metadata_bytes)
    object_before = object_path.read_bytes()
    buffer = DisposableAssetUploadBuffer(assets)

    with pytest.raises(OSError, match="metadata identity mismatch"):
        _prepare_existing_asset(
            buffer,
            upload_id="upload-hash-drift",
            content=content,
            mime="text/plain",
        )

    assert buffer._uploads == {}
    assert object_path.read_bytes() == object_before
    assert metadata_path.read_bytes() == drifted_metadata_bytes
