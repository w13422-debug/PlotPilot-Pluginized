from __future__ import annotations

import ast
from hashlib import sha256
from pathlib import Path

import pytest
from plotpilot_quality_suite import (
    CAPABILITY_ID,
    CandidateOnlyBundle,
    ConsistencyConstraint,
    Finding,
    QualityDomainState,
    QualityRequest,
    QualityRuntime,
    QualityRuntimeError,
    StyleDriftPolicy,
    TensionPolicy,
    capability_descriptor,
    freeze_source,
)


def source(content: str):
    digest = sha256(content.encode("utf-8")).hexdigest()
    return freeze_source(
        content,
        workspace_id="workspace-1",
        revision_id="revision-1",
        asset_id="asset-source-1",
        content_hash=digest,
    )


def test_domains_emit_separately_attributed_candidate_only_findings() -> None:
    frozen = source('Mara has blue eyes. A danger grows in the hall! "Run!"')
    request = QualityRequest(
        source=frozen,
        consistency=(
            ConsistencyConstraint(
                fact_id="hero-eyes",
                expected_value="green eyes",
                conflicting_phrases=("blue eyes",),
            ),
        ),
        tension=TensionPolicy(minimum_score=2, signals=("danger",)),
        style_drift=StyleDriftPolicy.from_text("Calm.", max_drift_basis_points=50),
    )

    result = QualityRuntime().run(request)

    assert tuple(item.domain for item in result.domains) == (
        "consistency",
        "tension",
        "style_drift",
    )
    assert all(item.state is QualityDomainState.CANDIDATE for item in result.domains)
    assert len(result.candidates) == 3
    for item in result.domains:
        assert item.candidate is not None
        assert isinstance(item.candidate, CandidateOnlyBundle)
        assert item.candidate.domain == item.domain
        bundle = item.candidate.bundle
        assert bundle.status == "proposed"
        assert bundle["publication_eligibility"] == "none"
        assert all(
            candidate["publication_eligibility"] == "none" for candidate in bundle.items
        )
        assert all(candidate["action"] == "review" for candidate in bundle.items)


def test_no_finding_input_stays_clean_without_candidate_output() -> None:
    result = QualityRuntime().run(QualityRequest(source=source("The room is quiet.")))

    assert tuple(item.state for item in result.domains) == (
        QualityDomainState.CLEAN,
        QualityDomainState.CLEAN,
        QualityDomainState.CLEAN,
    )
    assert result.candidates == ()
    assert result.failed_domains == ()


def test_malformed_source_and_finding_fail_closed_before_candidate_generation() -> None:
    runtime = QualityRuntime()
    with pytest.raises(QualityRuntimeError, match="source is malformed"):
        runtime.run(QualityRequest(source={"content": "unbound text"}))

    def malformed_finding(_source, _request):
        return (
            Finding(
                rule_id="bad.finding",
                severity="warning",
                message="unbound",
                start=0,
                end=1,
                excerpt="x",
                source_hash="0" * 64,
            ),
        )

    result = QualityRuntime(analyzers={"consistency": malformed_finding}).run(
        QualityRequest(source=source("x"))
    )
    bad = result.domains[0]
    assert bad.state is QualityDomainState.FAILED
    assert bad.findings == ()
    assert bad.candidate is None
    assert bad.failure is not None and "source_hash" in bad.failure
    assert all(item.state is QualityDomainState.CLEAN for item in result.domains[1:])


def test_faulted_domain_is_recoverable_and_other_domains_remain_isolated() -> None:
    def faulting_analyzer(_source, _request):
        raise RuntimeError("injected analyzer fault")

    result = QualityRuntime(analyzers={"consistency": faulting_analyzer}).run(
        QualityRequest(
            source=source("A danger approaches."),
            tension=TensionPolicy(minimum_score=2, signals=("danger",)),
        )
    )

    consistency, tension, style_drift = result.domains
    assert consistency.state is QualityDomainState.FAILED
    assert consistency.candidate is None
    assert consistency.failure == "RuntimeError: injected analyzer fault"
    assert tension.state is QualityDomainState.CANDIDATE
    assert tension.candidate is not None
    assert style_drift.state is QualityDomainState.CLEAN
    assert result.failed_domains == ("consistency",)


def test_runtime_has_no_core_database_or_publication_api() -> None:
    root = (
        Path(__file__).resolve().parents[2]
        / "first-party-plugins"
        / "quality-suite"
        / "backend"
        / "src"
    )
    imported: set[str] = set()
    function_names: set[str] = set()
    for path in sorted((root / "plotpilot_quality_suite").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_names.add(node.name)
    assert not any(
        name == "sqlite3" or name.startswith("sqlalchemy") for name in imported
    )
    assert not any(
        name == "plotpilot_core" or name.startswith("plotpilot_core.")
        for name in imported
    )
    assert "publish" not in function_names
    assert not hasattr(QualityRuntime, "publish")
    assert not hasattr(CandidateOnlyBundle, "publish")


def test_quality_capability_descriptor_is_deterministic_and_candidate_scoped() -> None:
    descriptor = capability_descriptor(release_id="a" * 64)
    assert descriptor["capability_id"] == CAPABILITY_ID == "quality.review/v1"
    assert descriptor["result_contract"] == "candidate-batch/v1"
    assert descriptor["supports"] == ["run"]
    assert descriptor["deterministic"] is True
    with pytest.raises(QualityRuntimeError, match="release_id"):
        capability_descriptor(release_id="not-a-release")
