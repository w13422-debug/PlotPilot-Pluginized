from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BACKEND = str(ROOT / "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from plotpilot_plugin_sdk.package import build_files_sha256


@pytest.fixture
def package_files() -> dict[str, bytes]:
    manifest = {
        "capabilities": [
            {
                "capability_id": "demo.echo/v1",
                "operations": ["run"],
                "result_contract": "artifact-bundle/v1",
            }
        ],
        "compatibility": {"core_api": ">=1.0 <2.0", "plugin_rpc": "1", "ui_host": "1"},
        "data": {"format": "demo-echo/v1", "root": "data/rules.json"},
        "display_name": "Lifecycle Echo",
        "kind": "data",
        "needs": [],
        "plugin_id": "com.plotpilot.lifecycle.echo",
        "schema": "plotpilot-plugin/v1",
        "version": "1.0.0",
    }
    plugin = (
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()
    ordinary = {"plugin.json": plugin, "data/rules.json": b'{"message":"hello"}\n'}
    return {**ordinary, "files.sha256": build_files_sha256(ordinary)}
