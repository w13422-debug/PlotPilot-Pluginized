from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from plotpilot_core.api.v2.plugins.ports import PluginLifecycleFacade
from plotpilot_core.api.v2.plugins.router import create_lifecycle_router
from plotpilot_core.supervisor import WorkerTicket
from plotpilot_core.supervisor.job_control import RunSnapshotWorker
from plotpilot_plugin_sdk.errors import ContractError, ErrorCode

RELEASE_A = "a" * 64
RELEASE_B = "b" * 64
SECRET = "secret-runtime-detail-must-not-cross-http"


class FakeJobs:
    def __init__(self, ticket: WorkerTicket) -> None:
        self.ticket = ticket
        self.requests: list[RunSnapshotWorker] = []

    def request_worker(self, run_snapshot: RunSnapshotWorker) -> WorkerTicket:
        self.requests.append(run_snapshot)
        return self.ticket


class FailingJobs:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def request_worker(self, run_snapshot: RunSnapshotWorker) -> WorkerTicket:
        del run_snapshot
        raise self.error


def _ticket(*, release_id: str = RELEASE_A) -> WorkerTicket:
    return WorkerTicket(
        worker_id="worker-1",
        lifecycle_id="lifecycle-1",
        retain_id="retain-1",
        release_id=release_id,
        pin_epoch=1,
        retire_epoch=1,
    )


def _payload(*, release_id: str = RELEASE_A) -> dict[str, str]:
    return {
        "run_snapshot_id": "run-snapshot-1",
        "worker_id": "worker-1",
        "plugin_id": "com.plotpilot.test",
        "generation_id": "generation-1",
        "release_id": release_id,
    }


def _client(jobs: FakeJobs | FailingJobs) -> TestClient:
    app = FastAPI()
    app.include_router(create_lifecycle_router(PluginLifecycleFacade(jobs)))
    return TestClient(app)


def test_lifecycle_request_route_projects_the_original_runsnapshot() -> None:
    jobs = FakeJobs(_ticket())

    response = _client(jobs).post("/api/v2/plugins/lifecycle/workers/request", json=_payload())

    assert response.status_code == 200
    assert response.json() == {
        "schema": "plugin-worker-ticket-view/v1",
        "run_snapshot_id": "run-snapshot-1",
        "worker_id": "worker-1",
        "lifecycle_id": "lifecycle-1",
        "retain_id": "retain-1",
        "release_id": RELEASE_A,
        "pin_epoch": 1,
        "retire_epoch": 1,
    }
    assert jobs.requests == [RunSnapshotWorker(**_payload())]


def test_lifecycle_request_rejects_extra_or_substituted_release_bytes() -> None:
    jobs = FakeJobs(_ticket(release_id=RELEASE_B))
    payload = _payload()
    payload["unexpected"] = "not allowed"

    invalid = _client(jobs).post("/api/v2/plugins/lifecycle/workers/request", json=payload)
    substituted = _client(jobs).post(
        "/api/v2/plugins/lifecycle/workers/request",
        json=_payload(),
    )

    assert invalid.status_code == 422
    assert invalid.json()["error_code"] == "invalid_request"
    assert substituted.status_code == 409
    assert substituted.json()["error_code"] == "release_mismatch"


@pytest.mark.parametrize(
    ("error", "status_code", "error_code", "message"),
    [
        (
            ContractError(ErrorCode.STALE_LEASE, SECRET),
            409,
            "worker_unavailable",
            "worker authority no longer holds the requested release",
        ),
        (
            ContractError(ErrorCode.RESULT_CONTRACT_MISMATCH, SECRET),
            502,
            "authority_contract_error",
            "worker authority returned an invalid result",
        ),
    ],
)
def test_lifecycle_route_redacts_known_contract_error_details(
    error: ContractError,
    status_code: int,
    error_code: str,
    message: str,
) -> None:
    response = _client(FailingJobs(error)).post(
        "/api/v2/plugins/lifecycle/workers/request",
        json=_payload(),
    )

    assert response.status_code == status_code
    assert response.json()["error_code"] == error_code
    assert response.json()["message"] == message
    assert SECRET not in response.text


@pytest.mark.parametrize(
    "error",
    [
        ContractError(9999, SECRET),
        RuntimeError(SECRET),
    ],
)
def test_lifecycle_route_redacts_unknown_authority_failures(error: Exception) -> None:
    response = _client(FailingJobs(error)).post(
        "/api/v2/plugins/lifecycle/workers/request",
        json=_payload(),
    )

    assert response.status_code == 502
    assert response.json()["error_code"] == "authority_contract_error"
    assert response.json()["message"] == "worker authority request failed"
    assert SECRET not in response.text


def test_lifecycle_route_rejects_non_object_body_without_invoking_authority() -> None:
    response = _client(FailingJobs(RuntimeError(SECRET))).post(
        "/api/v2/plugins/lifecycle/workers/request",
        json=[],
    )

    assert response.status_code == 422
    assert SECRET not in response.text
