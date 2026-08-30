"""Read-only façade over the accepted H_C PackageStore and Supervisor."""
from __future__ import annotations

import stat
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from plotpilot_core.plugins.package import PackageError, VerifiedPackage
from plotpilot_core.plugins.store import PackageConflictError
from plotpilot_core.supervisor import WorkerStatus

from .models import (
    PluginApiFault,
    project_release,
    project_release_list,
    project_worker_status,
)


class PackageStoreReader(Protocol):
    def get(
        self, plugin_id: str, version: str, package_hash: str | None = None
    ) -> VerifiedPackage: ...

    def list_releases(
        self, *, verify: bool = True
    ) -> tuple[VerifiedPackage, ...]: ...


class WorkerStatusReader(Protocol):
    def status(self, worker_id: str) -> WorkerStatus | None: ...


def _release_parent_state(
    packages: PackageStoreReader, plugin_id: str, version: str
) -> str:
    """Classify the concrete PackageStore parent without following links."""
    packages_root = getattr(packages, "packages_root", None)
    if not isinstance(packages_root, Path):
        return "unknown"
    try:
        root_mode = packages_root.lstat().st_mode
    except OSError:
        return "corrupt"
    if not stat.S_ISDIR(root_mode):
        return "corrupt"

    components = (
        packages_root / quote(plugin_id, safe=".-_~"),
        packages_root
        / quote(plugin_id, safe=".-_~")
        / quote(version, safe=".-_~"),
    )
    for path in components:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            try:
                root_mode = packages_root.lstat().st_mode
            except OSError:
                return "corrupt"
            return "missing" if stat.S_ISDIR(root_mode) else "corrupt"
        except OSError:
            return "corrupt"
        if not stat.S_ISDIR(mode):
            return "corrupt"
    return "present"


class PluginReadFacade:
    """Strictly read existing authorities; never simulate stopped commands."""

    def __init__(
        self, packages: PackageStoreReader, supervisor: WorkerStatusReader
    ) -> None:
        self._packages = packages
        self._supervisor = supervisor

    @staticmethod
    def _package_failure(exc: PackageError) -> PluginApiFault:
        if isinstance(exc, PackageConflictError):
            return PluginApiFault(
                409,
                "release_conflict",
                "requested release conflicts with the registered identity",
            )
        return PluginApiFault(
            500,
            "package_store_error",
            "PackageStore could not verify its durable release inventory",
        )

    def list_releases(self) -> dict[str, object]:
        try:
            packages = self._packages.list_releases(verify=True)
        except PackageError as exc:
            raise self._package_failure(exc) from exc
        return project_release_list(tuple(packages))

    def get_release(
        self, plugin_id: str, version: str, package_hash: str | None
    ) -> dict[str, object]:
        parent_before = _release_parent_state(
            self._packages, plugin_id, version
        )
        try:
            package = self._packages.get(plugin_id, version, package_hash)
        except FileNotFoundError as exc:
            # H_C PackageStore reuses this sentinel for both absence and a
            # non-directory parent.  Translate only stable concrete absence;
            # every unsupported or damaged layout remains fail-closed.
            if (
                exc.args == ((plugin_id, version),)
                and exc.errno is None
                and exc.filename is None
                and parent_before == "missing"
                and _release_parent_state(
                    self._packages, plugin_id, version
                )
                == "missing"
            ):
                raise PluginApiFault(
                    404,
                    "release_not_found",
                    "the requested plugin release does not exist",
                ) from exc
            raise PluginApiFault(
                500,
                "package_store_error",
                "PackageStore release bytes are incomplete",
            ) from exc
        except OSError as exc:
            raise PluginApiFault(
                500,
                "package_store_error",
                "PackageStore release bytes are incomplete",
            ) from exc
        except PackageError as exc:
            raise self._package_failure(exc) from exc
        return project_release(package)

    def get_worker_status(self, worker_id: str) -> dict[str, object]:
        status = self._supervisor.status(worker_id)
        if status is None:
            raise PluginApiFault(
                404,
                "worker_not_found",
                "the requested worker has no current supervisor lifecycle",
            )
        return project_worker_status(status)


__all__ = ["PackageStoreReader", "PluginReadFacade", "WorkerStatusReader"]
