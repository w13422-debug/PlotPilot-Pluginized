"""Preflight-first Story State ResultBundle materialization."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

try:
    from plotpilot_plugin_sdk import (
        assert_valid,
        canonical_bytes,
        hash_jcs,
        parse_json_bytes,
        sha256_hex,
        verify_result_bundle,
    )
except ModuleNotFoundError:  # pragma: no cover - repository runner fallback
    from backend.plotpilot_plugin_sdk import (
        assert_valid,
        canonical_bytes,
        hash_jcs,
        parse_json_bytes,
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
_RUNTIME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_RUNTIME_INPUT_SCHEMA = "story-state-runtime-input/v1"


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


def _runtime_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _RUNTIME_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical identity")
    return value


def _runtime_mapping(value: object, *, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{label} fields are not closed")
    return value


def _runtime_sequence(value: object, *, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be an array")
    return value


class _SnapshotReferenceAuthority:
    """Read-only reference bindings carried by a hash-bound input Asset."""

    def __init__(
        self,
        bindings: Mapping[tuple[str, str, str], AuthoritativeFactBinding],
    ) -> None:
        self._bindings = dict(bindings)

    def resolve_reference(
        self,
        *,
        workspace_id: str,
        entity_kind: str,
        entity_id: str,
    ) -> AuthoritativeFactBinding:
        binding = self._bindings.get((workspace_id, entity_kind, entity_id))
        if binding is None:
            raise ValueError("Story State reference is not bound by its RunSnapshot input")
        return binding


def _bound_story_runtime_input(
    context: Any,
) -> tuple[Mapping[str, Any], Any, Mapping[str, Any]]:
    """Load a canonical Story-State input Asset pinned by the RunSnapshot."""

    identity = getattr(context, "identity", None)
    snapshot = getattr(context, "run_snapshot", None)
    snapshot_asset = getattr(context, "run_snapshot_asset", None)
    assets = getattr(context, "assets", None)
    request = getattr(context, "request", None)
    if (
        identity is None
        or not isinstance(snapshot, Mapping)
        or snapshot_asset is None
        or not isinstance(request, Mapping)
        or not callable(getattr(assets, "read", None))
    ):
        raise ValueError(
            "Story State production execution requires a shared stdio-bound Attempt"
        )
    params = request.get("params")
    if (
        not isinstance(params, Mapping)
        or params.get("run_snapshot_asset_id")
        != getattr(identity, "run_snapshot_asset_id", None)
        or getattr(snapshot_asset, "asset_id", None)
        != getattr(identity, "run_snapshot_asset_id", None)
    ):
        raise ValueError("Story State RunSnapshot Asset binding drifted")
    parameters_asset_id = _runtime_id(
        snapshot.get("parameters_asset_id"), "parameters_asset_id"
    )
    matches = [
        item
        for item in snapshot.get("asset_hashes", ())
        if isinstance(item, Mapping) and item.get("asset_id") == parameters_asset_id
    ]
    if len(matches) != 1:
        raise ValueError("Story State parameters Asset is not RunSnapshot-bound")
    expected_hash = matches[0].get("sha256")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError("Story State parameters Asset hash is invalid")
    asset = assets.read(parameters_asset_id)
    if (
        getattr(asset, "asset_id", None) != parameters_asset_id
        or getattr(asset, "sha256", None) != expected_hash
        or not isinstance(getattr(asset, "content", None), bytes)
    ):
        raise ValueError("Story State parameters Asset authority drifted")
    try:
        parsed = parse_json_bytes(asset.content)
    except Exception as exc:
        raise ValueError("Story State parameters Asset is not JSON") from exc
    if not isinstance(parsed, Mapping) or canonical_bytes(parsed) != asset.content:
        raise ValueError("Story State parameters Asset is not canonical JSON")
    value = _runtime_mapping(
        parsed,
        fields={
            "schema",
            "run_snapshot",
            "execution",
            "request",
            "reference_bindings",
        },
        label="Story State runtime input",
    )
    if value["schema"] != _RUNTIME_INPUT_SCHEMA:
        raise ValueError("Story State runtime input schema is invalid")
    return value, identity, snapshot


def story_state_request_from_worker_context(
    context: Any,
) -> tuple[StoryStateRequest, FactReferenceAuthorityPort]:
    """Build the runtime request after all release and Attempt fences hold.

    The shared framed-stdio worker owns handshake, package, generation and
    RunSnapshot verification.  This private input adapter additionally binds
    the selected input bytes, no-settings Story-State profile, P2/Core-issued
    worker-run identity, and every proposal base/reference to that snapshot
    before ``StoryStateRuntime`` can reach an Asset-create or Candidate stage.
    """

    value, identity, snapshot = _bound_story_runtime_input(context)
    run_snapshot = _runtime_mapping(
        value["run_snapshot"],
        fields={
            "asset_id",
            "snapshot_id",
            "workspace_id",
            "parameters_asset_id",
        },
        label="Story State runtime RunSnapshot binding",
    )
    parameters_asset_id = snapshot["parameters_asset_id"]
    parameter_hashes = [
        item["sha256"]
        for item in snapshot["asset_hashes"]
        if item["asset_id"] == parameters_asset_id
    ]
    if len(parameter_hashes) != 1:
        raise ValueError("Story State parameters Asset hash binding drifted")
    expected_snapshot = {
        "asset_id": identity.run_snapshot_asset_id,
        "snapshot_id": identity.run_snapshot_id,
        "workspace_id": identity.workspace_id,
        "parameters_asset_id": parameters_asset_id,
    }
    if dict(run_snapshot) != expected_snapshot:
        raise ValueError("Story State runtime input crosses its RunSnapshot")

    execution = _runtime_mapping(
        value["execution"],
        fields={
            "plugin_id",
            "release_id",
            "package_hash",
            "data_generation_id",
            "capability_id",
            "settings_revision_id",
            "job_id",
            "step_id",
            "attempt_id",
            "lease_epoch",
            "worker_run_id",
        },
        label="Story State execution binding",
    )
    expected_execution = {
        "plugin_id": "com.plotpilot.story-state",
        "release_id": identity.plugin_release_id,
        "package_hash": identity.package_hash,
        "data_generation_id": identity.data_generation_id,
        "capability_id": "planning.story-state.settle/v1",
        "settings_revision_id": None,
        "job_id": identity.job_id,
        "step_id": identity.step_id,
        "attempt_id": identity.attempt_id,
        "lease_epoch": identity.lease_epoch,
        "worker_run_id": identity.operation_id,
    }
    if type(execution["lease_epoch"]) is not int or dict(execution) != expected_execution:
        raise ValueError("Story State execution Attempt binding drifted")
    releases = [
        item
        for item in snapshot["plugin_releases"]
        if item["plugin_id"] == expected_execution["plugin_id"]
    ]
    if len(releases) != 1 or (
        releases[0]["release_id"],
        releases[0]["package_hash"],
        releases[0]["data_generation_id"],
    ) != (
        identity.plugin_release_id,
        identity.package_hash,
        identity.data_generation_id,
    ):
        raise ValueError("Story State release/package/generation binding drifted")
    if any(
        item["plugin_id"] == expected_execution["plugin_id"]
        for item in snapshot["plugin_settings_revisions"]
    ):
        raise ValueError("Story State must not execute with a settings revision")

    request_value = _runtime_mapping(
        value["request"],
        fields={
            "bundle_id",
            "receipt_id",
            "created_at",
            "proposals",
            "known_entity_ids",
            "parent_receipt_ids",
            "terminal_detail_asset_id",
            "local_seq",
        },
        label="Story State execution request",
    )
    bundle_id = _runtime_id(request_value["bundle_id"], "bundle_id")
    receipt_id = _runtime_id(request_value["receipt_id"], "receipt_id")
    created_at = request_value["created_at"]
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("Story State created_at must be non-blank")
    known_values = _runtime_sequence(
        request_value["known_entity_ids"], label="Story State known_entity_ids"
    )
    known_entity_ids = frozenset(
        _runtime_id(item, "Story State known_entity_id") for item in known_values
    )
    if len(known_entity_ids) != len(known_values):
        raise ValueError("Story State known_entity_ids must be unique")
    parent_receipts = _runtime_sequence(
        request_value["parent_receipt_ids"], label="Story State parent_receipt_ids"
    )
    parent_receipt_ids = tuple(
        _runtime_id(item, "Story State parent_receipt_id") for item in parent_receipts
    )
    if len(set(parent_receipt_ids)) != len(parent_receipt_ids):
        raise ValueError("Story State parent_receipt_ids must be unique")
    terminal_detail_asset_id = request_value["terminal_detail_asset_id"]
    if terminal_detail_asset_id is not None:
        terminal_detail_asset_id = _runtime_id(
            terminal_detail_asset_id, "terminal_detail_asset_id"
        )
    local_seq = request_value["local_seq"]
    if type(local_seq) is not int or local_seq < 1:
        raise ValueError("Story State local_seq must be positive")

    snapshot_revisions = {
        (item["document_id"], item["revision_id"], item["content_hash"])
        for item in snapshot["input_revisions"]
    }
    bindings: dict[tuple[str, str, str], AuthoritativeFactBinding] = {}
    for raw in _runtime_sequence(
        value["reference_bindings"], label="Story State reference_bindings"
    ):
        binding_value = _runtime_mapping(
            raw,
            fields={"entity_kind", "document", "revision"},
            label="Story State reference binding",
        )
        entity_kind = _runtime_id(binding_value["entity_kind"], "reference entity_kind")
        document = parse_core_authority(
            _runtime_mapping(
                binding_value["document"],
                fields={
                    "schema",
                    "document_id",
                    "workspace_id",
                    "document_type",
                    "title",
                    "current_revision_id",
                    "created_at",
                    "updated_at",
                    "revision",
                },
                label="Story State reference document",
            ),
            expected_workspace_id=identity.workspace_id,
        )
        revision = parse_core_authority(
            _runtime_mapping(
                binding_value["revision"],
                fields={
                    "schema",
                    "revision_id",
                    "workspace_id",
                    "document_id",
                    "node_id",
                    "parent_revision_id",
                    "content_hash",
                    "created_by",
                    "source_candidate_id",
                    "created_at",
                    "revision_number",
                    "payload_schema",
                },
                label="Story State reference revision",
            ),
            expected_workspace_id=identity.workspace_id,
        )
        if (
            document["schema"] != "core-document/v1"
            or revision["schema"] != "core-revision/v1"
            or document["current_revision_id"] != revision["revision_id"]
            or revision["document_id"] != document["document_id"]
            or (
                document["document_id"],
                revision["revision_id"],
                revision["content_hash"],
            )
            not in snapshot_revisions
        ):
            raise ValueError("Story State reference binding is not RunSnapshot-backed")
        key = (identity.workspace_id, entity_kind, document["document_id"])
        if key in bindings:
            raise ValueError("Story State reference bindings must be unique")
        bindings[key] = AuthoritativeFactBinding(entity_kind, document, revision)

    proposals: list[Proposal] = []
    for raw in _runtime_sequence(request_value["proposals"], label="Story State proposals"):
        proposal_value = _runtime_mapping(
            raw,
            fields={
                "proposal_id",
                "operation_id",
                "item_id",
                "retry_id",
                "target",
                "payload",
                "parent_candidate_ids",
                "outcome",
                "error",
                "terminal_seq",
            },
            label="Story State proposal",
        )
        target = FactRef(
            **dict(
                _runtime_mapping(
                    proposal_value["target"],
                    fields={
                        "workspace_id",
                        "entity_kind",
                        "entity_id",
                        "revision_id",
                        "content_hash",
                    },
                    label="Story State proposal target",
                )
            )
        )
        if (
            target.workspace_id != identity.workspace_id
            or (target.entity_id, target.revision_id, target.content_hash)
            not in snapshot_revisions
        ):
            raise ValueError("Story State proposal target is not RunSnapshot-backed")
        parents = _runtime_sequence(
            proposal_value["parent_candidate_ids"],
            label="Story State parent_candidate_ids",
        )
        parent_candidate_ids = tuple(
            _runtime_id(item, "Story State parent_candidate_id") for item in parents
        )
        if len(set(parent_candidate_ids)) != len(parent_candidate_ids):
            raise ValueError("Story State parent_candidate_ids must be unique")
        if proposal_value["operation_id"] != identity.operation_id:
            raise ValueError("Story State proposal operation is not P2/Core-assigned")
        proposals.append(
            Proposal(
                proposal_id=_runtime_id(proposal_value["proposal_id"], "proposal_id"),
                operation_id=identity.operation_id,
                item_id=_runtime_id(proposal_value["item_id"], "item_id"),
                retry_id=_runtime_id(proposal_value["retry_id"], "retry_id"),
                target=target,
                payload=dict(
                    _runtime_mapping(
                        proposal_value["payload"],
                        fields={
                            "schema",
                            "state_kind",
                            "entity_id",
                            "body",
                            "references",
                        },
                        label="Story State proposal payload",
                    )
                ),
                parent_candidate_ids=parent_candidate_ids,
                outcome=proposal_value["outcome"],
                error=proposal_value["error"],
                terminal_seq=proposal_value["terminal_seq"],
            )
        )
    if not proposals:
        raise ValueError("Story State runtime input requires proposals")

    operation_material = {
        "schema": "story-state-terminal-operation/v1",
        "workspace_id": identity.workspace_id,
        "run_snapshot_hash": identity.run_snapshot_hash,
        "job_id": identity.job_id,
        "step_id": identity.step_id,
        "attempt_id": identity.attempt_id,
        "lease_epoch": identity.lease_epoch,
        "worker_run_id": identity.operation_id,
        "capability_id": expected_execution["capability_id"],
    }
    operation_hash = hash_jcs("story-state-terminal-operation/v1", operation_material)
    request = StoryStateRequest(
        operation_key=f"story-state-terminal-{operation_hash}",
        candidate_stage_operation_key=f"story-state-stage-{operation_hash}",
        bundle_id=bundle_id,
        receipt_id=receipt_id,
        input_snapshot_hash=identity.run_snapshot_hash,
        lineage=ExecutionLineage(
            expected_execution["plugin_id"],
            identity.plugin_release_id,
            identity.package_hash,
            expected_execution["capability_id"],
            identity.job_id,
            identity.step_id,
            identity.attempt_id,
            identity.lease_epoch,
        ),
        proposals=tuple(proposals),
        known_entity_ids=known_entity_ids,
        created_at=created_at,
        parent_receipt_ids=parent_receipt_ids,
        terminal_detail_asset_id=terminal_detail_asset_id,
        local_seq=local_seq,
    )
    return request, _SnapshotReferenceAuthority(bindings)


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
        known_parent_ids = {
            parent_candidate_id
            for proposal in request.proposals
            for parent_candidate_id in proposal.parent_candidate_ids
        }
        verify_result_bundle(
            bundle,
            snapshot_workspace_id=workspace_id,
            snapshot_hash_value=request.input_snapshot_hash,
            known_parent_ids=known_parent_ids,
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


def execute_story_state_from_worker_context(
    context: Any,
    terminal: TerminalPort,
) -> RuntimeResult:
    """Execute the private, snapshot-bound production input through one terminal."""

    request, reference_authority = story_state_request_from_worker_context(context)
    return StoryStateRuntime(terminal, reference_authority).execute(request)
