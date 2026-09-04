from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from plotpilot_story_state import (
    AuthoritativeFactBinding,
    CandidateCommit,
    ChapterSettlement,
    CoreProjectionAdapter,
    ExecutionLineage,
    FactRef,
    Proposal,
    StatePayload,
    StoryStateRequest,
    StoryStateRuntime,
    TerminalCommand,
    TerminalCompletion,
)

from backend.plotpilot_core.assets import AssetStore
from backend.plotpilot_core.candidates.application import CandidateApplication
from backend.plotpilot_core.candidates.service import CandidateService
from backend.plotpilot_core.domain import Document, Workspace
from backend.plotpilot_core.publication.application.service import (
    PublicationApplication,
)
from backend.plotpilot_core.publication.service import PublicationService
from backend.plotpilot_core.repositories import CoreAuthorityRepository
from backend.plotpilot_core.repositories.authority_application import (
    CoreAuthorityApplication,
)


class _CoreFactAuthority:
    """Read the exact Core document/revision pair for a runtime reference."""

    def __init__(self, core: CoreAuthorityApplication) -> None:
        self._core = core

    def resolve_reference(
        self,
        *,
        workspace_id: str,
        entity_kind: str,
        entity_id: str,
    ) -> AuthoritativeFactBinding:
        document = self._core.query(
            {
                "schema": "core-document-get-query/v1",
                "workspace_id": workspace_id,
                "document_id": entity_id,
            }
        )
        revision = self._core.query(
            {
                "schema": "core-revision-get-query/v1",
                "workspace_id": workspace_id,
                "revision_id": document["current_revision_id"],
            }
        )
        return AuthoritativeFactBinding(entity_kind, document, revision)


class _RuntimeOutputTerminal:
    """Capture runtime output; Core staging follows ChapterSettlement validation."""

    def __init__(self) -> None:
        self.commands: list[TerminalCommand] = []

    def complete(self, command: TerminalCommand) -> TerminalCompletion:
        self.commands.append(command)
        candidates = tuple(
            CandidateCommit(
                item["item_id"],
                f"runtime-output-{item['item_id']}",
                "created",
                "eligible",
            )
            for item in command.result_bundle["items"]
            if item["status"] == "complete"
        )
        return TerminalCompletion(
            accepted=True,
            outcome=command.outcome,
            bundle_id=command.result_bundle["bundle_id"],
            candidates=candidates,
            committed_receipt=dict(command.provenance_receipt),
        )


class _CoreProjectionAssets:
    """Read Core's immutable Asset and revision-content identities without writes."""

    def __init__(self, assets: AssetStore, repository: CoreAuthorityRepository) -> None:
        self._assets = assets
        self._repository = repository

    def read_asset(self, asset_id: str) -> bytes:
        if asset_id.startswith("revision-"):
            revision = self._repository.get_revision(asset_id.removeprefix("revision-"))
            return revision.content.encode("utf-8")
        return self._assets.read_asset(asset_id)


def _candidate_item(
    *,
    workspace_id: str,
    document_id: str,
    base,
    payload,
    item_id: str,
    payload_schema: str,
) -> dict[str, Any]:
    target = {
        "workspace_id": workspace_id,
        "entity_kind": "document",
        "entity_id": document_id,
    }
    return {
        "schema": "candidate-item/v1",
        "item_id": item_id,
        "item_kind": "document",
        "target": target,
        "mutation": {
            "mode": "replace",
            "payload_schema": payload_schema,
            "payload_hash": payload.sha256,
        },
        "payload_asset_id": payload.asset_id,
        "base": {
            "revision_id": base.revision_id,
            "content_hash": base.content_hash,
        },
        "write_set": [
            {
                **target,
                "revision_id": base.revision_id,
                "content_hash": base.content_hash,
            }
        ],
        "parent_candidate_ids": [],
        "source_refs": [],
        "status": "complete",
    }


def test_runtime_settlement_core_candidate_publication_and_projection_chain(tmp_path):
    repository = CoreAuthorityRepository(tmp_path / "core.db")
    assets = AssetStore(tmp_path / "assets")
    workspace_id = "workspace-runtime-projection-1"
    chapter_document_id = "chapter-runtime-projection-1"
    state_document_id = "character-runtime-projection-1"
    try:
        repository.create_workspace(Workspace(workspace_id, "Novel"))
        repository.create_document(Document(chapter_document_id, workspace_id, "Chapter"))
        repository.create_document(Document(state_document_id, workspace_id, "Character"))
        chapter_base = repository.publish_revision(
            document_id=chapter_document_id,
            content="draft chapter",
            expected_revision_id=None,
            created_by="editor-1",
            revision_id="chapter-runtime-base-1",
        )
        state_base = repository.publish_revision(
            document_id=state_document_id,
            content="old character state",
            expected_revision_id=None,
            created_by="editor-1",
            revision_id="character-runtime-base-1",
        )
        candidate_application = CandidateApplication(CandidateService(repository, assets))
        chapter_payload = assets.put(
            b"published chapter",
            mime="text/plain; charset=utf-8",
            logical_role="chapter_payload",
            provenance="test:runtime-projection",
        )
        chapter_candidate = CandidateService(repository, assets).stage(
            "chapter-runtime-stage-1",
            _candidate_item(
                workspace_id=workspace_id,
                document_id=chapter_document_id,
                base=chapter_base,
                payload=chapter_payload,
                item_id="chapter-runtime-item-1",
                payload_schema="core/document-text/v1",
            ),
        )
        chapter_publication = PublicationApplication(
            PublicationService(repository, assets)
        ).accept_v2(
            {
                "schema": "publication-command/v2",
                "publication_operation_key": "chapter-runtime-publication-1",
                "workspace_id": workspace_id,
                "candidate_id": chapter_candidate.candidate_id,
                "accepted_by": "editor-1",
            }
        )

        chapter_reference = FactRef(
            workspace_id,
            "bible",
            chapter_document_id,
            chapter_publication["revision_id"],
            chapter_publication["content_hash"],
        )
        payload = StatePayload(
            "character",
            state_document_id,
            {
                "name": "Lin",
                "role": "detective",
                "traits": ["calm"],
                "status": "active",
            },
            references=(chapter_reference,),
        )
        proposal = Proposal(
            "story-runtime-proposal-1",
            "p2-lifecycle-runtime-projection-1",
            "story-runtime-item-1",
            "story-runtime-retry-1",
            FactRef(
                workspace_id,
                "character",
                state_document_id,
                state_base.revision_id,
                state_base.content_hash,
            ),
            payload.to_dict(),
            parent_candidate_ids=(chapter_candidate.candidate_id,),
        )
        runtime_request = StoryStateRequest(
            operation_key="story-runtime-terminal-1",
            candidate_stage_operation_key="story-runtime-stage-1",
            bundle_id="story-runtime-bundle-1",
            receipt_id="story-runtime-receipt-1",
            input_snapshot_hash="a" * 64,
            lineage=ExecutionLineage(
                "com.plotpilot.story-state",
                "b" * 64,
                "c" * 64,
                "planning.story-state.settle/v1",
                "story-runtime-job-1",
                "story-runtime-step-1",
                "story-runtime-attempt-1",
                1,
            ),
            proposals=(proposal,),
            known_entity_ids=frozenset({chapter_document_id, state_document_id}),
            created_at="2026-09-04T00:00:00Z",
        )
        terminal = _RuntimeOutputTerminal()
        received_requests: list[Mapping[str, str]] = []

        def runtime_factory(request: Mapping[str, str]):
            received_requests.append(request)
            result = StoryStateRuntime(
                terminal,
                _CoreFactAuthority(CoreAuthorityApplication(repository)),
            ).execute(runtime_request)
            return [result.bundle]

        settlement = ChapterSettlement(runtime_factory)
        settled = settlement.settle(
            {
                "schema": "post-chapter-story-state-request/v1",
                "operation_key": "chapter-runtime-caller-operation-1",
                "workspace_id": workspace_id,
                "chapter_candidate_id": chapter_candidate.candidate_id,
                "chapter_publication_id": chapter_publication["publication_id"],
                "chapter_document_id": chapter_document_id,
                "chapter_revision_id": chapter_publication["revision_id"],
                "chapter_content_hash": chapter_publication["content_hash"],
            }
        )

        assert len(received_requests) == 1
        assert len(terminal.commands) == 1
        bundle = settled.bundles[0]
        item = bundle["items"][0]
        assert item["target"]["entity_id"] == state_document_id
        assert item["source_refs"] == [
            {
                "workspace_id": workspace_id,
                "source_type": "revision",
                "source_id": chapter_publication["revision_id"],
                "revision_or_hash": chapter_publication["content_hash"],
            }
        ]

        for prepared in terminal.commands[0].assets:
            role = (
                "story_state_payload"
                if prepared.asset_id == item["payload_asset_id"]
                else "story_state_runtime_artifact"
            )
            persisted = assets.put(
                prepared.content,
                mime=prepared.mime,
                logical_role=role,
                provenance="test:runtime-projection",
            )
            assert persisted.asset_id == prepared.asset_id
            assert persisted.sha256 == prepared.sha256

        staged = candidate_application.stage_story_state_batch(
            operation_key=received_requests[0]["operation_key"],
            workspace_id=workspace_id,
            items=bundle["items"],
        )
        assert [entry.stage_status for entry in staged] == ["created"]
        state_publication = PublicationApplication(
            PublicationService(repository, assets)
        ).accept_v2(
            {
                "schema": "publication-command/v2",
                "publication_operation_key": "story-runtime-publication-1",
                "workspace_id": workspace_id,
                "candidate_id": staged[0].candidate_id,
                "accepted_by": "editor-1",
            }
        )
        projection_input = candidate_application.story_state_projection_input(
            {
                "schema": "core-authority-query/v2",
                "workspace_id": workspace_id,
                "entity_kind": "document",
                "entity_id": state_document_id,
                "include_assets": True,
            }
        )
        projection = CoreProjectionAdapter(
            _CoreProjectionAssets(assets, repository)
        ).rebuild((projection_input,), expected_workspace_id=workspace_id)

        assert projection.records[0].candidate_id == staged[0].candidate_id
        assert projection.records[0].publication_id == state_publication["publication_id"]
        assert projection.records[0].fact.entity_id == state_document_id
        assert projection.records[0].fact.revision_id == state_publication["revision_id"]
        assert projection.records[0].payload.references == (chapter_reference,)
    finally:
        repository.close()
