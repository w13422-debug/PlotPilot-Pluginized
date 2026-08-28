from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeInterpreterProbe
from plotpilot_core.supervisor import venv as venv_module
from plotpilot_core.supervisor.venv import (
    OfflineVenvProvisioner,
    SubprocessInterpreterProbe,
    validate_offline_lock,
    validate_venv_identity,
)
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode


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


def prepare_offline_package(harness) -> None:
    wheelhouse = harness.route.package_root / "backend" / "wheels"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    dependency = wheelhouse / "dependency-1-py3-none-any.whl"
    dependency.write_bytes(b"dependency-wheel")
    dependency_hash = hashlib.sha256(dependency.read_bytes()).hexdigest()
    (harness.route.package_root / "backend" / "requirements.lock").write_text(
        f"dependency==1 --hash=sha256:{dependency_hash}\n",
        encoding="utf-8",
    )
    (harness.route.package_root / "backend" / "plugin.whl").write_bytes(b"wheel")
    harness.packages.package.manifest["backend"]["wheelhouse"] = "backend/wheels"


def test_offline_venv_is_content_addressed_nonreplacing_and_network_closed(harness, tmp_path: Path) -> None:
    prepare_offline_package(harness)
    route = replace(
        harness.route,
        venv_root=(tmp_path / "provisioned" / "venvs" / harness.route.plugin_id / harness.route.package_hash),
        python_executable=(tmp_path / "provisioned" / "venvs" / harness.route.plugin_id / harness.route.package_hash / "Scripts" / "python.exe"),
    )
    builder = FakeBuilder()
    runner = FakeRunner()
    probe = FakeInterpreterProbe()
    provisioner = OfflineVenvProvisioner(builder=builder, runner=runner, probe=probe)

    python = provisioner.ensure(route, harness.packages.package)
    assert python.is_file()
    assert len(builder.calls) == 1
    assert len(runner.calls) == 2
    assert len(probe.calls) == 3
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
    assert len(probe.calls) == 4


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
                "python_implementation": "cpython",
                "python_version": "3.12.10",
            }
        ),
        encoding="utf-8",
    )
    route = replace(harness.route, venv_root=root, python_executable=python)
    builder = FakeBuilder()
    with pytest.raises(ContractError):
        OfflineVenvProvisioner(
            builder=builder,
            runner=FakeRunner(),
            probe=FakeInterpreterProbe(),
        ).ensure(route, harness.packages.package)
    assert builder.calls == []


@pytest.mark.parametrize(
    ("implementation", "version"),
    [("cpython", "3.11.9"), ("cpython", "3.14.0"), ("pypy", "3.12.10")],
)
def test_marker_cannot_hide_incompatible_target_interpreter(
    harness,
    implementation: str,
    version: str,
) -> None:
    probe = FakeInterpreterProbe(implementation=implementation, version=version)
    with pytest.raises(ContractError) as caught:
        validate_venv_identity(harness.route, python_compatibility="3.12.*", probe=probe)
    assert caught.value.code == ErrorCode.INCOMPATIBLE_GENERATION
    assert probe.calls == [harness.route.python_executable]


def test_probe_failure_before_install_does_not_publish_final_hash_path(harness, tmp_path: Path) -> None:
    prepare_offline_package(harness)
    destination = tmp_path / "failed-probe" / harness.route.plugin_id / harness.route.package_hash
    route = replace(
        harness.route,
        venv_root=destination,
        python_executable=destination / "Scripts" / "python.exe",
    )
    probe = FakeInterpreterProbe(
        results=[ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "injected probe failure")]
    )
    builder = FakeBuilder()
    runner = FakeRunner()

    with pytest.raises(ContractError) as caught:
        OfflineVenvProvisioner(builder=builder, runner=runner, probe=probe).ensure(
            route,
            harness.packages.package,
        )
    assert caught.value.code == ErrorCode.INCOMPATIBLE_GENERATION
    assert not destination.exists()
    assert list(destination.parent.glob(".build-*.tmp")) == []
    assert runner.calls == []


@pytest.mark.parametrize("identity", ["pypy", "wrong_executable"])
def test_injected_probe_cannot_publish_nonexact_interpreter(harness, tmp_path: Path, identity: str) -> None:
    prepare_offline_package(harness)
    destination = tmp_path / identity / harness.route.plugin_id / harness.route.package_hash
    route = replace(
        harness.route,
        venv_root=destination,
        python_executable=destination / "Scripts" / "python.exe",
    )
    other = tmp_path / "other-python.exe"
    other.write_bytes(b"other")
    result = ("pypy", "3.12.10", None) if identity == "pypy" else ("cpython", "3.12.10", other)
    probe = FakeInterpreterProbe(results=[result])
    with pytest.raises(ContractError):
        OfflineVenvProvisioner(builder=FakeBuilder(), runner=FakeRunner(), probe=probe).ensure(
            route,
            harness.packages.package,
        )
    assert not destination.exists()


def test_failed_post_publish_probe_removes_owned_final_directory(harness, tmp_path: Path) -> None:
    prepare_offline_package(harness)
    destination = tmp_path / "post-publish-failure" / harness.route.plugin_id / harness.route.package_hash
    route = replace(
        harness.route,
        venv_root=destination,
        python_executable=destination / "Scripts" / "python.exe",
    )
    probe = FakeInterpreterProbe(
        results=[
            ("cpython", "3.12.10", None),
            ("cpython", "3.12.10", None),
            ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "post-publish damage"),
        ]
    )
    with pytest.raises(ContractError):
        OfflineVenvProvisioner(builder=FakeBuilder(), runner=FakeRunner(), probe=probe).ensure(
            route,
            harness.packages.package,
        )
    assert not destination.exists()


def test_post_install_probe_identity_change_does_not_publish(harness, tmp_path: Path) -> None:
    prepare_offline_package(harness)
    destination = tmp_path / "changed-probe" / harness.route.plugin_id / harness.route.package_hash
    route = replace(
        harness.route,
        venv_root=destination,
        python_executable=destination / "Scripts" / "python.exe",
    )
    probe = FakeInterpreterProbe(
        results=[
            ("cpython", "3.12.10", None),
            ("cpython", "3.12.11", None),
        ]
    )

    with pytest.raises(ContractError) as caught:
        OfflineVenvProvisioner(builder=FakeBuilder(), runner=FakeRunner(), probe=probe).ensure(
            route,
            harness.packages.package,
        )
    assert caught.value.code == ErrorCode.INCOMPATIBLE_GENERATION
    assert not destination.exists()
    assert list(destination.parent.glob(".build-*.tmp")) == []


def test_nonreplace_race_winner_is_reprobed_before_adoption(harness, tmp_path: Path, monkeypatch) -> None:
    prepare_offline_package(harness)
    destination = tmp_path / "race-winner" / harness.route.plugin_id / harness.route.package_hash
    route = replace(
        harness.route,
        venv_root=destination,
        python_executable=destination / "Scripts" / "python.exe",
    )
    probe = FakeInterpreterProbe()

    def publish_winner_and_lose_rename(source: Path, target: Path) -> None:
        del source
        winner_python = target / "Scripts" / "python.exe"
        winner_python.parent.mkdir(parents=True)
        winner_python.write_bytes(b"race-winner-python")
        (target / "plotpilot-venv.json").write_text(
            json.dumps(
                {
                    "schema": "plotpilot-venv/v1",
                    "plugin_id": route.plugin_id,
                    "release_id": route.release_id,
                    "package_hash": route.package_hash,
                    "python_implementation": "cpython",
                    "python_version": "3.12.10",
                }
            ),
            encoding="utf-8",
        )
        raise FileExistsError("injected competing publisher")

    monkeypatch.setattr(Path, "rename", publish_winner_and_lose_rename)
    adopted = OfflineVenvProvisioner(builder=FakeBuilder(), runner=FakeRunner(), probe=probe).ensure(
        route,
        harness.packages.package,
    )
    assert adopted == route.python_executable.resolve()
    assert probe.calls[-1] == route.python_executable
    assert len(probe.calls) == 3


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "bad_json", "non_cpython", "wrong_executable"])
def test_subprocess_interpreter_probe_fails_closed(harness, tmp_path: Path, monkeypatch, failure: str) -> None:
    python = harness.route.python_executable
    other = tmp_path / "other-python.exe"
    other.write_bytes(b"other")

    def run(*args, **kwargs):
        del args, kwargs
        if failure == "timeout":
            raise subprocess.TimeoutExpired("python", 1)
        if failure == "nonzero":
            raise subprocess.CalledProcessError(1, "python")
        if failure == "bad_json":
            return SimpleNamespace(stdout=b"not-json")
        payload = {
            "implementation": "pypy" if failure == "non_cpython" else "cpython",
            "version": "3.12.10",
            "executable": str(other if failure == "wrong_executable" else python),
        }
        return SimpleNamespace(stdout=json.dumps(payload).encode())

    monkeypatch.setattr(venv_module.subprocess, "run", run)
    with pytest.raises(ContractError) as caught:
        SubprocessInterpreterProbe(timeout_seconds=1).probe(python)
    assert caught.value.code == ErrorCode.INCOMPATIBLE_GENERATION


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
