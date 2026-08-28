from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import sys
from typing import Any, Mapping

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent))

from plotpilot_chapter_workflow import (
    BrokerEvent,
    ChapterOperation,
    ChapterRequest,
    ChapterTarget,
    ChapterWorkflow,
    ContextSource,
    ProducerRef,
    RewriteSelection,
    SkillRef,
    freeze_context_plan,
)


@dataclass
class FakeCore:
    assets: dict[str, bytes] = field(default_factory=dict)
    asset_mimes: dict[str, str] = field(default_factory=dict)
    stage_calls: list[tuple[str, str]] = field(default_factory=list)
    selection_calls: list[dict[str, Any]] = field(default_factory=list)
    selection_response: dict[str, Any] | None = None
    batch_calls: list[dict[str, Any]] = field(default_factory=list)
    batch_receipts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def create_asset(self, content: bytes, *, mime: str) -> Mapping[str, Any]:
        asset_id = f"asset-{len(self.assets) + 1}"
        raw = bytes(content)
        self.assets[asset_id] = raw
        self.asset_mimes[asset_id] = mime
        return {"asset_id": asset_id, "sha256": sha256(raw).hexdigest()}

    def stage_candidate(self, operation_key: str, bundle_id: str) -> list[str]:
        call = (operation_key, bundle_id)
        if call not in self.stage_calls:
            self.stage_calls.append(call)
        index = self.stage_calls.index(call) + 1
        return [f"candidate-{index}"]

    def read_rewrite_selection(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.selection_calls.append(dict(request))
        if self.selection_response is not None:
            return deepcopy(self.selection_response)
        return {
            "schema": "rewrite-selection-receipt/v1",
            "receipt_id": "selection-receipt-1",
            "workspace_id": request["workspace_id"],
            "document_id": request["document_id"],
            "base_revision_id": request["base_revision_id"],
            "base_content_hash": request["base_content_hash"],
            "current_revision_id": request["base_revision_id"],
            "total_codepoints": 100,
            "start_codepoint": request["start_codepoint"],
            "end_codepoint": request["end_codepoint"],
            "selected_text": request["selected_text"],
            "selected_hash": request["selected_hash"],
        }

    def stage_story_state_batch(self, command: Mapping[str, Any]) -> Mapping[str, Any]:
        copied = dict(command)
        self.batch_calls.append(copied)
        operation_key = copied["operation_key"]
        previous = self.batch_receipts.get(operation_key)
        if previous is not None:
            if (
                previous["chapter_publication_id"] != copied["chapter_publication_id"]
                or previous["batch_fingerprint"] != copied["batch_fingerprint"]
                or previous["bundle_ids"] != list(copied["bundle_ids"])
            ):
                raise ValueError("atomic batch operation_key reused with different input")
            replay = deepcopy(previous)
            replay["idempotent"] = True
            return replay
        first_index = len(self.stage_calls) + 1
        groups = [
            {"bundle_id": bundle_id, "candidate_ids": [f"candidate-{first_index + index}"]}
            for index, bundle_id in enumerate(copied["bundle_ids"])
        ]
        receipt = {
            "schema": "story-state-candidate-batch-result/v1",
            "receipt_id": f"settlement-receipt-{len(self.batch_receipts) + 1}",
            "operation_key": operation_key,
            "chapter_publication_id": copied["chapter_publication_id"],
            "batch_fingerprint": copied["batch_fingerprint"],
            "bundle_ids": list(copied["bundle_ids"]),
            "candidate_groups": groups,
            "idempotent": False,
        }
        self.batch_receipts[operation_key] = deepcopy(receipt)
        return receipt


@dataclass
class FakeBundles:
    bundles: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def persist_result_bundle(self, bundle: Mapping[str, Any]) -> str:
        copied = deepcopy(dict(bundle))
        bundle_id = copied["bundle_id"]
        if bundle_id in self.bundles and self.bundles[bundle_id] != copied:
            raise ValueError("bundle identity reused")
        self.bundles[bundle_id] = copied
        self.calls.append(bundle_id)
        return bundle_id


@dataclass
class FakeBroker:
    starts: list[Any] = field(default_factory=list)
    poll_events: dict[str, list[BrokerEvent]] = field(default_factory=dict)
    control_events: dict[tuple[str, str], list[BrokerEvent]] = field(default_factory=dict)
    poll_calls: list[tuple[str, int]] = field(default_factory=list)
    control_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def start(self, invocation: object) -> str:
        self.starts.append(invocation)
        return f"inv-{len(self.starts)}"

    def poll(self, invocation_id: str, after_event_seq: int):
        self.poll_calls.append((invocation_id, after_event_seq))
        return self.poll_events.pop(invocation_id, [])

    def pause(self, operation_key: str, invocation_id: str):
        self.control_calls.append(("pause", operation_key, invocation_id))
        return self.control_events.pop(("pause", invocation_id), [])

    def cancel(self, operation_key: str, invocation_id: str):
        self.control_calls.append(("cancel", operation_key, invocation_id))
        return self.control_events.pop(("cancel", invocation_id), [])


@dataclass
class FakePublication:
    calls: list[dict[str, Any]] = field(default_factory=list)
    response: dict[str, Any] | None = None

    def publish(self, command: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(dict(command))
        if self.response is not None:
            return deepcopy(self.response)
        return publication_result(command["candidate_id"], command["workspace_id"])


@dataclass
class FakeStoryState:
    bundles: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def propose_after_chapter(self, request: Mapping[str, Any]):
        self.calls.append(dict(request))
        return deepcopy(self.bundles)


def publication_result(candidate_id: str = "candidate-1", workspace_id: str = "ws-1") -> dict[str, Any]:
    return {
        "schema": "publication-result/v1",
        "publication_id": "publication-1",
        "candidate_id": candidate_id,
        "workspace_id": workspace_id,
        "entity_kind": "document",
        "entity_id": "chapter-1",
        "resulting_revision": {
            "revision_id": "revision-new",
            "workspace_id": workspace_id,
            "entity_kind": "document",
            "entity_id": "chapter-1",
            "content_hash": "d" * 64,
            "revision_number": 2,
        },
        "idempotent": False,
    }


def chain_ref(invocation: Any, index: int) -> dict[str, Any]:
    return {
        "schema": "skill-chain-ref/v1",
        "chain_result_id": f"chain-{index}",
        "asset_id": f"asset-chain-{index}",
        "asset_hash": sha256(f"chain-{index}".encode()).hexdigest(),
        "result_bundle_id": invocation.result_bundle_id,
        "result_item_id": invocation.result_item_id,
        "stream_id": None,
        "acked_prefix_hash": None,
    }


def event_script(chunks: tuple[bytes, ...], terminal: str = "completed", *, chain: Mapping[str, Any] | None = None):
    prefix = b""
    events: list[BrokerEvent] = []
    for seq, chunk in enumerate(chunks, 1):
        prefix += chunk
        events.append(BrokerEvent(seq, "acked", chunk, len(prefix), sha256(prefix).hexdigest()))
    events.append(BrokerEvent(len(events) + 1, terminal, prefix_hash=sha256(prefix).hexdigest(), skill_chain_ref=chain))
    return events


def make_request(
    operation: ChapterOperation = ChapterOperation.GENERATE,
    *,
    skills: tuple[SkillRef, ...] = (),
    operation_key: str = "write-1",
) -> ChapterRequest:
    source_text = "上一章\r\n林岚推开门。"
    source = ContextSource("chapter-0", "revision-0", "document", source_text, sha256(source_text.encode()).hexdigest())
    plan = freeze_context_plan(operation.capability_id, (source,), skills)
    selection = None
    if operation is ChapterOperation.REWRITE:
        selected = "旧句"
        selection = RewriteSelection(2, 4, selected, sha256(selected.encode()).hexdigest())
    return ChapterRequest(
        operation_key,
        operation,
        ChapterTarget("ws-1", "chapter-1", "revision-1", "a" * 64),
        plan,
        ProducerRef("com.plotpilot.chapter-workflow", "b" * 64, "job-1", "step-1", "attempt-1", 1, "receipt-1", "c" * 64),
        "写出紧张但克制的一章",
        selection,
    )


@pytest.fixture
def ports():
    core, broker, bundles, publication, story = FakeCore(), FakeBroker(), FakeBundles(), FakePublication(), FakeStoryState()
    workflow = ChapterWorkflow(
        core=core,
        broker=broker,
        result_bundles=bundles,
        publication=publication,
        story_state=story,
        rewrite_selections=core,
        settlement_batch=core,
    )
    return workflow, core, broker, bundles, publication, story


@pytest.fixture
def complete_session(ports):
    workflow, core, broker, bundles, publication, story = ports
    started = workflow.start(make_request())
    broker.poll_events[started.invocation_id] = event_script(("完整章节".encode(),))
    completed = workflow.poll(started.session_id)
    return completed, ports


def story_bundle(publication, *, bundle_id="story-bundle-1", workspace_id="ws-1", entity_kind="node_structure"):
    return {
        "schema": "result-bundle/v1",
        "contract_id": "candidate-batch/v1",
        "bundle_id": bundle_id,
        "bundle_type": "candidate_batch",
        "producer": {
            "plugin_id": "com.plotpilot.story-state",
            "release_id": "e" * 64,
            "capability_id": "planning.story-state.settle/v1",
            "job_id": "story-job-1",
            "step_id": "story-step-1",
            "attempt_id": "story-attempt-1",
            "lease_epoch": 1,
        },
        "input_snapshot_hash": "e" * 64,
        "items": [
            {
                "schema": "candidate-item/v1",
                "item_id": f"{bundle_id}-item",
                "item_kind": entity_kind,
                "target": {"workspace_id": workspace_id, "entity_kind": entity_kind, "entity_id": "story-node-1"},
                "mutation": {"mode": "structure_patch", "payload_schema": "story-state/proposal/v1", "payload_hash": "f" * 64},
                "payload_asset_id": "asset-story",
                "base": {"revision_id": "story-revision-1", "content_hash": "a" * 64},
                "write_set": [
                    {
                        "workspace_id": workspace_id,
                        "entity_kind": entity_kind,
                        "entity_id": "story-node-1",
                        "revision_id": "story-revision-1",
                        "content_hash": "a" * 64,
                    }
                ],
                "parent_candidate_ids": [publication.candidate_id],
                "source_refs": [
                    {
                        "workspace_id": publication.workspace_id,
                        "source_type": "revision",
                        "source_id": publication.entity_id,
                        "revision_or_hash": publication.revision_id,
                    }
                ],
                "status": "complete",
            }
        ],
        "warnings": [],
        "partial": False,
        "provenance_receipt_id": "story-receipt",
        "skill_chain_result_refs": [],
    }
