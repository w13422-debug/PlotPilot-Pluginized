from __future__ import annotations

import ast
import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from plotpilot_autopilot import (
    AutopilotIdentity,
    AutopilotRuntime,
    AutopilotRuntimeError,
    DurableStage,
    build_durable_dag,
)
from plotpilot_autopilot.runtime import CandidateProjection, build_worker

from backend.plotpilot_plugin_sdk.rpc import build_meta, build_request

RELEASE = "a" * 64
SNAPSHOT_HASH = "b" * 64
DEADLINE = "2026-09-04T00:00:00Z"


@dataclass(frozen=True, slots=True)
class MemoryAsset:
    asset_id: str
    content: bytes
    sha256: str


class MemoryAssets:
    """A structural Asset seam for focused domain-runtime tests."""

    def __init__(self) -> None:
        self._assets: dict[str, MemoryAsset] = {}
        self.created: list[tuple[str, bytes, str, str]] = []

    def put(self, asset_id: str, content: bytes) -> MemoryAsset:
        asset = MemoryAsset(asset_id, content, sha256(content).hexdigest())
        self._assets[asset_id] = asset
        return asset

    def read(self, asset_id: str) -> MemoryAsset:
        return self._assets[asset_id]

    def create(
        self,
        content: bytes | str,
        *,
        operation_key: str,
        mime: str = "application/json",
    ) -> MemoryAsset:
        data = content.encode("utf-8") if isinstance(content, str) else content
        asset_id = f"asset-created-{len(self.created) + 1}"
        asset = self.put(asset_id, data)
        self.created.append((asset_id, data, operation_key, mime))
        return asset


class FakeHost:
    """Strict Host result fixture without a second persistence authority."""

    def __init__(
        self,
        poll_responses: list[dict[str, object]],
        *,
        initial_event_seq: int = 0,
        completion_receipt_id: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.invoke_operation_keys: list[str] = []
        self.cancel_operation_keys: list[str] = []
        self.complete_operation_keys: list[str] = []
        self._poll_responses = deque(poll_responses)
        self._initial_event_seq = initial_event_seq
        self._children_by_key: dict[str, dict[str, object]] = {}
        self.completion_receipt_id = completion_receipt_id or _receipt_id("attempt-1")

    @property
    def child_count(self) -> int:
        return len(self._children_by_key)

    def call(
        self, method: str, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        copied = dict(params)
        self.calls.append((method, copied))
        if method == "host.capability.invoke/v1":
            return self._invoke(copied)
        if method == "host.capability.poll/v1":
            return self._poll(copied)
        if method == "host.capability.cancel/v1":
            return self._cancel(copied)
        if method == "host.job.complete/v1":
            return self._complete(copied)
        raise AssertionError(f"unexpected Host method: {method}")

    def _invoke(self, params: dict[str, object]) -> dict[str, object]:
        operation_key = params["operation_key"]
        assert isinstance(operation_key, str)
        self.invoke_operation_keys.append(operation_key)
        existing = self._children_by_key.get(operation_key)
        if existing is not None:
            return dict(existing)
        ordinal = len(self._children_by_key) + 1
        child = {
            "accepted": True,
            "child_job_id": f"child-job-{ordinal}",
            "child_step_id": f"child-step-{ordinal}",
            "child_run_snapshot_asset_id": f"asset-child-snapshot-{ordinal}",
            "child_run_snapshot_hash": sha256(operation_key.encode("utf-8")).hexdigest(),
            "child_result_contract": params["expected_result_contract"],
            "child_job_event_seq": self._initial_event_seq,
        }
        self._children_by_key[operation_key] = child
        return dict(child)

    def _poll(self, _params: dict[str, object]) -> dict[str, object]:
        if not self._poll_responses:
            raise AssertionError("test supplied no Host poll response")
        return dict(self._poll_responses.popleft())

    def _cancel(self, params: dict[str, object]) -> dict[str, object]:
        operation_key = params["operation_key"]
        assert isinstance(operation_key, str)
        self.cancel_operation_keys.append(operation_key)
        return {
            "accepted": True,
            "terminal_known": False,
            "child_state": "cancelling",
            "child_job_event_seq": 1,
        }

    def _complete(self, params: dict[str, object]) -> dict[str, object]:
        operation_key = params["operation_key"]
        assert isinstance(operation_key, str)
        self.complete_operation_keys.append(operation_key)
        return {
            "accepted": True,
            "attempt_state": "succeeded",
            "step_state": "succeeded",
            "job_state": "succeeded",
            "provenance_receipt_id": self.completion_receipt_id,
            "job_event_seq": 1,
            "core_event_high_water": 2,
        }


def _receipt_id(attempt_id: str) -> str:
    return "receipt-" + sha256(attempt_id.encode("utf-8")).hexdigest()[:48]


def identity(
    *,
    attempt_id: str = "attempt-1",
    lease_epoch: int = 1,
) -> AutopilotIdentity:
    return AutopilotIdentity(
        workspace_id="workspace-1",
        job_id="job-1",
        step_id="step-1",
        attempt_id=attempt_id,
        lease_epoch=lease_epoch,
        plugin_release_id=RELEASE,
        run_snapshot_hash=SNAPSHOT_HASH,
        created_at=DEADLINE,
    )


def one_stage_dag():
    return build_durable_dag(
        (
            DurableStage(
                stage_id="plan",
                binding_id="binding-plan",
                input_asset_id="asset-input-plan",
            ),
        )
    )


def pending_poll(*, next_event_seq: int = 0) -> dict[str, object]:
    return {
        "job_snapshot_asset_id": "asset-child-job-1",
        "job_event_page_asset_id": "asset-child-events-1",
        "next_job_event_seq": next_event_seq,
        "terminal": False,
        "result_bundle_asset_id": None,
        "provenance_receipt_id": None,
    }


def terminal_poll(*, next_event_seq: int = 1) -> dict[str, object]:
    return {
        "job_snapshot_asset_id": "asset-child-job-1",
        "job_event_page_asset_id": "asset-child-events-1",
        "next_job_event_seq": next_event_seq,
        "terminal": True,
        "result_bundle_asset_id": "asset-child-result-1",
        "provenance_receipt_id": "receipt-child-1",
    }


class Clock:
    def __init__(self, values: list[float]) -> None:
        self._values = deque(values)

    def __call__(self) -> float:
        if not self._values:
            raise AssertionError("test clock exhausted")
        return self._values.popleft()


def test_equal_cursor_waits_then_retries_to_a_terminal_stage() -> None:
    host = FakeHost([pending_poll(), terminal_poll()])
    slept: list[float] = []

    def sleeper(delay_seconds: float, _cancelled: object) -> bool:
        slept.append(delay_seconds)
        return False

    result = AutopilotRuntime(
        host,
        plugin_release_id=RELEASE,
        assets=MemoryAssets(),
        max_stage_polls=2,
        poll_budget_seconds=1.0,
        initial_backoff_seconds=0.05,
        max_backoff_seconds=0.2,
        monotonic=Clock([0.0, 0.0, 0.05]),
        sleeper=sleeper,
    ).run(identity(), one_stage_dag())

    assert result.completed is True
    assert result.completed_stage_ids == ("plan",)
    assert len(result.stage_effects) == 1
    assert slept == [0.05]
    assert [method for method, _ in host.calls] == [
        "host.capability.invoke/v1",
        "host.capability.poll/v1",
        "host.capability.poll/v1",
    ]


def test_poll_budget_exhaustion_is_pending_without_terminal_mutation() -> None:
    host = FakeHost([pending_poll()])
    slept: list[float] = []

    def sleeper(delay_seconds: float, _cancelled: object) -> bool:
        slept.append(delay_seconds)
        return False

    result = AutopilotRuntime(
        host,
        plugin_release_id=RELEASE,
        assets=MemoryAssets(),
        max_stage_polls=1,
        poll_budget_seconds=10.0,
        initial_backoff_seconds=0.05,
        max_backoff_seconds=0.2,
        monotonic=Clock([0.0, 0.0, 0.01]),
        sleeper=sleeper,
    ).run(identity(), one_stage_dag())

    methods = [method for method, _ in host.calls]
    assert result.pending is True
    assert result.pending_stage_id == "plan"
    assert result.completed_stage_ids == ()
    assert result.stage_effects == ()
    assert result.checkpoint is None
    assert result.retry_after_seconds is not None
    assert slept == [0.05]
    assert methods == ["host.capability.invoke/v1", "host.capability.poll/v1"]
    assert "host.checkpoint.commit/v1" not in methods
    assert "host.candidate.stage/v1" not in methods
    assert "host.job.complete/v1" not in methods


def test_cursor_regression_fails_closed_before_any_retry() -> None:
    host = FakeHost([pending_poll(next_event_seq=0)], initial_event_seq=1)

    with pytest.raises(AutopilotRuntimeError, match="moved backwards"):
        AutopilotRuntime(
            host,
            plugin_release_id=RELEASE,
            assets=MemoryAssets(),
            monotonic=Clock([0.0]),
        ).run(identity(), one_stage_dag())

    assert [method for method, _ in host.calls] == [
        "host.capability.invoke/v1",
        "host.capability.poll/v1",
    ]


def test_cancel_interrupts_wait_and_propagates_once_to_the_active_child() -> None:
    host = FakeHost([pending_poll()])
    runtime: AutopilotRuntime
    sleeps: list[float] = []

    def sleeper(delay_seconds: float, _cancelled: object) -> bool:
        sleeps.append(delay_seconds)
        assert runtime.cancel("operator-stop") is True
        return True

    runtime = AutopilotRuntime(
        host,
        plugin_release_id=RELEASE,
        assets=MemoryAssets(),
        max_stage_polls=2,
        poll_budget_seconds=1.0,
        initial_backoff_seconds=0.05,
        max_backoff_seconds=0.2,
        monotonic=Clock([0.0, 0.0]),
        sleeper=sleeper,
    )
    result = runtime.run(identity(), one_stage_dag())

    assert result.cancelled is True
    assert result.pending is False
    assert sleeps == [0.05]
    assert len(host.cancel_operation_keys) == 1
    assert runtime.cancel("operator-stop") is False
    assert len(host.cancel_operation_keys) == 1
    assert [method for method, _ in host.calls] == [
        "host.capability.invoke/v1",
        "host.capability.poll/v1",
        "host.capability.cancel/v1",
    ]


def test_candidate_completion_uses_bundle_and_complete_without_staging_or_publication() -> None:
    assets = MemoryAssets()
    host = FakeHost([terminal_poll()], completion_receipt_id=_receipt_id("attempt-1"))
    candidate = CandidateProjection(
        text="A completed Autopilot candidate.",
        mutation_mode="replace",
        workspace_id="workspace-1",
        document_id="document-1",
        base_revision_id="revision-1",
        base_content_hash="d" * 64,
        source_refs=(),
    )

    result = AutopilotRuntime(
        host,
        plugin_release_id=RELEASE,
        assets=assets,
    ).run(
        identity(),
        one_stage_dag(),
        candidate=candidate,
        worker_run_id="worker-run-1",
    )

    methods = [method for method, _ in host.calls]
    assert result.completed is True
    assert result.result_bundle is not None
    assert result.result_bundle["schema"] == "result-bundle/v1"
    assert result.result_bundle["contract_id"] == "candidate-batch/v1"
    assert result.result_bundle["bundle_type"] == "candidate_batch"
    item = result.result_bundle["items"][0]
    assert isinstance(item, Mapping)
    assert item["schema"] == "candidate-item/v1"
    assert item["mutation"]["mode"] == "replace"
    assert item["mutation"]["payload_schema"] == "core/document-text/v1"
    assert result.result_bundle_asset_id == assets.created[1][0]
    assert result.candidate_stage_operation_key is not None
    assert result.completion_operation_key is not None
    assert methods == [
        "host.capability.invoke/v1",
        "host.capability.poll/v1",
        "host.job.complete/v1",
    ]
    complete_params = host.calls[-1][1]
    assert complete_params["candidate_stage_operation_key"] == result.candidate_stage_operation_key
    assert complete_params["result_bundle_asset_id"] == result.result_bundle_asset_id
    assert "host.candidate.stage/v1" not in methods
    assert "host.checkpoint.commit/v1" not in methods
    assert not any("publication" in method for method in methods)


def _control_request(
    method: str,
    params: dict[str, object],
    *,
    request_id: str,
    operation_id: str,
    release_id: str,
) -> dict[str, object]:
    meta = build_meta(
        "control",
        generation_id="generation-1",
        plugin_release_id=release_id,
        deadline_at=DEADLINE,
        operation_id=operation_id,
    )
    return build_request(method, params, meta, request_id=request_id)


def test_shared_worker_registers_autopilot_lifecycle_and_descriptor() -> None:
    worker = build_worker(worker_instance_id="autopilot-worker-test")
    release_id = "e" * 64
    handshake = worker.handle(
        _control_request(
            "runtime.handshake",
            {
                "host_protocol": "1",
                "generation_id": "generation-1",
                "plugin_release_id": release_id,
                "data_generation_id": None,
            },
            request_id="00000000-0000-4000-8000-000000000001",
            operation_id="handshake-1",
            release_id=release_id,
        )
    )
    assert handshake is not None
    assert handshake["result"]["capabilities"] == ["autopilot.dag.run/v1"]

    describe = worker.handle(
        _control_request(
            "capability.describe",
            {"capability_id": "autopilot.dag.run/v1"},
            request_id="00000000-0000-4000-8000-000000000002",
            operation_id="describe-1",
            release_id=release_id,
        )
    )
    assert describe is not None
    descriptor = describe["result"]["descriptor"]
    assert descriptor["input_schema"] == "run-snapshot/v1"
    assert descriptor["output_schema"] == "result-bundle/v1"
    assert descriptor["result_contract"] == "candidate-batch/v1"
    assert descriptor["supports"] == ["run", "resume", "cancel"]
    domain = worker._domains["autopilot.dag.run/v1"]
    assert domain.start is not None and domain.resume is not None and domain.cancel is not None

    shutdown = worker.handle(
        _control_request(
            "runtime.shutdown",
            {"reason": "test", "deadline_at": DEADLINE},
            request_id="00000000-0000-4000-8000-000000000003",
            operation_id="shutdown-1",
            release_id=release_id,
        )
    )
    assert shutdown is not None and shutdown["result"] == {"accepted": True}
    assert worker.state == "shutdown"


def test_registered_start_handler_consumes_the_worker_bound_run_snapshot() -> None:
    parameters = {
        "schema": "autopilot-dag/v1",
        "stages": [
            {
                "stage_id": "plan",
                "binding_id": "binding-plan",
                "input_asset_id": "asset-input-plan",
                "parameters_asset_id": None,
                "expected_result_contract": "candidate-batch/v1",
                "propagate_cancel": True,
                "depends_on": [],
            }
        ],
        "candidate": {"text": "Worker-bound candidate.", "mode": "replace"},
    }
    parameter_bytes = json.dumps(
        parameters,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assets = MemoryAssets()
    assets.put("asset-autopilot-parameters-1", parameter_bytes)
    host = FakeHost([terminal_poll()])
    bound_context = SimpleNamespace(
        identity=SimpleNamespace(
            workspace_id="workspace-1",
            job_id="job-1",
            step_id="step-1",
            attempt_id="attempt-1",
            lease_epoch=1,
            plugin_release_id=RELEASE,
            run_snapshot_hash=SNAPSHOT_HASH,
            operation_id="worker-start-1",
        ),
        run_snapshot={
            "parameters_asset_id": "asset-autopilot-parameters-1",
            "asset_hashes": [
                {
                    "asset_id": "asset-autopilot-parameters-1",
                    "sha256": sha256(parameter_bytes).hexdigest(),
                }
            ],
            "scope": {"document_id": "document-1"},
            "workspace_id": "workspace-1",
            "input_revisions": [
                {
                    "document_id": "document-1",
                    "revision_id": "revision-1",
                    "content_hash": "d" * 64,
                }
            ],
            "snapshot_id": "snapshot-1",
            "snapshot_hash": SNAPSHOT_HASH,
        },
        assets=assets,
        host=host,
        meta={"deadline_at": DEADLINE},
    )
    worker = build_worker()
    domain = worker._domains["autopilot.dag.run/v1"]
    assert domain.start is not None

    result = domain.start({"checkpoint_asset_id": None}, bound_context)

    assert result["accepted"] is True
    assert result["provenance_receipt_id"] == _receipt_id("attempt-1")
    assert [method for method, _ in host.calls] == [
        "host.capability.invoke/v1",
        "host.capability.poll/v1",
        "host.job.complete/v1",
    ]


def test_runtime_keeps_protocol_and_persistence_authority_in_the_sdk_and_core() -> None:
    root = Path(__file__).resolve().parents[2]
    runtime_path = (
        root
        / "first-party-plugins"
        / "autopilot"
        / "backend"
        / "src"
        / "plotpilot_autopilot"
        / "runtime.py"
    )
    tree = ast.parse(runtime_path.read_text(encoding="utf-8"), filename=str(runtime_path))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }

    assert any(name.endswith("stdio_worker") for name in imported)
    assert not any(name == "plotpilot_core" or name.startswith("plotpilot_core.") for name in imported)
    assert "host.candidate.stage/v1" not in runtime_path.read_text(encoding="utf-8")
    assert "host.checkpoint.commit/v1" not in runtime_path.read_text(encoding="utf-8")
    assert "publish" not in function_names
