from __future__ import annotations

import base64
import copy
import json
from hashlib import sha256
from typing import Any

import pytest
from plotpilot_autopilot import (
    AutopilotIdentity,
    AutopilotRuntime,
    AutopilotRuntimeError,
    DAGValidationError,
    DurableStage,
    build_durable_dag,
)

RELEASE = "a" * 64
SNAPSHOT_HASH = "b" * 64


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class FakeHost:
    """Strict in-memory Host fixture; it is not a second production authority."""

    def __init__(self) -> None:
        self.assets: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.committed_checkpoint_asset_ids: list[str] = []
        self.last_checkpoint_asset_id: str | None = None
        self.crash_before_commit = 0
        self.crash_after_commit_ack = 0
        self._operations: dict[tuple[str, str], tuple[str, dict[str, object]]] = {}
        self._uploads: dict[str, dict[str, object]] = {}
        self._children: dict[str, dict[str, object]] = {}
        self.stage_operation_keys: list[str] = []

    def _idempotent(
        self,
        method: str,
        params: dict[str, object],
        build: Any,
    ) -> dict[str, object]:
        operation_key = params.get("operation_key")
        if not isinstance(operation_key, str):
            raise TypeError(f"{method} omitted operation_key")
        key = (method, operation_key)
        fingerprint = _canonical(params)
        prior = self._operations.get(key)
        if prior is not None:
            if prior[0] != fingerprint:
                raise ValueError("same idempotency key reused with different payload")
            return copy.deepcopy(prior[1])
        result = build()
        self._operations[key] = (fingerprint, copy.deepcopy(result))
        return result

    def _create_asset(self, params: dict[str, object]) -> dict[str, object]:
        def build() -> dict[str, object]:
            upload_id = params["upload_id"]
            if not isinstance(upload_id, str):
                raise TypeError("upload_id must be a string")
            chunk = base64.b64decode(str(params["base64_chunk"]), validate=True)
            if sha256(chunk).hexdigest() != params["chunk_hash"]:
                raise ValueError("upload chunk hash drift")
            state = self._uploads.setdefault(
                upload_id,
                {
                    "data": b"",
                    "expected_hash": params["expected_hash"],
                    "total_size": params["total_size"],
                    "completed": False,
                    "asset_id": None,
                },
            )
            if (
                state["expected_hash"] != params["expected_hash"]
                or state["total_size"] != params["total_size"]
                or state["completed"]
            ):
                raise ValueError("immutable upload identity drift")
            if params["offset"] != len(state["data"]):
                raise ValueError("upload offset is not contiguous")
            state["data"] = state["data"] + chunk
            if params["final"]:
                if (
                    len(state["data"]) != state["total_size"]
                    or sha256(state["data"]).hexdigest() != state["expected_hash"]
                ):
                    raise ValueError("final upload content drift")
                state["completed"] = True
                state["asset_id"] = "asset-" + str(state["expected_hash"])[:48]
                self.assets[str(state["asset_id"])] = state["data"]
            return {
                "upload_id": upload_id,
                "accepted_bytes": len(state["data"]),
                "completed": state["completed"],
                "asset_id": state["asset_id"],
            }

        return self._idempotent("host.asset.create/v1", params, build)

    def _read_asset(self, params: dict[str, object]) -> dict[str, object]:
        asset_id = params["asset_id"]
        if not isinstance(asset_id, str) or asset_id not in self.assets:
            raise ValueError("unknown immutable asset")
        offset = params["offset"]
        length = params["length"]
        if type(offset) is not int or type(length) is not int:
            raise ValueError("asset page range is invalid")
        data = self.assets[asset_id]
        chunk = data[offset : offset + length]
        next_offset = None if offset + len(chunk) == len(data) else offset + len(chunk)
        return {
            "base64_chunk": base64.b64encode(chunk).decode("ascii"),
            "next_offset": next_offset,
            "content_hash": sha256(chunk).hexdigest(),
        }

    def _invoke_stage(self, params: dict[str, object]) -> dict[str, object]:
        def build() -> dict[str, object]:
            operation_key = str(params["operation_key"])
            ordinal = len(self._children) + 1
            child_job_id = f"child-job-{ordinal}"
            result = {
                "accepted": True,
                "child_job_id": child_job_id,
                "child_step_id": f"child-step-{ordinal}",
                "child_run_snapshot_asset_id": f"asset-child-snapshot-{ordinal}",
                "child_run_snapshot_hash": sha256(
                    operation_key.encode("utf-8")
                ).hexdigest(),
                "child_result_contract": params["expected_result_contract"],
                "child_job_event_seq": 0,
            }
            self._children[child_job_id] = result
            self.stage_operation_keys.append(operation_key)
            return result

        return self._idempotent("host.capability.invoke/v1", params, build)

    def _poll_stage(self, params: dict[str, object]) -> dict[str, object]:
        child_job_id = params["child_job_id"]
        child = self._children.get(str(child_job_id))
        if child is None:
            raise ValueError("unknown child job")
        ordinal = str(child["child_job_id"]).rsplit("-", 1)[1]
        return {
            "job_snapshot_asset_id": f"asset-child-job-{ordinal}",
            "job_event_page_asset_id": f"asset-child-events-{ordinal}",
            "next_job_event_seq": 1,
            "terminal": True,
            "result_bundle_asset_id": f"asset-child-result-{ordinal}",
            "provenance_receipt_id": f"receipt-child-{ordinal}",
        }

    def _commit_checkpoint(self, params: dict[str, object]) -> dict[str, object]:
        if self.crash_before_commit:
            self.crash_before_commit -= 1
            raise RuntimeError("simulated crash before checkpoint acknowledgement")

        def build() -> dict[str, object]:
            checkpoint_asset_id = params["checkpoint_asset_id"]
            if (
                not isinstance(checkpoint_asset_id, str)
                or checkpoint_asset_id not in self.assets
            ):
                raise ValueError("checkpoint asset is absent")
            checkpoint = json.loads(self.assets[checkpoint_asset_id].decode("utf-8"))
            self.committed_checkpoint_asset_ids.append(checkpoint_asset_id)
            self.last_checkpoint_asset_id = checkpoint_asset_id
            return {
                "accepted": True,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "completed_units": checkpoint["completed_units"],
                "total_units": checkpoint["total_units"],
                "job_event_seq": len(self.committed_checkpoint_asset_ids),
            }

        result = self._idempotent("host.checkpoint.commit/v1", params, build)
        if self.crash_after_commit_ack:
            self.crash_after_commit_ack -= 1
            raise RuntimeError("simulated crash after checkpoint acknowledgement")
        return result

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, dict(params)))
        if method == "host.asset.create/v1":
            return self._create_asset(params)
        if method == "host.asset.read/v1":
            return self._read_asset(params)
        if method == "host.capability.invoke/v1":
            return self._invoke_stage(params)
        if method == "host.capability.poll/v1":
            return self._poll_stage(params)
        if method == "host.checkpoint.commit/v1":
            return self._commit_checkpoint(params)
        raise AssertionError(f"unexpected Host method: {method}")


def make_identity(*, release: str = RELEASE) -> AutopilotIdentity:
    return AutopilotIdentity(
        workspace_id="workspace-1",
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        lease_epoch=1,
        plugin_release_id=release,
        run_snapshot_hash=SNAPSHOT_HASH,
        created_at="2026-09-04T00:00:00Z",
    )


def serial_dag(*stage_ids: str):
    return build_durable_dag(
        tuple(
            DurableStage(
                stage_id=stage_id,
                binding_id=f"binding-{stage_id}",
                input_asset_id=f"asset-input-{stage_id}",
                depends_on=() if index == 0 else (stage_ids[index - 1],),
            )
            for index, stage_id in enumerate(stage_ids)
        )
    )


def test_durable_runtime_checkpoints_every_stage_through_only_host_ports() -> None:
    host = FakeHost()
    dag = serial_dag("plan", "draft", "audit")

    result = AutopilotRuntime(host, plugin_release_id=RELEASE).run(make_identity(), dag)

    assert result.completed is True
    assert result.completed_stage_ids == ("plan", "draft", "audit")
    assert result.checkpoint_asset_id == host.last_checkpoint_asset_id
    assert result.checkpoint is not None and isinstance(
        result.checkpoint.checkpoint, dict
    )
    assert len(host.stage_operation_keys) == 3
    assert len(host.committed_checkpoint_asset_ids) == 3
    checkpoints = [
        json.loads(host.assets[asset_id].decode("utf-8"))
        for asset_id in host.committed_checkpoint_asset_ids
    ]
    assert [item["checkpoint_seq"] for item in checkpoints] == [1, 2, 3]
    assert [item["completed_units"] for item in checkpoints] == [1, 2, 3]
    assert all(item["total_units"] == 3 for item in checkpoints)
    assert {method for method, _ in host.calls} == {
        "host.asset.create/v1",
        "host.capability.invoke/v1",
        "host.capability.poll/v1",
        "host.checkpoint.commit/v1",
    }


def test_empty_single_and_maximum_dags_are_deterministic() -> None:
    empty_host = FakeHost()
    empty = AutopilotRuntime(empty_host, plugin_release_id=RELEASE).run(
        make_identity(), serial_dag()
    )
    assert empty.completed_stage_ids == ()
    assert empty.checkpoint is None
    assert empty_host.calls == []

    one_host = FakeHost()
    one = AutopilotRuntime(one_host, plugin_release_id=RELEASE).run(
        make_identity(), serial_dag("only")
    )
    assert one.completed_stage_ids == ("only",)
    assert len(one_host.committed_checkpoint_asset_ids) == 1

    stages = tuple(
        DurableStage(
            stage_id=f"s{index:02d}",
            binding_id=f"binding-{index:02d}",
            input_asset_id=f"asset-input-{index:02d}",
            depends_on=() if index == 0 else (f"s{index - 1:02d}",),
        )
        for index in range(64)
    )
    maximum = build_durable_dag(stages, max_stages=64)
    assert maximum.ordered_stage_ids == tuple(f"s{index:02d}" for index in range(64))
    with pytest.raises(DAGValidationError, match="maximum"):
        build_durable_dag(
            stages + (DurableStage("s64", "binding-64", "asset-input-64"),),
            max_stages=64,
        )


def test_invalid_dags_fail_closed_before_any_host_effect() -> None:
    host = FakeHost()
    with pytest.raises(DAGValidationError, match="missing dependencies"):
        build_durable_dag(
            (DurableStage("stage-a", "binding-a", "asset-a", depends_on=("missing",)),)
        )
    with pytest.raises(DAGValidationError, match="cycle"):
        build_durable_dag(
            (
                DurableStage(
                    "stage-a", "binding-a", "asset-a", depends_on=("stage-b",)
                ),
                DurableStage(
                    "stage-b", "binding-b", "asset-b", depends_on=("stage-a",)
                ),
            )
        )
    assert host.calls == []


def test_same_release_recovery_reads_host_assets_and_does_not_repeat_stages() -> None:
    host = FakeHost()
    dag = serial_dag("plan", "draft")
    initial = AutopilotRuntime(host, plugin_release_id=RELEASE).run(
        make_identity(), dag
    )
    before = list(host.stage_operation_keys)

    recovered = AutopilotRuntime(host, plugin_release_id=RELEASE).run(
        make_identity(), dag, checkpoint_asset_id=initial.checkpoint_asset_id
    )

    assert recovered.completed_stage_ids == ("plan", "draft")
    assert recovered.checkpoint_asset_id == initial.checkpoint_asset_id
    assert host.stage_operation_keys == before
    assert [method for method, _ in host.calls].count("host.asset.read/v1") == 2


def test_stale_release_and_idempotency_payload_drift_are_rejected() -> None:
    host = FakeHost()
    dag = serial_dag("plan")
    result = AutopilotRuntime(host, plugin_release_id=RELEASE).run(make_identity(), dag)

    with pytest.raises(AutopilotRuntimeError, match="stale|belongs"):
        AutopilotRuntime(host, plugin_release_id="c" * 64).run(
            make_identity(release="c" * 64),
            dag,
            checkpoint_asset_id=result.checkpoint_asset_id,
        )

    params = {
        "operation_key": "operation-replay-1",
        "binding_id": "binding-a",
        "input_asset_id": "asset-a",
        "parameters_asset_id": None,
        "expected_result_contract": "candidate-batch/v1",
        "propagate_cancel": True,
    }
    host.call("host.capability.invoke/v1", params)
    with pytest.raises(ValueError, match="idempotency key"):
        host.call("host.capability.invoke/v1", {**params, "input_asset_id": "asset-b"})
