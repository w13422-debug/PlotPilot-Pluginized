"""Candidate-only runtime for consistency, tension, and style-drift checks."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from .bundles import (
    BundleInput,
    CandidateOnlyBundle,
    FrozenSource,
    ProvenanceError,
    build_candidate_only_bundle,
    coerce_quality_source,
)
from .consistency import ConsistencyConstraint, analyze_consistency
from .rules import Finding, ordered_findings, scan_language_style
from .style_drift import StyleDriftPolicy, analyze_style_drift
from .tension import TensionPolicy, analyze_tension

PLUGIN_ID = "com.plotpilot.quality-suite"
CAPABILITY_ID = "quality.review/v1"
RESULT_CONTRACT = "candidate-batch/v1"
DOMAIN_ORDER = ("consistency", "tension", "style_drift")
_RELEASE_RE = re.compile(r"^[0-9a-f]{64}$")


class QualityRuntimeError(ValueError):
    """A malformed quality request/result is rejected before any side effect."""


class QualityDomainState(str, Enum):
    CLEAN = "clean"
    CANDIDATE = "candidate"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class QualityRequest:
    """Frozen read-only inputs to all three quality domains."""

    source: BundleInput
    consistency: tuple[ConsistencyConstraint, ...] = ()
    tension: TensionPolicy | None = None
    style_drift: StyleDriftPolicy | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.consistency, tuple):
            raise QualityRuntimeError(
                "consistency constraints must be an immutable tuple"
            )
        if any(
            not isinstance(item, ConsistencyConstraint) for item in self.consistency
        ):
            raise QualityRuntimeError(
                "consistency constraints must be ConsistencyConstraint values"
            )
        fact_ids = tuple(item.fact_id for item in self.consistency)
        if len(set(fact_ids)) != len(fact_ids):
            raise QualityRuntimeError("consistency fact IDs must be unique")
        if self.tension is not None and not isinstance(self.tension, TensionPolicy):
            raise QualityRuntimeError("tension must be a TensionPolicy or None")
        if self.style_drift is not None and not isinstance(
            self.style_drift, StyleDriftPolicy
        ):
            raise QualityRuntimeError("style_drift must be a StyleDriftPolicy or None")


@dataclass(frozen=True, slots=True)
class QualityDomainResult:
    """One separately attributable outcome with no publication capability."""

    domain: str
    state: QualityDomainState
    findings: tuple[Finding, ...]
    candidate: CandidateOnlyBundle | None
    failure: str | None = None

    def __post_init__(self) -> None:
        if self.domain not in DOMAIN_ORDER:
            raise QualityRuntimeError("quality domain is not supported")
        if not isinstance(self.state, QualityDomainState):
            raise QualityRuntimeError("quality domain state is invalid")
        if not isinstance(self.findings, tuple) or any(
            not isinstance(item, Finding) for item in self.findings
        ):
            raise QualityRuntimeError("quality result findings are invalid")
        if self.state is QualityDomainState.CLEAN:
            if self.findings or self.candidate is not None or self.failure is not None:
                raise QualityRuntimeError(
                    "clean quality result cannot carry a candidate or failure"
                )
        elif self.state is QualityDomainState.CANDIDATE:
            if not self.findings or self.candidate is None or self.failure is not None:
                raise QualityRuntimeError(
                    "candidate result requires findings and a Candidate-only bundle"
                )
            if self.candidate.domain != self.domain:
                raise QualityRuntimeError("Candidate-only bundle domain drifted")
        elif (
            self.findings
            or self.candidate is not None
            or not isinstance(self.failure, str)
            or not self.failure
        ):
            raise QualityRuntimeError(
                "failed quality result must remain a recoverable empty Candidate path"
            )


@dataclass(frozen=True, slots=True)
class QualityRuntimeResult:
    """Pure aggregate; it is deliberately not a Core Candidate or publication command."""

    source: FrozenSource
    domains: tuple[QualityDomainResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source, FrozenSource):
            raise QualityRuntimeError("quality result source must be FrozenSource")
        if (
            not isinstance(self.domains, tuple)
            or tuple(item.domain for item in self.domains) != DOMAIN_ORDER
        ):
            raise QualityRuntimeError(
                "quality results must contain each domain in stable order"
            )

    @property
    def candidates(self) -> tuple[CandidateOnlyBundle, ...]:
        return tuple(
            item.candidate for item in self.domains if item.candidate is not None
        )

    @property
    def failed_domains(self) -> tuple[str, ...]:
        return tuple(
            item.domain
            for item in self.domains
            if item.state is QualityDomainState.FAILED
        )


QualityAnalyzer = Callable[[FrozenSource, QualityRequest], Iterable[Finding]]


def _consistency_analyzer(
    source: FrozenSource, request: QualityRequest
) -> tuple[Finding, ...]:
    return analyze_consistency(source.content, request.consistency)


def _tension_analyzer(
    source: FrozenSource, request: QualityRequest
) -> tuple[Finding, ...]:
    return analyze_tension(source.content, request.tension)


def _style_drift_analyzer(
    source: FrozenSource, request: QualityRequest
) -> tuple[Finding, ...]:
    values = list(scan_language_style(source.content))
    values.extend(analyze_style_drift(source.content, request.style_drift))
    return ordered_findings(values)


def _validate_findings(
    source: FrozenSource, values: tuple[Finding, ...]
) -> tuple[Finding, ...]:
    for finding in values:
        if finding.source_hash != source.content_hash:
            raise QualityRuntimeError(
                "quality finding source_hash does not match the frozen source"
            )
        if type(finding.start) is not int or type(finding.end) is not int:
            raise QualityRuntimeError("quality finding offsets must be integers")
        if (
            finding.start < 0
            or finding.end < finding.start
            or finding.end > len(source.content)
        ):
            raise QualityRuntimeError(
                "quality finding offsets are outside the frozen source"
            )
        if finding.excerpt != source.content[finding.start : finding.end]:
            raise QualityRuntimeError(
                "quality finding excerpt is not bound to the frozen source"
            )
        if finding.severity not in {"info", "warning", "error"}:
            raise QualityRuntimeError("quality finding severity is unsupported")
        if not isinstance(finding.rule_id, str) or not finding.rule_id:
            raise QualityRuntimeError("quality finding rule_id is invalid")
    return ordered_findings(values)


class QualityRuntime:
    """Run the three pure analyzers and return review-only Candidate projections."""

    def __init__(
        self, *, analyzers: Mapping[str, QualityAnalyzer] | None = None
    ) -> None:
        selected: dict[str, QualityAnalyzer] = {
            "consistency": _consistency_analyzer,
            "tension": _tension_analyzer,
            "style_drift": _style_drift_analyzer,
        }
        if analyzers is not None:
            if not isinstance(analyzers, Mapping):
                raise TypeError("analyzers must be a mapping")
            for domain, analyzer in analyzers.items():
                if domain not in selected or not callable(analyzer):
                    raise QualityRuntimeError(
                        "analyzer override must target one known callable domain"
                    )
                selected[domain] = analyzer
        self._analyzers = selected

    def run(self, request: QualityRequest) -> QualityRuntimeResult:
        if not isinstance(request, QualityRequest):
            raise TypeError("request must be a QualityRequest")
        try:
            source = coerce_quality_source(request.source)
        except (ProvenanceError, ValueError, TypeError) as exc:
            raise QualityRuntimeError(f"quality source is malformed: {exc}") from exc

        outcomes: list[QualityDomainResult] = []
        for domain in DOMAIN_ORDER:
            analyzer = self._analyzers[domain]
            try:
                findings = _validate_findings(source, tuple(analyzer(source, request)))
                if not findings:
                    outcome = QualityDomainResult(
                        domain=domain,
                        state=QualityDomainState.CLEAN,
                        findings=(),
                        candidate=None,
                    )
                else:
                    candidate = build_candidate_only_bundle(
                        source, domain=domain, findings=findings
                    )
                    outcome = QualityDomainResult(
                        domain=domain,
                        state=QualityDomainState.CANDIDATE,
                        findings=findings,
                        candidate=candidate,
                    )
            except Exception as exc:  # noqa: BLE001 - analyzer isolation is an intentional fault boundary
                # Every per-domain fault remains recoverable: this pure runtime
                # has not staged, committed, or published any Core state.
                outcome = QualityDomainResult(
                    domain=domain,
                    state=QualityDomainState.FAILED,
                    findings=(),
                    candidate=None,
                    failure=f"{type(exc).__name__}: {exc}",
                )
            outcomes.append(outcome)
        return QualityRuntimeResult(source=source, domains=tuple(outcomes))


def capability_descriptor(*, release_id: str) -> dict[str, object]:
    """Describe the deterministic review capability without a Host write port."""
    if not isinstance(release_id, str) or _RELEASE_RE.fullmatch(release_id) is None:
        raise QualityRuntimeError("release_id must be a lowercase SHA-256")
    return {
        "schema": "capability-provider/v1",
        "capability_id": CAPABILITY_ID,
        "provider": {"plugin_id": PLUGIN_ID, "release_id": release_id},
        "input_schema": "quality-review-request/v1",
        "output_schema": "quality-candidate-only-result/v1",
        "result_contract": RESULT_CONTRACT,
        "supports": ["run"],
        "deterministic": True,
        "accepted_data_formats": [],
    }


def main() -> None:
    """Installable entrypoint; Host composition supplies request transport."""
    raise RuntimeError(
        "QualityRuntime must be constructed by the PlotPilot Host composition"
    )


__all__ = [
    "CAPABILITY_ID",
    "DOMAIN_ORDER",
    "PLUGIN_ID",
    "RESULT_CONTRACT",
    "QualityDomainResult",
    "QualityDomainState",
    "QualityRequest",
    "QualityRuntime",
    "QualityRuntimeError",
    "QualityRuntimeResult",
    "capability_descriptor",
    "main",
]
