"""Fail-closed lookup for immutable backend and UI package routes."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from plotpilot_plugin_sdk.errors import ContractError, ErrorCode

from .models import (
    ImmutableRouteSource,
    ResolvedWorkerRoute,
    RuntimeRoute,
    SupervisorAuthority,
    UiBundle,
    WorkerFence,
)
from .venv import validate_venv_identity


class VerifiedPackageLike(Protocol):
    plugin_id: str
    version: str
    package_hash: str
    release_id: str
    manifest: Mapping[str, Any]
    source: Path | None

    def read_bytes(self, relative_path: str) -> bytes: ...


class PackageStoreLike(Protocol):
    def get(self, plugin_id: str, version: str, package_hash: str | None = None) -> VerifiedPackageLike: ...


def _strict_absolute_file(path: Path, root: Path, label: str) -> Path:
    if not path.is_absolute() or not root.is_absolute():
        raise ContractError(ErrorCode.ASSET_ERROR, f"{label} route must be absolute")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ContractError(ErrorCode.ASSET_ERROR, f"{label} escapes or is missing from its immutable root") from exc
    if path.is_symlink() or not resolved.is_file():
        raise ContractError(ErrorCode.ASSET_ERROR, f"{label} must be a regular non-symlink file")
    return resolved


class ImmutableRuntimeLookup:
    """Resolve only an exact Core-published generation/release/epoch tuple."""

    def __init__(
        self,
        routes: ImmutableRouteSource,
        packages: PackageStoreLike,
        authority: SupervisorAuthority,
    ) -> None:
        self._routes = routes
        self._packages = packages
        self._authority = authority

    def _route(self, fence: WorkerFence, owner_id: str) -> RuntimeRoute:
        current = self._authority.snapshot(fence.worker_id)
        if not fence.allowed or not fence.same_authority(current) or not self._authority.holds(fence, owner_id):
            raise ContractError(ErrorCode.RELEASE_RETIRING, "Core authority does not allow this worker")
        route = self._routes.get_route(
            fence.worker_id,
            fence.generation_id,
            fence.release_id,
            fence.retire_epoch,
        )
        if route is None:
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "immutable runtime route is absent")
        if (
            route.worker_id != fence.worker_id
            or route.plugin_id != fence.plugin_id
            or route.generation_id != fence.generation_id
            or route.release_id != fence.release_id
            or route.data_generation_id != fence.data_generation_id
            or route.retire_epoch != fence.retire_epoch
        ):
            raise ContractError(ErrorCode.STALE_LEASE, "immutable runtime route does not match current Core authority")
        return route

    def _package(self, route: RuntimeRoute) -> VerifiedPackageLike:
        package = self._packages.get(route.plugin_id, route.version, route.package_hash)
        if (
            package.plugin_id != route.plugin_id
            or package.version != route.version
            or package.package_hash != route.package_hash
            or package.release_id != route.release_id
        ):
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "package identity differs from immutable route")
        if package.source is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "published package has no immutable source path")
        try:
            source = package.source.resolve(strict=True)
            expected = route.package_root.resolve(strict=True)
        except OSError as exc:
            raise ContractError(ErrorCode.ASSET_ERROR, "published package root is missing") from exc
        if source != expected or package.source.is_symlink() or not source.is_dir():
            raise ContractError(ErrorCode.ASSET_ERROR, "package root differs from the published immutable route")
        return package

    def resolve_worker(self, fence: WorkerFence, owner_id: str) -> ResolvedWorkerRoute:
        route = self._route(fence, owner_id)
        package = self._package(route)
        backend = package.manifest.get("backend")
        if not isinstance(backend, Mapping) or backend.get("entrypoint") != route.entrypoint:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "backend entrypoint differs from immutable route")
        for field in ("wheel", "requirements_lock"):
            member = backend.get(field)
            if not isinstance(member, str):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, f"backend.{field} is absent")
            package.read_bytes(member)
        declared = package.manifest.get("capabilities")
        if not isinstance(declared, list):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "manifest capabilities are absent")
        capabilities: list[str] = []
        for item in declared:
            if not isinstance(item, Mapping) or not isinstance(item.get("capability_id"), str):
                raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "manifest capability identity is invalid")
            capabilities.append(item["capability_id"])
        if len(capabilities) != len(set(capabilities)):
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "manifest capabilities contain duplicates")
        compatibility = package.manifest.get("compatibility")
        if not isinstance(compatibility, Mapping) or compatibility.get("python") != "3.12.*":
            raise ContractError(ErrorCode.INCOMPATIBLE_GENERATION, "package Python compatibility is not frozen 3.12.*")
        executable = validate_venv_identity(route, python_compatibility="3.12.*")
        return ResolvedWorkerRoute(
            route=route,
            package_root=route.package_root.resolve(strict=True),
            python_executable=executable,
            capabilities=tuple(capabilities),
        )

    def resolve_ui_bundle(self, fence: WorkerFence, owner_id: str) -> UiBundle:
        route = self._route(fence, owner_id)
        package = self._package(route)
        if route.ui_entry is None or route.ui_bundle_hash is None:
            raise ContractError(ErrorCode.ASSET_ERROR, "release has no immutable UI bundle")
        ui = package.manifest.get("ui")
        if not isinstance(ui, Mapping) or ui.get("runtime") != "worker-ui/v1" or ui.get("entry") != route.ui_entry:
            raise ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, "UI entry differs from immutable route")
        content = package.read_bytes(route.ui_entry)
        observed = hashlib.sha256(content).hexdigest()
        if observed != route.ui_bundle_hash:
            raise ContractError(
                ErrorCode.ASSET_ERROR,
                "UI bundle content hash differs from immutable route",
                details={"expected": route.ui_bundle_hash, "actual": observed},
            )
        return UiBundle(
            release_id=route.release_id,
            bundle_hash=observed,
            route_path=f"/__plotpilot/plugin-worker/{route.release_id}/{observed}/worker.js",
            content=content,
        )


__all__ = ["ImmutableRuntimeLookup", "PackageStoreLike", "VerifiedPackageLike"]
