"""Deterministic style-signature drift checks for Candidate-only output."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .rules import Finding, finding_from_span

_SENTENCE_END_RE = re.compile(r"[.!?]")


class StyleDriftError(ValueError):
    """Raised when a style baseline or threshold is malformed."""


def _strict_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise StyleDriftError(f"{field} must be a non-empty string")
    if value.startswith("\ufeff"):
        raise StyleDriftError(f"{field} cannot contain a UTF-8 BOM")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise StyleDriftError(f"{field} must be strict UTF-8") from exc
    return value


@dataclass(frozen=True, slots=True)
class StyleSignature:
    """Integer-only signature so comparison remains cross-platform deterministic."""

    character_count: int
    sentence_count: int
    dialogue_marks: int
    emphasis_marks: int

    def __post_init__(self) -> None:
        for value, field in (
            (self.character_count, "character_count"),
            (self.sentence_count, "sentence_count"),
            (self.dialogue_marks, "dialogue_marks"),
            (self.emphasis_marks, "emphasis_marks"),
        ):
            if type(value) is not int or value < 0:
                raise StyleDriftError(f"{field} must be a non-negative integer")
        if self.character_count == 0 or self.sentence_count == 0:
            raise StyleDriftError(
                "a style baseline must contain at least one sentence character"
            )


def style_signature(content: str) -> StyleSignature:
    """Build a bounded, explainable signature from frozen text only."""
    text = _strict_text(content, "content")
    endings = len(_SENTENCE_END_RE.findall(text))
    return StyleSignature(
        character_count=len(text),
        sentence_count=max(1, endings),
        dialogue_marks=text.count('"')
        + text.count("'")
        + text.count("“")
        + text.count("”"),
        emphasis_marks=text.count("!") + text.count("?"),
    )


@dataclass(frozen=True, slots=True)
class StyleDriftPolicy:
    baseline: StyleSignature
    max_drift_basis_points: int = 3_500

    def __post_init__(self) -> None:
        if not isinstance(self.baseline, StyleSignature):
            raise StyleDriftError("baseline must be a StyleSignature")
        if (
            type(self.max_drift_basis_points) is not int
            or not 0 <= self.max_drift_basis_points <= 10_000
        ):
            raise StyleDriftError("max_drift_basis_points must be between 0 and 10000")

    @classmethod
    def from_text(
        cls, baseline: str, *, max_drift_basis_points: int = 3_500
    ) -> StyleDriftPolicy:
        return cls(
            style_signature(baseline), max_drift_basis_points=max_drift_basis_points
        )


def _rate_delta_basis_points(
    candidate_count: int, candidate_total: int, baseline_count: int, baseline_total: int
) -> int:
    return (
        abs(candidate_count * baseline_total - baseline_count * candidate_total)
        * 10_000
        // max(1, baseline_count * candidate_total)
    )


def drift_basis_points(
    candidate: StyleSignature, baseline: StyleSignature
) -> tuple[int, tuple[str, ...]]:
    """Return the largest relative signature delta and every tied metric name."""
    values = {
        "sentence_length": _rate_delta_basis_points(
            candidate.character_count,
            candidate.sentence_count,
            baseline.character_count,
            baseline.sentence_count,
        ),
        "dialogue_rate": _rate_delta_basis_points(
            candidate.dialogue_marks,
            candidate.character_count,
            baseline.dialogue_marks,
            baseline.character_count,
        ),
        "emphasis_rate": _rate_delta_basis_points(
            candidate.emphasis_marks,
            candidate.character_count,
            baseline.emphasis_marks,
            baseline.character_count,
        ),
    }
    score = max(values.values())
    return score, tuple(
        sorted(name for name, value in values.items() if value == score)
    )


def analyze_style_drift(
    content: str, policy: StyleDriftPolicy | None
) -> tuple[Finding, ...]:
    """Emit a single review-only finding when a frozen baseline is exceeded."""
    if policy is None:
        return ()
    if not isinstance(policy, StyleDriftPolicy):
        raise StyleDriftError("policy must be a StyleDriftPolicy or None")
    candidate = style_signature(content)
    score, metrics = drift_basis_points(candidate, policy.baseline)
    if score <= policy.max_drift_basis_points:
        return ()
    return (
        finding_from_span(
            content,
            rule_id="style_drift.profile",
            severity="warning",
            message=(
                f"Style drift {score}bp exceeds the configured {policy.max_drift_basis_points}bp "
                f"threshold for {', '.join(metrics)}; review before any candidate action."
            ),
            start=0,
            end=len(content),
        ),
    )


__all__ = [
    "StyleDriftError",
    "StyleDriftPolicy",
    "StyleSignature",
    "analyze_style_drift",
    "drift_basis_points",
    "style_signature",
]
