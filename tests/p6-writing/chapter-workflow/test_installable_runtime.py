from __future__ import annotations

import json
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
import tomllib
from plotpilot_chapter_workflow import ChapterWorkflow, SessionState
from plotpilot_chapter_workflow.worker import (
    CAPABILITY_IDS,
    PLUGIN_ID,
    ChapterWorkflowWorker,
    ChapterWorkflowWorkerError,
)
from plotpilot_plugin_sdk import assert_valid

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "first-party-plugins" / "chapter-workflow"
BACKEND = PLUGIN / "backend"
PACKAGE = "plotpilot_chapter_workflow"
ENTRYPOINT = f"{PACKAGE}.worker:main"
SDK_REQUIREMENT = "plotpilot-plugin-sdk==0.1.2"
SDK_HASH = "e85421b20a9fb07b17e602afecc992f530513b01dcd16038dc2f869df1cb1bb4"


def _payload() -> dict[str, object]:
    content = "上一章\r\n林岚推开门。"
    return {
        "operation_key": "write-1",
        "target": {
            "workspace_id": "ws-1",
            "document_id": "chapter-1",
            "base_revision_id": "revision-1",
            "base_content_hash": "a" * 64,
        },
        "context_sources": [
            {
                "source_id": "chapter-0",
                "revision_id": "revision-0",
                "kind": "document",
                "content": content,
                "content_hash": sha256(content.encode()).hexdigest(),
            }
        ],
        "skills": [],
        "producer": {
            "plugin_id": PLUGIN_ID,
            "release_id": "b" * 64,
            "job_id": "job-1",
            "step_id": "step-1",
            "attempt_id": "attempt-1",
            "lease_epoch": 1,
            "provenance_receipt_id": "receipt-1",
            "input_snapshot_hash": "c" * 64,
        },
        "instruction": "写出紧张但克制的一章",
    }


def test_manifest_package_and_entrypoint_agree() -> None:
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))

    assert_valid("plotpilot-plugin/v1", manifest)
    assert manifest["schema"] == "plotpilot-plugin/v1"
    assert manifest["plugin_id"] == PLUGIN_ID
    assert manifest["backend"]["entrypoint"] == ENTRYPOINT
    assert manifest["backend"]["wheel"] == "backend/plotpilot_chapter_workflow-1.0.0-py3-none-any.whl"
    assert manifest["backend"]["requirements_lock"] == "backend/requirements.lock"
    assert [item["capability_id"] for item in manifest["capabilities"]] == list(CAPABILITY_IDS)
    assert pyproject["project"]["name"] == "plotpilot-chapter-workflow"
    assert pyproject["project"]["version"] == manifest["version"] == "1.0.0"
    assert pyproject["project"]["dependencies"] == [SDK_REQUIREMENT]
    assert pyproject["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]

    lock = (BACKEND / "requirements.lock").read_text(encoding="utf-8")
    assert f"{SDK_REQUIREMENT} --hash=sha256:{SDK_HASH}" in lock
    assert all(
        line.startswith("#") or not line or " --hash=sha256:" in line
        for line in lock.splitlines()
    )


def test_worker_imports_from_an_isolated_package_context(tmp_path: Path) -> None:
    site = tmp_path / "site-packages"
    shutil.copytree(BACKEND / "src" / PACKAGE, site / PACKAGE)
    shutil.copytree(ROOT / "backend" / "plotpilot_plugin_sdk", site / "plotpilot_plugin_sdk")
    code = (
        "import importlib, sys; "
        f"sys.path.insert(0, {str(site)!r}); "
        "module = importlib.import_module('plotpilot_chapter_workflow.worker'); "
        "assert callable(module.main); "
        "assert module.PLUGIN_ID == 'com.plotpilot.chapter-workflow'"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_worker_dispatches_to_the_accepted_workflow(ports) -> None:
    workflow, _core, broker, _bundles, _publication, _story = ports
    assert isinstance(workflow, ChapterWorkflow)

    snapshot = ChapterWorkflowWorker(workflow).dispatch(
        "writing.chapter.draft/v1", _payload()
    )

    assert snapshot.state is SessionState.RUNNING
    assert len(broker.starts) == 1
    assert broker.starts[0].capability_id == "writing.chapter.draft/v1"


def test_worker_rejects_malformed_payload_before_broker_dispatch(ports) -> None:
    workflow, _core, broker, _bundles, _publication, _story = ports
    malformed = _payload()
    malformed["unknown"] = "nope"

    with pytest.raises(ChapterWorkflowWorkerError):
        ChapterWorkflowWorker(workflow).dispatch("writing.chapter.draft/v1", malformed)

    assert broker.starts == []