"""Browser-only composition wrapper around the existing PlotPilot app.

The existing Home/Workbench routes stay the product surface.  P0 adds only
diagnostic endpoints before the legacy SPA catch-all; it does not create a
second UI or a second business data writer.
"""
from __future__ import annotations

from typing import Any

from plotpilot_core.bootstrap.composition import build_composition


_composition = build_composition()


def _attach_diagnostics(created: Any) -> Any:
    created.get("/api/v1/integration/health", tags=["integration"])(_health)
    created.get("/api/v1/integration/contracts", tags=["integration"])(_contracts)
    # interfaces.main installs a final SPA catch-all.  Move these two explicit
    # routes in front of it so the diagnostic contract remains observable.
    routes = getattr(created.router, "routes", [])
    added = routes[-2:]
    del routes[-2:]
    catchall_index = next((index for index, route in enumerate(routes) if getattr(route, "path", "") == "/{full_path:path}"), len(routes))
    for offset, route in enumerate(added):
        routes.insert(catchall_index + offset, route)
    return created


async def _health() -> dict[str, Any]:
    return _composition.diagnostics()


async def _contracts() -> dict[str, Any]:
    return {"status": "ok", "contract_version": "1.2.0", "owner": "P0", "source": "contracts/json-schema"}


def create_app() -> Any:
    # Importing the legacy app is the explicit reuse seam; its Home/Workbench
    # implementation remains read-only in P0.
    from interfaces.main import app as legacy_app

    return _attach_diagnostics(legacy_app)


app = create_app()

__all__ = ["app", "create_app"]
