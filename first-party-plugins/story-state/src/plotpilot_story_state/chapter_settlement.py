"""Publication-bound post-chapter Story-State proposal adapter.

This module consumes the closed request emitted after a *Core publication*.
It only returns Candidate ResultBundles to Chapter Workflow's injected staging
seam.  It cannot publish a Candidate and deliberately owns no persistent
ledger: Core's operation/batch authority remains the replay authority.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

try:
    from plotpilot_plugin_sdk import (
        canonical_bytes,
        hash_jcs,
        parse_json_bytes,
        verify_result_bundle,
    )
except ModuleNotFoundError:  # pragma: no cover - repository-source fallback
    from backend.plotpilot_plugin_sdk import (
        canonical_bytes,
        hash_jcs,
        parse_json_bytes,
        verify_result_bundle,
    )


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_FIELDS = {
    "schema",
    "operation_key",
    "workspace_id",
    "chapter_candidate_id",
    "chapter_publication_id",
    "chapter_document_id",
    "chapter_revision_id",
    "chapter_content_hash",
}


class ChapterSettlementError(ValueError):
    """Raised before Story-State Candidate output is handed to Core."""


@dataclass(frozen=True, slots=True)
class PublishedChapter:
    operation_key: str
    workspace_id: str
    candidate_id: str
    publication_id: str
    document_id: str
    revision_id: str
    content_hash: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PublishedChapter:
        if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
            raise ChapterSettlementError(
                "post-chapter Story-State request must be closed"
            )
        if value.get("schema") != "post-chapter-story-state-request/v1":
            raise ChapterSettlementError(
                "post-chapter Story-State request schema is invalid"
            )
        names = {
            "operation_key": "operation_key",
            "workspace_id": "workspace_id",
            "candidate_id": "chapter_candidate_id",
            "publication_id": "chapter_publication_id",
            "document_id": "chapter_document_id",
            "revision_id": "chapter_revision_id",
        }
        parsed: dict[str, str] = {}
        for target, source in names.items():
            raw = value[source]
            if not isinstance(raw, str) or _ID.fullmatch(raw) is None:
                raise ChapterSettlementError(f"{source} must be a canonical identity")
            parsed[target] = raw
        content_hash = value["chapter_content_hash"]
        if not isinstance(content_hash, str) or _HASH.fullmatch(content_hash) is None:
            raise ChapterSettlementError(
                "chapter_content_hash must be a lowercase SHA-256"
            )
        return cls(content_hash=content_hash, **parsed)

    def to_request(self) -> dict[str, str]:
        return {
            "schema": "post-chapter-story-state-request/v1",
            "operation_key": self.operation_key,
            "workspace_id": self.workspace_id,
            "chapter_candidate_id": self.candidate_id,
            "chapter_publication_id": self.publication_id,
            "chapter_document_id": self.document_id,
            "chapter_revision_id": self.revision_id,
            "chapter_content_hash": self.content_hash,
        }


class ChapterProposalFactory(Protocol):
    """A composed deterministic planner; it has no Core publication port."""

    def __call__(self, request: Mapping[str, str]) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class ChapterSettlementResult:
    chapter: PublishedChapter
    fingerprint: str
    bundles: tuple[Mapping[str, Any], ...]
    replayed: bool


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    # Canonical JSON gives the caller an independent JSON-only value without
    # retaining mutable input aliases from a generator or a Chapter worker.
    parsed = parse_json_bytes(canonical_bytes(dict(value)))
    if not isinstance(parsed, Mapping):  # defensive; canonical input was an object
        raise ChapterSettlementError("Story-State ResultBundle must be an object")
    return MappingProxyType(dict(parsed))


def _validate_bundle(
    bundle: Mapping[str, Any], chapter: PublishedChapter
) -> Mapping[str, Any]:
    try:
        verify_result_bundle(
            bundle,
            snapshot_workspace_id=chapter.workspace_id,
            known_parent_ids={chapter.candidate_id},
        )
    except Exception as exc:  # a proposal must not cross an invalid Candidate batch
        raise ChapterSettlementError(
            f"Story-State proposal ResultBundle is invalid: {exc}"
        ) from exc
    if (
        bundle.get("schema") != "result-bundle/v1"
        or bundle.get("contract_id") != "candidate-batch/v1"
        or bundle.get("bundle_type") != "candidate_batch"
        or bundle.get("partial") is not False
    ):
        raise ChapterSettlementError(
            "Story-State settlement must return a complete Candidate batch"
        )
    items = bundle.get("items")
    if not isinstance(items, list) or not items:
        raise ChapterSettlementError(
            "Story-State settlement requires at least one Candidate item"
        )
    chapter_ref = {
        "workspace_id": chapter.workspace_id,
        "source_type": "core.revision",
        "source_id": chapter.document_id,
        "revision_or_hash": chapter.revision_id,
    }
    for item in items:
        if not isinstance(item, Mapping) or item.get("status") != "complete":
            raise ChapterSettlementError(
                "Story-State settlement may return only complete Candidates"
            )
        target = item.get("target")
        if (
            not isinstance(target, Mapping)
            or target.get("workspace_id") != chapter.workspace_id
        ):
            raise ChapterSettlementError(
                "Story-State Candidate crosses the published chapter Workspace"
            )
        if target.get("entity_kind") == "document":
            raise ChapterSettlementError(
                "Story-State settlement cannot target chapter正文"
            )
        refs = item.get("source_refs")
        if not isinstance(refs, list) or chapter_ref not in refs:
            raise ChapterSettlementError(
                "Story-State Candidate is not source-bound to the published chapter Revision"
            )
    return _frozen_mapping(bundle)


class ChapterSettlement:
    """Idempotent request adapter over an injected Candidate-bundle factory."""

    def __init__(self, proposal_factory: ChapterProposalFactory | None = None) -> None:
        self._proposal_factory = proposal_factory
        self._lock = threading.RLock()
        self._by_operation: dict[str, ChapterSettlementResult] = {}
        self._publication_fingerprints: dict[str, str] = {}
        self._by_publication: dict[str, ChapterSettlementResult] = {}

    def propose_after_chapter(
        self, request: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], ...]:
        return self.settle(request).bundles

    def settle(self, request: Mapping[str, Any]) -> ChapterSettlementResult:
        chapter = PublishedChapter.from_mapping(request)
        canonical_request = chapter.to_request()
        fingerprint = hash_jcs(
            "post-chapter-story-state-settlement/v1", canonical_request
        )
        with self._lock:
            existing = self._by_operation.get(chapter.operation_key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise ChapterSettlementError(
                        "settlement operation_key was reused with a different publication payload"
                    )
                return ChapterSettlementResult(
                    existing.chapter, existing.fingerprint, existing.bundles, True
                )
            previous_fingerprint = self._publication_fingerprints.get(
                chapter.publication_id
            )
            if previous_fingerprint is not None:
                if previous_fingerprint != fingerprint:
                    raise ChapterSettlementError(
                        "chapter publication identity was reused with a different payload"
                    )
                previous = self._by_publication[chapter.publication_id]
                replay = ChapterSettlementResult(
                    chapter, fingerprint, previous.bundles, True
                )
                self._by_operation[chapter.operation_key] = replay
                return replay
            if self._proposal_factory is None:
                raise ChapterSettlementError(
                    "published chapter settlement requires an injected Story-State proposal factory"
                )
            try:
                raw_bundles = tuple(
                    self._proposal_factory(MappingProxyType(canonical_request))
                )
            except ChapterSettlementError:
                raise
            except Exception as exc:
                raise ChapterSettlementError(
                    f"Story-State proposal factory failed: {exc}"
                ) from exc
            bundles = tuple(_validate_bundle(bundle, chapter) for bundle in raw_bundles)
            bundle_ids = [bundle.get("bundle_id") for bundle in bundles]
            item_ids = [
                item.get("item_id") for bundle in bundles for item in bundle["items"]
            ]
            if len(bundle_ids) != len(set(bundle_ids)) or len(item_ids) != len(
                set(item_ids)
            ):
                raise ChapterSettlementError(
                    "Story-State settlement contains duplicate bundle or item identity"
                )
            result = ChapterSettlementResult(chapter, fingerprint, bundles, False)
            self._by_operation[chapter.operation_key] = result
            self._publication_fingerprints[chapter.publication_id] = fingerprint
            self._by_publication[chapter.publication_id] = result
            return result


__all__ = [
    "ChapterProposalFactory",
    "ChapterSettlement",
    "ChapterSettlementError",
    "ChapterSettlementResult",
    "PublishedChapter",
]
