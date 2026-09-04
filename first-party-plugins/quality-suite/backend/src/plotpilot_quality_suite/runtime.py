"""Candidate-only runtime for consistency, tension, and style-drift checks."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Any

from .bundles import (
    BundleInput,
    CandidateOnlyBundle,
    FrozenSource,
    ProvenanceError,
    QualityCandidateDomain,
    build_candidate_only_bundle,
    build_quality_candidate_batch,
    canonical_json_bytes,
    coerce_quality_source,
    freeze_source,
)
from .consistency import ConsistencyConstraint, analyze_consistency
from .rules import Finding, ordered_findings, scan_language_style
from .style_drift import StyleDriftPolicy, analyze_style_drift
from .tension import TensionPolicy, analyze_tension

try:  # The packaged worker exposes the SDK at top level; repository tests use backend.
    from backend.plotpilot_plugin_sdk.stdio_worker import (
        AttemptIdentity,
        FramedStdioWorker,
        WorkerContext,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised by the installed plugin path
    from plotpilot_plugin_sdk.stdio_worker import (
        AttemptIdentity,
        FramedStdioWorker,
        WorkerContext,
    )

PLUGIN_ID = "com.plotpilot.quality-suite"
CAPABILITY_ID = "quality.review/v1"
RESULT_CONTRACT = "candidate-batch/v1"
DOMAIN_ORDER = ("consistency", "tension", "style_drift")
_RELEASE_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PARAMETERS_SCHEMA = "quality-review-parameters/v1"
_PLUGIN_VERSION = "1.0.0"


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


@dataclass(frozen=True, slots=True)
class QualityRunBinding:
    """All Host-authoritative identity required before a Quality side effect."""

    identity: AttemptIdentity
    document_id: str
    revision_id: str
    content_hash: str
    source_asset_id: str
    parameters_asset_id: str | None
    parameters_asset_hash: str | None
    worker_run_id: str
    provenance_receipt_id: str
    candidate_stage_operation_key: str
    terminal_operation_key: str
    domain_asset_operation_keys: tuple[tuple[str, str], ...]
    bundle_asset_operation_key: str

    def domain_asset_operation_key(self, domain: str) -> str:
        for candidate_domain, operation_key in self.domain_asset_operation_keys:
            if candidate_domain == domain:
                return operation_key
        raise QualityRuntimeError("Quality domain has no bound Asset operation key")


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


def _require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise QualityRuntimeError(f"{label} is not a canonical identifier")
    return value


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _RELEASE_RE.fullmatch(value) is None:
        raise QualityRuntimeError(f"{label} is not a lowercase SHA-256")
    return value


def _stable_operation_key(prefix: str, value: Mapping[str, object]) -> str:
    """Derive a bounded idempotency key from already-bound immutable identity."""

    return f"{prefix}-{sha256(canonical_json_bytes(value)).hexdigest()[:48]}"


def _validate_candidate_stage(
    response: Mapping[str, object],
    items: list[dict[str, object]],
) -> None:
    """Require the exact Core Candidate mapping before terminal completion."""

    if response.get("accepted") is not True:
        raise QualityRuntimeError("Host rejected Quality Candidate staging")
    staged = response.get("staged_items")
    if not isinstance(staged, list) or len(staged) != len(items):
        raise QualityRuntimeError("Host staged Quality Candidate mapping is incomplete")
    if [entry.get("item_id") if isinstance(entry, Mapping) else None for entry in staged] != [
        item["item_id"] for item in items
    ]:
        raise QualityRuntimeError("Host staged Quality Candidate identity drifted")

    for item, entry in zip(items, staged, strict=True):
        if not isinstance(entry, Mapping):
            raise QualityRuntimeError("Host staged Quality Candidate entry is invalid")
        status = item["status"]
        if status in {"failed", "skipped"}:
            if (
                entry.get("candidate_id") is not None
                or entry.get("stage_status") != status
                or entry.get("publication_eligibility") != "none"
            ):
                raise QualityRuntimeError(
                    "Host staged unusable Quality item with conflicting authority"
                )
            continue
        _require_identifier(entry.get("candidate_id"), "staged candidate_id")
        if entry.get("stage_status") not in {"created", "existing"}:
            raise QualityRuntimeError("Host staged Quality Candidate status drifted")
        expected_eligibility = "eligible" if status == "complete" else "review_only"
        if entry.get("publication_eligibility") != expected_eligibility:
            raise QualityRuntimeError(
                "Host staged Quality Candidate eligibility drifted"
            )
    job_event_seq = response.get("job_event_seq")
    if type(job_event_seq) is not int or job_event_seq < 0:
        raise QualityRuntimeError("Host staged Quality Candidate event sequence is invalid")


def _default_receipt_id(attempt_id: str) -> str:
    """Match Core's default preallocated-receipt derivation for an Attempt."""

    return f"receipt-{sha256(attempt_id.encode('utf-8')).hexdigest()[:48]}"


def _matching_snapshot_asset(
    assets: object,
    *,
    asset_id: str | None = None,
    content_hash: str | None = None,
) -> tuple[str, str]:
    if not isinstance(assets, list):
        raise QualityRuntimeError("RunSnapshot asset_hashes is malformed")
    matches: list[Mapping[str, object]] = []
    for item in assets:
        if not isinstance(item, Mapping):
            raise QualityRuntimeError("RunSnapshot asset_hashes contains a non-object")
        if asset_id is not None and item.get("asset_id") != asset_id:
            continue
        if content_hash is not None and item.get("sha256") != content_hash:
            continue
        matches.append(item)
    if len(matches) != 1:
        raise QualityRuntimeError(
            "RunSnapshot does not bind exactly one required Quality Asset"
        )
    selected = matches[0]
    return (
        _require_identifier(selected.get("asset_id"), "RunSnapshot Asset ID"),
        _require_hash(selected.get("sha256"), "RunSnapshot Asset hash"),
    )


def _bind_quality_run(context: WorkerContext) -> QualityRunBinding:
    """Validate all identity and source selectors before any domain Asset I/O."""

    identity = context.identity
    snapshot = context.run_snapshot
    if identity is None or not isinstance(snapshot, Mapping):
        raise QualityRuntimeError("Quality worker received no bound Attempt RunSnapshot")
    if identity.capability_id != CAPABILITY_ID:
        raise QualityRuntimeError("Quality worker capability identity drifted")

    workspace_id = _require_identifier(
        snapshot.get("workspace_id"),
        "RunSnapshot workspace_id",
    )
    snapshot_id = _require_identifier(
        snapshot.get("snapshot_id"),
        "RunSnapshot snapshot_id",
    )
    snapshot_hash = _require_hash(
        snapshot.get("snapshot_hash"),
        "RunSnapshot snapshot_hash",
    )
    if (
        identity.workspace_id != workspace_id
        or identity.run_snapshot_id != snapshot_id
        or identity.run_snapshot_hash != snapshot_hash
    ):
        raise QualityRuntimeError("Quality Attempt and RunSnapshot identity drifted")

    releases = snapshot.get("plugin_releases")
    if not isinstance(releases, list):
        raise QualityRuntimeError("RunSnapshot plugin_releases is malformed")
    release_matches = [
        item
        for item in releases
        if isinstance(item, Mapping) and item.get("plugin_id") == PLUGIN_ID
    ]
    if len(release_matches) != 1:
        raise QualityRuntimeError(
            "RunSnapshot must bind exactly one Quality plugin release"
        )
    release = release_matches[0]
    release_id = _require_hash(
        release.get("release_id"),
        "RunSnapshot Quality release_id",
    )
    package_hash = _require_hash(
        release.get("package_hash"),
        "RunSnapshot Quality package_hash",
    )
    data_generation_id = identity.data_generation_id
    if data_generation_id is not None:
        data_generation_id = _require_identifier(
            data_generation_id,
            "Attempt data_generation_id",
        )
    if (
        identity.plugin_release_id != release_id
        or identity.package_hash != package_hash
        or release.get("data_generation_id") != data_generation_id
    ):
        raise QualityRuntimeError("Quality package/release/data-generation drifted")

    scope = snapshot.get("scope")
    if not isinstance(scope, Mapping):
        raise QualityRuntimeError("RunSnapshot scope is malformed")
    if scope.get("operation") != CAPABILITY_ID:
        raise QualityRuntimeError("RunSnapshot operation is not Quality review")
    document_id = _require_identifier(
        scope.get("document_id"),
        "RunSnapshot Quality document_id",
    )

    revisions = snapshot.get("input_revisions")
    if not isinstance(revisions, list):
        raise QualityRuntimeError("RunSnapshot input_revisions is malformed")
    matches = [
        item
        for item in revisions
        if isinstance(item, Mapping) and item.get("document_id") == document_id
    ]
    if len(matches) != 1:
        raise QualityRuntimeError(
            "RunSnapshot must bind exactly one revision for the Quality document"
        )
    revision = matches[0]
    revision_id = _require_identifier(
        revision.get("revision_id"),
        "RunSnapshot input revision_id",
    )
    content_hash = _require_hash(
        revision.get("content_hash"),
        "RunSnapshot input content_hash",
    )
    source_asset_id, source_asset_hash = _matching_snapshot_asset(
        snapshot.get("asset_hashes"),
        content_hash=content_hash,
    )
    if source_asset_hash != content_hash:
        raise QualityRuntimeError("Quality source Asset hash drifted")

    parameters_asset_id = snapshot.get("parameters_asset_id")
    parameters_asset_hash: str | None = None
    if parameters_asset_id is not None:
        parameters_asset_id = _require_identifier(
            parameters_asset_id,
            "RunSnapshot parameters_asset_id",
        )
        _, parameters_asset_hash = _matching_snapshot_asset(
            snapshot.get("asset_hashes"),
            asset_id=parameters_asset_id,
        )

    worker_run_id = _require_identifier(
        identity.operation_id,
        "Attempt operation_id",
    )
    stable_identity: dict[str, object] = {
        "schema": "quality-review-operation/v1",
        "plugin_id": PLUGIN_ID,
        "generation_id": _require_identifier(
            identity.generation_id,
            "Attempt generation_id",
        ),
        "release_id": _require_hash(
            identity.plugin_release_id,
            "Attempt plugin_release_id",
        ),
        "data_generation_id": data_generation_id,
        "package_hash": _require_hash(identity.package_hash, "Attempt package_hash"),
        "workspace_id": workspace_id,
        "job_id": _require_identifier(identity.job_id, "Attempt job_id"),
        "step_id": _require_identifier(identity.step_id, "Attempt step_id"),
        "capability_id": CAPABILITY_ID,
        "run_snapshot_asset_id": _require_identifier(
            identity.run_snapshot_asset_id,
            "Attempt run_snapshot_asset_id",
        ),
        "run_snapshot_id": snapshot_id,
        "run_snapshot_hash": snapshot_hash,
        "document_id": document_id,
        "revision_id": revision_id,
        "content_hash": content_hash,
        "source_asset_id": source_asset_id,
    }
    attempt_identity = {
        **stable_identity,
        "attempt_id": _require_identifier(identity.attempt_id, "Attempt attempt_id"),
        "lease_epoch": identity.lease_epoch,
        "operation_id": worker_run_id,
    }
    if type(identity.lease_epoch) is not int or identity.lease_epoch < 1:
        raise QualityRuntimeError("Attempt lease_epoch is invalid")

    candidate_stage_operation_key = _stable_operation_key(
        "quality-candidate-stage",
        stable_identity,
    )
    domain_asset_operation_keys = tuple(
        (
            domain,
            _stable_operation_key(
                "quality-domain-output",
                {**attempt_identity, "domain": domain},
            ),
        )
        for domain in (*DOMAIN_ORDER, "summary")
    )
    return QualityRunBinding(
        identity=identity,
        document_id=document_id,
        revision_id=revision_id,
        content_hash=content_hash,
        source_asset_id=source_asset_id,
        parameters_asset_id=parameters_asset_id,
        parameters_asset_hash=parameters_asset_hash,
        # The shared worker authenticated this Host meta value.  P2 binds its
        # lifecycle identity to the operation value that starts this Attempt.
        worker_run_id=worker_run_id,
        provenance_receipt_id=_default_receipt_id(identity.attempt_id),
        candidate_stage_operation_key=candidate_stage_operation_key,
        terminal_operation_key=_stable_operation_key(
            "quality-terminal",
            attempt_identity,
        ),
        domain_asset_operation_keys=domain_asset_operation_keys,
        bundle_asset_operation_key=_stable_operation_key(
            "quality-result-bundle",
            attempt_identity,
        ),
    )


def _decode_parameters(raw: bytes) -> Mapping[str, object]:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise QualityRuntimeError("Quality parameters cannot have a UTF-8 BOM")
    try:
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualityRuntimeError("Quality parameters must be UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise QualityRuntimeError("Quality parameters must be an object")
    try:
        canonical_json_bytes(value)
    except ValueError as exc:
        raise QualityRuntimeError("Quality parameters are not deterministic JSON") from exc
    allowed = {"schema", "consistency", "tension", "style_drift"}
    if set(value) - allowed:
        raise QualityRuntimeError("Quality parameters contain unsupported fields")
    schema = value.get("schema")
    if schema is not None and schema != _PARAMETERS_SCHEMA:
        raise QualityRuntimeError("Quality parameters schema is unsupported")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) for item in value
    ):
        raise QualityRuntimeError(f"{label} must be a non-empty string array")
    return tuple(value)


def _quality_request_from_parameters(
    source: FrozenSource,
    parameters: Mapping[str, object],
) -> QualityRequest:
    consistency_raw = parameters.get("consistency", [])
    if not isinstance(consistency_raw, list):
        raise QualityRuntimeError("Quality consistency parameters must be an array")
    constraints: list[ConsistencyConstraint] = []
    for item in consistency_raw:
        if not isinstance(item, Mapping):
            raise QualityRuntimeError("Quality consistency item must be an object")
        required = {"fact_id", "expected_value", "conflicting_phrases"}
        allowed = {*required, "severity"}
        if not required.issubset(item) or set(item) - allowed:
            raise QualityRuntimeError("Quality consistency item fields are invalid")
        constraints.append(
            ConsistencyConstraint(
                fact_id=str(item["fact_id"]),
                expected_value=str(item["expected_value"]),
                conflicting_phrases=_string_tuple(
                    item["conflicting_phrases"],
                    "Quality conflicting_phrases",
                ),
                severity=str(item.get("severity", "error")),
            )
        )

    tension_raw = parameters.get("tension")
    tension: TensionPolicy | None = None
    if tension_raw is not None:
        if not isinstance(tension_raw, Mapping) or set(tension_raw) != {
            "minimum_score",
            "signals",
        }:
            raise QualityRuntimeError("Quality tension parameters are invalid")
        tension = TensionPolicy(
            minimum_score=tension_raw["minimum_score"],
            signals=_string_tuple(tension_raw["signals"], "Quality tension signals"),
        )

    style_raw = parameters.get("style_drift")
    style_drift: StyleDriftPolicy | None = None
    if style_raw is not None:
        if not isinstance(style_raw, Mapping):
            raise QualityRuntimeError("Quality style_drift parameters are invalid")
        allowed = {"baseline", "baseline_text", "max_drift_basis_points"}
        baselines = [field for field in ("baseline", "baseline_text") if field in style_raw]
        if set(style_raw) - allowed or len(baselines) != 1:
            raise QualityRuntimeError("Quality style_drift parameters are invalid")
        style_drift = StyleDriftPolicy.from_text(
            style_raw[baselines[0]],
            max_drift_basis_points=style_raw.get("max_drift_basis_points", 3_500),
        )

    return QualityRequest(
        source=source,
        consistency=tuple(constraints),
        tension=tension,
        style_drift=style_drift,
    )


def _load_quality_request(
    context: WorkerContext,
    binding: QualityRunBinding,
) -> QualityRequest:
    source_asset = context.assets.read(binding.source_asset_id)
    if (
        source_asset.asset_id != binding.source_asset_id
        or source_asset.sha256 != binding.content_hash
    ):
        raise QualityRuntimeError("Quality source Asset drifted from its RunSnapshot")
    source = freeze_source(
        source_asset.content,
        workspace_id=binding.identity.workspace_id,
        revision_id=binding.revision_id,
        asset_id=source_asset.asset_id,
        content_hash=binding.content_hash,
        asset_hash=source_asset.sha256,
    )
    if binding.parameters_asset_id is None:
        return QualityRequest(source=source)
    parameters_asset = context.assets.read(binding.parameters_asset_id)
    if (
        parameters_asset.asset_id != binding.parameters_asset_id
        or parameters_asset.sha256 != binding.parameters_asset_hash
    ):
        raise QualityRuntimeError(
            "Quality parameters Asset drifted from its RunSnapshot"
        )
    return _quality_request_from_parameters(
        source,
        _decode_parameters(parameters_asset.content),
    )


def _finding_payload(finding: Finding) -> dict[str, object]:
    return {
        "rule_id": finding.rule_id,
        "severity": finding.severity,
        "message": finding.message,
        "start": finding.start,
        "end": finding.end,
        "excerpt": finding.excerpt,
        "source_hash": finding.source_hash,
    }


def _domain_report_bytes(
    binding: QualityRunBinding,
    source: FrozenSource,
    outcome: QualityDomainResult,
) -> bytes:
    return canonical_json_bytes(
        {
            "schema": "quality-review-report/v1",
            "domain": outcome.domain,
            "state": outcome.state.value,
            "source": {
                "workspace_id": source.workspace_id,
                "document_id": binding.document_id,
                "revision_id": source.revision_id,
                "asset_id": source.asset_id,
                "content_hash": source.content_hash,
            },
            "findings": [_finding_payload(item) for item in outcome.findings],
            "failure": outcome.failure,
        }
    )


def _all_failed_summary_bytes(
    binding: QualityRunBinding,
    source: FrozenSource,
    result: QualityRuntimeResult,
) -> bytes:
    return canonical_json_bytes(
        {
            "schema": "quality-review-report/v1",
            "domain": "summary",
            "state": "partial",
            "source": {
                "workspace_id": source.workspace_id,
                "document_id": binding.document_id,
                "revision_id": source.revision_id,
                "asset_id": source.asset_id,
                "content_hash": source.content_hash,
            },
            "findings": [],
            "failure": None,
            "failed_domains": list(result.failed_domains),
        }
    )


def _candidate_producer(binding: QualityRunBinding) -> dict[str, object]:
    identity = binding.identity
    return {
        "plugin_id": PLUGIN_ID,
        "release_id": identity.plugin_release_id,
        "capability_id": identity.capability_id,
        "job_id": identity.job_id,
        "step_id": identity.step_id,
        "attempt_id": identity.attempt_id,
        "lease_epoch": identity.lease_epoch,
    }


def _run_quality_job(
    _params: Mapping[str, Any],
    context: WorkerContext,
) -> Mapping[str, object]:
    """Run all review domains, upload immutable reports, and finish once."""

    binding = _bind_quality_run(context)
    request = _load_quality_request(context, binding)
    result = QualityRuntime().run(request)
    domains: list[QualityCandidateDomain] = []
    for outcome in result.domains:
        report_bytes = _domain_report_bytes(binding, result.source, outcome)
        report_asset = context.assets.create(
            report_bytes,
            mime="application/json",
            operation_key=binding.domain_asset_operation_key(outcome.domain),
        )
        domains.append(
            QualityCandidateDomain(
                domain=outcome.domain,
                status=(
                    "failed"
                    if outcome.state is QualityDomainState.FAILED
                    else "partial"
                ),
                payload_asset_id=report_asset.asset_id,
                payload_hash=report_asset.sha256,
                failure=outcome.failure,
            )
        )
    if all(item.status == "failed" for item in domains):
        summary_bytes = _all_failed_summary_bytes(binding, result.source, result)
        summary_asset = context.assets.create(
            summary_bytes,
            mime="application/json",
            operation_key=binding.domain_asset_operation_key("summary"),
        )
        domains.append(
            QualityCandidateDomain(
                domain="summary",
                status="partial",
                payload_asset_id=summary_asset.asset_id,
                payload_hash=summary_asset.sha256,
            )
        )

    result_bundle = build_quality_candidate_batch(
        result.source,
        document_id=binding.document_id,
        input_snapshot_hash=binding.identity.run_snapshot_hash,
        producer=_candidate_producer(binding),
        provenance_receipt_id=binding.provenance_receipt_id,
        domains=domains,
    )
    bundle_asset = context.assets.create(
        result_bundle.json_bytes,
        mime="application/json",
        operation_key=binding.bundle_asset_operation_key,
    )
    bundle_value = result_bundle.to_dict()
    stage = context.host.call(
        "host.candidate.stage/v1",
        {
            "operation_key": binding.candidate_stage_operation_key,
            "result_bundle_asset_id": bundle_asset.asset_id,
            "input_snapshot_hash": binding.identity.run_snapshot_hash,
        },
    )
    _validate_candidate_stage(stage, bundle_value["items"])
    completion = context.host.call(
        "host.job.complete/v1",
        {
            "operation_key": binding.terminal_operation_key,
            "worker_run_id": binding.worker_run_id,
            "outcome": "partial",
            "result_bundle_asset_id": bundle_asset.asset_id,
            "candidate_stage_operation_key": binding.candidate_stage_operation_key,
            "terminal_detail_asset_id": None,
            "local_seq": 1,
        },
    )
    if (
        completion.get("accepted") is not True
        or completion.get("provenance_receipt_id")
        != binding.provenance_receipt_id
    ):
        raise QualityRuntimeError("Host completion did not preserve Quality provenance")
    return {
        "accepted": True,
        "worker_run_id": binding.worker_run_id,
        "provenance_receipt_id": binding.provenance_receipt_id,
        "output_streams": [],
    }


def capability_descriptor(*, release_id: str) -> dict[str, object]:
    """Describe the deterministic Host-executed Quality Candidate capability."""

    if not isinstance(release_id, str) or _RELEASE_RE.fullmatch(release_id) is None:
        raise QualityRuntimeError("release_id must be a lowercase SHA-256")
    return {
        "schema": "capability-provider/v1",
        "capability_id": CAPABILITY_ID,
        "provider": {"plugin_id": PLUGIN_ID, "release_id": release_id},
        "input_schema": "run-snapshot/v1",
        "output_schema": "result-bundle/v1",
        "result_contract": RESULT_CONTRACT,
        "supports": ["run"],
        "deterministic": True,
        "accepted_data_formats": [],
    }


def create_worker() -> FramedStdioWorker:
    """Register the Quality domain with the shared framed-stdio worker."""

    worker = FramedStdioWorker(
        plugin_id=PLUGIN_ID,
        plugin_version=_PLUGIN_VERSION,
    )
    worker.register_domain(
        CAPABILITY_ID,
        descriptor=lambda release_id: capability_descriptor(release_id=release_id),
        operations=(CAPABILITY_ID,),
        start=_run_quality_job,
    )
    return worker


def main() -> None:
    """Run the shared Host-framed worker selected by the plugin manifest."""

    create_worker().serve()


__all__ = [
    "CAPABILITY_ID",
    "DOMAIN_ORDER",
    "PLUGIN_ID",
    "RESULT_CONTRACT",
    "QualityDomainResult",
    "QualityDomainState",
    "QualityRequest",
    "QualityRunBinding",
    "QualityRuntime",
    "QualityRuntimeError",
    "QualityRuntimeResult",
    "capability_descriptor",
    "create_worker",
    "main",
]
