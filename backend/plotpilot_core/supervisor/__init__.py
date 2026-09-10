"""P2 plugin worker supervision primitives (composition remains P0-owned)."""

from .authority import ProductionSupervisorAuthority, SQLiteSupervisorAuthority
from .lookup import ImmutableRuntimeLookup
from .models import (
    AttemptFence,
    InstallFence,
    ResolvedWorkerRoute,
    RuntimeRoute,
    UiBundle,
    WorkerFence,
    WorkerState,
    WorkerStatus,
    WorkerTicket,
)
from .process import IsolatedVenvProcessFactory, isolated_environment
from .routes import (
    PackageGenerationRouteSource,
    ProductionRouteSource,
    RouteAvailability,
    RouteAvailabilityReason,
)
from .rpc import FramedRpcSession, RpcEvent
from .supervisor import PluginProcessSupervisor, SupervisorConfig
from .venv import OfflineVenvProvisioner

__all__ = [
    "AttemptFence",
    "FramedRpcSession",
    "ImmutableRuntimeLookup",
    "InstallFence",
    "IsolatedVenvProcessFactory",
    "OfflineVenvProvisioner",
    "PackageGenerationRouteSource",
    "PluginProcessSupervisor",
    "ProductionRouteSource",
    "ProductionSupervisorAuthority",
    "ResolvedWorkerRoute",
    "RouteAvailability",
    "RouteAvailabilityReason",
    "RpcEvent",
    "RuntimeRoute",
    "SQLiteSupervisorAuthority",
    "SupervisorConfig",
    "UiBundle",
    "WorkerFence",
    "WorkerState",
    "WorkerStatus",
    "WorkerTicket",
    "isolated_environment",
]
