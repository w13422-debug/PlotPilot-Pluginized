from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256

import pytest
from plotpilot_autopilot import (
    AutopilotIdentity,
    AutopilotRuntime,
    AutopilotRuntimeError,
    CheckpointState,
    DurableStage,
    StageEffect,
    build_checkpoint_envelope,
    build_durable_dag,
)

RELEASE = "a" * 64
SNAPSHOT_HASH = "b" * 64
CREATED_AT = "2026-09-04T00:00:00Z"


@dataclass(frozen=True, slots=True)
class MemoryAsset:
    asset_id: str
    content: bytes
    sha256: str


class MemoryAssets:
    def __init__(self) -> None:
        self._assets: dict[str, MemoryAsset] = {}
        self.reads: list[str] = []

    def put(self, asset_id: str, content: bytes) -> None:
        self._assets[asset_id] = MemoryAsset(
            asset_id,
            content,
            sha256(content).hexdigest(),
        )

    def read(self, asset_id: str) -> MemoryAsset:
        self.reads.append(asset_id)
        return self._assets[asset_id]

    def create(
        self,
        _content: bytes | str,
        *,
        operation_key: str,
        mime: str = "application/json",
    ) -> MemoryAsset:
        raise AssertionError(f"unexpected Asset create: {operation_key} ({mime})")


class NoHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call(
        self, method: str, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.calls.append((method, dict(params)))
        raise AssertionError(f"recovered completed DAG called Host: {method}")


class ReplayHost:
    """Models the accepted Host idempotent child-invocation seam only."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.invoke_keys: list[str] = []
        self._children: dict[str, dict[str, object]] = {}
        self._polls = deque(
            [
                {
                    "job_snapshot_asset_id": "asset-child-job-1",
                    "job_event_page_asset_id": "asset-child-events-1",
                    "next_job_event_seq": 0,
                    "terminal": False,
                    "result_bundle_asset_id": None,
                    "provenance_receipt_id": None,
                },
                {
                    "job_snapshot_asset_id": "asset-child-job-1",
                    "job_event_page_asset_id": "asset-child-events-1",
                    "next_job_event_seq": 1,
                    "terminal": True,
                    "result_bundle_asset_id": "asset-child-result-1",
                    "provenance_receipt_id": "receipt-child-1",
                },
            ]
        )

    def call(
        self, method: str, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        copied = dict(params)
        self.calls.append((method, copied))
        if method == "host.capability.invoke/v1":
            return self._invoke(copied)
        if method == "host.capability.poll/v1":
            return dict(self._polls.popleft())
        raise AssertionError(f"unexpected Host method: {method}")

    def _invoke(self, params: dict[str, object]) -> dict[str, object]:
        operation_key = params["operation_key"]
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


def identity(
    *,
    attempt_id: str = "attempt-source",
    lease_epoch: int = 1,
    workspace_id: str = "workspace-1",
    job_id: str = "job-1",
    step_id: str = "step-1",
    plugin_release_id: str = RELEASE,
    run_snapshot_hash: str = SNAPSHOT_HASH,
) -> AutopilotIdentity:
    return AutopilotIdentity(
        workspace_id=workspace_id,
        job_id=job_id,
        step_id=step_id,
        attempt_id=attempt_id,
        lease_epoch=lease_epoch,
        plugin_release_id=plugin_release_id,
        run_snapshot_hash=run_snapshot_hash,
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


def _stage_operation_key(
    value: AutopilotIdentity,
    *,
    dag_hash: str,
    payload_hash: str,
) -> str:
    parts = (
        "stage",
        value.workspace_id,
        value.job_id,
        value.step_id,
        value.plugin_release_id,
        value.run_snapshot_hash,
        dag_hash,
        payload_hash,
        "plan",
    )
    return "autopilot-stage-" + sha256("\n".join(parts).encode("utf-8")).hexdigest()[:48]


def checkpoint_fixture(
    source: AutopilotIdentity,
    *,
    dag_hash: str,
    payload_hash: str,
) -> tuple[MemoryAssets, str, StageEffect]:
    effect = StageEffect(
        stage_id="plan",
        operation_key=_stage_operation_key(
            source,
            dag_hash=dag_hash,
            payload_hash=payload_hash,
        ),
        child_job_id="child-job-1",
        child_step_id="child-step-1",
        child_run_snapshot_asset_id="asset-child-snapshot-1",
        child_run_snapshot_hash="c" * 64,
        result_bundle_asset_id="asset-child-result-1",
        provenance_receipt_id="receipt-child-1",
        child_result_contract="candidate-batch/v1",
    )
    state = CheckpointState.build(
        source,
        dag_hash=dag_hash,
        previous_checkpoint_hash=None,
        completed_stages=(effect,),
    )
    envelope = build_checkpoint_envelope(
        state,
        state_asset_id="asset-runtime-state-1",
        total_units=1,
    )
    assets = MemoryAssets()
    assets.put("asset-runtime-state-1", state.json_bytes)
    assets.put("asset-checkpoint-current-1", envelope.json_bytes)
    return assets, "asset-checkpoint-current-1", effect


def test_resume_reads_only_the_current_core_selected_checkpoint_and_direct_source() -> None:
    dag = one_stage_dag()
    source = identity()
    current = identity(attempt_id="attempt-current", lease_epoch=2)
    assets, checkpoint_asset_id, effect = checkpoint_fixture(
        source,
        dag_hash=dag.dag_hash,
        payload_hash=dag.dag_hash,
    )
    host = NoHost()
    runtime = AutopilotRuntime(host, plugin_release_id=RELEASE, assets=assets)

    with pytest.raises(AutopilotRuntimeError, match="direct lineage"):
        runtime.run(source, dag, checkpoint_asset_id=checkpoint_asset_id)

    result = runtime.run(
        current,
        dag,
        checkpoint_asset_id=checkpoint_asset_id,
        resume_of_attempt_id=source.attempt_id,
    )

    assert result.completed_stage_ids == ("plan",)
    assert result.stage_effects == (effect,)
    assert result.checkpoint_asset_id == checkpoint_asset_id
    assert assets.reads == [checkpoint_asset_id, "asset-runtime-state-1"]
    assert host.calls == []


def test_resume_rejects_an_older_checkpoint_not_on_the_direct_attempt_edge() -> None:
    dag = one_stage_dag()
    source = identity(attempt_id="attempt-1")
    current = identity(attempt_id="attempt-3", lease_epoch=3)
    assets, checkpoint_asset_id, _effect = checkpoint_fixture(
        source,
        dag_hash=dag.dag_hash,
        payload_hash=dag.dag_hash,
    )

    with pytest.raises(AutopilotRuntimeError, match="direct lineage"):
        AutopilotRuntime(NoHost(), plugin_release_id=RELEASE, assets=assets).run(
            current,
            dag,
            checkpoint_asset_id=checkpoint_asset_id,
            resume_of_attempt_id="attempt-2",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workspace_id", "workspace-2"),
        ("job_id", "job-2"),
        ("step_id", "step-2"),
        ("plugin_release_id", "d" * 64),
        ("run_snapshot_hash", "e" * 64),
    ],
)
def test_resume_rejects_stable_identity_drift(field: str, value: str) -> None:
    dag = one_stage_dag()
    source = identity()
    current = replace(
        identity(attempt_id="attempt-current", lease_epoch=2),
        **{field: value},
    )
    assets, checkpoint_asset_id, _effect = checkpoint_fixture(
        source,
        dag_hash=dag.dag_hash,
        payload_hash=dag.dag_hash,
    )

    with pytest.raises(AutopilotRuntimeError, match="identity drifted"):
        AutopilotRuntime(
            NoHost(),
            plugin_release_id=current.plugin_release_id,
            assets=assets,
        ).run(
            current,
            dag,
            checkpoint_asset_id=checkpoint_asset_id,
            resume_of_attempt_id=source.attempt_id,
        )


def test_resume_rejects_checkpoint_effects_bound_to_another_payload_hash() -> None:
    dag = one_stage_dag()
    source = identity()
    current = identity(attempt_id="attempt-current", lease_epoch=2)
    assets, checkpoint_asset_id, _effect = checkpoint_fixture(
        source,
        dag_hash=dag.dag_hash,
        payload_hash=dag.dag_hash,
    )

    with pytest.raises(AutopilotRuntimeError, match="payload"):
        AutopilotRuntime(NoHost(), plugin_release_id=RELEASE, assets=assets).run(
            current,
            dag,
            checkpoint_asset_id=checkpoint_asset_id,
            resume_of_attempt_id=source.attempt_id,
            payload_hash="f" * 64,
        )


def test_pending_retry_reuses_the_same_stage_operation_key_across_attempts() -> None:
    host = ReplayHost()
    dag = one_stage_dag()
    first = AutopilotRuntime(
        host,
        plugin_release_id=RELEASE,
        assets=MemoryAssets(),
        max_stage_polls=1,
        poll_budget_seconds=1.0,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.1,
        monotonic=lambda: 0.0,
        sleeper=lambda *_args: False,
    ).run(identity(attempt_id="attempt-1", lease_epoch=1), dag)

    second = AutopilotRuntime(
        host,
        plugin_release_id=RELEASE,
        assets=MemoryAssets(),
        max_stage_polls=1,
        poll_budget_seconds=1.0,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.1,
        monotonic=lambda: 0.0,
        sleeper=lambda *_args: False,
    ).run(identity(attempt_id="attempt-2", lease_epoch=2), dag)

    assert first.pending is True
    assert first.stage_effects == ()
    assert second.completed_stage_ids == ("plan",)
    assert host.invoke_keys[0] == host.invoke_keys[1]
    assert len(host._children) == 1
    assert [method for method, _ in host.calls] == [
        "host.capability.invoke/v1",
        "host.capability.poll/v1",
        "host.capability.invoke/v1",
        "host.capability.poll/v1",
    ]