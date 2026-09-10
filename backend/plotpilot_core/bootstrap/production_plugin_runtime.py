"""P0 composition of the real production plugin runtime authority graph."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from plotpilot_plugin_sdk.errors import ContractError

from ..assets import AssetStore
from ..plugins.lifecycle.repository import LifecycleRepository
from ..plugins.lifecycle.retirement import RetirementManager
from ..plugins.store import PackageStore
from ..repositories.authority import CoreAuthorityRepository
from ..repositories.execution import ExecutionAuthority
from ..supervisor.authority import SQLiteSupervisorAuthority
from ..supervisor.lookup import ImmutableRuntimeLookup
from ..supervisor.process import IsolatedVenvProcessFactory
from ..supervisor.routes import (
    PackageGenerationRouteSource,
    RouteAvailabilityReason,
)
from ..supervisor.supervisor import PluginProcessSupervisor, SupervisorConfig
from ..supervisor.venv import InterpreterProbe, SubprocessInterpreterProbe


class ExistingCoreRuntime(Protocol):
    repository: CoreAuthorityRepository
    assets: AssetStore


# This is a frozen product catalog, not an install source.  Installed
# executable identities and routes always come from Generation + PackageStore.
FIRST_PARTY_CAPABILITIES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "com.plotpilot.autopilot": ("autopilot.dag.run/v1",),
    "com.plotpilot.chapter-workflow": (
        "writing.chapter.continue/v1",
        "writing.chapter.draft/v1",
        "writing.chapter.rewrite/v1",
    ),
    "com.plotpilot.export-suite": ("writing.export/v1",),
    "com.plotpilot.project-planner": ("planning.project.generate/v1",),
    "com.plotpilot.prompt-skill-runtime": ("prompt.skill.execute/v2",),
    "com.plotpilot.provider.openai-compatible": (),
    "com.plotpilot.quality-suite": ("quality.review/v1",),
    "com.plotpilot.story-state": ("planning.story-state.settle/v1",),
})


@dataclass(frozen=True, slots=True)
class PluginAvailability:
    plugin_id: str
    capability_ids: tuple[str, ...]
    status: str
    reason: RouteAvailabilityReason
    generation_id: str | None
    release_id: str | None


@dataclass(frozen=True, slots=True)
class CapabilityAvailability:
    plugin_id: str
    capability_id: str
    status: str
    reason: RouteAvailabilityReason
    generation_id: str | None
    release_id: str | None


@dataclass(slots=True)
class ProductionPluginRuntime:
    """References to the one real authority graph; it does not own Core shutdown."""

    core_runtime: ExistingCoreRuntime
    data_root: Path
    repository: CoreAuthorityRepository
    assets: AssetStore
    lifecycle_repository: LifecycleRepository
    package_store: PackageStore
    execution_authority: ExecutionAuthority
    retirement_manager: RetirementManager
    route_source: PackageGenerationRouteSource
    supervisor_authority: SQLiteSupervisorAuthority
    lookup: ImmutableRuntimeLookup
    process_factory: IsolatedVenvProcessFactory
    supervisor: PluginProcessSupervisor

    @property
    def lifecycle(self) -> LifecycleRepository:
        return self.lifecycle_repository

    @property
    def packages(self) -> PackageStore:
        return self.package_store

    @property
    def execution(self) -> ExecutionAuthority:
        return self.execution_authority

    @property
    def retirement(self) -> RetirementManager:
        return self.retirement_manager

    @property
    def routes(self) -> PackageGenerationRouteSource:
        return self.route_source

    @property
    def authority(self) -> SQLiteSupervisorAuthority:
        return self.supervisor_authority

    @property
    def processes(self) -> IsolatedVenvProcessFactory:
        return self.process_factory

    def _current_plugin_ids(self) -> set[str]:
        try:
            state = self.lifecycle_repository.generation_state()
        except (ContractError, KeyError, sqlite3.Error, TypeError, ValueError):
            return set()
        if state.current is None:
            return set()
        return {str(member["plugin_id"]) for member in state.current["members"]}

    def plugin_availability(self) -> tuple[PluginAvailability, ...]:
        plugin_ids = set(FIRST_PARTY_CAPABILITIES).union(self._current_plugin_ids())
        result: list[PluginAvailability] = []
        for plugin_id in sorted(plugin_ids, key=lambda value: value.encode("utf-8")):
            availability = self.route_source.availability(plugin_id)
            route = availability.route
            capabilities = FIRST_PARTY_CAPABILITIES.get(plugin_id, ())
            if route is not None:
                try:
                    package = self.package_store.require_release(
                        release_id=route.release_id,
                        plugin_id=route.plugin_id,
                        package_hash=route.package_hash,
                    )
                    declared = package.manifest.get("capabilities")
                    if not isinstance(declared, list):
                        raise TypeError("manifest capabilities are absent")
                    values = tuple(
                        sorted(
                            (str(item["capability_id"]) for item in declared),
                            key=lambda value: value.encode("utf-8"),
                        )
                    )
                    if len(values) != len(set(values)):
                        raise ValueError("duplicate capability identity")
                    capabilities = values
                except (ContractError, KeyError, OSError, TypeError, ValueError):
                    availability = type(availability)(
                        plugin_id,
                        "Disabled",
                        RouteAvailabilityReason.ROUTE_IDENTITY_MISMATCH,
                        None,
                    )
                    route = None
            result.append(
                PluginAvailability(
                    plugin_id=plugin_id,
                    capability_ids=capabilities,
                    status=availability.status,
                    reason=availability.reason,
                    generation_id=None if route is None else route.generation_id,
                    release_id=None if route is None else route.release_id,
                )
            )
        return tuple(result)

    availability = plugin_availability

    def capability_availability(self) -> tuple[CapabilityAvailability, ...]:
        return tuple(
            CapabilityAvailability(
                plugin_id=plugin.plugin_id,
                capability_id=capability_id,
                status=plugin.status,
                reason=plugin.reason,
                generation_id=plugin.generation_id,
                release_id=plugin.release_id,
            )
            for plugin in self.plugin_availability()
            for capability_id in plugin.capability_ids
        )


def _production_data_root(
    repository: CoreAuthorityRepository,
    assets: AssetStore,
    requested: str | Path | None,
) -> Path:
    if repository.database == ":memory:" or repository.database.startswith("file::memory:"):
        raise ValueError("production plugin authority requires a durable Core database")
    database = Path(repository.database).absolute().resolve(strict=True)
    inferred = database.parent.parent
    root = inferred if requested is None else Path(requested).absolute().resolve()
    if database != (root / "core" / "core.db").resolve(strict=False):
        raise ValueError("Core database is outside the canonical production data root")
    if Path(assets.root).absolute().resolve(strict=True) != (
        root / "assets"
    ).resolve(strict=True):
        raise ValueError("AssetStore is outside the canonical production data root")
    return root


def build_production_plugin_runtime(
    core_runtime: ExistingCoreRuntime,
    *,
    data_root: str | Path | None = None,
    interpreter_probe: InterpreterProbe | None = None,
    supervisor_config: SupervisorConfig | None = None,
) -> ProductionPluginRuntime:
    """Compose WU-2A over the exact existing WebUI Core objects.

    This function never mounts HTTP, provisions a venv, or starts a process.
    """

    repository = core_runtime.repository
    assets = core_runtime.assets
    root = _production_data_root(repository, assets, data_root)
    with repository.read_connection() as connection:
        lifecycle = LifecycleRepository(
            connection,
            transaction_factory=repository.transaction,
            core_authority_binding=repository,
        )
    packages = PackageStore(root / "plugins")
    execution = ExecutionAuthority(repository, assets)
    retirement = RetirementManager(lifecycle)
    probe = interpreter_probe or SubprocessInterpreterProbe()
    routes = PackageGenerationRouteSource(
        lifecycle,
        packages,
        retirement,
        venv_root=root / "plugins" / "venvs",
        data_root=root / "plugins" / "data",
        working_root=root / "runtime" / "plugin-workers",
        interpreter_probe=probe,
    )
    authority = SQLiteSupervisorAuthority(
        repository,
        lifecycle,
        retirement,
        execution,
        routes,
    )
    lookup = ImmutableRuntimeLookup(routes, packages, authority, probe)
    processes = IsolatedVenvProcessFactory()
    supervisor = PluginProcessSupervisor(
        authority=authority,
        lookup=lookup,
        processes=processes,
        config=supervisor_config,
    )
    return ProductionPluginRuntime(
        core_runtime=core_runtime,
        data_root=root,
        repository=repository,
        assets=assets,
        lifecycle_repository=lifecycle,
        package_store=packages,
        execution_authority=execution,
        retirement_manager=retirement,
        route_source=routes,
        supervisor_authority=authority,
        lookup=lookup,
        process_factory=processes,
        supervisor=supervisor,
    )


__all__ = [
    "FIRST_PARTY_CAPABILITIES",
    "CapabilityAvailability",
    "ExistingCoreRuntime",
    "PluginAvailability",
    "ProductionPluginRuntime",
    "build_production_plugin_runtime",
]
