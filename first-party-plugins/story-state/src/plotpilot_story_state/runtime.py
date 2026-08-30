"""Preflight-first Story State ResultBundle materialization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

try:
    from plotpilot_plugin_sdk import (
        assert_valid,
        canonical_bytes,
        hash_jcs,
        sha256_hex,
        verify_result_bundle,
    )
except ModuleNotFoundError:  # pragma: no cover - repository runner fallback
    from backend.plotpilot_plugin_sdk import (
        assert_valid,
        canonical_bytes,
        hash_jcs,
        sha256_hex,
        verify_result_bundle,
    )

try:
    from plotpilot_plugin_sdk.core_api import parse_core_authority
except ModuleNotFoundError:  # pragma: no cover - repository runner fallback
    from backend.plotpilot_plugin_sdk.core_api import parse_core_authority

from .domain import FactRef, Proposal
from .payloads import StatePayload
from .ports import (
    AuthoritativeFactBinding,
    CandidateCommit,
    FactReferenceAuthorityPort,
    PreparedAsset,
    TerminalCommand,
    TerminalCompletion,
    TerminalPort,
)

_PUBLIC_KIND = {"relationship": "relation_set"}


def _public_kind(state_kind: str) -> str:
    return _PUBLIC_KIND.get(state_kind, "document")


def _asset(content: bytes) -> PreparedAsset:
    digest = sha256_hex(content)
    return PreparedAsset(f"asset-sha256-{digest}", content, digest)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ExecutionLineage:
    plugin_id: str
    release_id: str
    package_hash: str
    capability_id: str
    job_id: str
    step_id: str
    attempt_id: str
    lease_epoch: int

    def __post_init__(self) -> None:
        for field in ("plugin_id", "capability_id", "job_id", "step_id", "attempt_id"):
            if not getattr(self, field).strip():
                raise ValueError(f"{field} must not be blank")
        for field in ("release_id", "package_hash"):
            value = getattr(self, field)
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{field} must be lowercase SHA-256")
        if isinstance(self.lease_epoch, bool) or self.lease_epoch < 1:
            raise ValueError("lease_epoch must be a positive integer")

    def producer(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "release_id": self.release_id,
            "capability_id": self.capability_id,
            "job_id": self.job_id,
            "step_id": self.step_id,
            "attempt_id": self.attempt_id,
            "lease_epoch": self.lease_epoch,
        }


@dataclass(frozen=True, slots=True)
class SkillChainEvidence:
    """Opaque evidence consumed by the Prompt Runtime authority."""

    chain: Mapping[str, Any]
    receipts: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.chain, Mapping):
            raise TypeError("Skill chain must be an object")
        object.__setattr__(self, "receipts", tuple(self.receipts))


@dataclass(frozen=True, slots=True)
class StoryStateRequest:
    operation_key: str
    candidate_stage_operation_key: str
    bundle_id: str
    receipt_id: str
    input_snapshot_hash: str
    lineage: ExecutionLineage
    proposals: tuple[Proposal, ...]
    known_entity_ids: frozenset[str]
    created_at: str
    skill_chains: tuple[SkillChainEvidence, ...] = ()
    parent_receipt_ids: tuple[str, ...] = ()
    terminal_detail_asset_id: str | None = None
    local_seq: int = 1

    def __post_init__(self) -> None:
        for field in ("operation_key", "candidate_stage_operation_key", "bundle_id", "receipt_id", "created_at"):
            if not getattr(self, field).strip():
                raise ValueError(f"{field} must not be blank")
        if len(self.input_snapshot_hash) != 64 or any(char not in "0123456789abcdef" for char in self.input_snapshot_hash):
            raise ValueError("input_snapshot_hash must be lowercase SHA-256")
        object.__setattr__(self, "proposals", tuple(self.proposals))
        object.__setattr__(self, "skill_chains", tuple(self.skill_chains))
        object.__setattr__(self, "parent_receipt_ids", tuple(self.parent_receipt_ids))
        object.__setattr__(self, "known_entity_ids", frozenset(self.known_entity_ids))
        if not self.proposals:
            raise ValueError("Story State request requires proposals")


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    schema: str
    outcome: str
    bundle: Mapping[str, Any]
    candidates: tuple[CandidateCommit, ...]
    committed_receipt: Mapping[str, Any]
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "outcome": self.outcome,
            "bundle": _thaw(self.bundle),
            "candidates": [
                {
                    "item_id": item.item_id,
                    "candidate_id": item.candidate_id,
                    "stage_status": item.stage_status,
                    "publication_eligibility": item.publication_eligibility,
                }
                for item in self.candidates
            ],
            "committed_receipt": _thaw(self.committed_receipt),
            "replayed": self.replayed,
        }


def _verify_chain_through_prompt_runtime(evidence: SkillChainEvidence) -> None:
    """The only Story State Skill-chain semantic adapter."""

    from plotpilot_prompt_skill_runtime import verify_chain

    verify_chain(evidence.chain, evidence.receipts)


def _verify_chain_for_snapshot(
    evidence: SkillChainEvidence,
    *,
    input_snapshot_hash: str,
) -> Mapping[str, Any]:
    _verify_chain_through_prompt_runtime(evidence)
    chain = evidence.chain
    if chain["run_snapshot_hash"] != input_snapshot_hash:
        raise ValueError("Story State Skill chain belongs to a foreign input snapshot")
    return chain


def _chain_asset_and_ref(chain: Mapping[str, Any]) -> tuple[PreparedAsset, dict[str, Any]]:
    content = canonical_bytes(dict(chain))
    asset = _asset(content)
    return asset, {
        "schema": "skill-chain-ref/v1",
        "chain_result_id": chain["chain_id"],
        "asset_id": asset.asset_id,
        "asset_hash": asset.sha256,
        "result_bundle_id": chain["result_bundle_id"],
        "result_item_id": chain["result_item_id"],
        "stream_id": chain["stream_id"],
        "acked_prefix_hash": chain["acked_prefix_hash"],
    }


def _candidate_item(proposal: Proposal, payload: StatePayload, asset: PreparedAsset) -> dict[str, Any]:
    entity_kind = _public_kind(payload.state_kind)
    status = "complete" if proposal.outcome == "success" else "failed"
    source_refs = [
        {
            "workspace_id": ref.workspace_id,
            "source_type": "revision",
            "source_id": ref.revision_id,
            "revision_or_hash": ref.content_hash,
        }
        for ref in payload.references
    ]
    return {
        "schema": "candidate-item/v1",
        "item_id": proposal.item_id,
        "item_kind": entity_kind,
        "target": {
            "workspace_id": proposal.target.workspace_id,
            "entity_kind": entity_kind,
            "entity_id": proposal.target.entity_id,
        },
        "mutation": {
            "mode": "relation_patch" if entity_kind == "relation_set" else "replace",
            "payload_schema": f"story-state/{payload.state_kind}/v1",
            "payload_hash": asset.sha256,
        },
        "payload_asset_id": asset.asset_id,
        "base": {
            "revision_id": proposal.target.revision_id,
            "content_hash": proposal.target.content_hash,
        },
        "write_set": [
            {
                "workspace_id": proposal.target.workspace_id,
                "entity_kind": entity_kind,
                "entity_id": proposal.target.entity_id,
                "revision_id": proposal.target.revision_id,
                "content_hash": proposal.target.content_hash,
            }
        ],
        "parent_candidate_ids": list(proposal.parent_candidate_ids),
        "source_refs": source_refs,
        "status": status,
    }


def _receipt(request: StoryStateRequest, bundle: Mapping[str, Any], bundle_hash: str) -> dict[str, Any]:
    staged_items = [proposal.item_id for proposal in request.proposals if proposal.outcome == "success"]
    value: dict[str, Any] = {
        "schema": "provenance-receipt/v1",
        "receipt_id": request.receipt_id,
        "plugin_id": request.lineage.plugin_id,
        "release_id": request.lineage.release_id,
        "package_hash": request.lineage.package_hash,
        "capability_id": request.lineage.capability_id,
        "job_id": request.lineage.job_id,
        "step_id": request.lineage.step_id,
        "attempt_id": request.lineage.attempt_id,
        "lease_epoch": request.lineage.lease_epoch,
        "run_snapshot_hash": request.input_snapshot_hash,
        "bundle_id": bundle["bundle_id"],
        "bundle_hash": bundle_hash,
        "parent_receipt_ids": list(request.parent_receipt_ids),
        "model_receipt_ids": [],
        "skill_chain_result_refs": list(bundle["skill_chain_result_refs"]),
        "staged_items": staged_items,
        "created_at": request.created_at,
    }
    value["receipt_hash"] = hash_jcs("provenance-receipt/v1", value)
    assert_valid("provenance-receipt/v1", value)
    return value


def _validate_committed_receipt(committed: Mapping[str, Any], prepared: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(committed)
    assert_valid("provenance-receipt/v1", value)
    expected_hash = hash_jcs("provenance-receipt/v1", {key: item for key, item in value.items() if key != "receipt_hash"})
    if value["receipt_hash"] != expected_hash:
        raise ValueError("committed provenance receipt hash drift")
    stable_fields = set(prepared) - {"created_at", "receipt_hash"}
    if any(value[field] != prepared[field] for field in stable_fields):
        raise ValueError("committed provenance receipt lineage drift")
    return value


class StoryStateRuntime:
    def __init__(
        self,
        terminal: TerminalPort,
        reference_authority: FactReferenceAuthorityPort | None = None,
    ) -> None:
        self._terminal = terminal
        self._reference_authority = reference_authority

    def _validate_authoritative_reference(
        self,
        reference: FactRef,
        *,
        workspace_id: str,
    ) -> None:
        authority = self._reference_authority
        if authority is None:
            raise ValueError("Story State reference requires authoritative resolution")
        binding = authority.resolve_reference(
            workspace_id=reference.workspace_id,
            entity_kind=reference.entity_kind,
            entity_id=reference.entity_id,
        )
        if not isinstance(binding, AuthoritativeFactBinding):
            raise TypeError("Fact reference authority returned an invalid binding")
        document = parse_core_authority(
            binding.document,
            expected_workspace_id=workspace_id,
        )
        revision = parse_core_authority(
            binding.revision,
            expected_workspace_id=workspace_id,
        )
        if document["schema"] != "core-document/v1" or revision["schema"] != "core-revision/v1":
            raise ValueError("Fact reference authority requires a Core document and Revision")
        actual = (
            binding.entity_kind,
            document["workspace_id"],
            document["document_id"],
            document["current_revision_id"],
            revision["workspace_id"],
            revision["document_id"],
            revision["node_id"],
            revision["revision_id"],
            revision["content_hash"],
        )
        expected = (
            reference.entity_kind,
            reference.workspace_id,
            reference.entity_id,
            reference.revision_id,
            reference.workspace_id,
            reference.entity_id,
            None,
            reference.revision_id,
            reference.content_hash,
        )
        if actual != expected:
            raise ValueError("Story State reference authority binding drift")

    def execute(self, request: StoryStateRequest) -> RuntimeResult:
        """Validate the full operation, then perform exactly one terminal call."""

        item_ids: set[str] = set()
        workspace_ids = {proposal.target.workspace_id for proposal in request.proposals}
        if len(workspace_ids) != 1:
            raise ValueError("Story State request cannot cross workspaces")
        workspace_id = next(iter(workspace_ids))
        batch_entity_ids = frozenset(proposal.target.entity_id for proposal in request.proposals)
        validated: list[tuple[Proposal, StatePayload]] = []
        for proposal in request.proposals:
            if proposal.item_id in item_ids:
                raise ValueError("Story State item IDs must be unique")
            item_ids.add(proposal.item_id)
            payload = StatePayload.coerce(proposal.payload)
            if payload.state_kind != proposal.target.entity_kind or payload.entity_id != proposal.target.entity_id:
                raise ValueError("Story State payload identity does not match its target")
            payload.validate_reference_closure(
                workspace_id=proposal.target.workspace_id,
                known_entities=request.known_entity_ids,
                batch_entity_ids=batch_entity_ids,
            )
            for reference in payload.references:
                self._validate_authoritative_reference(reference, workspace_id=workspace_id)
            validated.append((proposal, payload))

        verified_chains = tuple(
            _verify_chain_for_snapshot(
                evidence,
                input_snapshot_hash=request.input_snapshot_hash,
            )
            for evidence in request.skill_chains
        )
        if len({chain["chain_id"] for chain in verified_chains}) != len(verified_chains):
            raise ValueError("Story State Skill-chain identities must be unique")

        successes = sum(proposal.outcome == "success" for proposal in request.proposals)
        if successes == 0:
            raise ValueError("all-failed Story State work must use a diagnostic terminal contract")

        payload_assets: list[PreparedAsset] = []
        items: list[dict[str, Any]] = []
        for proposal, payload in validated:
            asset = _asset(canonical_bytes(payload.to_dict()))
            payload_assets.append(asset)
            items.append(_candidate_item(proposal, payload, asset))

        chain_assets: list[PreparedAsset] = []
        chain_refs: list[dict[str, Any]] = []
        for chain in verified_chains:
            asset, ref = _chain_asset_and_ref(chain)
            chain_assets.append(asset)
            chain_refs.append(ref)

        partial = successes != len(request.proposals)
        bundle = {
            "schema": "result-bundle/v1",
            "contract_id": "candidate-batch/v1",
            "bundle_id": request.bundle_id,
            "bundle_type": "candidate_batch",
            "producer": request.lineage.producer(),
            "input_snapshot_hash": request.input_snapshot_hash,
            "items": items,
            "warnings": [
                {"code": "story_state_item_failed", "message": proposal.error or "", "details_asset_id": None}
                for proposal in request.proposals
                if proposal.outcome == "failure"
            ],
            "partial": partial,
            "provenance_receipt_id": request.receipt_id,
            "skill_chain_result_refs": chain_refs,
        }
        verify_result_bundle(
            bundle,
            snapshot_workspace_id=workspace_id,
            snapshot_hash_value=request.input_snapshot_hash,
        )
        bundle_asset = _asset(canonical_bytes(bundle))
        receipt = _receipt(request, bundle, bundle_asset.sha256)
        assets_by_id: dict[str, PreparedAsset] = {}
        for planned in payload_assets + chain_assets + [bundle_asset]:
            existing = assets_by_id.get(planned.asset_id)
            if existing is not None and existing != planned:
                raise ValueError("content-addressed Asset plan drift")
            assets_by_id[planned.asset_id] = planned
        assets = tuple(assets_by_id.values())
        command = TerminalCommand(
            operation_key=request.operation_key,
            candidate_stage_operation_key=request.candidate_stage_operation_key,
            outcome="partial" if partial else "succeeded",
            result_bundle=bundle,
            result_bundle_asset=bundle_asset,
            assets=assets,
            provenance_receipt=receipt,
            terminal_detail_asset_id=request.terminal_detail_asset_id,
            local_seq=request.local_seq,
        )

        completion = self._terminal.complete(command)
        self._validate_completion(completion, command, request)
        committed = _validate_committed_receipt(completion.committed_receipt, receipt)
        return RuntimeResult(
            "story-state-terminal-result/v1",
            completion.outcome,
            _freeze(bundle),
            completion.candidates,
            _freeze(committed),
            completion.replayed,
        )

    @staticmethod
    def _validate_completion(
        completion: TerminalCompletion,
        command: TerminalCommand,
        request: StoryStateRequest,
    ) -> None:
        if completion.outcome != command.outcome or completion.bundle_id != request.bundle_id:
            raise ValueError("terminal completion identity drift")
        expected = [proposal.item_id for proposal in request.proposals if proposal.outcome == "success"]
        actual = [candidate.item_id for candidate in completion.candidates]
        if actual != expected or len({candidate.candidate_id for candidate in completion.candidates}) != len(actual):
            raise ValueError("terminal Candidate mapping is not exact")
        if any(candidate.publication_eligibility != "eligible" for candidate in completion.candidates):
            raise ValueError("complete Story State Candidate is not publication-eligible")
