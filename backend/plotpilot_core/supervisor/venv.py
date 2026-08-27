"""Offline, content-addressed venv materialization for verified releases."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import uuid
import venv
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from plotpilot_plugin_sdk.errors import ContractError, ErrorCode

from .models import RuntimeRoute
from .process import isolated_environment

_MARKER = "plotpilot-venv.json"
_LOCK_LINE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9][A-Za-z0-9._+!-]*)"
    r"(?P<hashes>(?:\s+--hash=sha256:[0-9a-f]{64})+)$"
)


class VerifiedPackageLike(Protocol):
    plugin_id: str
    version: str
    package_hash: str
    release_id: str
    manifest: Mapping[str, object]
    source: Path | None


class VenvBuilder(Protocol):
    def create(self, destination: Path) -> None: ...


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> None: ...


class StdlibVenvBuilder:
    def create(self, destination: Path) -> None:
        venv.EnvBuilder(with_pip=True, clear=False, symlinks=False, upgrade=False).create(destination)


class SubprocessCommandRunner:
    def __init__(self, *, timeout_seconds: float = 300.0) -> None:
        self.timeout_seconds = timeout_seconds

    def run(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> None:
        subprocess.run(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            shell=False,
            timeout=self.timeout_seconds,
        )


def _member(root: Path, relative: object, *, directory: bool = False) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ContractError(ErrorCode.ASSET_ERROR, "venv package member is not a canonical relative path")
    candidate = root / Path(*relative.split("/"))
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ContractError(ErrorCode.ASSET_ERROR, "venv package member escapes or is missing") from exc
    if candidate.is_symlink() or (not resolved.is_dir() if directory else not resolved.is_file()):
        raise ContractError(ErrorCode.ASSET_ERROR, "venv package member has the wrong file type")
    return resolved


def _python_at(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _validate_content_address(route: RuntimeRoute) -> None:
    expected_plugin_component = quote(route.plugin_id, safe=".-_~")
    if route.venv_root.name != route.package_hash or route.venv_root.parent.name != expected_plugin_component:
        raise ContractError(
            ErrorCode.INCOMPATIBLE_GENERATION,
            "venv path is not content-addressed as <plugin_id>/<package_hash>",
        )


def _expected_marker(route: RuntimeRoute, python_version: str) -> dict[str, object]:
    return {
        "schema": "plotpilot-venv/v1",
        "plugin_id": route.plugin_id,
        "release_id": route.release_id,
        "package_hash": route.package_hash,
        "python_version": python_version,
    }


def validate_venv_identity(route: RuntimeRoute, *, python_compatibility: str) -> Path:
    """Verify the exact marker and standard interpreter before any spawn."""

    destination = route.venv_root
    _validate_content_address(route)
    if destination.is_symlink() or not destination.is_dir():
        raise ContractError(ErrorCode.ASSET_ERROR, "venv route is not a regular directory")
    try:
        marker = json.loads((destination / _MARKER).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "venv identity marker is missing or invalid") from exc
    if not isinstance(marker, dict) or set(marker) != {
        "schema",
        "plugin_id",
        "release_id",
        "package_hash",
        "python_version",
    }:
        raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "venv identity marker is not closed")
    version = marker.get("python_version")
    expected_prefix = python_compatibility.removesuffix("*")
    if (
        marker.get("schema") != "plotpilot-venv/v1"
        or marker.get("plugin_id") != route.plugin_id
        or marker.get("release_id") != route.release_id
        or marker.get("package_hash") != route.package_hash
        or not isinstance(version, str)
        or not version.startswith(expected_prefix)
    ):
        raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "venv marker differs from exact release or Python profile")
    python = _python_at(destination)
    if route.python_executable != python or not python.is_file() or python.is_symlink():
        raise ContractError(ErrorCode.ASSET_ERROR, "venv route does not select its standard interpreter")
    return python.resolve(strict=True)


def validate_offline_lock(lock: Path, wheelhouse: Path) -> None:
    """Accept only hash-pinned distributions satisfied by local wheel bytes."""

    try:
        text = lock.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise ContractError(ErrorCode.ASSET_ERROR, "requirements lock is not strict UTF-8") from exc
    logical: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
            continue
        logical.append((pending + line).strip())
        pending = ""
    if pending:
        raise ContractError(ErrorCode.ASSET_ERROR, "requirements lock ends with a continuation")
    wheel_index: dict[tuple[str, str], set[str]] = {}
    for wheel in wheelhouse.iterdir():
        if wheel.is_symlink() or not wheel.is_file() or wheel.suffix.lower() != ".whl":
            continue
        parts = wheel.name[:-4].split("-")
        if len(parts) < 5:
            raise ContractError(ErrorCode.ASSET_ERROR, "wheelhouse contains a non-canonical wheel filename")
        name = re.sub(r"[-_.]+", "-", parts[0]).lower()
        version = parts[1]
        wheel_index.setdefault((name, version), set()).add(hashlib.sha256(wheel.read_bytes()).hexdigest())
    forbidden = ("://", "git+", "hg+", "svn+", "bzr+", "--index", "--extra-index", "--find-links", "--trusted-host", "--editable", "-e ", " @ ", "../", "..\\")
    for line in logical:
        lowered = line.lower()
        if any(token in lowered for token in forbidden) or line.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", line):
            raise ContractError(ErrorCode.ASSET_ERROR, "requirements lock contains a remote, editable, or external path")
        match = _LOCK_LINE.fullmatch(line)
        if match is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "requirements lock is not a closed hash-pinned wheel profile")
        name = re.sub(r"[-_.]+", "-", match.group("name")).lower()
        hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", match.group("hashes")))
        local_hashes = wheel_index.get((name, match.group("version")), set())
        if not hashes.intersection(local_hashes):
            raise ContractError(ErrorCode.ASSET_ERROR, "requirements lock is not satisfied by verified local wheel bytes")


class OfflineVenvProvisioner:
    """Build one rebuildable venv per exact package hash, without network."""

    def __init__(
        self,
        *,
        builder: VenvBuilder | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self._builder = builder or StdlibVenvBuilder()
        self._runner = runner or SubprocessCommandRunner()

    def _existing(self, route: RuntimeRoute, *, python_compatibility: str) -> Path | None:
        destination = route.venv_root
        _validate_content_address(route)
        if not destination.exists():
            return None
        return validate_venv_identity(route, python_compatibility=python_compatibility)

    def ensure(self, route: RuntimeRoute, package: VerifiedPackageLike) -> Path:
        """Create then non-replacing-publish a release venv.

        Dependencies are installed exclusively from the verified package's
        wheelhouse/lock.  The plugin wheel is installed with ``--no-deps``
        after the hash-locked dependency transaction.
        """

        if (
            package.plugin_id != route.plugin_id
            or package.version != route.version
            or package.package_hash != route.package_hash
            or package.release_id != route.release_id
            or package.source is None
        ):
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "venv package identity differs from route")
        backend = package.manifest.get("backend")
        if not isinstance(backend, Mapping):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "code package has no backend manifest")
        compatibility = package.manifest.get("compatibility")
        if not isinstance(compatibility, Mapping) or compatibility.get("python") != "3.12.*":
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "package Python compatibility is not the frozen profile")
        python_compatibility = "3.12.*"
        existing = self._existing(route, python_compatibility=python_compatibility)
        if existing is not None:
            return existing
        package_root = package.source.resolve(strict=True)
        if package_root != route.package_root.resolve(strict=True):
            raise ContractError(ErrorCode.ASSET_ERROR, "venv package root differs from route")
        lock = _member(package_root, backend.get("requirements_lock"))
        wheel = _member(package_root, backend.get("wheel"))
        wheelhouse = _member(package_root, backend.get("wheelhouse"), directory=True)
        validate_offline_lock(lock, wheelhouse)

        destination = route.venv_root
        if not destination.is_absolute():
            raise ContractError(ErrorCode.ASSET_ERROR, "venv route must be absolute")
        if route.python_executable != _python_at(destination):
            raise ContractError(ErrorCode.ASSET_ERROR, "venv route does not select its standard interpreter")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Do not repeat the 64-character package hash in the staging name.
        # The final content-addressed path is already long on Windows, and a
        # hash-prefixed UUID staging directory can exceed the legacy MAX_PATH
        # boundary before ``venv`` creates ``Scripts/python.exe``.
        temporary = destination.parent / f".build-{uuid.uuid4().hex}.tmp"
        if temporary.exists():
            raise ContractError(ErrorCode.ASSET_ERROR, "venv temporary path already exists")
        try:
            self._builder.create(temporary)
            python = _python_at(temporary)
            if not python.is_file() or python.is_symlink():
                raise ContractError(ErrorCode.ASSET_ERROR, "venv builder did not create an isolated Python")
            env = isolated_environment(python)
            env.update(
                {
                    "PIP_NO_INDEX": "1",
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                    "PIP_CONFIG_FILE": os.devnull,
                    "PIP_REQUIRE_VIRTUALENV": "1",
                }
            )
            common = [
                str(python),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-index",
                "--disable-pip-version-check",
                "--no-input",
            ]
            self._runner.run(
                [*common, "--require-hashes", "--find-links", str(wheelhouse), "-r", str(lock)],
                cwd=package_root,
                env=env,
            )
            self._runner.run([*common, "--no-deps", str(wheel)], cwd=package_root, env=env)
            marker = json.dumps(
                _expected_marker(route, platform.python_version()),
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            (temporary / _MARKER).write_text(marker, encoding="utf-8", newline="\n")
            try:
                temporary.rename(destination)
            except FileExistsError:
                # A cross-process publisher won.  Adopt only the exact marker.
                existing = self._existing(route, python_compatibility=python_compatibility)
                if existing is None:
                    raise
                return existing
            return _python_at(destination).resolve(strict=True)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)


__all__ = [
    "CommandRunner",
    "OfflineVenvProvisioner",
    "StdlibVenvBuilder",
    "SubprocessCommandRunner",
    "VenvBuilder",
    "validate_offline_lock",
    "validate_venv_identity",
]
