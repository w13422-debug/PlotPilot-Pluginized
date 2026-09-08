"""Read-only adapter from the frozen Core Story-State projection input v2."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

try:
    from plotpilot_plugin_sdk import canonical_bytes, parse_json_bytes
    from plotpilot_plugin_sdk.core_api_v2 import parse_story_state_projection_input_v2
except ModuleNotFoundError:  # pragma: no cover - repository-source fallback
    from backend.plotpilot_plugin_sdk import canonical_bytes, parse_json_bytes
    from backend.plotpilot_plugin_sdk.core_api_v2 import (
        parse_story_state_projection_input_v2,
    )

from .domain import FactRef, Projection, build_projection
from .payloads import StatePayload


class CoreProjectionAdapterError(ValueError):
    """A Core readback is malformed, stale, or cannot be made byte-equivalent."""


class ProjectionAssetReader(Protocol):
    def read_asset(self, asset_id: str) -> bytes: ...


class CoreProjectionQueryPort(Protocol):
    def read_story_state_projection(
        self, query: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProductionProjectionRecord:
    projection_input_id: str
    publication_id: str
    candidate_id: str
    workspace_id: str
    revision_id: str
    revision_number: int
    release_id: str
    package_hash: str
    fact: FactRef
    payload: StatePayload

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_input_id": self.projection_input_id,
            "publication_id": self.publication_id,
            "candidate_id": self.candidate_id,
            "workspace_id": self.workspace_id,
            "revision_id": self.revision_id,
            "revision_number": self.revision_number,
            "release_id": self.release_id,
            "package_hash": self.package_hash,
            "fact": {
                "workspace_id": self.fact.workspace_id,
                "entity_kind": self.fact.entity_kind,
                "entity_id": self.fact.entity_id,
                "revision_id": self.fact.revision_id,
                "content_hash": self.fact.content_hash,
            },
            "payload": self.payload.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ProductionProjection:
    projection: Projection
    records: tuple[ProductionProjectionRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "story-state-production-projection/v1",
            "records": [record.to_dict() for record in self.records],
            "source_revisions": list(self.projection.source_revisions),
            "by_scope": [
                {
                    "workspace_id": scope[0],
                    "entity_kind": scope[1],
                    "entities": [list(key) for key in entities],
                }
                for scope, entities in self.projection.by_scope.items()
            ],
            "edges": [
                [list(source), relation, list(target)]
                for source, relation, target in self.projection.edges
            ],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_dict())

    def assert_not_stale_after(self, previous: ProductionProjection) -> None:
        """Reject a lower/equal conflicting revision without retaining state."""

        before = {record.fact.key: record for record in previous.records}
        for record in self.records:
            old = before.get(record.fact.key)
            if old is None:
                continue
            if record.revision_number < old.revision_number:
                raise CoreProjectionAdapterError(
                    "stale Publication projection cannot replace a newer Story-State revision"
                )
            if (
                record.revision_number == old.revision_number
                and record.revision_id != old.revision_id
            ):
                raise CoreProjectionAdapterError(
                    "same Story-State revision number has conflicting publication identity"
                )


def _read(
    asset_reader: ProjectionAssetReader, asset_id: str, expected_hash: str, label: str
) -> bytes:
    try:
        raw = bytes(asset_reader.read_asset(asset_id))
    except Exception as exc:
        raise CoreProjectionAdapterError(f"{label} Asset is unavailable") from exc
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise CoreProjectionAdapterError(
            f"{label} Asset hash does not match Core projection"
        )
    return raw


def projection_record(
    value: Mapping[str, Any],
    *,
    asset_reader: ProjectionAssetReader,
    expected_workspace_id: str | None = None,
) -> ProductionProjectionRecord:
    try:
        parsed = parse_story_state_projection_input_v2(
            value, expected_workspace_id=expected_workspace_id
        )
    except Exception as exc:
        raise CoreProjectionAdapterError(
            f"Core Story-State projection input is invalid: {exc}"
        ) from exc
    candidate = parsed["candidate"]
    publication = parsed["publication"]
    revision = parsed["current_revision"]
    assets = {asset["asset_id"]: asset for asset in parsed["assets"]}
    payload_asset = assets[candidate["payload_asset_id"]]
    content_asset = assets[revision["content_asset_id"]]
    payload_bytes = _read(
        asset_reader,
        payload_asset["asset_id"],
        payload_asset["sha256"],
        "Candidate payload",
    )
    content_bytes = _read(
        asset_reader,
        content_asset["asset_id"],
        content_asset["sha256"],
        "current Revision",
    )
    if (
        payload_bytes != content_bytes
        or candidate["payload_hash"] != revision["content_hash"]
    ):
        raise CoreProjectionAdapterError(
            "published Candidate payload is not byte-equivalent to the current Revision"
        )
    try:
        raw_payload = parse_json_bytes(content_bytes)
        if not isinstance(raw_payload, Mapping):
            raise TypeError("payload is not an object")
        payload = StatePayload.from_mapping(raw_payload)
    except Exception as exc:
        raise CoreProjectionAdapterError(
            f"published Story-State payload is invalid: {exc}"
        ) from exc
    expected_kind = (
        "relation_set" if payload.state_kind == "relationship" else "document"
    )
    target = candidate["target"]
    if (
        target["entity_kind"] != expected_kind
        or target["entity_id"] != payload.entity_id
    ):
        raise CoreProjectionAdapterError(
            "Core Candidate target does not bind the published Story-State payload"
        )
    if candidate["status"] != "complete":
        raise CoreProjectionAdapterError(
            "partial Candidate cannot become a Story-State projection"
        )
    return ProductionProjectionRecord(
        projection_input_id=parsed["projection_input_id"],
        publication_id=publication["publication_id"],
        candidate_id=candidate["candidate_id"],
        workspace_id=parsed["workspace_id"],
        revision_id=revision["revision_id"],
        revision_number=revision["revision_number"],
        release_id=parsed["provenance"]["release_id"],
        package_hash=parsed["provenance"]["package_hash"],
        fact=FactRef(
            parsed["workspace_id"],
            payload.state_kind,
            payload.entity_id,
            revision["revision_id"],
            revision["content_hash"],
        ),
        payload=payload,
    )


def rebuild_production_projection(
    inputs: Sequence[Mapping[str, Any]],
    *,
    asset_reader: ProjectionAssetReader,
    expected_workspace_id: str | None = None,
) -> ProductionProjection:
    """Rebuild a disposable canonical projection from Core-owned publications."""

    records = tuple(
        projection_record(
            value,
            asset_reader=asset_reader,
            expected_workspace_id=expected_workspace_id,
        )
        for value in inputs
    )
    if len({record.publication_id for record in records}) != len(records):
        raise CoreProjectionAdapterError(
            "projection contains duplicate Publication identities"
        )
    if len({record.candidate_id for record in records}) != len(records):
        raise CoreProjectionAdapterError(
            "projection contains duplicate Candidate identities"
        )
    ordered = tuple(
        sorted(
            records,
            key=lambda record: (
                record.fact.key,
                record.revision_number,
                record.publication_id,
            ),
        )
    )
    lookup = {
        (record.fact.workspace_id, record.fact.entity_id): record.fact.key
        for record in ordered
    }
    if len(lookup) != len(ordered):
        raise CoreProjectionAdapterError(
            "projection entity identity is ambiguous within a Workspace"
        )
    edges: list[tuple[tuple[str, str, str], str, tuple[str, str, str]]] = []
    for record in ordered:
        if record.payload.state_kind != "relationship":
            continue
        source = lookup.get(
            (record.workspace_id, record.payload.body["source_entity_id"])
        )
        target = lookup.get(
            (record.workspace_id, record.payload.body["target_entity_id"])
        )
        if source is None or target is None:
            raise CoreProjectionAdapterError(
                "published relationship is not closed over the authoritative projection"
            )
        edges.append((source, record.payload.body["relation_type"], target))
    return ProductionProjection(
        build_projection((record.fact for record in ordered), edges), ordered
    )


class CoreProjectionAdapter:
    """Typed read adapter; it never reaches a repository or keeps derived truth."""

    def __init__(self, asset_reader: ProjectionAssetReader) -> None:
        self._asset_reader = asset_reader

    def rebuild(
        self,
        inputs: Sequence[Mapping[str, Any]],
        *,
        expected_workspace_id: str | None = None,
    ) -> ProductionProjection:
        return rebuild_production_projection(
            inputs,
            asset_reader=self._asset_reader,
            expected_workspace_id=expected_workspace_id,
        )

    def read_one(
        self, port: CoreProjectionQueryPort, *, workspace_id: str, document_id: str
    ) -> ProductionProjection:
        query = {
            "schema": "core-authority-query/v2",
            "workspace_id": workspace_id,
            "entity_kind": "document",
            "entity_id": document_id,
            "include_assets": True,
        }
        try:
            value = port.read_story_state_projection(MappingProxyType(query))
        except Exception as exc:
            raise CoreProjectionAdapterError(
                "Core Story-State projection query failed"
            ) from exc
        return self.rebuild((value,), expected_workspace_id=workspace_id)


__all__ = [
    "CoreProjectionAdapter",
    "CoreProjectionAdapterError",
    "CoreProjectionQueryPort",
    "ProductionProjection",
    "ProductionProjectionRecord",
    "ProjectionAssetReader",
    "projection_record",
    "rebuild_production_projection",
]
