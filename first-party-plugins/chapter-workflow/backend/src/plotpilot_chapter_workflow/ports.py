"""Injection-only P0/P3/P5 seams for Chapter Workflow.

No implementation port imports a Core repository, P5 Story State runtime, or
database handle.  P0 composition supplies adapters after the Wave D source
candidate is accepted.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CoreAuthorityPort(Protocol):
    """Subset of the published SDK CoreAuthorityPort used by this plugin."""

    def create_asset(self, content: bytes, *, mime: str) -> object: ...

    def stage_candidate(self, operation_key: str, bundle_id: str) -> list[str]: ...


@runtime_checkable
class ResultBundlePort(Protocol):
    """P3-owned durable ResultBundle seam; never a second Core authority."""

    def persist_result_bundle(self, bundle: Mapping[str, Any]) -> str: ...


@runtime_checkable
class BrokerPort(Protocol):
    """P3 Broker seam for one raw-generation or one ordered Skill child."""

    def start(self, invocation: object) -> str: ...

    def poll(self, invocation_id: str, after_event_seq: int) -> Sequence[object]: ...

    def pause(self, operation_key: str, invocation_id: str) -> Sequence[object]: ...

    def cancel(self, operation_key: str, invocation_id: str) -> Sequence[object]: ...


@runtime_checkable
class PublicationPort(Protocol):
    """P0 HTTP/SDK adapter for publication-command/v1."""

    def publish(self, command: Mapping[str, Any]) -> Mapping[str, Any]: ...


@runtime_checkable
class StoryStateSettlementPort(Protocol):
    """P5-owned post-chapter proposal seam.

    Returned values are ResultBundles containing Story State *Candidates*;
    this port never mutates Story State and Chapter Workflow never imports P5.
    """

    def propose_after_chapter(self, request: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]: ...


__all__ = [
    "BrokerPort",
    "CoreAuthorityPort",
    "PublicationPort",
    "ResultBundlePort",
    "StoryStateSettlementPort",
]
