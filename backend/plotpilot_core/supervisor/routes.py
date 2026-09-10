"""Production-only immutable routes from Core Generation and PackageStore authority."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import quote

from plotpilot_plugin_sdk.errors import ContractError, ErrorCode
from plotpilot_plugin_sdk.package import normalize_relative_path

from ..plugins.lifecycle.repository import LifecycleRepository
from ..plugins.lifecycle.retirement import RetirementManager
from ..plugins.package import VerifiedPackage
from ..plugins.store import PackageStore
from .models import RuntimeRoute
from .venv import (
    InterpreterProbe,
    SubprocessInterpreterProbe,
    validate_offline_lock,
    validate_venv_identity,
)


class RouteAvailabilityReason(str, Enum):
    """Finite private status vocabulary; this is deliberately not an HTTP contract."""

    ENABLED = "enabled"
    CURRENT_GENERATION_MISSING = "current_generation_missing"
    SAFE_MODE = "safe_mode"
    PLUGIN_NOT_IN_CURRENT_GENERATION = "plugin_not_in_current_generation"
    RELEASE_NOT_EXECUTABLE = "release_not_executable"
    PACKAGE_UNAVAILABLE_OR_INVALID = "package_unavailable_or_invalid"
    BACKEND_ARTIFACTS_UNAVAILABLE_OR_INVALID = (
        "backend_artifacts_unavailable_or_invalid"
    )
    PREPARED_VENV_UNAVAILABLE_OR_INVALID = "prepared_venv_unavailable_or_invalid"
    PREPARED_DATA_UNAVAILABLE_OR_INVALID = "prepared_data_unavailable_or_invalid"
    RUNTIME_ROOT_UNAVAILABLE = "runtime_root_unavailable"
    ROUTE_IDENTITY_MISMATCH = "route_identity_mismatch"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"


@dataclass(frozen=True, slots=True)
class RouteAvailability:
    worker_id: str
    status: str
    reason: RouteAvailabilityReason
    route: RuntimeRoute | None

    def __post_init__(self) -> None:
        if self.status not in {"Enabled", "Disabled"}:
            raise ValueError("route availability status must be Enabled or Disabled")
        if (self.status == "Enabled") != (self.route is not None):
            raise ValueError("only an Enabled availability may carry a route")
        if (self.reason is RouteAvailabilityReason.ENABLED) != (
            self.status == "Enabled"
        ):
            raise ValueError("route availability reason contradicts status")

    @property
    def enabled(self) -> bool:
        return self.status == "Enabled"


def _disabled(
    worker_id: str, reason: RouteAvailabilityReason
) -> RouteAvailability:
    return RouteAvailability(worker_id, "Disabled", reason, None)


def _is_reparse(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (callable(is_junction) and is_junction())


def _has_reparse_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _is_reparse(current):
            return True
    return False


def _strict_directory(path: Path, *, label: str) -> Path:
    if not path.is_absolute() or _has_reparse_component(path):
        raise ContractError(
            ErrorCode.ASSET_ERROR,
            f"{label} must be an absolute non-reparse directory",
        )
    try:
        result = path.resolve(strict=True)
    except OSError as exc:
        raise ContractError(ErrorCode.ASSET_ERROR, f"{label} is missing") from exc
    if not result.is_dir() or _is_reparse(result):
        raise ContractError(ErrorCode.ASSET_ERROR, f"{label} is not a regular directory")
    return result


def _package_file(
    package: VerifiedPackage,
    package_root: Path,
    relative: object,
    *,
    label: str,
) -> Path:
    if not isinstance(relative, str):
        raise ContractError(ErrorCode.ASSET_ERROR, f"{label} is absent")
    canonical = normalize_relative_path(relative)
    expected = package.read_bytes(canonical)
    candidate = package_root.joinpath(*canonical.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(package_root)
    except (OSError, ValueError) as exc:
        raise ContractError(ErrorCode.ASSET_ERROR, f"{label} is outside the package") from exc
    if candidate.is_symlink() or not resolved.is_file() or resolved.read_bytes() != expected:
        raise ContractError(ErrorCode.ASSET_ERROR, f"{label} is not immutable package bytes")
    return resolved


def _package_directory(
    package_root: Path,
    relative: object,
    *,
    label: str,
) -> Path:
    if not isinstance(relative, str):
        raise ContractError(ErrorCode.ASSET_ERROR, f"{label} is absent")
    canonical = normalize_relative_path(relative)
    candidate = package_root.joinpath(*canonical.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(package_root)
    except (OSError, ValueError) as exc:
        raise ContractError(ErrorCode.ASSET_ERROR, f"{label} is outside the package") from exc
    if _has_reparse_component(candidate) or not resolved.is_dir():
        raise ContractError(ErrorCode.ASSET_ERROR, f"{label} is not an immutable directory")
    return resolved


class PackageGenerationRouteSource:
    """Project routes from only the current Generation and reverified package bytes.

    One logical worker has the exact identifier of its Generation member's
    ``plugin_id``.  No alias or source checkout is accepted.
    """

    def __init__(
        self,
        lifecycle: LifecycleRepository,
        packages: PackageStore,
        retirement: RetirementManager,
        *,
        venv_root: Path | None = None,
        data_root: Path | None = None,
        working_root: Path | None = None,
        interpreter_probe: InterpreterProbe | None = None,
    ) -> None:
        if retirement.repository is not lifecycle:
            raise ValueError("route retirement and lifecycle repositories must be identical")
        self.lifecycle = lifecycle
        self.packages = packages
        self.retirement = retirement
        self.venv_root = Path(venv_root or packages.root / "venvs").absolute()
        self.data_root = Path(data_root or packages.root / "data").absolute()
        self.working_root = Path(
            working_root or packages.root.parent / "runtime" / "plugin-workers"
        ).absolute()
        self.interpreter_probe = interpreter_probe or SubprocessInterpreterProbe()
        for root in (self.venv_root, self.data_root, self.working_root):
            root.mkdir(parents=True, exist_ok=True)
            _strict_directory(root, label="production runtime root")

    @staticmethod
    def _member(generation: Mapping[str, Any], worker_id: str) -> Mapping[str, Any] | None:
        for member in generation["members"]:
            if member["plugin_id"] == worker_id:
                return member
        return None

    def _package(
        self, member: Mapping[str, Any]
    ) -> tuple[VerifiedPackage, Path]:
        package = self.packages.require_release(
            release_id=member["release_id"],
            plugin_id=member["plugin_id"],
            package_hash=member["package_hash"],
        )
        if package.kind != "code" or package.source is None:
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "Generation member is not a published code package",
            )
        package_root = _strict_directory(package.source, label="package root")
        expected_root = self.packages.package_path(*package.identity).resolve(strict=True)
        if package_root != expected_root:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "published package root differs from PackageStore identity",
            )
        return package, package_root

    @staticmethod
    def _backend(
        package: VerifiedPackage, package_root: Path
    ) -> tuple[Mapping[str, Any], Path, Path, Path]:
        backend = package.manifest.get("backend")
        compatibility = package.manifest.get("compatibility")
        if (
            not isinstance(backend, Mapping)
            or not isinstance(compatibility, Mapping)
            or compatibility.get("python") != "3.12.*"
        ):
            raise ContractError(
                ErrorCode.INCOMPATIBLE_GENERATION,
                "package backend/Python profile is not executable",
            )
        wheel = _package_file(
            package, package_root, backend.get("wheel"), label="backend wheel"
        )
        lock = _package_file(
            package,
            package_root,
            backend.get("requirements_lock"),
            label="requirements lock",
        )
        wheelhouse = _package_directory(
            package_root, backend.get("wheelhouse"), label="wheelhouse"
        )
        validate_offline_lock(lock, wheelhouse)
        return backend, wheel, lock, wheelhouse

    def _compose(
        self,
        generation: Mapping[str, Any],
        member: Mapping[str, Any],
        retire_epoch: int,
    ) -> RouteAvailability:
        worker_id = str(member["plugin_id"])
        try:
            package, package_root = self._package(member)
        except (ContractError, KeyError, OSError, TypeError, ValueError):
            return _disabled(
                worker_id, RouteAvailabilityReason.PACKAGE_UNAVAILABLE_OR_INVALID
            )

        try:
            backend, _wheel, _lock, _wheelhouse = self._backend(
                package, package_root
            )
        except (ContractError, KeyError, OSError, TypeError, ValueError):
            return _disabled(
                worker_id,
                RouteAvailabilityReason.BACKEND_ARTIFACTS_UNAVAILABLE_OR_INVALID,
            )

        entrypoint = backend.get("entrypoint")
        if not isinstance(entrypoint, str):
            return _disabled(
                worker_id,
                RouteAvailabilityReason.BACKEND_ARTIFACTS_UNAVAILABLE_OR_INVALID,
            )

        ui_entry: str | None = None
        ui_hash = member["ui_bundle_hash"]
        if ui_hash is not None:
            ui = package.manifest.get("ui")
            if not isinstance(ui, Mapping) or not isinstance(ui.get("entry"), str):
                return _disabled(
                    worker_id, RouteAvailabilityReason.ROUTE_IDENTITY_MISMATCH
                )
            ui_entry = ui["entry"]
            try:
                if hashlib.sha256(package.read_bytes(ui_entry)).hexdigest() != ui_hash:
                    return _disabled(
                        worker_id, RouteAvailabilityReason.ROUTE_IDENTITY_MISMATCH
                    )
            except (ContractError, OSError, ValueError):
                return _disabled(
                    worker_id, RouteAvailabilityReason.ROUTE_IDENTITY_MISMATCH
                )

        plugin_component = quote(worker_id, safe=".-_~")
        generation_component = quote(generation["generation_id"], safe=".-_~")
        venv = self.venv_root / plugin_component / member["package_hash"]
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        private_data: Path | None = None
        data_generation_id = member["data_generation_id"]
        if data_generation_id is not None:
            data_component = quote(data_generation_id, safe=".-_~")
            private_data = (
                self.data_root / plugin_component / "generations" / data_component
            )
            try:
                private_data = _strict_directory(
                    private_data, label="prepared private data generation"
                )
                database = private_data / "plugin.db"
                if database.is_symlink() or not database.is_file():
                    raise ContractError(
                        ErrorCode.ASSET_ERROR,
                        "prepared private data generation has no plugin.db",
                    )
            except (ContractError, OSError):
                return _disabled(
                    worker_id, RouteAvailabilityReason.PREPARED_DATA_UNAVAILABLE_OR_INVALID
                )

        working = (
            self.working_root
            / plugin_component
            / generation_component
            / member["release_id"]
        )
        try:
            working.mkdir(parents=True, exist_ok=True)
            working = _strict_directory(working, label="worker runtime root")
        except (ContractError, OSError):
            return _disabled(
                worker_id, RouteAvailabilityReason.RUNTIME_ROOT_UNAVAILABLE
            )

        try:
            route = RuntimeRoute(
                worker_id=worker_id,
                plugin_id=worker_id,
                version=package.version,
                generation_id=generation["generation_id"],
                release_id=member["release_id"],
                package_hash=member["package_hash"],
                data_generation_id=data_generation_id,
                retire_epoch=retire_epoch,
                package_root=package_root,
                venv_root=venv,
                working_root=working,
                private_data_root=private_data,
                python_executable=python,
                entrypoint=entrypoint,
                ui_entry=ui_entry,
                ui_bundle_hash=ui_hash,
            )
        except (ContractError, OSError, TypeError, ValueError):
            return _disabled(
                worker_id, RouteAvailabilityReason.ROUTE_IDENTITY_MISMATCH
            )
        try:
            validate_venv_identity(
                route,
                python_compatibility="3.12.*",
                probe=self.interpreter_probe,
            )
        except (ContractError, OSError, TypeError, ValueError):
            return _disabled(
                worker_id,
                RouteAvailabilityReason.PREPARED_VENV_UNAVAILABLE_OR_INVALID,
            )
        return RouteAvailability(
            worker_id, "Enabled", RouteAvailabilityReason.ENABLED, route
        )

    def availability(self, worker_id: str) -> RouteAvailability:
        try:
            state = self.lifecycle.generation_state()
        except (ContractError, KeyError, sqlite3.Error, TypeError, ValueError):
            return _disabled(worker_id, RouteAvailabilityReason.AUTHORITY_UNAVAILABLE)
        if state.safe_mode:
            return _disabled(worker_id, RouteAvailabilityReason.SAFE_MODE)
        generation = state.current
        if generation is None:
            return _disabled(
                worker_id, RouteAvailabilityReason.CURRENT_GENERATION_MISSING
            )
        member = self._member(generation, worker_id)
        if member is None:
            return _disabled(
                worker_id, RouteAvailabilityReason.PLUGIN_NOT_IN_CURRENT_GENERATION
            )
        try:
            retirement = self.retirement.require_executable(member["release_id"])
            retire_epoch = retirement["retire_epoch"]
            if not isinstance(retire_epoch, int) or isinstance(retire_epoch, bool):
                raise TypeError("invalid retirement epoch")
        except (ContractError, KeyError, sqlite3.Error, TypeError, ValueError):
            return _disabled(
                worker_id, RouteAvailabilityReason.RELEASE_NOT_EXECUTABLE
            )
        return self._compose(generation, member, retire_epoch)

    current_route = availability

    def get_route(
        self,
        worker_id: str,
        generation_id: str,
        release_id: str,
        retire_epoch: int,
    ) -> RuntimeRoute | None:
        availability = self.availability(worker_id)
        route = availability.route
        if route is None or (
            route.generation_id != generation_id
            or route.release_id != release_id
            or route.retire_epoch != retire_epoch
        ):
            return None
        return route


ProductionRouteSource = PackageGenerationRouteSource


__all__ = [
    "PackageGenerationRouteSource",
    "ProductionRouteSource",
    "RouteAvailability",
    "RouteAvailabilityReason",
]
