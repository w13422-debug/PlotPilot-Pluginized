from __future__ import annotations

import hashlib
import json
import os
import platform
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from plotpilot_core.supervisor.venv import OfflineVenvProvisioner, validate_offline_lock
from plotpilot_plugin_sdk.errors import ContractError


@dataclass
class FakeBuilder:
    calls: list[Path] = field(default_factory=list)

    def create(self, destination: Path) -> None:
        self.calls.append(destination)
        python = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python.parent.mkdir(parents=True)
        python.write_bytes(b"python")


@dataclass
class FakeRunner:
    calls: list[tuple[tuple[str, ...], Path, Mapping[str, str]]] = field(default_factory=list)

    def run(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> None:
        self.calls.append((tuple(argv), cwd, dict(env)))


def test_offline_venv_is_content_addressed_nonreplacing_and_network_closed(harness, tmp_path: Path) -> None:
    wheelhouse = harness.route.package_root / "backend" / "wheels"
    wheelhouse.mkdir(parents=True)
    dependency = wheelhouse / "dependency-1-py3-none-any.whl"
    dependency.write_bytes(b"dependency-wheel")
    dependency_hash = hashlib.sha256(dependency.read_bytes()).hexdigest()
    (harness.route.package_root / "backend" / "requirements.lock").write_text(
        f"dependency==1 --hash=sha256:{dependency_hash}\n",
        encoding="utf-8",
    )
    (harness.route.package_root / "backend" / "plugin.whl").write_bytes(b"wheel")
    route = replace(
        harness.route,
        venv_root=(tmp_path / "provisioned" / "venvs" / harness.route.plugin_id / harness.route.package_hash),
        python_executable=(tmp_path / "provisioned" / "venvs" / harness.route.plugin_id / harness.route.package_hash / "Scripts" / "python.exe"),
    )
    harness.packages.package.manifest["backend"]["wheelhouse"] = "backend/wheels"
    builder = FakeBuilder()
    runner = FakeRunner()
    provisioner = OfflineVenvProvisioner(builder=builder, runner=runner)

    python = provisioner.ensure(route, harness.packages.package)
    assert python.is_file()
    assert len(builder.calls) == 1
    assert len(runner.calls) == 2
    dependency_argv, cwd, env = runner.calls[0]
    assert cwd == route.package_root
    assert "--no-index" in dependency_argv
    assert "--require-hashes" in dependency_argv
    assert env["PIP_NO_INDEX"] == "1"
    assert env["PIP_CONFIG_FILE"] == os.devnull
    assert "PYTHONPATH" not in env
    wheel_argv = runner.calls[1][0]
    assert "--no-deps" in wheel_argv

    assert provisioner.ensure(route, harness.packages.package) == python
    assert len(builder.calls) == 1
    assert len(runner.calls) == 2


def test_existing_venv_identity_mismatch_fails_without_replacement(harness, tmp_path: Path) -> None:
    root = tmp_path / "venvs" / harness.route.plugin_id / harness.route.package_hash
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"python")
    (root / "plotpilot-venv.json").write_text(
        json.dumps(
            {
                "schema": "plotpilot-venv/v1",
                "plugin_id": "other",
                "release_id": "d" * 64,
                "package_hash": "e" * 64,
                "python_version": platform.python_version(),
            }
        ),
        encoding="utf-8",
    )
    route = replace(harness.route, venv_root=root, python_executable=python)
    builder = FakeBuilder()
    with pytest.raises(ContractError):
        OfflineVenvProvisioner(builder=builder, runner=FakeRunner()).ensure(route, harness.packages.package)
    assert builder.calls == []


@pytest.mark.parametrize(
    "line",
    [
        "dependency @ https://example.invalid/dependency.whl",
        "--find-links https://example.invalid/wheels",
        "git+https://example.invalid/repo.git",
        "-e ../outside",
    ],
)
def test_offline_lock_rejects_remote_vcs_editable_and_external_paths(tmp_path: Path, line: str) -> None:
    lock = tmp_path / "requirements.lock"
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    lock.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ContractError):
        validate_offline_lock(lock, wheels)


def test_offline_lock_rejects_missing_or_hash_mismatched_wheel(tmp_path: Path) -> None:
    lock = tmp_path / "requirements.lock"
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    lock.write_text("dependency==1 --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")
    with pytest.raises(ContractError):
        validate_offline_lock(lock, wheels)
    (wheels / "dependency-1-py3-none-any.whl").write_bytes(b"wrong")
    with pytest.raises(ContractError):
        validate_offline_lock(lock, wheels)
