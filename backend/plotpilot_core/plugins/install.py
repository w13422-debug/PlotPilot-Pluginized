"""Crash-tolerant local staging for plugin package installation.

This slice intentionally stops at package publication.  Generation/LKG and
worker lifecycle decisions remain in their owning P2 modules; a staging
attempt here only verifies and snapshots bytes before handing them to the
immutable :class:`PackageStore`.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from plotpilot_plugin_sdk.canonical import parse_json_bytes
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode

from .package import (
    CompatibilityProfile,
    PackageError,
    PackageIntegrityError,
    PackageLimits,
    VerifiedPackage,
    verify_package,
)
from .store import PackageStore, PackageStoreError


class InstallError(PackageError):
    """A staging operation is invalid or could not be recovered safely."""

    def __init__(self, message: str, *, code: int | ErrorCode = ErrorCode.INVALID_TRANSITION, details: Any = None) -> None:
        super().__init__(message, code=code, details=details)


class InstallState(str, Enum):
    SELECTED = "selected"
    STAGED = "staged"
    VERIFIED = "verified"
    PACKAGE_PUBLISHED = "package_published"
    FAILED = "failed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class StagedPackage:
    """A verified package copied under a unique staging operation."""

    install_operation_id: str
    package: VerifiedPackage
    stage_dir: Path
    package_dir: Path
    state: InstallState = InstallState.STAGED
    base_generation_id: str | None = None

    @property
    def operation_id(self) -> str:
        return self.install_operation_id

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.package.identity


InstallStaging = StagedPackage


_MARKER_NAME = "install.json"
_MARKER_SCHEMA = "plotpilot-install-staging/v1"


def _safe_operation_id(value: str | None) -> str:
    if value is None:
        return uuid.uuid4().hex
    if not isinstance(value, str) or not value or len(value) > 128 or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for ch in value):
        raise InstallError("install_operation_id must be a safe path component")
    return value


def _write_marker(path: Path, value: Mapping[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("xb") as handle:
            handle.write((json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class InstallStager:
    """Verify package input, copy it into staging, then publish exactly once."""

    def __init__(
        self,
        store: PackageStore,
        *,
        staging_root: str | os.PathLike[str] | None = None,
        limits: PackageLimits | None = None,
    ) -> None:
        self.store = store
        self.limits = limits if limits is not None else store.limits
        self.staging_root = Path(staging_root) if staging_root is not None else store.staging_root
        self.staging_root = self.staging_root.absolute()
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def _stage_dir(self, operation_id: str) -> Path:
        path = self.staging_root / _safe_operation_id(operation_id)
        try:
            path.relative_to(self.staging_root)
        except ValueError as exc:
            raise InstallError("staging path escapes staging root") from exc
        if path.is_symlink():
            raise InstallError("symlink staging path is forbidden")
        return path

    def _marker(self, staged: StagedPackage) -> dict[str, Any]:
        return {
            "schema": _MARKER_SCHEMA,
            "install_operation_id": staged.install_operation_id,
            "state": staged.state.value,
            "base_generation_id": staged.base_generation_id,
            "plugin_id": staged.package.plugin_id,
            "version": staged.package.version,
            "package_hash": staged.package.package_hash,
            "release_id": staged.package.release_id,
        }

    def _read_existing(self, operation_id: str) -> StagedPackage:
        stage_dir = self._stage_dir(operation_id)
        marker_path = stage_dir / _MARKER_NAME
        package_dir = stage_dir / "package"
        if not marker_path.is_file() or marker_path.is_symlink() or not package_dir.is_dir() or package_dir.is_symlink():
            raise InstallError("existing staging operation is incomplete")
        try:
            marker = parse_json_bytes(marker_path.read_bytes())
        except Exception as exc:
            raise InstallError(f"invalid staging marker: {exc}") from exc
        if not isinstance(marker, Mapping) or marker.get("schema") != _MARKER_SCHEMA:
            raise InstallError("invalid staging marker shape")
        try:
            package = verify_package(
                package_dir,
                expected_package_hash=marker.get("package_hash"),
                expected_release_id=marker.get("release_id"),
                limits=self.limits,
            )
        except ContractError as exc:
            raise InstallError(f"staged package no longer verifies: {exc}", code=exc.code) from exc
        if package.plugin_id != marker.get("plugin_id") or package.version != marker.get("version"):
            raise InstallError("staging marker identity does not match package")
        try:
            state = InstallState(marker.get("state", InstallState.STAGED.value))
        except ValueError as exc:
            raise InstallError("staging marker has an unknown state") from exc
        return StagedPackage(
            install_operation_id=operation_id,
            package=package,
            stage_dir=stage_dir,
            package_dir=package_dir,
            state=state,
            base_generation_id=marker.get("base_generation_id"),
        )

    def stage(
        self,
        source: str | os.PathLike[str] | bytes | bytearray | Mapping[str, bytes],
        *,
        install_operation_id: str | None = None,
        base_generation_id: str | None = None,
        expected_package_hash: str | None = None,
        expected_release_id: str | None = None,
        expected_files_sha256: bytes | None = None,
        compatibility: CompatibilityProfile | Mapping[str, Any] | None = None,
    ) -> StagedPackage:
        operation_id = _safe_operation_id(install_operation_id)
        stage_dir = self._stage_dir(operation_id)
        if stage_dir.exists():
            existing = self._read_existing(operation_id)
            if expected_package_hash is not None and existing.package.package_hash != expected_package_hash:
                raise InstallError("install operation already stages a different package", code=ErrorCode.ASSET_ERROR)
            if base_generation_id is not None and existing.base_generation_id != base_generation_id:
                raise InstallError("install operation base generation differs", code=ErrorCode.INCOMPATIBLE_GENERATION)
            return existing

        # Verify the untrusted source before creating any durable staging
        # marker.  No published store path is touched on failure.
        package = verify_package(
            source,
            expected_package_hash=expected_package_hash,
            expected_release_id=expected_release_id,
            expected_files_sha256=expected_files_sha256,
            limits=self.limits,
            compatibility=compatibility,
        )
        temp_dir = self.staging_root / f".{operation_id}.{uuid.uuid4().hex}.tmp"
        package_dir = temp_dir / "package"
        try:
            temp_dir.mkdir(parents=True, exist_ok=False)
            materialized_files = dict(package.files)
            # Keep the exact generated manifest in the staging snapshot even
            # though it is excluded from ``package.files`` for hashing.
            materialized_files["files.sha256"] = package.files_sha256
            for relative, content in materialized_files.items():
                target = package_dir / Path(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            staged = StagedPackage(
                install_operation_id=operation_id,
                package=package,
                stage_dir=self.staging_root / operation_id,
                package_dir=self.staging_root / operation_id / "package",
                state=InstallState.STAGED,
                base_generation_id=base_generation_id,
            )
            # Verify the copied snapshot, not merely the source read, before
            # making the operation discoverable for crash reconciliation.
            copied = verify_package(
                package_dir,
                expected_package_hash=package.package_hash,
                expected_release_id=package.release_id,
                expected_files_sha256=package.files_sha256,
                limits=self.limits,
                compatibility=compatibility,
            )
            staged = StagedPackage(
                install_operation_id=staged.install_operation_id,
                package=copied,
                stage_dir=staged.stage_dir,
                package_dir=staged.package_dir,
                state=staged.state,
                base_generation_id=staged.base_generation_id,
            )
            _write_marker(temp_dir / _MARKER_NAME, self._marker(staged))
            # os.rename does not replace a directory on Windows.  The final
            # existence check in the exception path protects POSIX callers;
            # the staging operation ID remains single-writer by contract.
            temp_dir.rename(stage_dir)
            return staged
        except FileExistsError:
            shutil.rmtree(temp_dir, ignore_errors=True)
            existing = self._read_existing(operation_id)
            if existing.identity != package.identity:
                raise InstallError("install operation raced with a different package", code=ErrorCode.ASSET_ERROR)
            return existing
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    stage_package = stage

    def verify(self, staged: StagedPackage | str) -> StagedPackage:
        operation_id = staged.install_operation_id if isinstance(staged, StagedPackage) else staged
        current = self._read_existing(operation_id)
        if current.state in {InstallState.FAILED, InstallState.SUPERSEDED}:
            raise InstallError("staging operation cannot be verified from its terminal failure state")
        verified = StagedPackage(
            install_operation_id=current.install_operation_id,
            package=current.package,
            stage_dir=current.stage_dir,
            package_dir=current.package_dir,
            state=InstallState.VERIFIED,
            base_generation_id=current.base_generation_id,
        )
        _write_marker(verified.stage_dir / _MARKER_NAME, self._marker(verified))
        return verified

    verify_staged = verify

    def publish(self, staged: StagedPackage | str) -> VerifiedPackage:
        verified = self.verify(staged)
        try:
            package = self.store.publish(verified.package)
        except ContractError:
            _write_marker(
                verified.stage_dir / _MARKER_NAME,
                self._marker(
                    StagedPackage(
                        install_operation_id=verified.install_operation_id,
                        package=verified.package,
                        stage_dir=verified.stage_dir,
                        package_dir=verified.package_dir,
                        state=InstallState.FAILED,
                        base_generation_id=verified.base_generation_id,
                    )
                ),
            )
            raise
        _write_marker(
            verified.stage_dir / _MARKER_NAME,
            self._marker(
                StagedPackage(
                    install_operation_id=verified.install_operation_id,
                    package=package,
                    stage_dir=verified.stage_dir,
                    package_dir=verified.package_dir,
                    state=InstallState.PACKAGE_PUBLISHED,
                    base_generation_id=verified.base_generation_id,
                )
            ),
        )
        return package

    publish_package = publish

    def load(self, install_operation_id: str) -> StagedPackage:
        return self._read_existing(install_operation_id)

    get = load

    def discard(self, staged: StagedPackage | str) -> None:
        current = self.load(staged.install_operation_id if isinstance(staged, StagedPackage) else staged)
        if current.state == InstallState.PACKAGE_PUBLISHED:
            raise InstallError("published staging cannot be discarded")
        root = current.stage_dir
        try:
            root.relative_to(self.staging_root)
        except ValueError as exc:
            raise InstallError("staging path escapes staging root") from exc
        if root == self.staging_root or root.is_symlink():
            raise InstallError("invalid staging path")
        shutil.rmtree(root)


# Functional aliases for callers that do not need to retain a stager object.
def stage_package(store: PackageStore, source: Any, **kwargs: Any) -> StagedPackage:
    return InstallStager(store).stage(source, **kwargs)


def verify_staged_package(store: PackageStore, staged: StagedPackage | str, **kwargs: Any) -> StagedPackage:
    return InstallStager(store).verify(staged)


def publish_staged_package(store: PackageStore, staged: StagedPackage | str) -> VerifiedPackage:
    return InstallStager(store).publish(staged)


__all__ = [
    "InstallError",
    "InstallStager",
    "InstallState",
    "InstallStaging",
    "StagedPackage",
    "publish_staged_package",
    "stage_package",
    "verify_staged_package",
]
