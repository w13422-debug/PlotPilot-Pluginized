from __future__ import annotations

import json
import io
import zipfile
from pathlib import Path

import pytest

from backend.plotpilot_core.plugins.install import InstallStager, InstallState
from backend.plotpilot_core.plugins.package import PackagePathError, PackageTypeError, verify_package
from backend.plotpilot_core.plugins.store import PackageConflictError, PackageStore
from plotpilot_plugin_sdk.package import build_files_sha256


def _files() -> dict[str, bytes]:
    manifest = {
        "capabilities": [{"capability_id": "demo.echo/v1", "operations": ["run"], "result_contract": "artifact-bundle/v1"}],
        "compatibility": {"core_api": ">=1.0 <2.0", "plugin_rpc": "1", "ui_host": "1"},
        "data": {"format": "demo-echo/v1", "root": "data/rules.json"},
        "display_name": "Golden Echo", "kind": "data", "needs": [],
        "plugin_id": "com.plotpilot.golden.echo", "schema": "plotpilot-plugin/v1", "version": "1.0.0",
    }
    plugin = (json.dumps(manifest, separators=(",", ":")) + "\n").encode()
    ordinary = {"plugin.json": plugin, "data/rules.json": b'{"message":"hello"}\n'}
    return {**ordinary, "files.sha256": build_files_sha256(ordinary)}


def test_staging_snapshots_source_and_publish_does_not_overwrite(tmp_path: Path) -> None:
    files = _files()
    source = tmp_path / "source"
    for path, value in files.items():
        target = source / Path(*path.split("/")); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(value)
    store = PackageStore(tmp_path / "store")
    stager = InstallStager(store)
    staged = stager.stage(source, install_operation_id="install-1")
    source.joinpath("data/rules.json").write_bytes(b"mutated\n")
    published = stager.publish(staged)
    assert published.read_bytes("data/rules.json") == b'{"message":"hello"}\n'
    assert stager.load("install-1").state == InstallState.PACKAGE_PUBLISHED


def test_zip_traversal_and_data_executable_are_rejected() -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape", b"x")
    with pytest.raises(PackagePathError):
        verify_package(archive.getvalue())

    files = _files()
    files["data/evil.py"] = b"print('no')\n"
    ordinary = {key: value for key, value in files.items() if key != "files.sha256"}
    files["files.sha256"] = build_files_sha256(ordinary)
    with pytest.raises(PackageTypeError):
        verify_package(files)


def test_same_plugin_version_different_hash_conflicts(tmp_path: Path) -> None:
    store = PackageStore(tmp_path / "store")
    first = _files()
    store.publish(first)
    second = _files()
    second["data/rules.json"] = b'{"message":"different"}\n'
    ordinary = {key: value for key, value in second.items() if key != "files.sha256"}
    second["files.sha256"] = build_files_sha256(ordinary)
    with pytest.raises(PackageConflictError):
        store.publish(second)
