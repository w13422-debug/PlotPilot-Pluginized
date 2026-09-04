from __future__ import annotations

import copy
import hashlib
import json

import pytest
from support import (
    authority_rows,
    bundleless_receipt,
    complete_kwargs,
    make_candidate_bundle,
)

from backend.plotpilot_core.publication import PublicationService
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ErrorCode,
    derive_operation_context_identity,
)
from backend.plotpilot_plugin_sdk.verifier import hash_without_field


def stage_kwargs(
    execution_stack,
    bundle_asset,
    receipt,
    *,
    operation_key="stage-1",
    worker_run_id="worker-run-1",
):
    meta = copy.deepcopy(complete_kwargs(bundle_asset, receipt)["operation_meta"])
    meta["operation_id"] = "rpc-stage-1"
    return {
        "job_id": "job-1",
        "step_id": "step-1",
        "attempt_id": "attempt-1",
        "lease_epoch": 1,
        "operation_key": operation_key,
        "worker_run_id": worker_run_id,
        "result_bundle_asset_id": bundle_asset.asset_id,
        "input_snapshot_hash": execution_stack["snapshot"]["snapshot_hash"],
        "operation_meta": meta,
    }


def prepare_candidate_stage(execution_stack, bundle_asset, receipt, **overrides):
    kwargs = stage_kwargs(execution_stack, bundle_asset, receipt)
    kwargs.update(overrides)
    return execution_stack["authority"].stage_candidate_batch(**kwargs)


def candidate_stage_rows(repository):
    return {
        table: [
            tuple(row)
            for row in repository._connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
        ]
        for table in (
            "candidate_batch_operation",
            "candidate",
            "execution_candidate_binding",
            "execution_outcome",
        )
    }


def test_success_terminal_commit_has_one_consistent_receipt_event_outcome(
    execution_stack,
):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    prepared = prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    candidate_id = prepared["staged_items"][0]["candidate_id"]
    assert prepared == {
        "accepted": True,
        "staged_items": [
            {
                "item_id": "item-1",
                "candidate_id": candidate_id,
                "stage_status": "created",
                "publication_eligibility": "eligible",
            }
        ],
        "job_event_seq": 0,
    }
    assert (
        execution_stack["repository"]
        ._connection.execute(
            "SELECT status FROM candidate WHERE candidate_id=?", (candidate_id,)
        )
        .fetchone()[0]
        == "prepared"
    )
    commit = execution_stack["authority"].complete_attempt(
        **complete_kwargs(bundle_asset, receipt)
    )
    assert not commit.replayed
    assert commit.to_dict() == {
        "accepted": True,
        "attempt_state": "succeeded",
        "step_state": "succeeded",
        "job_state": "succeeded",
        "provenance_receipt_id": "receipt-1",
        "job_event_seq": 1,
        "core_event_high_water": 2,
    }
    repository = execution_stack["repository"]
    assert (
        repository._connection.execute(
            "SELECT state FROM execution_attempt"
        ).fetchone()[0]
        == "succeeded"
    )
    assert (
        repository._connection.execute("SELECT state FROM execution_step").fetchone()[0]
        == "succeeded"
    )
    job = repository._connection.execute("SELECT * FROM execution_job").fetchone()
    assert job["job_state"] == "succeeded"
    assert job["job_event_high_water"] == 1 and job["core_event_high_water"] == 2
    core_events = [
        row[0]
        for row in repository._connection.execute(
            "SELECT json_extract(event_json,'$.event_type') FROM execution_core_event ORDER BY core_event_seq"
        )
    ]
    assert core_events == ["job.state.changed", "job.terminal"]
    candidate = repository._connection.execute("SELECT * FROM candidate").fetchone()
    assert candidate["candidate_id"] == candidate_id and candidate["status"] == "staged"
    assert (
        repository._connection.execute(
            "SELECT count(*) FROM candidate_batch_operation"
        ).fetchone()[0]
        == 1
    )
    assert repository.get_document("doc-1").content == "old"
    publication = PublicationService(repository, execution_stack["assets"]).accept(
        "publish-1", candidate["candidate_id"], created_by="user"
    )
    assert repository.get_document("doc-1").content == "new text"
    assert publication.candidate_id == candidate["candidate_id"]
    assert (
        repository._connection.execute(
            "SELECT publication_id FROM execution_publication_binding WHERE job_id='job-1' AND item_id='item-1'"
        ).fetchone()[0]
        == publication.publication_id
    )
    event_types = [
        json.loads(row[0])["event_type"]
        for row in repository._connection.execute(
            "SELECT event_json FROM execution_core_event ORDER BY core_event_seq"
        )
    ]
    assert event_types == ["job.state.changed", "job.terminal", "revision.published"]
    replay = PublicationService(repository, execution_stack["assets"]).accept(
        "publish-1", candidate["candidate_id"], created_by="user"
    )
    assert replay == publication
    assert (
        repository._connection.execute(
            "SELECT count(*) FROM execution_core_event"
        ).fetchone()[0]
        == 3
    )


def test_terminal_ledger_uses_the_frozen_operation_context_identity(execution_stack):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    kwargs = complete_kwargs(bundle_asset, receipt)
    prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    execution_stack["authority"].complete_attempt(**kwargs)
    expected = derive_operation_context_identity(
        kwargs["operation_meta"], expected_lease_epoch=1
    )
    row = (
        execution_stack["repository"]
        ._connection.execute("SELECT context_identity FROM p3_host_operation_ledger")
        .fetchone()
    )
    assert row[0] == expected


def test_prepared_stage_restart_replay_is_exact_and_payload_hash_is_not_in_key(
    execution_stack,
):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    kwargs = stage_kwargs(execution_stack, bundle_asset, receipt)
    first = execution_stack["authority"].stage_candidate_batch(**kwargs)
    before = candidate_stage_rows(execution_stack["repository"])
    operation = (
        execution_stack["repository"]
        ._connection.execute(
            "SELECT operation_key,payload_hash,item_ids_json FROM candidate_batch_operation"
        )
        .fetchone()
    )
    expected_context = derive_operation_context_identity(
        kwargs["operation_meta"], expected_lease_epoch=1
    )
    expected_key = hashlib.sha256(
        (
            f"candidate-operation/v1\n{expected_context}\nhost.job.complete/v1\nstage-1"
        ).encode()
    ).hexdigest()
    assert tuple(operation) == (
        expected_key,
        operation["payload_hash"],
        '["item-1"]',
    )
    assert len(operation["payload_hash"]) == 64
    assert operation["payload_hash"] not in operation["operation_key"]

    execution_stack["repository"].close()
    from backend.plotpilot_core.assets import AssetStore
    from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository

    reopened = CoreAuthorityRepository(execution_stack["database"])
    execution_stack["repository"] = reopened
    execution_stack["authority"] = ExecutionAuthority(
        reopened, AssetStore(execution_stack["asset_root"])
    )
    replay = execution_stack["authority"].stage_candidate_batch(**kwargs)
    assert replay == first
    assert candidate_stage_rows(reopened) == before


def test_canonical_equivalent_bundle_replay_keeps_the_original_candidate_mapping(
    execution_stack,
):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    first = prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    bundle = json.loads(execution_stack["assets"].read(bundle_asset.asset_id))
    equivalent = execution_stack["assets"].put(
        json.dumps(bundle, ensure_ascii=False, indent=2).encode(),
        mime="application/json",
        logical_role="result_bundle",
        provenance="test",
    )
    assert equivalent.asset_id != bundle_asset.asset_id
    replay = prepare_candidate_stage(
        execution_stack,
        equivalent,
        receipt,
        result_bundle_asset_id=equivalent.asset_id,
    )
    assert replay == first
    assert (
        execution_stack["repository"]
        ._connection.execute("SELECT count(*) FROM candidate")
        .fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    "drift",
    ["workspace", "job", "step", "attempt", "lease", "worker", "payload"],
)
def test_same_stage_key_identity_or_payload_drift_is_zero_mutation(
    execution_stack, drift
):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    before = candidate_stage_rows(execution_stack["repository"])
    kwargs = stage_kwargs(execution_stack, bundle_asset, receipt)
    if drift == "job":
        kwargs["job_id"] = "job-other"
    elif drift == "step":
        kwargs["step_id"] = "step-other"
    elif drift == "attempt":
        kwargs["attempt_id"] = "attempt-other"
    elif drift == "lease":
        kwargs["lease_epoch"] = 2
    elif drift == "worker":
        kwargs["worker_run_id"] = "worker-run-other"
    else:
        bundle = json.loads(execution_stack["assets"].read(bundle_asset.asset_id))
        if drift == "workspace":
            bundle["items"][0]["target"]["workspace_id"] = "ws-other"
            bundle["items"][0]["write_set"][0]["workspace_id"] = "ws-other"
        else:
            bundle["warnings"] = ["same-key payload drift"]
        drifted = execution_stack["assets"].put(
            json.dumps(
                bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode(),
            mime="application/json",
            logical_role="result_bundle",
            provenance="test",
        )
        kwargs["result_bundle_asset_id"] = drifted.asset_id
    with pytest.raises(ContractError):
        execution_stack["authority"].stage_candidate_batch(**kwargs)
    assert candidate_stage_rows(execution_stack["repository"]) == before


def test_completion_requires_the_exact_prepared_stage(execution_stack):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    kwargs = complete_kwargs(bundle_asset, receipt)
    before = candidate_stage_rows(execution_stack["repository"])
    with pytest.raises(ContractError) as missing:
        execution_stack["authority"].complete_attempt(**kwargs)
    assert missing.value.code == int(ErrorCode.INVALID_TRANSITION)
    assert candidate_stage_rows(execution_stack["repository"]) == before

    prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    prepared = candidate_stage_rows(execution_stack["repository"])
    kwargs["candidate_stage_operation_key"] = "stage-other"
    with pytest.raises(ContractError) as mismatched:
        execution_stack["authority"].complete_attempt(**kwargs)
    assert mismatched.value.code == int(ErrorCode.INVALID_TRANSITION)
    assert candidate_stage_rows(execution_stack["repository"]) == prepared


def test_attempt_terminal_does_not_publish_a_false_job_terminal_with_sibling_steps(
    execution_two_step_stack,
):
    execution_stack = execution_two_step_stack
    authority = execution_stack["authority"]
    authority.start_attempt(
        job_id="job-1",
        step_id="step-2",
        attempt_id="attempt-2",
        worker_run_id="worker-run-2",
        plugin_id="com.plotpilot.demo",
        release_id="e" * 64,
        package_hash="a" * 64,
        capability_id="writing.chapter.draft/v1",
        generation_id="generation-1",
        preallocated_receipt_id="receipt-2",
    )
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    first_kwargs = complete_kwargs(bundle_asset, receipt)
    prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    commit = authority.complete_attempt(**first_kwargs)
    assert commit.result["attempt_state"] == "succeeded"
    assert commit.result["step_state"] == "succeeded"
    assert commit.result["job_state"] == "running"
    job = (
        execution_stack["repository"]
        ._connection.execute(
            "SELECT job_state,result_bundle_asset_id,provenance_receipt_id,core_event_high_water FROM execution_job"
        )
        .fetchone()
    )
    assert tuple(job) == ("running", None, None, 1)
    events = [
        json.loads(row[0])["event_type"]
        for row in execution_stack["repository"]._connection.execute(
            "SELECT event_json FROM execution_core_event ORDER BY core_event_seq"
        )
    ]
    assert events == ["job.state.changed"]

    receipt2 = bundleless_receipt(receipt)
    receipt2.update(receipt_id="receipt-2", step_id="step-2", attempt_id="attempt-2")
    receipt2["receipt_hash"] = hash_without_field(
        receipt2, "receipt_hash", "provenance-receipt/v1"
    )
    second = complete_kwargs(bundle_asset, receipt2, outcome="failed")
    second.update(
        step_id="step-2",
        attempt_id="attempt-2",
        operation_key="complete-2",
        worker_run_id="worker-run-2",
        result_bundle_asset_id=None,
        candidate_stage_operation_key=None,
        provenance_receipt=receipt2,
    )
    second["operation_meta"].update(
        step_id="step-2", attempt_id="attempt-2", operation_id="rpc-complete-2"
    )
    final = authority.complete_attempt(**second)
    assert final.result["job_state"] == "failed"
    assert final.result["job_event_seq"] == 2
    assert final.result["core_event_high_water"] == 3
    assert (
        execution_stack["repository"]
        ._connection.execute("SELECT count(*) FROM execution_outcome")
        .fetchone()[0]
        == 2
    )
    events = [
        json.loads(row[0])["event_type"]
        for row in execution_stack["repository"]._connection.execute(
            "SELECT event_json FROM execution_core_event ORDER BY core_event_seq"
        )
    ]
    assert events == ["job.state.changed", "job.state.changed", "job.terminal"]
    replayed_first = authority.complete_attempt(**first_kwargs)
    assert replayed_first.replayed and replayed_first.result["job_state"] == "running"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("lease_epoch", 2, ErrorCode.STALE_LEASE),
        ("generation_id", "generation-other", ErrorCode.INCOMPATIBLE_GENERATION),
        ("plugin_release_id", "f" * 64, ErrorCode.INCOMPATIBLE_GENERATION),
    ],
)
def test_operation_meta_is_fenced_before_committed_replay(
    execution_stack, field, value, code
):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    kwargs = complete_kwargs(bundle_asset, receipt)
    prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    execution_stack["authority"].complete_attempt(**kwargs)
    drift = copy.deepcopy(kwargs)
    drift["operation_meta"][field] = value
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].complete_attempt(**drift)
    assert caught.value.code == int(code)


def test_partial_requires_usable_and_incomplete_items(execution_stack):
    bundle_asset, receipt, _item = make_candidate_bundle(
        execution_stack, partial=True, item_status="partial"
    )
    prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    commit = execution_stack["authority"].complete_attempt(
        **complete_kwargs(bundle_asset, receipt, outcome="partial")
    )
    assert commit.result["job_state"] == "partial"
    assert (
        execution_stack["repository"]
        ._connection.execute("SELECT status FROM candidate")
        .fetchone()[0]
        == "staged"
    )


def test_partial_rejects_all_complete_items(execution_stack):
    bundle_asset, receipt, _item = make_candidate_bundle(
        execution_stack, partial=True, item_status="complete"
    )
    before = authority_rows(execution_stack["repository"])
    with pytest.raises(ContractError) as caught:
        prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    assert caught.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)
    assert authority_rows(execution_stack["repository"]) == before


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_bundleless_failed_or_cancelled_is_atomic(execution_stack, outcome):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    repository = execution_stack["repository"]
    if outcome == "cancelled":
        repository._connection.execute(
            "UPDATE execution_attempt SET state='cancelling'"
        )
        repository._connection.execute(
            "UPDATE execution_job SET job_state='cancelling'"
        )
    kwargs = complete_kwargs(bundle_asset, receipt, outcome=outcome)
    kwargs.update(
        result_bundle_asset_id=None,
        candidate_stage_operation_key=None,
        provenance_receipt=bundleless_receipt(receipt),
    )
    commit = execution_stack["authority"].complete_attempt(**kwargs)
    assert commit.result["job_state"] == outcome
    stored_receipt = json.loads(
        repository._connection.execute(
            "SELECT receipt_json FROM execution_receipt"
        ).fetchone()[0]
    )
    assert stored_receipt["bundle_id"] is None and stored_receipt["bundle_hash"] is None
    assert (
        repository._connection.execute("SELECT count(*) FROM candidate").fetchone()[0]
        == 0
    )


def test_terminal_replay_after_restart_is_byte_exact_and_singleton(execution_stack):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    kwargs = complete_kwargs(bundle_asset, receipt)
    prepared = prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    first = execution_stack["authority"].complete_attempt(**kwargs)
    before = candidate_stage_rows(execution_stack["repository"])
    execution_stack["repository"].close()
    from backend.plotpilot_core.assets import AssetStore
    from backend.plotpilot_core.repositories.authority import CoreAuthorityRepository

    reopened = CoreAuthorityRepository(execution_stack["database"])
    execution_stack["repository"] = reopened
    execution_stack["authority"] = ExecutionAuthority(
        reopened, AssetStore(execution_stack["asset_root"])
    )
    second = execution_stack["authority"].complete_attempt(**kwargs)
    assert (
        second.replayed
        and second.response_frame == first.response_frame
        and second.to_dict() == first.to_dict()
    )
    assert candidate_stage_rows(reopened) == before
    stage_replay = execution_stack["authority"].stage_candidate_batch(
        **stage_kwargs(execution_stack, bundle_asset, receipt)
    )
    assert stage_replay["staged_items"] == prepared["staged_items"]
    assert candidate_stage_rows(reopened) == before


def test_payload_or_lineage_drift_is_rejected_without_mutation(execution_stack):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    kwargs = complete_kwargs(bundle_asset, receipt)
    prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    execution_stack["authority"].complete_attempt(**kwargs)
    before = authority_rows(execution_stack["repository"])
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].complete_attempt(**{**kwargs, "local_seq": 2})
    assert caught.value.code == int(ErrorCode.DUPLICATE_REQUEST)
    assert authority_rows(execution_stack["repository"]) == before


def test_new_completion_operation_cannot_mutate_an_already_terminal_job(
    execution_stack,
):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    kwargs = complete_kwargs(bundle_asset, receipt)
    prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    execution_stack["authority"].complete_attempt(**kwargs)
    before = authority_rows(execution_stack["repository"])
    retry = copy.deepcopy(kwargs)
    retry["operation_key"] = "complete-other"
    retry["operation_meta"]["operation_id"] = "rpc-complete-other"
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].complete_attempt(**retry)
    assert caught.value.code == int(ErrorCode.INVALID_TRANSITION)
    assert authority_rows(execution_stack["repository"]) == before


@pytest.mark.parametrize("field", ["lease", "producer", "receipt", "snapshot"])
def test_stale_lease_producer_receipt_or_snapshot_drift_is_zero_mutation(
    execution_stack, field
):
    bundle_asset, receipt, _item = make_candidate_bundle(execution_stack)
    kwargs = complete_kwargs(bundle_asset, receipt)
    prepare_candidate_stage(execution_stack, bundle_asset, receipt)
    if field == "lease":
        kwargs["lease_epoch"] = 2
    elif field == "receipt":
        bad = copy.deepcopy(receipt)
        bad["receipt_id"] = "receipt-other"
        kwargs["provenance_receipt"] = bad
    else:
        raw = execution_stack["assets"].read(bundle_asset.asset_id)
        import json

        bundle = json.loads(raw)
        if field == "producer":
            bundle["producer"]["attempt_id"] = "attempt-other"
        else:
            bundle["input_snapshot_hash"] = "0" * 64
        bundle_asset = execution_stack["assets"].put(
            json.dumps(
                bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode(),
            mime="application/json",
            logical_role="result_bundle",
            provenance="test",
        )
        kwargs["result_bundle_asset_id"] = bundle_asset.asset_id
    before = authority_rows(execution_stack["repository"])
    with pytest.raises((ContractError, Exception)):
        execution_stack["authority"].complete_attempt(**kwargs)
    assert authority_rows(execution_stack["repository"]) == before
