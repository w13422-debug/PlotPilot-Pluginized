from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


import pytest
from fastapi.testclient import TestClient

from backend.plotpilot_core.api.v1.core import (
    ROUTE_ALLOWLIST,
    CoreHttpAdapter,
    create_core_router,
    route_inventory,
)
from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.publication import PublicationService
from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository
from backend.plotpilot_core.repositories.authority_application import (
    CoreAuthorityApplication,
)
from interfaces.api.settings import BackendSettings


class _RecordingLifecycle:
    def __init__(self) -> None:
        self.started_route_counts: list[int] = []
        self.shutdown_count = 0

    def startup(self, registered_route_count: int) -> None:
        self.started_route_counts.append(registered_route_count)

    def shutdown(self) -> None:
        self.shutdown_count += 1


def _assert_fresh_root_import(tmp_path: Path) -> None:
    data_dir = tmp_path / "fresh-root-import"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PLOTPILOT_PROD_DATA_DIR"] = str(data_dir)
    env["LOG_FILE"] = str(tmp_path / "fresh-root-import.log")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", "import interfaces.main"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        result.stdout.decode("utf-8", errors="replace")
        + result.stderr.decode("utf-8", errors="replace")
    )


def _settings(tmp_path: Path, name: str) -> BackendSettings:
    frontend_dir = tmp_path / name
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html>WebUI</html>", encoding="utf-8")
    return BackendSettings(
        disable_auto_daemon=True,
        frontend_dir=frontend_dir,
        log_file=str(tmp_path / f"{name}.log"),
    )


def test_create_app_mounts_one_real_core_runtime_before_spa_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _assert_fresh_root_import(tmp_path)

    monkeypatch.setenv("PLOTPILOT_PROD_DATA_DIR", str(tmp_path / "module-default"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "module-default.log"))

    from application import paths

    module_data_dir = tmp_path / "module-default"
    module_data_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "DATA_DIR", module_data_dir)

    from interfaces import main

    lifecycle = _RecordingLifecycle()
    monkeypatch.setattr(main, "_get_lifecycle", lambda: lifecycle)

    first_data_dir = tmp_path / "first"
    second_data_dir = tmp_path / "second"
    monkeypatch.setattr(paths, "DATA_DIR", first_data_dir)
    first_app = main.create_app(_settings(tmp_path, "frontend-first"))
    monkeypatch.setattr(paths, "DATA_DIR", second_data_dir)
    second_app = main.create_app(_settings(tmp_path, "frontend-second"))

    first_runtime = first_app.state.webui_runtime
    second_runtime = second_app.state.webui_runtime

    assert type(first_runtime.repository) is CoreAuthorityRepository
    assert type(first_runtime.assets) is AssetStore
    assert type(first_runtime.publication_service) is PublicationService
    assert type(first_runtime.authority) is CoreAuthorityApplication
    assert type(first_runtime.adapter) is CoreHttpAdapter
    assert first_runtime.authority.repository is first_runtime.repository
    assert first_runtime.publication_service.repository is first_runtime.repository
    assert first_runtime.publication_service.assets is first_runtime.assets
    assert first_runtime.authority.publication_service is first_runtime.publication_service
    assert first_runtime.adapter.authority is first_runtime.authority
    assert first_runtime.adapter.publication_service is first_runtime.publication_service
    assert first_runtime.adapter.assets is first_runtime.assets
    assert first_runtime.plugin_runtime.core_runtime is first_runtime.core_runtime
    assert first_runtime.plugin_runtime.repository is first_runtime.repository
    assert first_runtime.plugin_runtime.assets is first_runtime.assets
    assert first_runtime.job_runtime.plugin_runtime is first_runtime.plugin_runtime
    assert first_runtime.job_runtime.repository is first_runtime.repository
    assert first_runtime.job_runtime.assets is first_runtime.assets
    assert (
        first_runtime.job_runtime.execution_authority
        is first_runtime.plugin_runtime.execution_authority
    )
    assert (
        first_runtime.m4_adapters.execution
        is first_runtime.plugin_runtime.execution_authority
    )
    assert first_runtime.private_generation_facade.repository is first_runtime.repository
    assert first_runtime.private_generation_facade.assets is first_runtime.assets
    assert (
        first_runtime.private_generation_facade.job_runtime
        is first_runtime.job_runtime
    )
    assert first_runtime.repository is not second_runtime.repository
    assert first_runtime.authority is not second_runtime.authority
    assert Path(first_runtime.repository.database) == first_data_dir / "core" / "core.db"
    assert Path(second_runtime.repository.database) == second_data_dir / "core" / "core.db"

    core_router = create_core_router(first_runtime.adapter)
    assert len(core_router.routes) == 26
    assert route_inventory(core_router) == ROUTE_ALLOWLIST

    close_calls: list[str] = []
    repository_close = first_runtime.repository.close

    def close_repository() -> None:
        close_calls.append("close")
        repository_close()

    monkeypatch.setattr(first_runtime.repository, "close", close_repository)

    with TestClient(first_app) as client:
        legacy = client.get("/api/v1/novels/")
        assert legacy.status_code == 200

        initial = client.get(
            "/api/v1/core/workspaces",
            params={"limit": 100, "offset": 0},
        )
        assert initial.status_code == 200
        assert initial.json()["schema"] == "core-workspace-page/v1"
        assert initial.json()["items"] == []

        created = client.post(
            "/api/v1/core/workspaces",
            json={
                "schema": "core-workspace-create-command/v1",
                "operation_key": "webui-create",
                "workspace_id": "webui-workspace",
                "workspace_kind": "WritingProject",
                "title": "WebUI Workspace",
            },
        )
        assert created.status_code == 201
        created_body = created.json()

        fetched = client.get("/api/v1/core/workspaces/webui-workspace")
        assert fetched.status_code == 200
        assert fetched.json()["workspace_id"] == "webui-workspace"

        listed = client.get(
            "/api/v1/core/workspaces",
            params={"limit": 100, "offset": 0},
        )
        assert listed.status_code == 200
        assert [item["workspace_id"] for item in listed.json()["items"]] == [
            "webui-workspace"
        ]

        core_v2 = client.get(
            "/api/v2/core/workspaces/webui-workspace/candidates",
            params={"limit": 50},
        )
        assert core_v2.status_code == 200
        assert core_v2.json()["schema"] == "candidate-list-result/v2"

        jobs_v2 = client.get(
            "/api/v2/jobs/webui-workspace",
            params={"limit": 50},
        )
        assert jobs_v2.status_code == 200
        assert jobs_v2.json()["schema"] == "job-list-result/v2"

        private_generation = client.get(
            "/api/v1/webui/workspaces/webui-workspace/chapters/"
            "missing-chapter/generation"
        )
        assert private_generation.status_code == 404
        assert private_generation.json()["schema"] == (
            "webui-chapter-generation-error/v1"
        )

        deleted = client.request(
            "DELETE",
            "/api/v1/core/workspaces/webui-workspace",
            json={
                "schema": "core-workspace-delete-command/v1",
                "operation_key": "webui-delete",
                "workspace_id": "webui-workspace",
                "expected_revision": created_body["revision"],
            },
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        after_delete = client.get(
            "/api/v1/core/workspaces",
            params={"limit": 100, "offset": 0},
        )
        assert after_delete.status_code == 200
        assert after_delete.json()["items"] == []

        persisted = client.post(
            "/api/v1/core/workspaces",
            json={
                "schema": "core-workspace-create-command/v1",
                "operation_key": "webui-persisted-create",
                "workspace_id": "webui-persisted",
                "workspace_kind": "WritingProject",
                "title": "Persisted Workspace",
            },
        )
        assert persisted.status_code == 201

    assert first_runtime.closed
    assert close_calls == ["close"]
    first_runtime.close()
    assert close_calls == ["close"]

    reopened = CoreAuthorityRepository(first_data_dir / "core" / "core.db")
    try:
        assert reopened.get_workspace("webui-persisted").title == "Persisted Workspace"
    finally:
        reopened.close()

    with TestClient(second_app) as second_client:
        isolated = second_client.get(
            "/api/v1/core/workspaces",
            params={"limit": 100, "offset": 0},
        )
        assert isolated.status_code == 200
        assert isolated.json()["items"] == []

    assert second_runtime.closed
    assert len(lifecycle.started_route_counts) == 2
    assert lifecycle.shutdown_count == 2


def test_create_app_calls_legacy_core_then_spa_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from application import paths
    from backend.plotpilot_core.bootstrap import webui_runtime
    from interfaces import main

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "order-data")
    calls: list[str] = []
    monkeypatch.setattr(
        main,
        "register_api_routes",
        lambda app: calls.append("legacy"),
    )
    monkeypatch.setattr(
        webui_runtime,
        "mount_webui_runtime",
        lambda app: calls.append("runtime"),
    )
    monkeypatch.setattr(
        main,
        "_register_spa_fallback",
        lambda app: calls.append("spa"),
    )

    main.create_app(_settings(tmp_path, "frontend-order"))

    assert calls == ["legacy", "runtime", "spa"]
