"""Unmounted private WebUI HTTP routes."""

from .chapter_generation import (
    PRIVATE_GENERATION_PREFIX,
    PRIVATE_GENERATION_ROUTE_ALLOWLIST,
    create_private_generation_router,
    route_inventory,
)

__all__ = [
    "PRIVATE_GENERATION_PREFIX",
    "PRIVATE_GENERATION_ROUTE_ALLOWLIST",
    "create_private_generation_router",
    "route_inventory",
]
