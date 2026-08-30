"""Disposable projection from a closed Core Publication authority set."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

try:
    from plotpilot_plugin_sdk import assert_valid, hash_jcs, parse_json_bytes
except ModuleNotFoundError:  # pragma: no cover - repository runner fallback
    from backend.plotpilot_plugin_sdk import assert_valid, hash_jcs, parse_json_bytes

try:
    from plotpilot_plugin_sdk.core_api import (
        parse_asset_contract,
        parse_core_authority,
        parse_publication,
    )
except ModuleNotFoundError:  # pragma: no cover - repository runner fallback
    from backend.plotpilot_plugin_sdk.core_api import (
        parse_asset_contract,
        parse_core_authority,
        parse_publication,
    )

from .domain import FactRef, Projection, build_projection
from .payloads import StatePayload


@dataclass(frozen=True, slots=True)
class CandidateAuthority:
    candidate_id: str
    state: str
    item: Mapping[str, Any]
    job_id: str
    step_id: str
    attempt_id: str
    bundle_id: str
    provenance_receipt_id: str

    def __post_init__(self) -> None:
        for field in (
            "candidate_id",
            "job_id",
            "step_id",
            "attempt_id",
            "bundle_id",
            "provenance_receipt_id",
        ):
            if not getattr(self, field).strip():
                raise ValueError(f"{field} must not be blank")
        if self.state != "published":
            raise ValueError("projection accepts only published Candidates")


@dataclass(frozen=True, slots=True)
class PublicationReceiptRef:
    publication_id: str
    candidate_id: str
    revision_id: str
    provenance_receipt_id: str
    job_id: str
    step_id: str
    attempt_id: str
    bundle_id: str
    item_id: str

    def __post_init__(self) -> None:
        for field in (
            "publication_id",
            "candidate_id",
            "revision_id",
            "provenance_receipt_id",
            "job_id",
            "step_id",
            "attempt_id",
            "bundle_id",
            "item_id",
        ):
            if not getattr(self, field).strip():
                raise ValueError(f"{field} must not be blank")


@dataclass(frozen=True, slots=True)
class PublicationAnchor:
    publication_id: str
    candidate_id: str
    revision_id: str
    receipt_id: str
    asset_id: str
    fact: FactRef


@dataclass(frozen=True, slots=True)
class PublishedStateRecord:
    publication: Mapping[str, Any]
    current_revision: Mapping[str, Any]
    current_pointer_revision_id: str
    candidate: CandidateAuthority
    publication_receipt: PublicationReceiptRef
    asset_metadata: Mapping[str, Any]
    payload_bytes: bytes
    provenance_receipt: Mapping[str, Any]

    def validate(self) -> tuple[PublicationAnchor, StatePayload]:
        item = dict(self.candidate.item)
        receipt = dict(self.provenance_receipt)
        assert_valid("candidate-item/v1", item)
        assert_valid("provenance-receipt/v1", receipt)
        target = item["target"]
        publication = parse_publication(
            self.publication,
            expected_workspace_id=target["workspace_id"],
        )
        revision = parse_core_authority(
            self.current_revision,
            expected_workspace_id=publication["workspace_id"],
        )
        asset = parse_asset_contract(self.asset_metadata)
        if publication["schema"] != "publication-result/v1":
            raise ValueError("projection requires a Publication result")
        if revision["schema"] != "core-revision/v1":
            raise ValueError("projection requires a current Core Revision")
        if asset["schema"] != "asset-metadata/v1":
            raise ValueError("projection requires Asset metadata")
        if receipt["receipt_hash"] != hash_jcs(
            "provenance-receipt/v1",
            {key: value for key, value in receipt.items() if key != "receipt_hash"},
        ):
            raise ValueError("Publication provenance receipt hash drift")

        result_revision = publication["resulting_revision"]
        publication_identity = (
            publication["publication_id"],
            publication["candidate_id"],
            result_revision["revision_id"],
        )
        if publication_identity != (
            self.publication_receipt.publication_id,
            self.publication_receipt.candidate_id,
            self.publication_receipt.revision_id,
        ):
            raise ValueError("Publication receipt identity drift")
        if publication["candidate_id"] != self.candidate.candidate_id:
            raise ValueError("Publication does not reference the authoritative Candidate")
        if revision["revision_id"] != self.current_pointer_revision_id:
            raise ValueError("Publication Revision is not the current authority pointer")
        if revision["source_candidate_id"] != self.candidate.candidate_id:
            raise ValueError("Revision source Candidate drift")
        current_target = (
            revision["workspace_id"],
            "document" if revision["document_id"] is not None else "node_structure",
            revision["document_id"] or revision["node_id"],
        )
        publication_target = (
            publication["workspace_id"],
            publication["entity_kind"],
            publication["entity_id"],
        )
        if current_target != publication_target:
            raise ValueError("current Revision target does not match Publication authority")
        revision_identity = (
            revision["revision_id"],
            revision["content_hash"],
            revision["revision_number"],
        )
        if revision_identity != (
            result_revision["revision_id"],
            result_revision["content_hash"],
            result_revision["revision_number"],
        ):
            raise ValueError("nested Publication Revision drift")
        if publication_target != (
            target["workspace_id"],
            target["entity_kind"],
            target["entity_id"],
        ):
            raise ValueError("Publication target does not match Candidate target")
        if item["status"] != "complete" or item["item_id"] != self.publication_receipt.item_id:
            raise ValueError("Publication Candidate item is not the committed complete item")

        payload_bytes = bytes(self.payload_bytes)
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        if (
            asset["size"] != len(payload_bytes)
            or asset["mime"] != "application/json"
            or asset["logical_role"] != "story_state_payload"
            or asset["rebuildable"] is not False
        ):
            raise ValueError("Publication Asset semantic closure/content closure drift")
        if (
            asset["asset_id"] != item["payload_asset_id"]
            or asset["sha256"] != payload_hash
            or item["mutation"]["payload_hash"] != payload_hash
            or revision["content_hash"] != payload_hash
            or result_revision["content_hash"] != payload_hash
        ):
            raise ValueError("Publication Asset/Revision content closure drift")
        if revision["payload_schema"] != item["mutation"]["payload_schema"]:
            raise ValueError("Publication payload schema drift")
        raw_payload = parse_json_bytes(payload_bytes)
        if not isinstance(raw_payload, Mapping):
            raise TypeError("published Story State payload is not an object")
        payload = StatePayload.from_mapping(raw_payload)
        if payload.entity_id != target["entity_id"]:
            raise ValueError("published Story State payload identity drift")
        expected_target_kind = "relation_set" if payload.state_kind == "relationship" else "document"
        if target["entity_kind"] != expected_target_kind:
            raise ValueError("published Story State kind/target drift")
        if item["mutation"]["payload_schema"] != f"story-state/{payload.state_kind}/v1":
            raise ValueError("published Story State private schema drift")

        authority = self.candidate
        receipt_ref = self.publication_receipt
        receipt_identity = (
            receipt["receipt_id"],
            receipt["job_id"],
            receipt["step_id"],
            receipt["attempt_id"],
            receipt["bundle_id"],
        )
        expected_receipt_identity = (
            authority.provenance_receipt_id,
            authority.job_id,
            authority.step_id,
            authority.attempt_id,
            authority.bundle_id,
        )
        ref_receipt_identity = (
            receipt_ref.provenance_receipt_id,
            receipt_ref.job_id,
            receipt_ref.step_id,
            receipt_ref.attempt_id,
            receipt_ref.bundle_id,
        )
        if receipt_identity != expected_receipt_identity or receipt_identity != ref_receipt_identity:
            raise ValueError("Publication execution receipt lineage drift")
        if item["item_id"] not in receipt["staged_items"]:
            raise ValueError("Publication Candidate is absent from committed receipt")

        fact = FactRef(
            publication["workspace_id"],
            payload.state_kind,
            payload.entity_id,
            revision["revision_id"],
            revision["content_hash"],
        )
        return (
            PublicationAnchor(
                publication["publication_id"],
                authority.candidate_id,
                revision["revision_id"],
                receipt["receipt_id"],
                asset["asset_id"],
                fact,
            ),
            payload,
        )


@dataclass(frozen=True, slots=True)
class AuthoritativeProjection:
    projection: Projection
    anchors: tuple[PublicationAnchor, ...]


def rebuild_projection(records: Sequence[PublishedStateRecord]) -> AuthoritativeProjection:
    """Validate every authority closure before constructing a disposable index."""

    validated = tuple(record.validate() for record in records)
    anchors = tuple(value[0] for value in validated)
    if len({anchor.publication_id for anchor in anchors}) != len(anchors):
        raise ValueError("projection Publication IDs must be unique")
    if len({anchor.candidate_id for anchor in anchors}) != len(anchors):
        raise ValueError("projection Candidate IDs must be unique")
    entity_lookup: dict[tuple[str, str], tuple[str, str, str]] = {}
    for anchor in anchors:
        lookup_key = anchor.fact.workspace_id, anchor.fact.entity_id
        if lookup_key in entity_lookup:
            raise ValueError("projection entity IDs must be unambiguous within a workspace")
        entity_lookup[lookup_key] = anchor.fact.key
    relations: list[tuple[tuple[str, str, str], str, tuple[str, str, str]]] = []
    for anchor, payload in validated:
        if payload.state_kind != "relationship":
            continue
        source = entity_lookup.get((anchor.fact.workspace_id, payload.body["source_entity_id"]))
        target = entity_lookup.get((anchor.fact.workspace_id, payload.body["target_entity_id"]))
        if source is None or target is None:
            raise ValueError("published relationship references an absent authoritative fact")
        relations.append((source, payload.body["relation_type"], target))
    projection = build_projection((anchor.fact for anchor in anchors), relations)
    return AuthoritativeProjection(projection, anchors)
