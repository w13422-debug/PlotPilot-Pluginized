import copy

import pytest
from plotpilot_plugin_sdk import (
    hash_jcs,
    sha256_hex,
    verify_result_bundle,
    verify_skill_chain,
)
from plotpilot_prompt_skill_runtime import verify_chain
from plotpilot_story_state import SkillChainEvidence, StoryStateRuntime

from .support import (
    RecordingTerminal,
    WrongCandidateTerminal,
    proposal,
    request,
    two_step_chain,
)


def _rehash_receipt(receipt):
    receipt["receipt_hash"] = hash_jcs(
        "skill-run-receipt/v1",
        {key: value for key, value in receipt.items() if key != "receipt_hash"},
    )


def _rehash_chain(chain, receipts):
    chain["receipt_hashes"] = [receipt["receipt_hash"] for receipt in receipts]
    final_hash = chain["final_output_hash"] or "-"
    chain["chain_hash"] = sha256_hex(
        b"skill-chain/v1\n"
        + b"\n".join(receipt["receipt_hash"].encode("ascii") for receipt in receipts)
        + f"\n{final_hash}\n".encode("ascii")
    )


def test_distinct_h0_h1_h2_chain_is_accepted_by_all_three_and_committed_once():
    evidence = two_step_chain()
    assert len({evidence.receipts[0]["input_hash"], evidence.receipts[0]["output_hash"], evidence.receipts[1]["output_hash"]}) == 3
    verify_skill_chain(evidence.chain, evidence.receipts)
    verify_chain(evidence.chain, evidence.receipts)
    terminal = RecordingTerminal()
    result = StoryStateRuntime(terminal).execute(request(skill_chains=(evidence,)))
    assert len(terminal.calls) == 1
    assert result.outcome == "succeeded"
    assert result.committed_receipt["created_at"] == "2026-08-30T00:00:01Z"
    assert result.to_dict()["schema"] == "story-state-terminal-result/v1"


def test_recomputed_h0_step_two_receipt_is_rejected_before_any_write():
    evidence = two_step_chain()
    chain = copy.deepcopy(dict(evidence.chain))
    receipts = [copy.deepcopy(dict(item)) for item in evidence.receipts]
    receipts[1]["input_asset_id"] = receipts[0]["input_asset_id"]
    receipts[1]["input_hash"] = receipts[0]["input_hash"]
    _rehash_receipt(receipts[1])
    _rehash_chain(chain, receipts)
    verify_skill_chain(chain, receipts)
    with pytest.raises(Exception, match="previous output"):
        verify_chain(chain, receipts)
    terminal = RecordingTerminal()
    with pytest.raises(Exception, match="previous output"):
        StoryStateRuntime(terminal).execute(
            request(skill_chains=(SkillChainEvidence(chain, tuple(receipts)),))
        )
    assert terminal.calls == []


def test_public_invalid_claim_receipt_is_rejected_before_terminal_call():
    evidence = two_step_chain()
    chain = copy.deepcopy(dict(evidence.chain))
    receipts = [copy.deepcopy(dict(item)) for item in evidence.receipts]
    receipts[0]["model_claimed"] = True
    receipts[0]["claim_evidence_asset_id"] = None
    _rehash_receipt(receipts[0])
    receipts[1]["previous_receipt_hash"] = receipts[0]["receipt_hash"]
    _rehash_receipt(receipts[1])
    _rehash_chain(chain, receipts)
    terminal = RecordingTerminal()
    with pytest.raises(Exception, match="claim evidence"):
        StoryStateRuntime(terminal).execute(
            request(skill_chains=(SkillChainEvidence(chain, tuple(receipts)),))
        )
    assert terminal.calls == []


def test_partial_bundle_is_public_valid_and_commits_only_complete_item():
    values = (
        proposal(item_id="item-ok"),
        proposal(item_id="item-failed", outcome="failure", error="dependency unavailable"),
    )
    terminal = RecordingTerminal()
    result = StoryStateRuntime(terminal).execute(request(proposals=values))
    command = terminal.calls[0]
    verify_result_bundle(command.result_bundle, snapshot_workspace_id="workspace-1")
    assert command.result_bundle["partial"] is True
    assert [item["status"] for item in command.result_bundle["items"]] == ["complete", "failed"]
    assert result.outcome == "partial"
    assert [item.item_id for item in result.candidates] == ["item-ok"]
    assert result.committed_receipt["staged_items"] == ("item-ok",)


def test_unknown_payload_identity_and_cross_workspace_fail_before_mutation():
    source = proposal()
    bad_payload = dict(source.payload)
    bad_payload["unknown"] = 7
    bad = type(source)(
        source.proposal_id,
        source.operation_id,
        source.item_id,
        source.retry_id,
        source.target,
        bad_payload,
    )
    terminal = RecordingTerminal()
    with pytest.raises(ValueError, match="top-level"):
        StoryStateRuntime(terminal).execute(request(proposals=(bad,)))
    assert terminal.calls == []
    cross = proposal(item_id="cross")
    cross = type(cross)(
        cross.proposal_id,
        cross.operation_id,
        cross.item_id,
        cross.retry_id,
        type(cross.target)("workspace-2", "character", "character-1", "revision-base-1", "a" * 64),
        cross.payload,
    )
    with pytest.raises(ValueError, match="cross workspaces|reference crosses workspace"):
        StoryStateRuntime(terminal).execute(request(proposals=(proposal(), cross)))
    assert terminal.calls == []


def test_relationship_references_require_fact_or_same_batch_closure():
    relationship = proposal(item_id="item-relation", entity_id="relationship-1", kind="relationship")
    terminal = RecordingTerminal()
    with pytest.raises(ValueError, match="unbound entity reference"):
        StoryStateRuntime(terminal).execute(request(proposals=(relationship,)))
    assert terminal.calls == []
    values = (
        proposal(item_id="item-character-1", entity_id="character-1"),
        proposal(item_id="item-character-2", entity_id="character-2"),
        relationship,
    )
    result = StoryStateRuntime(terminal).execute(request(proposals=values))
    assert len(terminal.calls) == 1
    assert [item.item_id for item in result.candidates] == [
        "item-character-1",
        "item-character-2",
        "item-relation",
    ]


def test_terminal_candidate_mapping_and_committed_receipt_are_closed():
    terminal = WrongCandidateTerminal()
    with pytest.raises(ValueError, match="mapping is not exact"):
        StoryStateRuntime(terminal).execute(request())
    assert len(terminal.calls) == 1


def test_retry_replays_one_terminal_call_and_converges_on_committed_receipt():
    terminal = RecordingTerminal()
    runtime = StoryStateRuntime(terminal)
    first = runtime.execute(request())
    second = runtime.execute(request())
    assert len(terminal.calls) == 2
    assert first.committed_receipt == second.committed_receipt
    assert second.replayed is True
