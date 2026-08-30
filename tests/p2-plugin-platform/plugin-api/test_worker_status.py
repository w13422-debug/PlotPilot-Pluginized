from __future__ import annotations

import base64
import dataclasses

import pytest
from helpers import StatusSource, make_app, worker_status
from plotpilot_core.plugins.store import PackageStore


def test_worker_status_projects_closed_typed_dto(
    package_store: PackageStore,
) -> None:
    status = worker_status()
    _, client = make_app(package_store, StatusSource(status))

    response = client.get("/api/v1/plugins/workers/worker.echo/status")

    assert response.status_code == 200
    assert response.json() == {
        "schema": "plugin-worker-status-view/v1",
        "worker_id": "worker.echo",
        "lifecycle_id": "lifecycle.echo",
        "state": "ready",
        "release_id": "a" * 64,
        "pin_epoch": 2,
        "retire_epoch": 3,
        "clients": 1,
        "worker_instance_id": "instance.echo",
        "failure": None,
        "exit_code": None,
        "stderr_tail_base64": base64.b64encode(b"last stderr\n").decode("ascii"),
    }


def test_missing_worker_is_typed_404(package_store: PackageStore) -> None:
    _, client = make_app(package_store, StatusSource())

    response = client.get("/api/v1/plugins/workers/worker.missing/status")

    assert response.status_code == 404
    assert response.json()["error_code"] == "worker_not_found"


class ExplodingStatus:
    def status(self, worker_id: str):
        raise KeyError(worker_id)


def test_internal_worker_keyerror_is_not_disguised_as_404(
    package_store: PackageStore,
) -> None:
    _, client = make_app(package_store, ExplodingStatus())

    response = client.get("/api/v1/plugins/workers/worker.echo/status")

    assert response.status_code == 500
    assert response.status_code != 404


@pytest.mark.parametrize(
    "status",
    [
        dataclasses.replace(worker_status(), state="ready"),
        dataclasses.replace(worker_status(), release_id="A" * 64),
        dataclasses.replace(worker_status(), clients=-1),
    ],
)
def test_worker_authority_drift_is_typed_502(
    package_store: PackageStore, status: object
) -> None:
    _, client = make_app(package_store, StatusSource(status))  # type: ignore[arg-type]

    response = client.get("/api/v1/plugins/workers/worker.echo/status")

    assert response.status_code == 502
    assert response.json()["error_code"] == "authority_contract_error"


def test_invalid_worker_id_is_422_before_status_lookup(
    package_store: PackageStore,
) -> None:
    source = ExplodingStatus()
    _, client = make_app(package_store, source)

    response = client.get("/api/v1/plugins/workers/bad%20id/status")

    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_identifier"
