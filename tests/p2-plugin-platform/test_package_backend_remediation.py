from __future__ import annotations

import dataclasses
import io
import json
import zipfile
from pathlib import Path

import pytest

from backend.plotpilot_core.plugins.package import (
    DEFAULT_MAX_FILE_SIZE,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_PATH_LENGTH,
    DEFAULT_MAX_TOTAL_SIZE,
    PackageIntegrityError,
    PackageLimits,
    PackagePathError,
    PackageTypeError,
    read_package_files,
    verify_package,
)
from backend.plotpilot_core.plugins.store import PackageStore
from plotpilot_plugin_sdk.package import build_files_sha256


def _ordinary_files() -> dict[str, bytes]:
    manifest = {
        "capabilities": [
            {
                "capability_id": "demo.echo/v1",
                "operations": ["run"],
                "result_contract": "artifact-bundle/v1",
            }
        ],
        "compatibility": {
            "core_api": ">=1.0 <2.0",
            "plugin_rpc": "1",
            "ui_host": "1",
        },
        "data": {"format": "demo-echo/v1", "root": "data/rules.json"},
        "display_name": "Golden Echo",
        "kind": "data",
        "needs": [],
        "plugin_id": "com.plotpilot.golden.echo",
        "schema": "plotpilot-plugin/v1",
        "version": "1.0.0",
    }
    return {
        "plugin.json": (json.dumps(manifest, separators=(",", ":")) + "\n").encode(),
        "data/rules.json": b'{"message":"hello"}\n',
    }


def _complete_files(ordinary: dict[str, bytes] | None = None) -> dict[str, bytes]:
    ordinary = dict(_ordinary_files() if ordinary is None else ordinary)
    return {**ordinary, "files.sha256": build_files_sha256(ordinary)}


def _write_folder(root: Path, files: dict[str, bytes]) -> None:
    for relative, content in files.items():
        target = root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for relative, content in files.items():
            archive.writestr(relative, content)
    return stream.getvalue()


def test_valid_folder_and_zip_require_and_retain_canonical_files_manifest(tmp_path: Path) -> None:
    files = _complete_files()
    folder = tmp_path / "package"
    _write_folder(folder, files)

    from_folder = verify_package(folder)
    from_zip = verify_package(_zip_bytes(files))

    assert from_folder.identity == from_zip.identity
    assert from_folder.files_sha256 == files["files.sha256"]
    assert from_folder.read_bytes("data/rules.json") == b'{"message":"hello"}\n'


@pytest.mark.parametrize("source_kind", ["mapping", "folder", "zip"])
def test_missing_or_tampered_files_manifest_is_rejected(
    tmp_path: Path, source_kind: str
) -> None:
    files = _complete_files()
    files.pop("files.sha256")
    if source_kind == "mapping":
        source: object = files
    elif source_kind == "folder":
        folder = tmp_path / "missing"
        _write_folder(folder, files)
        source = folder
    else:
        source = _zip_bytes(files)
    with pytest.raises(PackageIntegrityError):
        verify_package(source)  # type: ignore[arg-type]

    tampered = _complete_files()
    tampered["files.sha256"] = tampered["files.sha256"] + b"x"
    if source_kind == "mapping":
        source = tampered
    elif source_kind == "folder":
        folder = tmp_path / "tampered"
        _write_folder(folder, tampered)
        source = folder
    else:
        source = _zip_bytes(tampered)
    with pytest.raises(PackageIntegrityError):
        verify_package(source)  # type: ignore[arg-type]


def test_noncanonical_files_manifest_name_is_rejected() -> None:
    files = _ordinary_files()
    files["FILES.SHA256"] = build_files_sha256(files)
    with pytest.raises(PackagePathError):
        verify_package(files)


def test_verified_package_is_reverified_and_store_reads_back_immutable_bytes(
    tmp_path: Path,
) -> None:
    verified = verify_package(_complete_files())
    reverified = verify_package(verified)
    assert reverified.identity == verified.identity
    assert reverified.files_sha256 == verified.files_sha256

    store = PackageStore(tmp_path / "store")
    published = store.publish(verified)
    loaded = store.get(verified.plugin_id, verified.version)
    assert published.identity == verified.identity
    assert loaded.read_bytes("data/rules.json") == b'{"message":"hello"}\n'
    assert (store.package_path(*verified.identity) / "files.sha256").read_bytes() == verified.files_sha256


def test_store_rejects_forged_verified_package_before_registry_write(tmp_path: Path) -> None:
    verified = verify_package(_complete_files())
    forged_files = dict(verified.files)
    forged_files.pop("data/rules.json")
    forged = dataclasses.replace(verified, files=forged_files)
    store = PackageStore(tmp_path / "store")

    with pytest.raises(PackageIntegrityError):
        store.publish(forged)
    assert store.list_releases(verify=False) == ()


@pytest.mark.parametrize("suffix", [".js", ".mjs", ".cjs", ".wasm"])
def test_data_plugin_rejects_executable_content_suffixes(suffix: str) -> None:
    ordinary = _ordinary_files()
    ordinary[f"data/evil{suffix}"] = b"not executed"
    with pytest.raises(PackageTypeError):
        verify_package(_complete_files(ordinary))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_files", 1.0),
        ("max_file_size", float("inf")),
        ("max_total_size", True),
        ("max_path_length", 1.5),
        ("max_files", DEFAULT_MAX_FILES + 1),
        ("max_file_size", DEFAULT_MAX_FILE_SIZE + 1),
        ("max_total_size", DEFAULT_MAX_TOTAL_SIZE + 1),
        ("max_path_length", DEFAULT_MAX_PATH_LENGTH + 1),
    ],
)
def test_package_limits_require_exact_int_within_frozen_range(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        PackageLimits(**{field: value})


def test_lower_max_path_length_is_enforced_and_boundary_is_valid() -> None:
    path = "data/x"
    at_boundary = PackageLimits(max_path_length=len(path))
    assert read_package_files({path: b"x"}, limits=at_boundary) == {path: b"x"}

    below_boundary = PackageLimits(max_path_length=len(path) - 1)
    with pytest.raises(PackagePathError):
        read_package_files({path: b"x"}, limits=below_boundary)
