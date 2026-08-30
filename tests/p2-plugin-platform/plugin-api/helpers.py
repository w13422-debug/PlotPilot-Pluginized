from __future__ import annotations

import json
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from plotpilot_core.api.v1.plugins import (
    PluginApiBoundaryMiddleware,
    PluginReadFacade,
    create_plugin_router,
)
from plotpilot_core.supervisor import WorkerState, WorkerStatus
from plotpilot_plugin_sdk.package import build_files_sha256


def package_files(
    *,
    plugin_id: str = "com.plotpilot.api.echo",
    version: str = "1.0.0",
    display_name: str = "API Echo",
) -> dict[str, bytes]:
    manifest = {
        "capabilities": [
            {
                "capability_id": "demo.echo/v1",
                "operations": ["run"],
                "result_contract": "artifact-bundle/v1",
            }
        ],
        "compatibility": {
            "core_api": ">=1.0 <2.0",
            "plugin_rpc": "1",
            "ui_host": "1",
        },
        "data": {"format": "demo-echo/v1", "root": "data/rules.json"},
        "display_name": display_name,
        "kind": "data",
        "needs": [],
        "plugin_id": plugin_id,
        "schema": "plotpilot-plugin/v1",
        "settings": None,
        "version": version,
    }
    ordinary = {
        "plugin.json": (
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8"),
        "data/rules.json": b'{"message":"hello"}\n',
    }
    return {**ordinary, "files.sha256": build_files_sha256(ordinary)}


@dataclass
class StatusSource:
    value: WorkerStatus | None = None

    def status(self, worker_id: str) -> WorkerStatus | None:
        return self.value


def worker_status(**changes: object) -> WorkerStatus:
    values: dict[str, object] = {
        "worker_id": "worker.echo",
        "lifecycle_id": "lifecycle.echo",
        "state": WorkerState.READY,
        "release_id": "a" * 64,
        "pin_epoch": 2,
        "retire_epoch": 3,
        "clients": 1,
        "worker_instance_id": "instance.echo",
        "failure": None,
        "exit_code": None,
        "stderr_tail": b"last stderr\n",
    }
    values.update(changes)
    return WorkerStatus(**values)  # type: ignore[arg-type]


def make_app(packages: object, supervisor: object) -> tuple[FastAPI, TestClient]:
    facade = PluginReadFacade(packages, supervisor)  # type: ignore[arg-type]
    router = create_plugin_router(facade)
    app = FastAPI()
    app.add_middleware(PluginApiBoundaryMiddleware, routes=tuple(router.routes))
    app.include_router(router)

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> HTMLResponse:
        return HTMLResponse(f"<html>SPA:{full_path}</html>")

    return app, TestClient(app, raise_server_exceptions=False)
