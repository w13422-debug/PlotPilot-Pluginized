from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest

from backend.plotpilot_core.assets import AssetReferenceError, AssetStore


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


@pytest.mark.parametrize(
    "asset_id",
    ["asset-a", "asset-sha256-" + ("0" * 64)],
)
def test_invalid_and_unknown_asset_references_are_typed(tmp_path, asset_id):
    store = AssetStore(tmp_path / "assets")

    with pytest.raises(AssetReferenceError):
        store.describe(asset_id)
    with pytest.raises(AssetReferenceError):
        store.read(asset_id)


def test_missing_object_remains_a_storage_failure(tmp_path):
    store = AssetStore(tmp_path / "assets")
    asset = store.put(
        b"object",
        mime="text/plain",
        logical_role="candidate_payload",
        provenance="test:missing-object",
    )
    (store.objects / asset.sha256[:2] / asset.sha256).unlink()

    with pytest.raises(FileNotFoundError):
        store.read(asset.asset_id)


def test_asset_publish_uses_short_same_directory_temp_under_long_stage_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_name = f".b-{'a' * 32}.stage"
    unpadded_root = tmp_path / stage_name / "assets"
    padding_size = 170 - len(os.fspath(unpadded_root)) - 1
    store_root = (
        tmp_path / ("p" * padding_size) / stage_name / "assets"
        if padding_size > 0
        else unpadded_root
    )
    store = AssetStore(store_root)
    content = b"windows-long-path-publication"
    digest = hashlib.sha256(content).hexdigest()
    object_path = store.objects / digest[:2] / digest
    metadata_path = store.metadata / f"{digest}.json"
    legacy_temp = object_path.with_name(
        f".{object_path.name}.{os.getpid()}.{'0' * 32}.tmp"
    )
    linked_paths: list[tuple[Path, Path]] = []
    real_link = os.link

    def recording_link(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        linked_paths.append((Path(source), Path(target)))
        real_link(source, target)

    monkeypatch.setattr(os, "link", recording_link)

    first = store.put(
        content,
        mime="text/plain",
        logical_role="core_snapshot",
        provenance="backup:test",
    )
    second = store.put(
        content,
        mime="text/plain",
        logical_role="core_snapshot",
        provenance="backup:test",
    )

    assert len(os.fspath(metadata_path)) < 260
    assert len(os.fspath(legacy_temp)) >= 260
    assert first == second
    assert store.read(first.asset_id) == content
    assert store.describe(first.asset_id) == first
    assert len(linked_paths) == 2
    assert {target for _, target in linked_paths} == {object_path, metadata_path}
    for temp, target in linked_paths:
        assert temp.parent == target.parent
        assert temp.name.startswith(".tmp-")
        assert len(temp.name) < len(target.name)
        assert not temp.exists()


def test_asset_publish_race_collision_fails_closed_and_cleans_owned_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "objects" / "aa" / ("a" * 64)
    target.parent.mkdir(parents=True)
    competing_bytes = b"competing-publisher"
    owned_temps: list[Path] = []

    def losing_link(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        owned_temps.append(Path(source))
        Path(destination).write_bytes(competing_bytes)
        raise FileExistsError

    monkeypatch.setattr(os, "link", losing_link)

    with pytest.raises(OSError, match="content-address collision"):
        AssetStore._publish_nonreplace(target, b"candidate-bytes")

    assert target.read_bytes() == competing_bytes
    assert len(owned_temps) == 1
    assert not owned_temps[0].exists()


def test_asset_publish_does_not_remove_unowned_temp_name_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "objects" / "bb" / ("b" * 64)
    target.parent.mkdir(parents=True)
    fixed_uuid = uuid.UUID(int=0)
    unowned_temp = target.with_name(f".tmp-{fixed_uuid.hex}")
    unowned_temp.write_bytes(b"other-publisher")
    monkeypatch.setattr(
        "backend.plotpilot_core.assets.store.uuid.uuid4",
        lambda: fixed_uuid,
    )

    with pytest.raises(FileExistsError):
        AssetStore._publish_nonreplace(target, b"candidate-bytes")

    assert not target.exists()
    assert unowned_temp.read_bytes() == b"other-publisher"
