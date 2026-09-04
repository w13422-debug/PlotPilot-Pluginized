from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest
from plotpilot_autopilot import (
    AutopilotIdentity,
    AutopilotRuntime,
    AutopilotRuntimeError,
    DurableStage,
    build_durable_dag,
)
from plotpilot_autopilot.runtime import build_worker

RELEASE = "a" * 64
SNAPSHOT_HASH = "b" * 64
CREATED_AT = "2026-09-04T00:00:00Z"


class NoAssets:
    def read(self, _asset_id: str) -> object:
        raise AssertionError("fault-recovery test should not read an Asset")

    def create(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("fault-recovery test should not create an Asset")


class ReplayHost:
    """One durable child ledger with an injected lost first poll ACK."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.invoke_keys: list[str] = []
        self._children: dict[str, dict[str, object]] = {}
        self._drop_first_poll = True

    def call(
        self, method: str, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        copied = dict(params)
        self.calls.append((method, copied))
        if method == "host.capability.invoke/v1":
            operation_key = copied["operation_key"]
            assert isinstance(operation_key, str)
            self.invoke_keys.append(operation_key)
            existing = self._children.get(operation_key)
            if existing is not None:
                return dict(existing)
            child = {
                "accepted": True,
                "child_job_id": "child-job-1",
                "child_step_id": "child-step-1",
                "child_run_snapshot_asset_id": "asset-child-snapshot-1",
                "child_run_snapshot_hash": "c" * 64,
                "child_result_contract": "candidate-batch/v1",
                "child_job_event_seq": 0,
            }
            self._children[operation_key] = child
            return dict(child)
        if method == "host.capability.poll/v1":
            if self._drop_first_poll:
                self._drop_first_poll = False
                raise ConnectionError("injected lost poll acknowledgement")
            return {
                "job_snapshot_asset_id": "asset-child-job-1",
                "job_event_page_asset_id": "asset-child-events-1",
                "next_job_event_seq": 1,
                "terminal": True,
                "result_bundle_asset_id": "asset-child-result-1",
                "provenance_receipt_id": "receipt-child-1",
            }
        raise AssertionError(f"unexpected Host method: {method}")


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
        created_at=CREATED_AT,
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


def _run(host: ReplayHost, value: AutopilotIdentity, *, payload_hash: str | None = None):
    return AutopilotRuntime(
        host,
        plugin_release_id=RELEASE,
        assets=NoAssets(),
        max_stage_polls=1,
        poll_budget_seconds=1.0,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.1,
        monotonic=lambda: 0.0,
        sleeper=lambda *_args: False,
    ).run(value, one_stage_dag(), payload_hash=payload_hash)


def test_transport_retry_reuses_the_same_child_operation_across_attempts() -> None:
    host = ReplayHost()

    with pytest.raises(AutopilotRuntimeError, match="lost poll acknowledgement"):
        _run(host, identity(attempt_id="attempt-1", lease_epoch=1))

    recovered = _run(host, identity(attempt_id="attempt-2", lease_epoch=2))

    assert recovered.completed_stage_ids == ("plan",)
    assert host.invoke_keys[0] == host.invoke_keys[1]
    assert len(host._children) == 1
    assert [method for method, _ in host.calls] == [
        "host.capability.invoke/v1",
        "host.capability.poll/v1",
        "host.capability.invoke/v1",
        "host.capability.poll/v1",
    ]


@pytest.mark.parametrize(
    "changed",
    [
        replace(identity(), workspace_id="workspace-2"),
        replace(identity(), run_snapshot_hash="d" * 64),
    ],
)
def test_replay_key_rejects_stable_identity_or_payload_drift(
    changed: AutopilotIdentity,
) -> None:
    baseline_host = ReplayHost()
    with pytest.raises(AutopilotRuntimeError):
        _run(baseline_host, identity())

    changed_host = ReplayHost()
    with pytest.raises(AutopilotRuntimeError):
        _run(changed_host, changed)

    assert baseline_host.invoke_keys[0] != changed_host.invoke_keys[0]

    payload_host = ReplayHost()
    with pytest.raises(AutopilotRuntimeError):
        _run(payload_host, identity(), payload_hash="e" * 64)
    assert baseline_host.invoke_keys[0] != payload_host.invoke_keys[0]


def test_checkpoint_resume_is_not_registered_without_a_commit_path() -> None:
    worker = build_worker(worker_instance_id="autopilot-no-resume")
    domain = worker._domains["autopilot.dag.run/v1"]

    assert domain.start is not None
    assert domain.cancel is not None
    assert domain.resume is None
