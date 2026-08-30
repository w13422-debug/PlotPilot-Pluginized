"""Read-only Plugin API source slice; P0 owns production composition."""

from .composition import build_composition_delta
from .ports import PluginReadFacade
from .router import (
    PLUGIN_API_PREFIX,
    ROUTE_ALLOWLIST,
    PluginApiBoundaryMiddleware,
    create_plugin_router,
    route_inventory,
)

__all__ = [
    "PLUGIN_API_PREFIX",
    "ROUTE_ALLOWLIST",
    "PluginApiBoundaryMiddleware",
    "PluginReadFacade",
    "build_composition_delta",
    "create_plugin_router",
    "route_inventory",
]
