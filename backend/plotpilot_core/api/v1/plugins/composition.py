"""Generate the bounded P0 Composition Delta from actual router inventory."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .router import ROUTE_ALLOWLIST, route_inventory

STOPPED_ROUTES = (
    ("GET", "/api/v1/plugins/installations"),
    ("GET", "/api/v1/plugins/installations/{install_operation_id}"),
    ("GET", "/api/v1/plugins/releases/by-id/{release_id}/retirement"),
    ("GET", "/api/v1/plugins/generations/state"),
    ("GET", "/api/v1/plugins/generations/{generation_id}"),
    ("POST", "/api/v1/plugins/installations"),
    ("POST", "/api/v1/plugins/installations/{install_operation_id}/lkg/promote"),
    ("GET", "/api/v1/plugins/settings/{plugin_id}/{settings_revision_id}"),
    ("POST", "/api/v1/plugins/settings/drafts"),
    ("POST", "/api/v1/plugins/settings/validations"),
    ("POST", "/api/v1/plugins/safe-mode/{install_operation_id}/enter"),
    ("POST", "/api/v1/plugins/safe-mode/exit"),
)


def build_composition_delta(router: APIRouter) -> dict[str, Any]:
    inventory = list(route_inventory(router))
    allowlist = list(ROUTE_ALLOWLIST)
    if inventory != allowlist:
        raise ValueError("actual Plugin API route inventory differs from its allowlist")
    return {
        "schema": "plotpilot-plugin-api-composition-delta/v1",
        "generation_id": "NW-P2-PLUGIN-API-G2",
        "task_id": "WAVE-D-G2-NW-P2-PLUGIN-API-SOURCE-01",
        "exact_base": "8e3d3a7268e0d28559a7212790e3ab9f22c1453a",
        "actual_route_inventory": inventory,
        "route_allowlist": allowlist,
        "inventory_matches_allowlist": True,
        "route_states": [
            {
                **item,
                "source_state": "ready",
                "integration_state": "stopped",
                "authority": (
                    "accepted H_C PackageStore"
                    if item["route_id"].startswith("plugin_release")
                    else "accepted H_C PluginProcessSupervisor"
                ),
                "blocked_by": [
                    "NW-P0-RUNTIME-COMPOSITION-02",
                    "P0-PUBLIC-ERROR-FAMILY",
                ],
            }
            for item in inventory
        ],
        "stopped_routes": [
            {
                "method": method,
                "path": path,
                "source_state": "stopped",
                "integration_state": "stopped",
                "reason": "no fully composed accepted authority exists at H_C",
            }
            for method, path in STOPPED_ROUTES
        ],
        "authority_seams": [
            {
                "seam_id": "P1-SHARED-LIFECYCLE-TRANSACTION",
                "owner": "P1",
                "state": "stopped",
                "requirement": "one authoritative lifecycle transaction and formal migration path",
            },
            {
                "seam_id": "P1-CORE-EVENT-WRITER",
                "owner": "P1",
                "state": "stopped",
                "requirement": "lifecycle commands append through the accepted Core Event writer",
            },
            {
                "seam_id": "P1-IMMUTABLE-QUALIFICATION-AUTHORITY",
                "owner": "P1",
                "state": "stopped",
                "requirement": "LKG promotion loads immutable qualification evidence internally",
            },
            {
                "seam_id": "P1-DURABLE-SETTINGS-AUTHORITY",
                "owner": "P1",
                "state": "stopped",
                "requirement": "Settings revision, receipt and CAS are loaded inside one command transaction",
            },
            {
                "seam_id": "P1-RETIREMENT-OPERATION-LEDGER",
                "owner": "P1",
                "state": "stopped",
                "requirement": "payload-bound operation ledger and internal pin release",
            },
            {
                "seam_id": "P1-CANDIDATE-PUBLICATION-RETIREMENT-BARRIER",
                "owner": "P1",
                "state": "stopped",
                "requirement": "publication provenance remains resolvable before physical package deletion",
            },
        ],
        "required_integration_gates": [
            {
                "gate_id": "P0-WORKSPACE-PLAN-SWITCH-CAS",
                "name": "Workspace Plan-switch CAS",
                "owner": "P0",
                "state": "stopped",
                "requirement": "authoritative Workspace Plan-switch CAS must fail stale bases closed before plugin-management activation",
            },
            {
                "gate_id": "NW-P0-RUNTIME-COMPOSITION-02",
                "name": "NW-P0-RUNTIME-COMPOSITION-02",
                "owner": "P0",
                "state": "stopped",
                "requirement": "inject the accepted PackageStore and Supervisor façade, install the PluginApiBoundaryMiddleware, and mount every allowlisted route before the SPA catch-all",
            },
            {
                "gate_id": "P0-PUBLIC-ERROR-FAMILY",
                "name": "public error-family gate",
                "owner": "P0",
                "state": "stopped",
                "requirement": "adjudicate and publish the public error family before treating plugin-api-local-error/v1 as a public contract",
            },
        ],
        "mount_contract": {
            "owner": "P0",
            "state": "stopped",
            "required_node": "NW-P0-RUNTIME-COMPOSITION-02",
            "required_order": "all /api/v1/plugins APIRoutes precede every /{full_path:path} SPA catch-all",
            "boundary_guard": "PluginApiBoundaryMiddleware",
            "unknown_or_wrong_method_must_not_be_served_by_spa": True,
        },
        "dependencies": [
            "accepted H_C PackageStore",
            "accepted H_C Lifecycle",
            "accepted H_C PluginProcessSupervisor",
            "Plugin Management G2 activation follows this source",
            "P0 authoritative Workspace Plan-switch CAS",
            "NW-P0-RUNTIME-COMPOSITION-02",
            "P0 public error-family adjudication",
        ],
        "stopped_capabilities": [
            "installation, Generation and retirement GETs without composed authority",
            "all install, Settings, LKG promotion, safe-mode, Plan-switch and retirement mutations",
            "external pin releaser and app mount",
            "public schema and generated SDK changes",
        ],
        "integration_status": "stopped",
        "blockers": [
            "P0 authoritative Workspace Plan-switch CAS",
            "NW-P0-RUNTIME-COMPOSITION-02",
            "P0 public error-family adjudication",
            "stopped P1 mutation authority seams",
        ],
        "finding_targets": {
            "NW-P2-API-SOL-F-001": "mutation routes remain absent and stopped",
            "NW-P2-API-SOL-F-002": "LKG promotion remains absent and stopped",
            "NW-P2-API-SOL-F-003": "safe-mode and external pin release remain absent and stopped",
            "NW-P2-API-SOL-F-004": "retained inputs and authority projections are closed and typed",
            "NW-P2-API-SOL-F-005": "IDs, exact SemVer, lowercase hashes and Base64 are canonical",
            "NW-P2-API-SOL-F-006": "true PackageStore absence is a local typed 404 while corruption is not",
            "NW-P2-API-SOL-F-007": "Delta is generated from actual routes and names every P0 gate",
            "NW-P2-API-SOL-F-008": "concrete store, boundary and inventory negatives are tested",
        },
    }


__all__ = ["STOPPED_ROUTES", "build_composition_delta"]
