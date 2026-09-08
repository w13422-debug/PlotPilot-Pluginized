"""Bounded deterministic tension-floor checks for Candidate-only output."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .rules import Finding, finding_from_span

DEFAULT_TENSION_SIGNALS = (
    "danger",
    "threat",
    "chase",
    "secret",
    "betrayal",
    "countdown",
    "conflict",
    "dangerous",
)


class TensionError(ValueError):
    """Raised for an invalid deterministic tension policy."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise TensionError(f"{field} must be a non-empty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise TensionError(f"{field} must be strict UTF-8") from exc
    return value


@dataclass(frozen=True, slots=True)
class TensionPolicy:
    """A caller-selected floor over exact, auditable tension signals."""

    minimum_score: int
    signals: tuple[str, ...] = DEFAULT_TENSION_SIGNALS

    def __post_init__(self) -> None:
        if type(self.minimum_score) is not int or not 0 <= self.minimum_score <= 10_000:
            raise TensionError("minimum_score must be a bounded non-negative integer")
        if not isinstance(self.signals, tuple) or not self.signals:
            raise TensionError("signals must be a non-empty immutable tuple")
        if len(set(self.signals)) != len(self.signals):
            raise TensionError("signals cannot contain duplicates")
        for signal in self.signals:
            _text(signal, "tension signal")


def tension_score(
    content: str, signals: Iterable[str] = DEFAULT_TENSION_SIGNALS
) -> int:
    """Count exact signal occurrences; no model call or hidden state is used."""
    _text(content, "content")
    frozen = tuple(signals)
    if not frozen:
        raise TensionError("signals cannot be empty")
    score = 0
    for signal in sorted(frozen):
        _text(signal, "tension signal")
        start = 0
        while True:
            index = content.find(signal, start)
            if index < 0:
                break
            score += 1
            start = index + len(signal)
    return score


def analyze_tension(content: str, policy: TensionPolicy | None) -> tuple[Finding, ...]:
    """Emit one review-only floor finding when configured tension is too low."""
    _text(content, "content")
    if policy is None:
        return ()
    if not isinstance(policy, TensionPolicy):
        raise TensionError("policy must be a TensionPolicy or None")
    if policy.minimum_score == 0:
        return ()
    score = tension_score(content, policy.signals)
    if score >= policy.minimum_score:
        return ()
    return (
        finding_from_span(
            content,
            rule_id="tension.below_floor",
            severity="warning",
            message=(
                f"Tension score {score} is below the configured minimum {policy.minimum_score}; "
                "review the scene before any explicit candidate action."
            ),
            start=0,
            end=len(content),
        ),
    )


__all__ = [
    "DEFAULT_TENSION_SIGNALS",
    "TensionError",
    "TensionPolicy",
    "analyze_tension",
    "tension_score",
]
