from __future__ import annotations

from dataclasses import replace
from typing import Any

from plotpilot_plugin_sdk import hash_jcs
from plotpilot_prompt_skill_runtime import ChainAnchor, SkillStep, run_skill_chain
from plotpilot_story_state import (
    CandidateCommit,
    ExecutionLineage,
    FactRef,
    Proposal,
    SkillChainEvidence,
    StatePayload,
    StoryStateRequest,
    TerminalCommand,
    TerminalCompletion,
)

HASH = "a" * 64
SNAPSHOT_HASH = "b" * 64


def lineage() -> ExecutionLineage:
    return ExecutionLineage(
        "com.plotpilot.story-state",
        "c" * 64,
        "d" * 64,
        "planning.story-state.update/v1",
        "job-story-1",
        "step-story-1",
        "attempt-story-1",
        1,
    )


def payload(kind: str = "character", entity_id: str = "character-1") -> StatePayload:
    bodies: dict[str, dict[str, Any]] = {
        "bible": {"title": "主设定", "premise": "一座城市遗忘昨天", "themes": ["记忆"], "status": "active"},
        "character": {"name": "林岚", "role": "侦探", "traits": ["敏锐", "克制"], "status": "active"},
        "relationship": {
            "source_entity_id": "character-1",
            "target_entity_id": "character-2",
            "relation_type": "trusts",
            "status": "active",
        },
        "world": {"name": "雾城", "description": "终年有雾", "rules": ["午夜禁行"]},
        "item": {"name": "旧怀表", "description": "停在零点", "status": "active", "owner_entity_id": None},
        "foreshadowing": {
            "description": "怀表会逆转",
            "introduced_revision_id": "rev-0",
            "status": "planted",
            "resolution_revision_id": None,
        },
        "story_evolution": {"summary": "侦探决定入城", "affected_entity_ids": [], "predecessor_entity_id": None},
    }
    return StatePayload(kind, entity_id, bodies[kind])


def proposal(
    *,
    item_id: str = "item-story-1",
    entity_id: str = "character-1",
    kind: str = "character",
    outcome: str = "success",
    error: str | None = None,
    retry_id: str = "try-1",
    terminal_seq: int = 1,
) -> Proposal:
    return Proposal(
        f"proposal-{item_id}-{retry_id}",
        "operation-story-1",
        item_id,
        retry_id,
        FactRef("workspace-1", kind, entity_id, "revision-base-1", HASH),
        payload(kind, entity_id).to_dict(),
        outcome=outcome,
        error=error,
        terminal_seq=terminal_seq,
    )


def two_step_chain(*, bundle_id: str = "bundle-story-1", item_id: str = "item-story-1") -> SkillChainEvidence:
    outputs = iter((b"H1-distinct", b"H2-distinct"))
    execution = run_skill_chain(
        chain_id="chain-story-1",
        run_snapshot_hash=SNAPSHOT_HASH,
        initial_input=b"H0-distinct",
        steps=(
            SkillStep("skill.alpha", "1" * 64, "2" * 64, 10),
            SkillStep("skill.beta", "3" * 64, "4" * 64, 20),
        ),
        execute=lambda _step, _content: next(outputs),
        anchor=ChainAnchor.bundle(bundle_id, item_id),
    )
    return SkillChainEvidence(execution.chain, execution.receipts)


def request(
    *,
    proposals: tuple[Proposal, ...] | None = None,
    skill_chains: tuple[SkillChainEvidence, ...] = (),
) -> StoryStateRequest:
    return StoryStateRequest(
        operation_key="terminal-story-1",
        candidate_stage_operation_key="stage-story-1",
        bundle_id="bundle-story-1",
        receipt_id="receipt-story-1",
        input_snapshot_hash=SNAPSHOT_HASH,
        lineage=lineage(),
        proposals=proposals or (proposal(),),
        known_entity_ids=frozenset({"character-1", "character-2"}),
        created_at="2026-08-30T00:00:00Z",
        skill_chains=skill_chains,
    )


class RecordingTerminal:
    def __init__(self, *, rewrite_receipt: bool = True) -> None:
        self.calls: list[TerminalCommand] = []
        self.rewrite_receipt = rewrite_receipt

    def complete(self, command: TerminalCommand) -> TerminalCompletion:
        self.calls.append(command)
        committed = dict(command.provenance_receipt)
        if self.rewrite_receipt:
            committed["created_at"] = "2026-08-30T00:00:01Z"
            committed["receipt_hash"] = hash_jcs(
                "provenance-receipt/v1",
                {key: value for key, value in committed.items() if key != "receipt_hash"},
            )
        candidates = tuple(
            CandidateCommit(item["item_id"], f"candidate-{item['item_id']}", "created", "eligible")
            for item in command.result_bundle["items"]
            if item["status"] == "complete"
        )
        return TerminalCompletion(
            True,
            command.outcome,
            command.result_bundle["bundle_id"],
            candidates,
            committed,
            replayed=len(self.calls) > 1,
        )


class WrongCandidateTerminal(RecordingTerminal):
    def complete(self, command: TerminalCommand) -> TerminalCompletion:
        result = super().complete(command)
        wrong = result.candidates + (replace(result.candidates[0], item_id="unrelated-item"),)
        return replace(result, candidates=wrong)
