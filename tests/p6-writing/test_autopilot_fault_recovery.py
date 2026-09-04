from __future__ import annotations

import base64
import copy
import json
from hashlib import sha256

import pytest
from plotpilot_autopilot import (
    AutopilotIdentity,
    AutopilotRuntime,
    AutopilotRuntimeError,
    DurableStage,
    build_durable_dag,
)

RELEASE = "a" * 64


class CrashHost:
    """Small strict Host fixture for acknowledgement-loss recovery cases."""

    def __init__(self) -> None:
        self.assets: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.committed_checkpoint_asset_ids: list[str] = []
        self.last_checkpoint_asset_id: str | None = None
        self.crash_before_commit = 0
        self.crash_after_commit_ack = 0
        self.stage_operation_keys: list[str] = []
        self._operations: dict[tuple[str, str], tuple[str, dict[str, object]]] = {}
        self._children: dict[str, dict[str, object]] = {}

    def _once(self, method: str, params: dict[str, object], build) -> dict[str, object]:
        operation_key = params.get("operation_key")
        if not isinstance(operation_key, str):
            raise TypeError("operation key is absent")
        key = (method, operation_key)
        fingerprint = json.dumps(params, sort_keys=True, separators=(",", ":"))
        previous = self._operations.get(key)
        if previous is not None:
            if previous[0] != fingerprint:
                raise ValueError("idempotency payload drift")
            return copy.deepcopy(previous[1])
        result = build()
        self._operations[key] = (fingerprint, copy.deepcopy(result))
        return result

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, dict(params)))
        if method == "host.asset.create/v1":
            return self._once(method, params, lambda: self._create(params))
        if method == "host.asset.read/v1":
            asset_id = params["asset_id"]
            if not isinstance(asset_id, str):
                raise ValueError("invalid asset id")
            content = self.assets[asset_id]
            offset = int(params["offset"])
            chunk = content[offset : offset + int(params["length"])]
            return {
                "base64_chunk": base64.b64encode(chunk).decode("ascii"),
                "next_offset": None
                if offset + len(chunk) == len(content)
                else offset + len(chunk),
                "content_hash": sha256(chunk).hexdigest(),
            }
        if method == "host.capability.invoke/v1":
            return self._once(method, params, lambda: self._invoke(params))
        if method == "host.capability.poll/v1":
            return self._poll(params)
        if method == "host.checkpoint.commit/v1":
            if self.crash_before_commit:
                self.crash_before_commit -= 1
                raise RuntimeError("simulated crash before checkpoint acknowledgement")
            result = self._once(method, params, lambda: self._commit(params))
            if self.crash_after_commit_ack:
                self.crash_after_commit_ack -= 1
                raise RuntimeError("simulated crash after checkpoint acknowledgement")
            return result
        raise AssertionError(method)

    def _create(self, params: dict[str, object]) -> dict[str, object]:
        chunk = base64.b64decode(str(params["base64_chunk"]), validate=True)
        if (
            params["offset"] != 0
            or params["final"] is not True
            or params["total_size"] != len(chunk)
            or sha256(chunk).hexdigest() != params["expected_hash"]
            or sha256(chunk).hexdigest() != params["chunk_hash"]
        ):
            raise ValueError("invalid one-page fixture upload")
        asset_id = "asset-" + sha256(chunk).hexdigest()[:48]
        self.assets[asset_id] = chunk
        return {
            "upload_id": params["upload_id"],
            "accepted_bytes": len(chunk),
            "completed": True,
            "asset_id": asset_id,
        }

    def _invoke(self, params: dict[str, object]) -> dict[str, object]:
        ordinal = len(self._children) + 1
        child_job_id = f"child-job-{ordinal}"
        self.stage_operation_keys.append(str(params["operation_key"]))
        result = {
            "accepted": True,
            "child_job_id": child_job_id,
            "child_step_id": f"child-step-{ordinal}",
            "child_run_snapshot_asset_id": f"asset-child-snapshot-{ordinal}",
            "child_run_snapshot_hash": sha256(
                str(params["operation_key"]).encode()
            ).hexdigest(),
            "child_result_contract": params["expected_result_contract"],
            "child_job_event_seq": 0,
        }
        self._children[child_job_id] = result
        return result

    def _poll(self, params: dict[str, object]) -> dict[str, object]:
        child = self._children[str(params["child_job_id"])]
        ordinal = str(child["child_job_id"]).rsplit("-", 1)[1]
        return {
            "job_snapshot_asset_id": f"asset-child-job-{ordinal}",
            "job_event_page_asset_id": f"asset-child-events-{ordinal}",
            "next_job_event_seq": 1,
            "terminal": True,
            "result_bundle_asset_id": f"asset-child-result-{ordinal}",
            "provenance_receipt_id": f"receipt-child-{ordinal}",
        }

    def _commit(self, params: dict[str, object]) -> dict[str, object]:
        checkpoint_asset_id = params["checkpoint_asset_id"]
        if not isinstance(checkpoint_asset_id, str):
            raise TypeError("invalid checkpoint asset")
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


def identity() -> AutopilotIdentity:
    return AutopilotIdentity(
        workspace_id="workspace-1",
        job_id="job-1",
        step_id="step-1",
        attempt_id="attempt-1",
        lease_epoch=1,
        plugin_release_id=RELEASE,
        run_snapshot_hash="b" * 64,
        created_at="2026-09-04T00:00:00Z",
    )


def dag(*stage_ids: str):
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


def _stage_call_keys(host: CrashHost) -> list[str]:
    return [
        str(params["operation_key"])
        for method, params in host.calls
        if method == "host.capability.invoke/v1"
    ]


def test_crash_before_checkpoint_ack_replays_the_same_stage_operation_key() -> None:
    host = CrashHost()
    host.crash_before_commit = 1
    plan = dag("plan")

    with pytest.raises(AutopilotRuntimeError, match="checkpoint.commit"):
        AutopilotRuntime(host, plugin_release_id=RELEASE).run(identity(), plan)

    assert host.committed_checkpoint_asset_ids == []
    assert len(host.stage_operation_keys) == 1
    first_stage_key = _stage_call_keys(host)[0]

    resumed = AutopilotRuntime(host, plugin_release_id=RELEASE).run(identity(), plan)

    assert resumed.completed_stage_ids == ("plan",)
    assert len(host.stage_operation_keys) == 1
    assert _stage_call_keys(host) == [first_stage_key, first_stage_key]
    assert len(host.committed_checkpoint_asset_ids) == 1


def test_crash_after_checkpoint_ack_resumes_without_skipping_or_duplicating_effects() -> (
    None
):
    host = CrashHost()
    host.crash_after_commit_ack = 1
    plan = dag("plan", "draft")

    with pytest.raises(AutopilotRuntimeError, match="checkpoint.commit"):
        AutopilotRuntime(host, plugin_release_id=RELEASE).run(identity(), plan)

    checkpoint_asset_id = host.last_checkpoint_asset_id
    assert checkpoint_asset_id is not None
    assert len(host.committed_checkpoint_asset_ids) == 1
    assert len(host.stage_operation_keys) == 1
    first_stage_key = host.stage_operation_keys[0]

    resumed = AutopilotRuntime(host, plugin_release_id=RELEASE).run(
        identity(), plan, checkpoint_asset_id=checkpoint_asset_id
    )

    assert resumed.completed_stage_ids == ("plan", "draft")
    assert len(host.committed_checkpoint_asset_ids) == 2
    assert len(host.stage_operation_keys) == 2
    assert host.stage_operation_keys[0] == first_stage_key
    assert _stage_call_keys(host).count(first_stage_key) == 1
    assert [method for method, _ in host.calls].count("host.asset.read/v1") == 2
