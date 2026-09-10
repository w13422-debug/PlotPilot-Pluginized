from __future__ import annotations

import json
import shutil
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from plotpilot_export_suite.worker import (
    ExportWorkerError,
    _run_export_job,
    capability_descriptor,
    create_worker,
    export_from_worker_context,
)
from plotpilot_plugin_sdk import assert_valid
from plotpilot_plugin_sdk.canonical import canonical_bytes
from plotpilot_plugin_sdk.verifier import request_key, snapshot_hash

PLUGIN = ROOT / "first-party-plugins" / "export-suite"
GOLDEN = ROOT / "contracts" / "golden" / "contract-publication-v1"


@dataclass(frozen=True)
class _HostAsset:
    asset_id: str
    content: bytes
    sha256: str


class _HostAssets:
    def __init__(self, assets: dict[str, bytes]) -> None:
        self.assets = dict(assets)
        self.reads: list[str] = []
        self.creates: list[tuple[str, bytes, str]] = []

    def read(self, asset_id: str) -> _HostAsset:
        self.reads.append(asset_id)
        content = self.assets[asset_id]
        return _HostAsset(asset_id, content, sha256(content).hexdigest())

    def create(self, content: bytes, *, mime: str, operation_key: str) -> _HostAsset:
        data = bytes(content)
        digest = sha256(data).hexdigest()
        asset_id = f"asset-sha256-{digest}"
        self.creates.append((operation_key, data, mime))
        self.assets[asset_id] = data
        return _HostAsset(asset_id, data, digest)


class _Host:
    def __init__(self, *, accepted: bool, receipt_id: str | None) -> None:
        self.accepted = accepted
        self.receipt_id = receipt_id
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, dict(params)))
        return {
            "accepted": self.accepted,
            "provenance_receipt_id": self.receipt_id,
        }


def _golden_context(
    *,
    completion_accepted: bool = True,
    completion_receipt_id: str | None = None,
) -> tuple[SimpleNamespace, _HostAssets]:
    manifest = json.loads((GOLDEN / "export-current-revisions.json").read_text("utf-8"))
    first = "第一章正文\n下一行".encode()
    second = "第二章正文".encode()
    bodies = (first, second)
    for item, body in zip(manifest["ordered_revisions"], bodies, strict=True):
        item["content_hash"] = sha256(body).hexdigest()
    manifest_bytes = canonical_bytes(manifest)

    snapshot = json.loads((GOLDEN / "export-run-snapshot.json").read_text("utf-8"))
    for item, body in zip(snapshot["input_revisions"], bodies, strict=True):
        item["content_hash"] = sha256(body).hexdigest()
    snapshot["asset_hashes"] = [{
        "asset_id": "asset-export-current-revisions",
        "sha256": sha256(manifest_bytes).hexdigest(),
    }]
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)

    assets = _HostAssets({
        "asset-export-current-revisions": manifest_bytes,
        "asset-content-a": first,
        "asset-content-b": second,
    })
    identity = SimpleNamespace(
        capability_id="writing.export/v1",
        workspace_id=snapshot["workspace_id"],
        run_snapshot_id=snapshot["snapshot_id"],
        run_snapshot_hash=snapshot["snapshot_hash"],
        operation_id="export-operation-1",
        plugin_release_id="a" * 64,
        job_id="job-export-1",
        step_id="step-export-1",
        attempt_id="attempt-export-1",
        lease_epoch=1,
    )
    host = _Host(
        accepted=completion_accepted,
        receipt_id=completion_receipt_id,
    )
    return SimpleNamespace(
        identity=identity,
        run_snapshot=snapshot,
        assets=assets,
        host=host,
    ), assets


def test_manifest_and_package_entrypoint_are_first_party_and_asset_only() -> None:
    manifest = json.loads((PLUGIN / "plugin.json").read_text("utf-8"))
    assert_valid("plotpilot-plugin/v1", manifest)
    assert manifest["plugin_id"] == "com.plotpilot.export-suite"
    assert manifest["capabilities"] == [{
        "capability_id": "writing.export/v1",
        "operations": ["run"],
        "result_contract": "artifact-bundle/v1",
    }]
    assert manifest["needs"] == [
        "host.asset.read/v1",
        "host.asset.create/v1",
        "host.job.complete/v1",
    ]
    assert manifest["backend"]["entrypoint"] == "plotpilot_export_suite.worker:main"
    migration = json.loads((PLUGIN / "migrations" / "manifest.json").read_text("utf-8"))
    assert migration == {
        "schema": "p3-module-migrations/v1",
        "module": "plotpilot_export_suite",
        "runner_owner": "P6",
        "steps": [],
    }


def test_source_package_imports_in_an_isolated_package_context(tmp_path: Path) -> None:
    staged_site = tmp_path / "site"
    shutil.copytree(
        PLUGIN / "backend" / "src" / "plotpilot_export_suite",
        staged_site / "plotpilot_export_suite",
    )
    code = (
        "import importlib, sys; "
        f"sys.path.insert(0, {str(staged_site)!r}); "
        "module = importlib.import_module('plotpilot_export_suite.worker'); "
        "assert callable(module.main)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_real_worker_registers_the_one_export_capability() -> None:
    descriptor = capability_descriptor(release_id="a" * 64)
    assert_valid("capability-provider/v1", descriptor)
    worker = create_worker()
    assert worker.plugin_id == "com.plotpilot.export-suite"
    assert worker.plugin_version == "1.0.0"


def test_worker_uses_the_golden_snapshot_through_assets_only_and_is_deterministic() -> None:
    context, assets = _golden_context()

    first = export_from_worker_context(
        context,
        novel_id="novel-1",
        title="星河",
        author="作者",
        premise="简介",
    )
    second = export_from_worker_context(
        context,
        novel_id="novel-1",
        title="星河",
        author="作者",
        premise="简介",
    )

    assert assets.reads == [
        "asset-export-current-revisions",
        "asset-content-a",
        "asset-content-b",
    ] * 2
    assert len(assets.creates) == 2
    assert assets.creates[0] == assets.creates[1]
    assert first.asset_id == second.asset_id
    assert first.receipt.receipt_id == second.receipt.receipt_id
    assert first.receipt.receipt_hash == second.receipt.receipt_hash
    assert first.asset_id in assets.assets
    assert not hasattr(first, "publish")


def test_mismatched_worker_snapshot_fails_before_output_asset_create() -> None:
    context, assets = _golden_context()
    malformed = deepcopy(context.run_snapshot)
    malformed["workspace_id"] = "ws-other"
    context.run_snapshot = malformed

    with pytest.raises(ExportWorkerError, match="mismatches worker identity"):
        export_from_worker_context(context, title="星河")
    assert assets.creates == []


def _expected_handler_receipt() -> str:
    preview, _ = _golden_context()
    return export_from_worker_context(preview).receipt.receipt_id


def test_production_handler_persists_one_export_and_result_bundle_then_completes() -> None:
    receipt_id = _expected_handler_receipt()
    context, assets = _golden_context(completion_receipt_id=receipt_id)

    response = _run_export_job({}, context)

    assert len(assets.creates) == 2
    export_data = assets.creates[0][1]
    export_mime = assets.creates[0][2]
    export_asset_id = f"asset-sha256-{sha256(export_data).hexdigest()}"
    completion_method, completion = context.host.calls[0]
    bundle_asset_id = completion["result_bundle_asset_id"]
    assert isinstance(bundle_asset_id, str)
    bundle = json.loads(assets.assets[bundle_asset_id].decode())
    assert_valid("result-bundle/v1", bundle)
    assert bundle["contract_id"] == "artifact-bundle/v1"
    assert bundle["bundle_type"] == "artifact"
    assert bundle["input_snapshot_hash"] == context.identity.run_snapshot_hash
    assert bundle["producer"] == {
        "plugin_id": "com.plotpilot.export-suite",
        "release_id": context.identity.plugin_release_id,
        "capability_id": context.identity.capability_id,
        "job_id": context.identity.job_id,
        "step_id": context.identity.step_id,
        "attempt_id": context.identity.attempt_id,
        "lease_epoch": context.identity.lease_epoch,
    }
    assert bundle["provenance_receipt_id"] == receipt_id
    assert bundle["items"] == [{
        "schema": "artifact-item/v1",
        "item_id": f"export-artifact-{sha256(export_data).hexdigest()}",
        "artifact_kind": "writing.export/v1",
        "payload_asset_id": export_asset_id,
        "payload_hash": sha256(export_data).hexdigest(),
        "mime": export_mime,
        "source_refs": [
            {
                "workspace_id": context.identity.workspace_id,
                "source_type": "core.chapter",
                "source_id": "doc-a",
                "revision_or_hash": "rev-a:" + sha256("第一章正文\n下一行".encode()).hexdigest(),
            },
            {
                "workspace_id": context.identity.workspace_id,
                "source_type": "core.chapter",
                "source_id": "doc-b",
                "revision_or_hash": "rev-b:" + sha256("第二章正文".encode()).hexdigest(),
            },
        ],
        "status": "complete",
    }]
    assert [method for method, _ in context.host.calls] == ["host.job.complete/v1"]
    assert completion_method == "host.job.complete/v1"
    assert completion == {
        "operation_key": completion["operation_key"],
        "worker_run_id": context.identity.operation_id,
        "outcome": "succeeded",
        "result_bundle_asset_id": bundle_asset_id,
        "candidate_stage_operation_key": None,
        "terminal_detail_asset_id": None,
        "local_seq": 1,
    }
    assert response == {
        "accepted": True,
        "worker_run_id": context.identity.operation_id,
        "provenance_receipt_id": receipt_id,
        "output_streams": [],
    }


@pytest.mark.parametrize(
    ("completion_accepted", "receipt_matches"),
    [(False, True), (True, False)],
)
def test_production_handler_rejects_terminal_completion_receipt_or_acceptance(
    completion_accepted: bool,
    receipt_matches: bool,
) -> None:
    receipt_id = _expected_handler_receipt()
    context, assets = _golden_context(
        completion_accepted=completion_accepted,
        completion_receipt_id=receipt_id if receipt_matches else "wrong-receipt",
    )

    with pytest.raises(ExportWorkerError, match="completion did not preserve Export receipt"):
        _run_export_job({}, context)
    assert len(assets.creates) == 2
    assert [method for method, _ in context.host.calls] == ["host.job.complete/v1"]


def test_worker_has_no_core_repository_or_filesystem_adapter() -> None:
    source = (PLUGIN / "backend" / "src" / "plotpilot_export_suite" / "worker.py").read_text("utf-8")
    for forbidden in (
        "plotpilot_core",
        "sqlite3",
        "Revision",
        "open(",
        "host.candidate.stage/v1",
        "host.publication",
    ):
        assert forbidden not in source
