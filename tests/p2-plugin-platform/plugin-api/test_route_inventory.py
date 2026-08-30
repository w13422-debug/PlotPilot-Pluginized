from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import APIRouter
from helpers import StatusSource, make_app
from plotpilot_core.api.v1.plugins import (
    ROUTE_ALLOWLIST,
    build_composition_delta,
    create_plugin_router,
    route_inventory,
)
from plotpilot_core.api.v1.plugins.composition import STOPPED_ROUTES
from plotpilot_core.plugins.store import PackageStore


class NeverCalledFacade:
    def list_releases(self):
        raise AssertionError("inventory must not execute endpoints")

    def get_release(self, plugin_id: str, version: str, package_hash: str | None):
        raise AssertionError("inventory must not execute endpoints")

    def get_worker_status(self, worker_id: str):
        raise AssertionError("inventory must not execute endpoints")


def inventory_router() -> APIRouter:
    return create_plugin_router(NeverCalledFacade())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "_evidence",
    [None],
    ids=["plugin-api-route-inventory"],
)
def test_actual_router_inventory_equals_independent_three_get_allowlist(
    _evidence: None,
) -> None:
    actual = route_inventory(inventory_router())

    assert actual == ROUTE_ALLOWLIST
    assert len(actual) == 3
    assert {item["method"] for item in actual} == {"GET"}
    assert {item["route_id"] for item in actual} == {
        "plugin_release_list",
        "plugin_release_exact",
        "plugin_worker_status",
    }
    actual_pairs = {(item["method"], item["path"]) for item in actual}
    assert actual_pairs.isdisjoint(set(STOPPED_ROUTES))


@pytest.mark.parametrize(
    "_evidence",
    [None],
    ids=["plugin-api-invalid-identity-regression"],
)
def test_slash_matching_does_not_capture_invalid_or_unrelated_paths(
    package_store: PackageStore,
    _evidence: None,
) -> None:
    _, client = make_app(package_store, StatusSource())

    invalid_id = client.get("/api/v1/plugins/releases/bad!id/1.0.0")
    invalid_semver = client.get(
        "/api/v1/plugins/releases/com.plotpilot.echo/not-semver"
    )
    invalid_slash_id = client.get(
        "/api/v1/plugins/releases/"
        f"{quote('bad!id/team', safe='')}/1.0.0"
    )
    invalid_slash_semver = client.get(
        "/api/v1/plugins/releases/"
        f"{quote('com.plotpilot/team', safe='')}/not-semver"
    )
    unrelated = client.get(
        "/api/v1/plugins/releases/com.plotpilot/team/1.0.0/retire"
    )

    assert invalid_id.status_code == 422
    assert invalid_id.json()["error_code"] == "invalid_identifier"
    assert invalid_semver.status_code == 422
    assert invalid_semver.json()["error_code"] == "invalid_semver"
    assert invalid_slash_id.status_code == 422
    assert invalid_slash_id.json()["error_code"] == "invalid_identifier"
    assert invalid_slash_semver.status_code == 422
    assert invalid_slash_semver.json()["error_code"] == "invalid_semver"
    assert unrelated.status_code == 404
    assert unrelated.json()["error_code"] == "route_not_found"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/plugins/releases",
        "/api/v1/plugins/releases/com.plotpilot.echo/1.0.0",
        "/api/v1/plugins/workers/worker.echo/status",
    ],
)
def test_wrong_methods_never_fall_through_to_spa(
    package_store: PackageStore, method: str, path: str
) -> None:
    _, client = make_app(package_store, StatusSource())

    response = client.request(method, path)

    assert response.status_code == 405
    assert response.headers["content-type"].startswith("application/json")
    if method == "HEAD":
        # HTTP HEAD suppresses the representation body by definition.
        assert response.content == b""
    else:
        assert response.json()["error_code"] == "method_not_allowed"
    assert "<html>" not in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        *STOPPED_ROUTES,
        ("GET", "/api/v1/plugins/unknown"),
        ("POST", "/api/v1/plugins/unknown"),
    ],
)
def test_stopped_and_unknown_paths_never_fall_through_to_spa(
    package_store: PackageStore, method: str, path: str
) -> None:
    _, client = make_app(package_store, StatusSource())

    response = client.request(method, path)

    assert response.status_code in {404, 405}
    assert response.headers["content-type"].startswith("application/json")
    assert "<html>" not in response.text


def test_boundary_guard_does_not_capture_non_plugin_spa_paths(
    package_store: PackageStore,
) -> None:
    _, client = make_app(package_store, StatusSource())

    response = client.get("/workbench")

    assert response.status_code == 200
    assert response.text == "<html>SPA:workbench</html>"


def test_committed_delta_is_generated_from_actual_inventory_and_names_all_gates() -> None:
    actual = build_composition_delta(inventory_router())
    path = Path("coordination/PPA-02/plugin-api/composition-delta-v1.json")
    committed = json.loads(path.read_text(encoding="utf-8"))

    assert committed == actual
    assert committed["actual_route_inventory"] == list(ROUTE_ALLOWLIST)
    assert committed["route_allowlist"] == list(ROUTE_ALLOWLIST)
    assert committed["inventory_matches_allowlist"] is True
    assert committed["integration_status"] == "stopped"
    assert committed["blockers"]
    rendered = json.dumps(committed, ensure_ascii=False)
    assert "Workspace Plan-switch CAS" in rendered
    assert "NW-P0-RUNTIME-COMPOSITION-02" in rendered
    assert "public error-family gate" in rendered
    assert "/{full_path:path}" in rendered


def test_delta_keeps_all_mutation_authority_seams_stopped() -> None:
    delta = build_composition_delta(inventory_router())

    assert {item["state"] for item in delta["authority_seams"]} == {"stopped"}
    assert {
        "P1-SHARED-LIFECYCLE-TRANSACTION",
        "P1-CORE-EVENT-WRITER",
        "P1-IMMUTABLE-QUALIFICATION-AUTHORITY",
        "P1-DURABLE-SETTINGS-AUTHORITY",
        "P1-RETIREMENT-OPERATION-LEDGER",
        "P1-CANDIDATE-PUBLICATION-RETIREMENT-BARRIER",
    } == {item["seam_id"] for item in delta["authority_seams"]}
