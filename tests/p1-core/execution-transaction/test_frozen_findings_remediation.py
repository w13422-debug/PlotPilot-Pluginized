from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from backend.plotpilot_core.candidates import CandidateError, CandidateService
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.publication import PublicationService
from backend.plotpilot_core.broker.service import CallerAttemptContext
from backend.plotpilot_core.repositories.execution import ExecutionAuthority
from backend.plotpilot_plugin_sdk import (
    ContractError,
    ErrorCode,
    canonical_bytes,
    sha256_hex,
    verify_skill_chain,
)
from backend.plotpilot_plugin_sdk.verifier import hash_without_field

from support import PACKAGE, RELEASE, authority_rows, complete_kwargs, make_candidate_bundle


def _replace_bundle(stack, bundle_asset, receipt, mutate):
    bundle = json.loads(stack["assets"].read(bundle_asset.asset_id))
    mutate(bundle)
    raw = canonical_bytes(bundle)
    stored = stack["assets"].put(
        raw, mime="application/json", logical_role="result_bundle", provenance="plugin:remediation-test"
    )
    updated = copy.deepcopy(receipt)
    updated["bundle_id"] = bundle["bundle_id"]
    updated["bundle_hash"] = stored.sha256
    updated["skill_chain_result_refs"] = list(bundle["skill_chain_result_refs"])
    updated["staged_items"] = [
        item["item_id"] for item in bundle["items"] if item["status"] not in {"failed", "skipped"}
    ]
    updated["receipt_hash"] = hash_without_field(updated, "receipt_hash", "provenance-receipt/v1")
    return stored, updated, bundle


def _start_second(stack, *, step_id="step-2", attempt_id="attempt-2", receipt_id="receipt-2"):
    stack["authority"].start_attempt(
        job_id="job-1", step_id=step_id, attempt_id=attempt_id,
        worker_run_id=f"worker-{attempt_id}", plugin_id="com.plotpilot.demo",
        release_id=RELEASE, package_hash=PACKAGE, capability_id="writing.chapter.draft/v1",
        generation_id="generation-1", preallocated_receipt_id=receipt_id,
        expected_result_contract="candidate-batch/v1",
    )


def _second_candidate(stack, *, parent_ids=()):
    first_asset, first_receipt, _ = make_candidate_bundle(stack)

    def mutate(bundle):
        bundle["bundle_id"] = "bundle-2"
        bundle["provenance_receipt_id"] = "receipt-2"
        bundle["producer"].update(step_id="step-2", attempt_id="attempt-2")
        bundle["items"][0]["parent_candidate_ids"] = list(parent_ids)

    asset, receipt, _ = _replace_bundle(stack, first_asset, first_receipt, mutate)
    receipt.update(receipt_id="receipt-2", step_id="step-2", attempt_id="attempt-2")
    receipt["receipt_hash"] = hash_without_field(receipt, "receipt_hash", "provenance-receipt/v1")
    kwargs = complete_kwargs(asset, receipt)
    kwargs.update(
        step_id="step-2", attempt_id="attempt-2", worker_run_id="worker-attempt-2",
        operation_key="complete-2", provenance_receipt=receipt,
    )
    kwargs["operation_meta"].update(
        step_id="step-2", attempt_id="attempt-2", operation_id="rpc-complete-2"
    )
    return kwargs


def _skill_chain_ref(stack, *, chain_id="chain-1"):
    chain = {
        "schema": "skill-chain-result/v1",
        "chain_id": chain_id,
        "run_snapshot_hash": stack["snapshot"]["snapshot_hash"],
        "result_bundle_id": "bundle-1",
        "result_item_id": "item-1",
        "stream_id": None,
        "acked_prefix_hash": None,
        "receipt_ids": ["skill-receipt-1"],
        "receipt_hashes": ["b" * 64],
        "chain_status": "succeeded",
        "input_hash": "a" * 64,
        "final_output_asset_id": None,
        "final_output_hash": None,
        "chain_hash": "c" * 64,
    }
    asset = stack["assets"].put(
        canonical_bytes(chain),
        mime="application/json",
        logical_role="skill_chain_result",
        provenance="skill:test",
    )
    return {
        "schema": "skill-chain-ref/v1",
        "chain_result_id": chain_id,
        "asset_id": asset.asset_id,
        "asset_hash": asset.sha256,
        "result_bundle_id": "bundle-1",
        "result_item_id": "item-1",
        "stream_id": None,
        "acked_prefix_hash": None,
    }


def test_f002_attempt_acquisition_has_one_active_monotonic_fence(execution_stack):
    authority = execution_stack["authority"]
    with pytest.raises(ContractError):
        authority.start_attempt(
            job_id="job-1", step_id="step-1", attempt_id="attempt-racing",
            worker_run_id="worker-racing", plugin_id="com.plotpilot.demo", release_id=RELEASE,
            package_hash=PACKAGE, capability_id="writing.chapter.draft/v1",
            generation_id="generation-1", expected_result_contract="candidate-batch/v1",
        )
    connection = execution_stack["repository"]._connection
    connection.execute("UPDATE execution_attempt SET state='failed' WHERE attempt_id='attempt-1'")
    connection.execute("UPDATE execution_step SET state='failed' WHERE step_id='step-1'")
    authority.start_attempt(
        job_id="job-1", step_id="step-1", attempt_id="attempt-2", worker_run_id="worker-2",
        plugin_id="com.plotpilot.demo", release_id=RELEASE, package_hash=PACKAGE,
        capability_id="writing.chapter.draft/v1", generation_id="generation-1",
        expected_result_contract="candidate-batch/v1",
    )
    assert connection.execute("SELECT lease_epoch FROM execution_attempt WHERE attempt_id='attempt-2'").fetchone()[0] == 2
    with pytest.raises(ContractError) as caught:
        authority.validate_attempt(CallerAttemptContext(
            "job-1", "step-1", "attempt-1", 1,
            generation_id="generation-1", plugin_release_id=RELEASE,
        ))
    assert caught.value.code == int(ErrorCode.STALE_LEASE)


def test_f004_frozen_result_contract_rejects_profile_swap(execution_stack):
    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)
    execution_stack["repository"]._connection.execute(
        "UPDATE execution_attempt SET expected_result_contract='artifact-bundle/v1'"
    )
    before = authority_rows(execution_stack["repository"])
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt))
    assert caught.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)
    assert authority_rows(execution_stack["repository"]) == before


def test_f005_run_snapshot_workspace_is_rechecked_before_staging(execution_stack):
    repository = execution_stack["repository"]
    repository.create_workspace(Workspace("ws-2", "Other"))
    repository.create_document(Document("doc-2", "ws-2", "Other doc"))
    base = repository.publish_revision(
        document_id="doc-2", content="other", expected_revision_id=None, created_by="user", revision_id="rev-ws-2"
    )
    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)

    def mutate(bundle):
        item = bundle["items"][0]
        item["target"] = {"workspace_id": "ws-2", "entity_kind": "document", "entity_id": "doc-2"}
        item["base"] = {"revision_id": base.revision_id, "content_hash": base.content_hash}
        item["write_set"] = [{
            "workspace_id": "ws-2", "entity_kind": "document", "entity_id": "doc-2",
            "revision_id": base.revision_id, "content_hash": base.content_hash,
        }]

    bad_asset, bad_receipt, _ = _replace_bundle(execution_stack, bundle_asset, receipt, mutate)
    with pytest.raises(ContractError):
        execution_stack["authority"].complete_attempt(**complete_kwargs(bad_asset, bad_receipt))
    assert repository.get_document("doc-2").content == "other"
    assert repository._connection.execute("SELECT count(*) FROM candidate").fetchone()[0] == 0


def test_f006_explicit_plan_freezes_membership_dag_and_output(execution_stack):
    connection = execution_stack["repository"]._connection
    connection.execute(
        "INSERT INTO execution_job(job_id,workspace_id,request_key,run_intent_id,run_snapshot_hash,run_snapshot_json,job_state,job_revision,created_at,updated_at) "
        "SELECT 'job-plan',workspace_id,'plan-request','plan-intent',run_snapshot_hash,run_snapshot_json,'queued',1,created_at,updated_at FROM execution_job WHERE job_id='job-1'"
    )
    authority = execution_stack["authority"]
    authority.freeze_plan("job-plan", [
        {"step_id": "plan-a", "depends_on": [], "result_contract": "artifact-bundle/v1"},
        {"step_id": "plan-b", "depends_on": ["plan-a"], "result_contract": "candidate-batch/v1"},
    ], output_step_id="plan-b")
    authority.start_attempt(
        job_id="job-plan", step_id="plan-a", attempt_id="plan-attempt-a", worker_run_id="plan-worker-a",
        plugin_id="com.plotpilot.demo", release_id=RELEASE, package_hash=PACKAGE,
        capability_id="capability.plan.a/v1", generation_id="generation-1",
        expected_result_contract="artifact-bundle/v1",
    )
    with pytest.raises(ContractError):
        authority.start_attempt(
            job_id="job-plan", step_id="plan-x", attempt_id="plan-attempt-x", worker_run_id="plan-worker-x",
            plugin_id="com.plotpilot.demo", release_id=RELEASE, package_hash=PACKAGE,
            capability_id="capability.plan.x/v1", generation_id="generation-1",
            expected_result_contract="artifact-bundle/v1",
        )
    assert connection.execute("SELECT output_step_id FROM execution_job WHERE job_id='job-plan'").fetchone()[0] == "plan-b"
    assert [row[0] for row in connection.execute(
        "SELECT step_id FROM execution_step WHERE job_id='job-plan' ORDER BY step_ordinal"
    )] == ["plan-a", "plan-b"]


@pytest.mark.parametrize(
    "corruption",
    [
        "UPDATE execution_job SET plan_frozen=0 WHERE job_id='job-1'",
        "UPDATE execution_step SET is_output=0 WHERE step_id='step-1'",
    ],
)
def test_f006_unfrozen_or_incomplete_plan_cannot_terminalize(execution_stack, corruption):
    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)
    connection = execution_stack["repository"]._connection
    connection.execute(corruption)
    before = authority_rows(execution_stack["repository"])
    before_states = tuple(connection.execute(
        "SELECT j.job_state,s.state,a.state FROM execution_job j "
        "JOIN execution_step s ON s.job_id=j.job_id JOIN execution_attempt a ON a.step_id=s.step_id "
        "WHERE a.attempt_id='attempt-1'"
    ).fetchone())
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt))
    assert caught.value.code == int(ErrorCode.INVALID_TRANSITION)
    assert authority_rows(execution_stack["repository"]) == before
    assert tuple(connection.execute(
        "SELECT j.job_state,s.state,a.state FROM execution_job j "
        "JOIN execution_step s ON s.job_id=j.job_id JOIN execution_attempt a ON a.step_id=s.step_id "
        "WHERE a.attempt_id='attempt-1'"
    ).fetchone()) == before_states == ("running", "running", "running")


def test_f007_core_materializes_receipt_and_rejects_unbound_parent(execution_stack):
    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)
    forged = copy.deepcopy(receipt)
    forged["parent_receipt_ids"] = ["missing-receipt"]
    forged["receipt_hash"] = hash_without_field(forged, "receipt_hash", "provenance-receipt/v1")
    with pytest.raises(ContractError):
        execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, forged))
    assert execution_stack["repository"]._connection.execute("SELECT count(*) FROM execution_receipt").fetchone()[0] == 0
    execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt))
    stored = json.loads(execution_stack["repository"]._connection.execute("SELECT receipt_json FROM execution_receipt").fetchone()[0])
    assert stored["created_at"] != receipt["created_at"]
    assert stored["receipt_hash"] == hash_without_field(stored, "receipt_hash", "provenance-receipt/v1")


@pytest.mark.parametrize("drift", ["missing", "hash", "identity"])
def test_f007_skill_chain_asset_and_identity_must_be_authoritative(execution_stack, drift):
    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)
    ref = _skill_chain_ref(execution_stack, chain_id="chain-authority")
    if drift == "missing":
        ref.update(asset_id=f"asset-sha256-{'d' * 64}", asset_hash="d" * 64)
    elif drift == "hash":
        ref["asset_hash"] = "e" * 64
    else:
        ref["chain_result_id"] = "chain-ref-drift"
    bad_asset, bad_receipt, _ = _replace_bundle(
        execution_stack, bundle_asset, receipt,
        lambda bundle: bundle.update(skill_chain_result_refs=[ref]),
    )
    before = authority_rows(execution_stack["repository"])
    with pytest.raises(ContractError):
        execution_stack["authority"].complete_attempt(**complete_kwargs(bad_asset, bad_receipt))
    assert authority_rows(execution_stack["repository"]) == before
    assert execution_stack["repository"]._connection.execute(
        "SELECT job_state FROM execution_job WHERE job_id='job-1'"
    ).fetchone()[0] == "running"


def test_f007_wrong_chain_hash_is_rejected_before_materialization(execution_stack):
    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)
    skill_receipt = json.loads(
        Path("contracts/examples/fixtures/skill-run-receipt.json").read_text(encoding="utf-8")
    )
    skill_receipt.update(
        chain_id="chain-semantic",
        run_snapshot_hash=execution_stack["snapshot"]["snapshot_hash"],
        result_bundle_id="bundle-1",
        result_item_id="item-1",
        output_asset_id=None,
        output_hash=None,
    )
    skill_receipt["receipt_hash"] = hash_without_field(
        skill_receipt, "receipt_hash", "skill-run-receipt/v1"
    )
    chain = {
        "schema": "skill-chain-result/v1",
        "chain_id": "chain-semantic",
        "run_snapshot_hash": execution_stack["snapshot"]["snapshot_hash"],
        "result_bundle_id": "bundle-1",
        "result_item_id": "item-1",
        "stream_id": None,
        "acked_prefix_hash": None,
        "receipt_ids": [skill_receipt["receipt_id"]],
        "receipt_hashes": [skill_receipt["receipt_hash"]],
        "chain_status": "succeeded",
        "input_hash": skill_receipt["input_hash"],
        "final_output_asset_id": None,
        "final_output_hash": None,
        "chain_hash": sha256_hex(
            b"skill-chain/v1\n" + skill_receipt["receipt_hash"].encode("ascii") + b"\n-\n"
        ),
    }
    verify_skill_chain(chain, [skill_receipt])
    chain["chain_hash"] = "0" * 64
    chain_asset = execution_stack["assets"].put(
        canonical_bytes(chain),
        mime="application/json",
        logical_role="skill_chain_result",
        provenance="skill:test",
    )
    ref = {
        "schema": "skill-chain-ref/v1",
        "chain_result_id": chain["chain_id"],
        "asset_id": chain_asset.asset_id,
        "asset_hash": chain_asset.sha256,
        "result_bundle_id": chain["result_bundle_id"],
        "result_item_id": chain["result_item_id"],
        "stream_id": chain["stream_id"],
        "acked_prefix_hash": chain["acked_prefix_hash"],
    }
    bad_asset, bad_receipt, _ = _replace_bundle(
        execution_stack,
        bundle_asset,
        receipt,
        lambda bundle: bundle.update(skill_chain_result_refs=[ref]),
    )
    execution_stack["authority"] = ExecutionAuthority(
        execution_stack["repository"],
        execution_stack["assets"],
        skill_receipt_reader=lambda chain_id: [skill_receipt]
        if chain_id == chain["chain_id"]
        else (),
    )
    before = authority_rows(execution_stack["repository"])
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].complete_attempt(
            **complete_kwargs(bad_asset, bad_receipt)
        )
    assert caught.value.code == int(ErrorCode.RESULT_CONTRACT_MISMATCH)
    assert authority_rows(execution_stack["repository"]) == before
    assert execution_stack["repository"]._connection.execute(
        "SELECT job_state FROM execution_job WHERE job_id='job-1'"
    ).fetchone()[0] == "running"


def test_f008_plugin_incomplete_stream_is_never_stageable(execution_stack):
    bundle_asset, receipt, item = make_candidate_bundle(execution_stack, partial=True, item_status="partial")

    def mutate(bundle):
        bundle["items"][0]["item_kind"] = "incomplete_stream"

    bad_asset, bad_receipt, bundle = _replace_bundle(execution_stack, bundle_asset, receipt, mutate)
    with pytest.raises(ContractError):
        execution_stack["authority"].complete_attempt(**complete_kwargs(bad_asset, bad_receipt, outcome="partial"))
    with pytest.raises(CandidateError):
        CandidateService(execution_stack["repository"], execution_stack["assets"]).stage("direct", bundle["items"][0])


def test_f009_publication_requires_complete_execution_evidence(execution_stack):
    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)
    execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt))
    connection = execution_stack["repository"]._connection
    candidate_id = connection.execute("SELECT candidate_id FROM candidate").fetchone()[0]
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DELETE FROM execution_receipt")
    connection.execute("PRAGMA foreign_keys=ON")
    before = execution_stack["repository"].get_document("doc-1").current_revision_id
    with pytest.raises(Exception):
        PublicationService(execution_stack["repository"], execution_stack["assets"]).accept(
            "publish-without-evidence", candidate_id, created_by="user"
        )
    assert execution_stack["repository"].get_document("doc-1").current_revision_id == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plugin_id", "com.plotpilot.other"),
        ("release_id", "f" * 64),
        ("package_hash", "b" * 64),
        ("generation_id", "generation-other"),
        ("capability_id", "writing.other/v1"),
    ],
)
def test_f009_attempt_identity_must_match_snapshot_release_before_terminal(execution_stack, field, value):
    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)
    connection = execution_stack["repository"]._connection
    connection.execute(f"UPDATE execution_attempt SET {field}=? WHERE attempt_id='attempt-1'", (value,))
    before = authority_rows(execution_stack["repository"])
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt))
    assert caught.value.code == int(ErrorCode.INCOMPATIBLE_GENERATION)
    assert authority_rows(execution_stack["repository"]) == before


def test_f009_publication_rechecks_snapshot_release_after_terminal_drift(execution_stack):
    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)
    execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt))
    connection = execution_stack["repository"]._connection
    candidate_id = connection.execute("SELECT candidate_id FROM candidate").fetchone()[0]
    connection.execute("UPDATE execution_attempt SET generation_id='generation-drift' WHERE attempt_id='attempt-1'")
    before = execution_stack["repository"].get_document("doc-1").current_revision_id
    with pytest.raises(Exception):
        PublicationService(execution_stack["repository"], execution_stack["assets"]).accept(
            "publish-generation-drift", candidate_id, created_by="user"
        )
    assert execution_stack["repository"].get_document("doc-1").current_revision_id == before


def test_f010_shared_connection_reader_waits_for_rollback(execution_stack):
    repository = execution_stack["repository"]
    written = Event()
    release = Event()

    def writer():
        try:
            with repository.transaction() as connection:
                connection.execute("UPDATE revision SET content='UNCOMMITTED' WHERE revision_id='rev-base'")
                written.set()
                assert release.wait(5)
                raise RuntimeError("rollback")
        except RuntimeError:
            return

    def reader():
        assert written.wait(5)
        return repository.get_revision("rev-base").content

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer_future = pool.submit(writer)
        reader_future = pool.submit(reader)
        assert written.wait(5)
        assert not reader_future.done()
        release.set()
        writer_future.result(5)
        assert reader_future.result(5) == "old"


def test_f011_candidate_identity_is_scoped_by_attempt_and_bundle(execution_two_step_stack):
    execution_stack = execution_two_step_stack
    _start_second(execution_stack)
    first_asset, first_receipt, _ = make_candidate_bundle(execution_stack)
    execution_stack["authority"].complete_attempt(**complete_kwargs(first_asset, first_receipt))
    execution_stack["authority"].complete_attempt(**_second_candidate(execution_stack))
    rows = execution_stack["repository"]._connection.execute(
        "SELECT attempt_id,bundle_id,item_id,candidate_id FROM execution_candidate_binding ORDER BY attempt_id"
    ).fetchall()
    assert len(rows) == 2 and rows[0]["candidate_id"] != rows[1]["candidate_id"]
    assert {row["attempt_id"] for row in rows} == {"attempt-1", "attempt-2"}


def test_f012_existing_visible_parent_candidate_is_accepted(execution_two_step_stack):
    execution_stack = execution_two_step_stack
    _start_second(execution_stack)
    first_asset, first_receipt, _ = make_candidate_bundle(execution_stack)
    execution_stack["authority"].complete_attempt(**complete_kwargs(first_asset, first_receipt))
    parent = execution_stack["repository"]._connection.execute(
        "SELECT candidate_id FROM execution_candidate_binding WHERE attempt_id='attempt-1'"
    ).fetchone()[0]
    execution_stack["authority"].complete_attempt(**_second_candidate(execution_stack, parent_ids=(parent,)))
    saved = json.loads(execution_stack["repository"]._connection.execute(
        "SELECT item_json FROM candidate WHERE candidate_id IN (SELECT candidate_id FROM execution_candidate_binding WHERE attempt_id='attempt-2')"
    ).fetchone()[0])
    assert saved["parent_candidate_ids"] == [parent]


@pytest.mark.parametrize("outcome", ["succeeded", "partial"])
def test_f013_cancelling_wins_with_cancelled_code_and_zero_writes(execution_stack, outcome):
    bundle_asset, receipt, _ = make_candidate_bundle(
        execution_stack, partial=outcome == "partial", item_status="partial" if outcome == "partial" else "complete"
    )
    connection = execution_stack["repository"]._connection
    connection.execute("UPDATE execution_attempt SET state='cancelling'")
    connection.execute("UPDATE execution_job SET job_state='cancelling'")
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt, outcome=outcome))
    assert caught.value.code == int(ErrorCode.CANCELLED)
    assert connection.execute("SELECT count(*) FROM execution_outcome").fetchone()[0] == 0


def test_f014_plugin_job_event_uses_current_plugin_namespace(execution_stack):
    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)
    execution_stack["authority"].complete_attempt(**complete_kwargs(bundle_asset, receipt))
    event = json.loads(execution_stack["repository"]._connection.execute(
        "SELECT event_json FROM execution_job_event"
    ).fetchone()[0])
    assert event["event_type"] == "plugin.com.plotpilot.demo.job.succeeded"


def test_f015_complete_replay_fails_closed_when_receipt_is_missing(execution_stack):
    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)
    kwargs = complete_kwargs(bundle_asset, receipt)
    execution_stack["authority"].complete_attempt(**kwargs)
    connection = execution_stack["repository"]._connection
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DELETE FROM execution_receipt")
    connection.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].complete_attempt(**kwargs)
    assert caught.value.code == int(ErrorCode.ASSET_ERROR)


def test_f015_complete_replay_rejects_post_terminal_package_hash_drift(execution_stack):
    bundle_asset, receipt, _ = make_candidate_bundle(execution_stack)
    kwargs = complete_kwargs(bundle_asset, receipt)
    execution_stack["authority"].complete_attempt(**kwargs)
    connection = execution_stack["repository"]._connection
    connection.execute("UPDATE execution_attempt SET package_hash=? WHERE attempt_id='attempt-1'", ("b" * 64,))
    before = authority_rows(execution_stack["repository"])
    with pytest.raises(ContractError) as caught:
        execution_stack["authority"].complete_attempt(**kwargs)
    assert caught.value.code == int(ErrorCode.ASSET_ERROR)
    assert authority_rows(execution_stack["repository"]) == before
