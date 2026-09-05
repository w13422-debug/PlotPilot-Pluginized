from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import plotpilot_quality_suite.runtime as quality_runtime
import pytest
from plotpilot_autopilot.runtime import build_worker

from backend.plotpilot_core.api.v1.jobs.rpc.command_query import AttemptStartBinding
from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.jobs.http_rpc.chapter_handlers import ChapterHostHandlerSet
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import (
    ContractError,
    canonical_bytes,
    release_id,
    sha256_hex,
)
from backend.plotpilot_plugin_sdk.rpc import build_meta, build_request
from backend.plotpilot_plugin_sdk.stdio_worker import (
    AttemptIdentity,
    HostAssetClient,
    WorkerContext,
)
from backend.plotpilot_plugin_sdk.verifier import (
    hash_without_field,
    request_key,
    snapshot_hash,
    verify_result_bundle,
)

PACKAGE_HASH = "a" * 64
GENERATION_ID = "generation-1"
DEADLINE = "9999-12-31T23:59:59Z"


def _receipt_id(attempt_id: str) -> str:
    return "receipt-" + sha256(attempt_id.encode("utf-8")).hexdigest()[:48]


def _snapshot(
    *,
    plugin_id: str,
    plugin_release_id: str,
    capability_id: str,
    source_asset_id: str,
    source_hash: str,
    revision_id: str,
    run_intent_id: str,
    parameters_asset_id: str | None = None,
    parameters_hash: str | None = None,
) -> dict[str, object]:
    if (parameters_asset_id is None) != (parameters_hash is None):
        raise ValueError("parameters Asset identity and hash must be paired")
    asset_hashes: list[dict[str, str]] = [
        {"asset_id": source_asset_id, "sha256": source_hash}
    ]
    if parameters_asset_id is not None and parameters_hash is not None:
        asset_hashes.append(
            {"asset_id": parameters_asset_id, "sha256": parameters_hash}
        )
    value: dict[str, object] = {
        "schema": "run-snapshot/v1",
        "snapshot_id": f"snapshot-{run_intent_id}",
        "core_contract_version": "1.2.0",
        "workspace_id": "workspace-1",
        "scope": {
            "document_id": "document-1",
            "node_id": None,
            "operation": capability_id,
        },
        "input_revisions": [
            {
                "document_id": "document-1",
                "revision_id": revision_id,
                "content_hash": source_hash,
            }
        ],
        "plan_revision_id": "plan-1",
        "plugin_releases": [
            {
                "plugin_id": plugin_id,
                "release_id": plugin_release_id,
                "package_hash": PACKAGE_HASH,
                "data_generation_id": GENERATION_ID,
            }
        ],
        "plugin_settings_revisions": [],
        "data_bindings": [],
        "skill_releases": [],
        "model_profile_revision_id": None,
        "parameters_asset_id": parameters_asset_id,
        "asset_hashes": asset_hashes,
        "request_key": "0" * 64,
        "run_intent_id": run_intent_id,
        "created_at": "2026-09-05T00:00:00Z",
        "snapshot_hash": "0" * 64,
    }
    value["request_key"] = request_key(value)
    value["snapshot_hash"] = snapshot_hash(value)
    return value


class _ReceiptResolver:
    def __init__(self, assets: AssetStore, attempt: AttemptStartBinding) -> None:
        self._assets = assets
        self._attempt = attempt

    def resolve_provenance_receipt(
        self, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        bundle_asset_id = request["params"]["result_bundle_asset_id"]
        assert isinstance(bundle_asset_id, str)
        bundle_bytes = self._assets.read(bundle_asset_id)
        bundle = json.loads(bundle_bytes)
        staged_items = [
            item["item_id"]
            for item in bundle["items"]
            if item["status"] not in {"failed", "skipped"}
        ]
        receipt: dict[str, object] = {
            "schema": "provenance-receipt/v1",
            "receipt_id": self._attempt.preallocated_receipt_id,
            "plugin_id": self._attempt.plugin_id,
            "release_id": self._attempt.release_id,
            "package_hash": self._attempt.package_hash,
            "capability_id": self._attempt.capability_id,
            "job_id": self._attempt.job_id,
            "step_id": self._attempt.step_id,
            "attempt_id": self._attempt.attempt_id,
            "lease_epoch": self._attempt.lease_epoch,
            "run_snapshot_hash": bundle["input_snapshot_hash"],
            "bundle_id": bundle["bundle_id"],
            "bundle_hash": sha256_hex(bundle_bytes),
            "parent_receipt_ids": [],
            "model_receipt_ids": [],
            "skill_chain_result_refs": bundle["skill_chain_result_refs"],
            "staged_items": staged_items,
            "created_at": "2026-09-05T00:00:00Z",
        }
        receipt["receipt_hash"] = hash_without_field(
            receipt,
            "receipt_hash",
            "provenance-receipt/v1",
        )
        return receipt


class _ProductionHost:
    """Thin synchronous adapter over the accepted production handler set."""

    def __init__(
        self,
        handlers: ChapterHostHandlerSet,
        *,
        attempt: AttemptStartBinding,
    ) -> None:
        self._handlers = handlers
        self._meta = build_meta(
            "attempt",
            generation_id=attempt.generation_id,
            plugin_release_id=attempt.release_id,
            deadline_at=DEADLINE,
            job_id=attempt.job_id,
            step_id=attempt.step_id,
            attempt_id=attempt.attempt_id,
            lease_epoch=attempt.lease_epoch,
        )
        self._request_seq = 0
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.requests: list[dict[str, Any]] = []

    def _request_id(self) -> str:
        self._request_seq += 1
        return f"00000000-0000-4000-8000-{self._request_seq:012d}"

    def _execute(self, request: Mapping[str, Any]) -> dict[str, object]:
        prepared = self._handlers[str(request["method"])](request)
        try:
            result = dict(prepared.result)
            prepared.commit()
        except BaseException:
            prepared.abort()
            raise
        return result

    def call(
        self, method: str, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        request = build_request(
            method,
            dict(params),
            self._meta,
            request_id=self._request_id(),
        )
        self.calls.append((method, dict(params)))
        self.requests.append(request)
        return self._execute(request)

    def replay_exact(self, method: str) -> Mapping[str, object]:
        request = next(
            request for request in reversed(self.requests) if request["method"] == method
        )
        return self._execute(request)

    def execute_request(self, request: Mapping[str, Any]) -> Mapping[str, object]:
        return self._execute(request)


@dataclass
class _Harness:
    repository: CoreAuthorityRepository
    assets: AssetStore
    authority: ExecutionAuthority
    attempt: AttemptStartBinding
    snapshot: dict[str, object]
    host: _ProductionHost
    asset_client: HostAssetClient
    base_revision_id: str
    base_content_hash: str
    source_asset_id: str
    run_snapshot_asset_id: str

    def close(self) -> None:
        self.repository.close()


def _harness(
    root: Path,
    *,
    plugin_id: str,
    capability_id: str,
    worker_run_id: str,
    run_intent_id: str,
) -> _Harness:
    root.mkdir(parents=True, exist_ok=True)
    repository = CoreAuthorityRepository(root / "core.db")
    assets = AssetStore(root / "assets")
    authority = ExecutionAuthority(repository, assets)
    repository.create_workspace(Workspace("workspace-1", "Novel"))
    repository.create_document(Document("document-1", "workspace-1", "Chapter"))
    source_text = "Mara has blue eyes. Danger rises in the hall!"
    base = repository.publish_revision(
        document_id="document-1",
        content=source_text,
        expected_revision_id=None,
        created_by="user",
        revision_id="revision-base",
    )
    source_asset = assets.put(
        source_text.encode("utf-8"),
        mime="text/plain;charset=utf-8",
        logical_role="source",
        provenance="test:production-composition",
    )
    plugin_release_id = release_id(plugin_id, "1.0.0", PACKAGE_HASH)
    parameters_asset_id: str | None = None
    parameters_hash: str | None = None
    if plugin_id == "com.plotpilot.autopilot":
        parameters_asset = assets.put(
            canonical_bytes({"schema": "autopilot-dag/v1", "stages": []}),
            mime="application/json",
            logical_role="parameters",
            provenance="test:production-composition",
        )
        parameters_asset_id = parameters_asset.asset_id
        parameters_hash = parameters_asset.sha256
    snapshot = _snapshot(
        plugin_id=plugin_id,
        plugin_release_id=plugin_release_id,
        capability_id=capability_id,
        source_asset_id=source_asset.asset_id,
        source_hash=source_asset.sha256,
        revision_id=base.revision_id,
        run_intent_id=run_intent_id,
        parameters_asset_id=parameters_asset_id,
        parameters_hash=parameters_hash,
    )
    authority.create_from_verified_snapshot("job-1", snapshot)
    authority.freeze_plan(
        "job-1",
        [
            {
                "step_id": "step-1",
                "depends_on": [],
                "result_contract": "candidate-batch/v1",
            }
        ],
        output_step_id="step-1",
    )
    attempt_id = "attempt-1"
    preallocated_receipt_id = _receipt_id(attempt_id)
    authority.start_attempt(
        job_id="job-1",
        step_id="step-1",
        attempt_id=attempt_id,
        worker_run_id=worker_run_id,
        plugin_id=plugin_id,
        release_id=plugin_release_id,
        package_hash=PACKAGE_HASH,
        capability_id=capability_id,
        generation_id=GENERATION_ID,
        lease_epoch=1,
        preallocated_receipt_id=preallocated_receipt_id,
        expected_result_contract="candidate-batch/v1",
    )
    attempt = AttemptStartBinding(
        "job-1",
        "step-1",
        attempt_id,
        worker_run_id,
        plugin_id,
        plugin_release_id,
        PACKAGE_HASH,
        capability_id,
        GENERATION_ID,
        1,
        preallocated_receipt_id,
        "candidate-batch/v1",
    )
    handlers = ChapterHostHandlerSet(
        authority,
        workspace_id="workspace-1",
        run_snapshot_hash=str(snapshot["snapshot_hash"]),
        attempt=attempt,
        provenance_receipt_resolver=_ReceiptResolver(assets, attempt),
        stream_commit_policy_resolver=lambda *_args: (_ for _ in ()).throw(
            AssertionError("stream policy must not run")
        ),
    )
    host = _ProductionHost(handlers, attempt=attempt)
    asset_client = HostAssetClient(
        host,  # type: ignore[arg-type]
        page_size=1024 * 1024,
        max_bytes=64 * 1024 * 1024,
        max_pages=256,
    )
    run_snapshot_asset = assets.put(
        canonical_bytes(snapshot),
        mime="application/json",
        logical_role="run_snapshot",
        provenance="test:production-composition",
    )
    return _Harness(
        repository,
        assets,
        authority,
        attempt,
        snapshot,
        host,
        asset_client,
        base.revision_id,
        base.content_hash,
        source_asset.asset_id,
        run_snapshot_asset.asset_id,
    )


def _assert_stage_before_complete(harness: _Harness) -> None:
    methods = [method for method, _params in harness.host.calls]
    stage_index = methods.index("host.candidate.stage/v1")
    complete_index = methods.index("host.job.complete/v1")
    assert stage_index < complete_index
    stage_params = harness.host.calls[stage_index][1]
    complete_params = harness.host.calls[complete_index][1]
    assert stage_params["operation_key"] == complete_params[
        "candidate_stage_operation_key"
    ]
    assert stage_params["result_bundle_asset_id"] == complete_params[
        "result_bundle_asset_id"
    ]
    assert stage_params["input_snapshot_hash"] == harness.snapshot["snapshot_hash"]
    assert not any("publication" in method or "publish" in method for method in methods)


def _assert_exact_replay(harness: _Harness) -> None:
    stage_first = harness.host.replay_exact("host.candidate.stage/v1")
    stage_second = harness.host.replay_exact("host.candidate.stage/v1")
    complete_first = harness.host.replay_exact("host.job.complete/v1")
    complete_second = harness.host.replay_exact("host.job.complete/v1")
    assert stage_first == stage_second
    assert complete_first == complete_second
    with harness.repository.read_connection() as connection:
        assert connection.execute("SELECT count(*) FROM candidate").fetchone()[0] > 0
        assert (
            connection.execute("SELECT count(*) FROM publication_receipt").fetchone()[0]
            == 0
        )


def _assert_conflicting_or_cross_attempt_replay_is_rejected(harness: _Harness) -> None:
    stage_request = next(
        request
        for request in harness.host.requests
        if request["method"] == "host.candidate.stage/v1"
    )
    conflicting = {
        **stage_request,
        "id": "00000000-0000-4000-8000-000000000901",
        "params": {
            **stage_request["params"],
            "input_snapshot_hash": "f" * 64,
        },
    }
    with pytest.raises(ContractError):
        harness.host.execute_request(conflicting)

    cross_attempt = {
        **stage_request,
        "id": "00000000-0000-4000-8000-000000000902",
        "meta": {**stage_request["meta"], "attempt_id": "attempt-foreign"},
    }
    with pytest.raises(ContractError, match="bound Attempt"):
        harness.host.execute_request(cross_attempt)


def test_autopilot_runtime_stages_then_completes_on_production_core(tmp_path: Path) -> None:
    operation_id = "worker-autopilot-1"
    worker_run_id = "autopilot-worker-run-" + sha256(
        f"worker-run\njob-1\nstep-1\nattempt-1\n1\n{operation_id}".encode()
    ).hexdigest()[:48]
    harness = _harness(
        tmp_path / "autopilot",
        plugin_id="com.plotpilot.autopilot",
        capability_id="autopilot.dag.run/v1",
        worker_run_id=worker_run_id,
        run_intent_id="intent-autopilot",
    )
    try:
        worker = build_worker()
        domain = worker._domains["autopilot.dag.run/v1"]
        assert domain.start is not None
        result = domain.start(
            {"checkpoint_asset_id": None},
            WorkerContext(
                request={},
                meta={"deadline_at": DEADLINE},
                host=harness.host,  # type: ignore[arg-type]
                assets=harness.asset_client,
                identity=AttemptIdentity(
                    generation_id=GENERATION_ID,
                    plugin_release_id=harness.attempt.release_id,
                    data_generation_id=GENERATION_ID,
                    package_hash=PACKAGE_HASH,
                    workspace_id="workspace-1",
                    job_id="job-1",
                    step_id="step-1",
                    attempt_id="attempt-1",
                    lease_epoch=1,
                    operation_id=operation_id,
                    capability_id="autopilot.dag.run/v1",
                    run_snapshot_asset_id=harness.run_snapshot_asset_id,
                    run_snapshot_id=str(harness.snapshot["snapshot_id"]),
                    run_snapshot_hash=str(harness.snapshot["snapshot_hash"]),
                ),
                run_snapshot=harness.snapshot,
                run_snapshot_asset=None,
            ),
        )

        assert result["accepted"] is True
        assert result["worker_run_id"] == harness.attempt.worker_run_id
        _assert_stage_before_complete(harness)
        stage_params = next(
            params
            for method, params in harness.host.calls
            if method == "host.candidate.stage/v1"
        )
        bundle = json.loads(
            harness.assets.read(str(stage_params["result_bundle_asset_id"]))
        )
        assert harness.snapshot["parameters_asset_id"] in {
            entry["asset_id"] for entry in harness.snapshot["asset_hashes"]
        }
        assert bundle["input_snapshot_hash"] == harness.snapshot["snapshot_hash"]
        assert bundle["producer"] == {
            "plugin_id": harness.attempt.plugin_id,
            "release_id": harness.attempt.release_id,
            "capability_id": harness.attempt.capability_id,
            "job_id": harness.attempt.job_id,
            "step_id": harness.attempt.step_id,
            "attempt_id": harness.attempt.attempt_id,
            "lease_epoch": harness.attempt.lease_epoch,
        }
        assert bundle["items"][0]["source_refs"] == [
            {
                "workspace_id": "workspace-1",
                "source_type": "revision",
                "source_id": harness.base_revision_id,
                "revision_or_hash": harness.base_content_hash,
            }
        ]
        with harness.repository.read_connection() as connection:
            assert connection.execute(
                "SELECT state FROM execution_attempt WHERE attempt_id='attempt-1'"
            ).fetchone()[0] == "succeeded"
            assert connection.execute(
                "SELECT status FROM candidate"
            ).fetchone()[0] == "staged"
        _assert_exact_replay(harness)
        _assert_conflicting_or_cross_attempt_replay_is_rejected(harness)
    finally:
        harness.close()


def test_quality_runtime_stages_then_completes_on_production_core(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path / "quality",
        plugin_id="com.plotpilot.quality-suite",
        capability_id="quality.review/v1",
        worker_run_id="worker-quality-1",
        run_intent_id="intent-quality",
    )
    try:
        attempt_identity = AttemptIdentity(
            generation_id=GENERATION_ID,
            plugin_release_id=harness.attempt.release_id,
            data_generation_id=GENERATION_ID,
            package_hash=PACKAGE_HASH,
            workspace_id="workspace-1",
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
            operation_id=harness.attempt.worker_run_id,
            capability_id="quality.review/v1",
            run_snapshot_asset_id=harness.run_snapshot_asset_id,
            run_snapshot_id=str(harness.snapshot["snapshot_id"]),
            run_snapshot_hash=str(harness.snapshot["snapshot_hash"]),
        )
        result = quality_runtime._run_quality_job(
            {"checkpoint_asset_id": None},
            WorkerContext(
                request={},
                meta={},
                host=harness.host,  # type: ignore[arg-type]
                assets=harness.asset_client,
                identity=attempt_identity,
                run_snapshot=harness.snapshot,
                run_snapshot_asset=None,
            ),
        )

        assert result["accepted"] is True
        _assert_stage_before_complete(harness)
        with harness.repository.read_connection() as connection:
            assert connection.execute(
                "SELECT state FROM execution_attempt WHERE attempt_id='attempt-1'"
            ).fetchone()[0] == "partial"
            assert connection.execute(
                "SELECT count(*) FROM candidate WHERE status='staged'"
            ).fetchone()[0] == 3
        _assert_exact_replay(harness)
        _assert_conflicting_or_cross_attempt_replay_is_rejected(harness)
    finally:
        harness.close()


def test_production_core_rejects_candidate_completion_without_stage(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path / "missing-stage",
        plugin_id="com.plotpilot.autopilot",
        capability_id="autopilot.dag.run/v1",
        worker_run_id="worker-autopilot-1",
        run_intent_id="intent-missing-stage",
    )
    try:
        payload = harness.assets.put(
            b"unstaged candidate",
            mime="text/plain;charset=utf-8",
            logical_role="candidate_payload",
            provenance="test:production-composition",
        )
        item = {
            "schema": "candidate-item/v1",
            "item_id": "candidate-item-unstaged",
            "item_kind": "document",
            "target": {
                "workspace_id": "workspace-1",
                "entity_kind": "document",
                "entity_id": "document-1",
            },
            "mutation": {
                "mode": "replace",
                "payload_schema": "core/document-text/v1",
                "payload_hash": payload.sha256,
            },
            "payload_asset_id": payload.asset_id,
            "base": {
                "revision_id": harness.base_revision_id,
                "content_hash": harness.base_content_hash,
            },
            "write_set": [
                {
                    "workspace_id": "workspace-1",
                    "entity_kind": "document",
                    "entity_id": "document-1",
                    "revision_id": harness.base_revision_id,
                    "content_hash": harness.base_content_hash,
                }
            ],
            "parent_candidate_ids": [],
            "source_refs": [],
            "status": "complete",
        }
        bundle = {
            "schema": "result-bundle/v1",
            "contract_id": "candidate-batch/v1",
            "bundle_id": "candidate-bundle-unstaged",
            "bundle_type": "candidate_batch",
            "producer": {
                "plugin_id": harness.attempt.plugin_id,
                "release_id": harness.attempt.release_id,
                "capability_id": harness.attempt.capability_id,
                "job_id": harness.attempt.job_id,
                "step_id": harness.attempt.step_id,
                "attempt_id": harness.attempt.attempt_id,
                "lease_epoch": harness.attempt.lease_epoch,
            },
            "input_snapshot_hash": harness.snapshot["snapshot_hash"],
            "items": [item],
            "warnings": [],
            "partial": False,
            "provenance_receipt_id": harness.attempt.preallocated_receipt_id,
            "skill_chain_result_refs": [],
        }
        verify_result_bundle(
            bundle,
            snapshot_workspace_id="workspace-1",
            snapshot_hash_value=str(harness.snapshot["snapshot_hash"]),
        )
        bundle_asset = harness.assets.put(
            canonical_bytes(bundle),
            mime="application/json",
            logical_role="result_bundle",
            provenance="test:production-composition",
        )

        with pytest.raises(ContractError, match="durable prepared stage"):
            harness.host.call(
                "host.job.complete/v1",
                {
                    "operation_key": "complete-without-stage",
                    "worker_run_id": harness.attempt.worker_run_id,
                    "outcome": "succeeded",
                    "result_bundle_asset_id": bundle_asset.asset_id,
                    "candidate_stage_operation_key": "missing-stage-key",
                    "terminal_detail_asset_id": None,
                    "local_seq": 1,
                },
            )
        with harness.repository.read_connection() as connection:
            assert connection.execute("SELECT count(*) FROM candidate").fetchone()[0] == 0
            assert connection.execute(
                "SELECT state FROM execution_attempt WHERE attempt_id='attempt-1'"
            ).fetchone()[0] == "running"
    finally:
        harness.close()
