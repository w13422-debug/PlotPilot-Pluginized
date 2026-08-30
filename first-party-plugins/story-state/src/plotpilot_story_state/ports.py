"""Typed read-only authority and terminal seams; no Core database handle."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class AuthoritativeFactBinding:
    """Read-only Core authority returned for one semantic Story State fact."""

    entity_kind: str
    document: Mapping[str, Any]
    revision: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.entity_kind, str) or not self.entity_kind.strip():
            raise ValueError("authoritative Fact entity_kind must not be blank")
        if not isinstance(self.document, Mapping) or not isinstance(self.revision, Mapping):
            raise TypeError("authoritative Fact binding requires document and Revision objects")


class FactReferenceAuthorityPort(Protocol):
    """Resolve a stable fact identity without trusting a caller Revision/hash."""

    def resolve_reference(
        self,
        *,
        workspace_id: str,
        entity_kind: str,
        entity_id: str,
    ) -> AuthoritativeFactBinding: ...


@dataclass(frozen=True, slots=True)
class PreparedAsset:
    asset_id: str
    content: bytes
    sha256: str
    mime: str = "application/json"

    def __post_init__(self) -> None:
        content = bytes(self.content)
        digest = hashlib.sha256(content).hexdigest()
        if self.sha256 != digest or self.asset_id != f"asset-sha256-{digest}":
            raise ValueError("prepared Asset identity does not match its bytes")
        if not self.mime.strip():
            raise ValueError("prepared Asset mime must not be blank")
        object.__setattr__(self, "content", content)


@dataclass(frozen=True, slots=True)
class CandidateCommit:
    item_id: str
    candidate_id: str
    stage_status: str
    publication_eligibility: str

    def __post_init__(self) -> None:
        if not self.item_id.strip() or not self.candidate_id.strip():
            raise ValueError("Candidate commit identities must not be blank")
        if self.stage_status not in {"created", "idempotent"}:
            raise ValueError("Candidate commit stage_status is not terminal")
        if self.publication_eligibility not in {"eligible", "review_only"}:
            raise ValueError("Candidate publication eligibility is invalid")


@dataclass(frozen=True, slots=True)
class TerminalCommand:
    operation_key: str
    candidate_stage_operation_key: str
    outcome: str
    result_bundle: Mapping[str, Any]
    result_bundle_asset: PreparedAsset
    assets: tuple[PreparedAsset, ...]
    provenance_receipt: Mapping[str, Any]
    terminal_detail_asset_id: str | None
    local_seq: int

    def __post_init__(self) -> None:
        if not self.operation_key.strip() or not self.candidate_stage_operation_key.strip():
            raise ValueError("terminal operation keys must not be blank")
        if self.outcome not in {"succeeded", "partial"}:
            raise ValueError("Story State terminal outcome must be succeeded or partial")
        if isinstance(self.local_seq, bool) or self.local_seq < 1:
            raise ValueError("local_seq must be a positive integer")
        assets = tuple(self.assets)
        if len({asset.asset_id for asset in assets}) != len(assets):
            raise ValueError("terminal Asset plan contains duplicate IDs")
        if self.result_bundle_asset.asset_id not in {asset.asset_id for asset in assets}:
            raise ValueError("result Bundle Asset must be in the atomic Asset plan")
        object.__setattr__(self, "assets", assets)


@dataclass(frozen=True, slots=True)
class TerminalCompletion:
    accepted: bool
    outcome: str
    bundle_id: str
    candidates: tuple[CandidateCommit, ...]
    committed_receipt: Mapping[str, Any]
    replayed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool) or not self.accepted:
            raise ValueError("terminal completion was not accepted")
        if self.outcome not in {"succeeded", "partial"}:
            raise ValueError("terminal completion outcome is invalid")
        if not self.bundle_id.strip() or not isinstance(self.replayed, bool):
            raise ValueError("terminal completion identity is invalid")
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if not isinstance(self.committed_receipt, Mapping):
            raise TypeError("terminal completion requires the committed receipt")


class TerminalPort(Protocol):
    """One call performs Asset, Candidate and terminal authority mutation."""

    def complete(self, command: TerminalCommand) -> TerminalCompletion: ...
