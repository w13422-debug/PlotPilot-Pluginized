"""Deterministic contradiction probes for Candidate-only quality output."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .rules import Finding, finding_from_span

_FACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SEVERITIES = frozenset({"info", "warning", "error"})


class ConsistencyError(ValueError):
    """Raised before malformed consistency data can produce a finding."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConsistencyError(f"{field} must be a non-empty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ConsistencyError(f"{field} must be strict UTF-8") from exc
    return value


@dataclass(frozen=True, slots=True)
class ConsistencyConstraint:
    """One frozen fact and the literal phrases that would contradict it.

    Semantic extraction stays outside this deterministic plugin.  The caller
    supplies reviewed contradictory phrases from an authoritative snapshot;
    this analyzer only produces review-only evidence when a phrase appears.
    """

    fact_id: str
    expected_value: str
    conflicting_phrases: tuple[str, ...]
    severity: str = "error"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.fact_id, str)
            or _FACT_ID_RE.fullmatch(self.fact_id) is None
        ):
            raise ConsistencyError("fact_id must be a PlotPilot identifier")
        _text(self.expected_value, "expected_value")
        if (
            not isinstance(self.conflicting_phrases, tuple)
            or not self.conflicting_phrases
        ):
            raise ConsistencyError(
                "conflicting_phrases must be a non-empty immutable tuple"
            )
        if len(set(self.conflicting_phrases)) != len(self.conflicting_phrases):
            raise ConsistencyError("conflicting_phrases cannot contain duplicates")
        for phrase in self.conflicting_phrases:
            _text(phrase, "conflicting phrase")
        if self.severity not in _SEVERITIES:
            raise ConsistencyError("severity must be a supported Quality severity")


def analyze_consistency(
    content: str, constraints: Iterable[ConsistencyConstraint] = ()
) -> tuple[Finding, ...]:
    """Return deterministic contradiction findings without changing ``content``."""
    _text(content, "content")
    frozen = tuple(constraints)
    if any(not isinstance(item, ConsistencyConstraint) for item in frozen):
        raise ConsistencyError("constraints must contain ConsistencyConstraint values")
    fact_ids = [item.fact_id for item in frozen]
    if len(set(fact_ids)) != len(fact_ids):
        raise ConsistencyError("consistency fact IDs must be unique")

    found: list[Finding] = []
    for constraint in sorted(frozen, key=lambda item: item.fact_id):
        for phrase in sorted(constraint.conflicting_phrases):
            start = 0
            while True:
                index = content.find(phrase, start)
                if index < 0:
                    break
                end = index + len(phrase)
                found.append(
                    finding_from_span(
                        content,
                        rule_id="consistency.conflict",
                        severity=constraint.severity,
                        message=(
                            f"{constraint.fact_id} expects {constraint.expected_value!r}; "
                            f"the frozen contradiction phrase {phrase!r} requires review"
                        ),
                        start=index,
                        end=end,
                    )
                )
                start = end
    return tuple(
        sorted(
            found,
            key=lambda item: (
                item.start,
                item.end,
                item.rule_id,
                item.severity,
                item.message,
            ),
        )
    )


__all__ = ["ConsistencyConstraint", "ConsistencyError", "analyze_consistency"]
