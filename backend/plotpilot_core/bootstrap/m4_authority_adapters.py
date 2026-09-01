"""Composition root for the P1 Core Candidate/Publication authority slice."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..api.v2.core.candidate_publication import CoreCandidatePublicationAdapter
from ..api.v2.core.router import build_core_router
from ..assets import AssetStore
from ..candidates.application import CandidateApplication
from ..candidates.service import CandidateService
from ..publication.application.service import PublicationApplication
from ..publication.service import PublicationService
from ..repositories.authority import CoreAuthorityRepository
from ..repositories.execution import ExecutionAuthority


@dataclass(frozen=True, slots=True)
class M4AuthorityAdapters:
    """The only composition that joins v2 reads with the Core transaction root."""

    candidates: CandidateApplication
    publication: PublicationApplication
    execution: ExecutionAuthority
    http: CoreCandidatePublicationAdapter

    def router(self) -> Any:
        return build_core_router(self.http)


def build_m4_authority_adapters(
    repository: CoreAuthorityRepository,
    assets: AssetStore,
    *,
    execution_authority: ExecutionAuthority | None = None,
) -> M4AuthorityAdapters:
    """Build adapters without adding a second database, writer, or API v1 path."""
    repository.ensure_chapter_authority_schema()
    execution = execution_authority or ExecutionAuthority(repository, assets)
    candidates = CandidateApplication(
        CandidateService(repository, assets), execution_authority=execution
    )
    publication = PublicationApplication(PublicationService(repository, assets))
    http = CoreCandidatePublicationAdapter(candidates, publication)
    return M4AuthorityAdapters(candidates, publication, execution, http)


__all__ = ["M4AuthorityAdapters", "build_m4_authority_adapters"]
