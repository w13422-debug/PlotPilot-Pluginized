from __future__ import annotations

import hashlib

import pytest

from backend.plotpilot_core.assets import AssetStore


def test_asset_put_is_immutable_idempotent_and_range_readable(tmp_path):
    store = AssetStore(tmp_path / "assets")
    content = ("第一章\n" * 300_000).encode()
    first = store.put(content, mime="text/plain", logical_role="document", provenance="user:1")
    second = store.put(content, mime="text/plain", logical_role="document", provenance="user:1")

    assert first == second
    assert first.sha256 == hashlib.sha256(content).hexdigest()
    assert first.size > 1_000_000
    assert store.read(first.asset_id, offset=13, length=4096) == content[13 : 13 + 4096]
    assert store.read(first.asset_id) == content


def test_asset_detects_tampering(tmp_path):
    store = AssetStore(tmp_path / "assets")
    asset = store.put(b"authority", mime="text/plain", logical_role="candidate_payload", provenance="plugin:p")
    digest = asset.sha256
    (store.objects / digest[:2] / digest).write_bytes(b"tampered")
    with pytest.raises(OSError, match="hash mismatch"):
        store.read(asset.asset_id)
