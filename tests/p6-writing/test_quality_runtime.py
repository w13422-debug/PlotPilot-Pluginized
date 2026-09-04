from __future__ import annotations

import ast
import base64
import json
from collections import deque
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

import plotpilot_quality_suite.runtime as quality_runtime_module
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
from plotpilot_quality_suite.runtime import create_worker

from backend.plotpilot_plugin_sdk import (
    canonical_bytes,
    release_id,
    verify_result_bundle,
)
from backend.plotpilot_plugin_sdk.framing import FrameDecoder, encode_frame
from backend.plotpilot_plugin_sdk.rpc import build_meta, build_request
from backend.plotpilot_plugin_sdk.verifier import request_key, snapshot_hash


def source(content: str):
    digest = sha256(content.encode("utf-8")).hexdigest()
    return freeze_source(
        content,
        workspace_id="workspace-1",
        revision_id="revision-1",
        asset_id="asset-source-1",
        content_hash=digest,
    )


QUALITY_PACKAGE_HASH = "a" * 64
QUALITY_RELEASE = release_id("com.plotpilot.quality-suite", "1.0.0", QUALITY_PACKAGE_HASH)


def _refresh_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    snapshot["request_key"] = request_key(snapshot)
    snapshot["snapshot_hash"] = snapshot_hash(snapshot)
    return snapshot


def quality_snapshot(
    content: bytes,
    *,
    document_id: str | None = "document-1",
    package_hash: str = QUALITY_PACKAGE_HASH,
    release: str | None = None,
    parameters: bytes | None = None,
) -> dict[str, object]:
    content_hash = sha256(content).hexdigest()
    assets: list[dict[str, str]] = [
        {"asset_id": "asset-source-1", "sha256": content_hash}
    ]
    parameters_asset_id: str | None = None
    if parameters is not None:
        parameters_asset_id = "asset-parameters-1"
        assets.append(
            {
                "asset_id": parameters_asset_id,
                "sha256": sha256(parameters).hexdigest(),
            }
        )
    snapshot: dict[str, object] = {
        "schema": "run-snapshot/v1",
        "snapshot_id": "snapshot-1",
        "core_contract_version": "1.2.0",
        "workspace_id": "workspace-1",
        "scope": {
            "document_id": document_id,
            "node_id": None,
            "operation": CAPABILITY_ID,
        },
        "input_revisions": [
            {
                "document_id": "document-1",
                "revision_id": "revision-1",
                "content_hash": content_hash,
            }
        ],
        "plan_revision_id": "plan-1",
        "plugin_releases": [
            {
                "plugin_id": "com.plotpilot.quality-suite",
                "release_id": release
                or release_id(
                    "com.plotpilot.quality-suite",
                    "1.0.0",
                    package_hash,
                ),
                "package_hash": package_hash,
                "data_generation_id": None,
            }
        ],
        "plugin_settings_revisions": [],
        "data_bindings": [],
        "skill_releases": [],
        "model_profile_revision_id": None,
        "parameters_asset_id": parameters_asset_id,
        "asset_hashes": sorted(assets, key=lambda item: item["asset_id"]),
        "request_key": "0" * 64,
        "run_intent_id": "intent-1",
        "created_at": "2026-09-04T00:00:00Z",
        "snapshot_hash": "0" * 64,
    }
    return _refresh_snapshot(snapshot)


def _meta(
    context: str,
    *,
    release: str = QUALITY_RELEASE,
    operation_id: str,
    attempt_id: str = "attempt-1",
    lease_epoch: int = 1,
) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "generation_id": "generation-1",
        "plugin_release_id": release,
        "deadline_at": "2026-09-04T12:00:00Z",
        "operation_id": operation_id,
    }
    if context == "attempt":
        kwargs.update(
            {
                "job_id": "job-1",
                "step_id": "step-1",
                "attempt_id": attempt_id,
                "lease_epoch": lease_epoch,
            }
        )
    return build_meta(context, **kwargs)


def _request(
    method: str,
    params: Mapping[str, object],
    meta: Mapping[str, object],
    request_id: str,
) -> dict[str, object]:
    return build_request(method, params, meta, request_id=request_id)


class QualityHostSession:
    """A bounded in-memory Host that responds only to the shared worker ports."""

    def __init__(self, *, assets: Mapping[str, bytes], receipt_id: str) -> None:
        self.assets = dict(assets)
        self.created_assets: dict[str, bytes] = {}
        self.completions: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []
        self._incoming: deque[bytes] = deque()
        self._output_decoder = FrameDecoder()
        self._uploads: dict[str, bytearray] = {}
        self._receipt_id = receipt_id

    def queue(self, message: Mapping[str, object]) -> None:
        self._incoming.append(encode_frame(dict(message)))

    def read1(self, size: int) -> bytes:
        if not self._incoming:
            return b""
        chunk = self._incoming.popleft()
        if len(chunk) <= size:
            return chunk
        self._incoming.appendleft(chunk[size:])
        return chunk[:size]

    def write(self, data: bytes) -> int:
        for message in self._output_decoder.feed(data):
            self.messages.append(message)
            if "method" in message:
                self._respond_to_host_call(message)
        return len(data)

    def flush(self) -> None:
        return None

    def _respond_to_host_call(self, message: Mapping[str, object]) -> None:
        method = message["method"]
        params = message["params"]
        assert isinstance(method, str)
        assert isinstance(params, Mapping)
        if method == "host.asset.read/v1":
            asset_id = params["asset_id"]
            offset = params["offset"]
            length = params["length"]
            assert isinstance(asset_id, str)
            assert isinstance(offset, int)
            assert isinstance(length, int)
            raw = self.assets[asset_id]
            chunk = raw[offset : offset + length]
            next_offset = offset + len(chunk)
            result: dict[str, object] = {
                "base64_chunk": base64.b64encode(chunk).decode("ascii"),
                "next_offset": next_offset if next_offset < len(raw) else None,
                "content_hash": sha256(chunk).hexdigest(),
            }
        elif method == "host.asset.create/v1":
            upload_id = params["upload_id"]
            offset = params["offset"]
            encoded = params["base64_chunk"]
            final = params["final"]
            assert isinstance(upload_id, str)
            assert isinstance(offset, int)
            assert isinstance(encoded, str)
            assert isinstance(final, bool)
            uploaded = self._uploads.setdefault(upload_id, bytearray())
            assert offset == len(uploaded)
            uploaded.extend(base64.b64decode(encoded, validate=True))
            if final:
                raw = bytes(uploaded)
                assert sha256(raw).hexdigest() == params["expected_hash"]
                asset_id = f"asset-output-{len(self.created_assets) + 1}"
                self.assets[asset_id] = raw
                self.created_assets[asset_id] = raw
                result = {
                    "upload_id": upload_id,
                    "accepted_bytes": len(uploaded),
                    "completed": True,
                    "asset_id": asset_id,
                }
            else:
                result = {
                    "upload_id": upload_id,
                    "accepted_bytes": len(uploaded),
                    "completed": False,
                    "asset_id": None,
                }
        elif method == "host.job.complete/v1":
            self.completions.append(dict(params))
            result = {
                "accepted": True,
                "attempt_state": "partial",
                "step_state": "partial",
                "job_state": "partial",
                "provenance_receipt_id": self._receipt_id,
                "job_event_seq": 1,
                "core_event_high_water": 1,
            }
        else:
            raise AssertionError(f"unexpected Host method: {method}")
        request_id = message["id"]
        assert isinstance(request_id, str)
        self._incoming.appendleft(
            encode_frame({"jsonrpc": "2.0", "id": request_id, "result": result})
        )


def _worker_session(
    snapshot: Mapping[str, object],
    *,
    source_content: bytes,
    parameters: bytes | None = None,
    operation_id: str = "worker-run-1",
    attempt_id: str = "attempt-1",
    lease_epoch: int = 1,
) -> tuple[QualityHostSession, dict[str, object]]:
    release = snapshot["plugin_releases"][0]["release_id"]
    assert isinstance(release, str)
    receipt_id = f"receipt-{sha256(attempt_id.encode('utf-8')).hexdigest()[:48]}"
    assets = {
        "asset-run-snapshot-1": canonical_bytes(snapshot),
        "asset-source-1": source_content,
    }
    if parameters is not None:
        assets["asset-parameters-1"] = parameters
    session = QualityHostSession(assets=assets, receipt_id=receipt_id)
    handshake = _request(
        "runtime.handshake",
        {
            "host_protocol": "1",
            "generation_id": "generation-1",
            "plugin_release_id": release,
            "data_generation_id": None,
        },
        _meta("control", release=release, operation_id="handshake-1"),
        "00000000-0000-4000-8000-000000000001",
    )
    start = _request(
        "job.start",
        {
            "capability_id": CAPABILITY_ID,
            "run_snapshot_asset_id": "asset-run-snapshot-1",
            "checkpoint_asset_id": None,
            "secrets": [],
        },
        _meta(
            "attempt",
            release=release,
            operation_id=operation_id,
            attempt_id=attempt_id,
            lease_epoch=lease_epoch,
        ),
        "00000000-0000-4000-8000-000000000002",
    )
    shutdown = _request(
        "runtime.shutdown",
        {"reason": "test", "deadline_at": "2026-09-04T12:00:00Z"},
        _meta("control", release=release, operation_id="shutdown-1"),
        "00000000-0000-4000-8000-000000000003",
    )
    for message in (handshake, start, shutdown):
        session.queue(message)
    create_worker().serve(stdin=session, stdout=session)
    return session, start


def _response_for(
    messages: list[Mapping[str, object]], request: Mapping[str, object]
) -> Mapping[str, object]:
    matches = [message for message in messages if message.get("id") == request["id"]]
    assert len(matches) == 1
    return matches[0]


def _successful_start(
    session: QualityHostSession,
    request: Mapping[str, object],
) -> Mapping[str, object]:
    response = _response_for(session.messages, request)
    assert "error" not in response
    result = response["result"]
    assert isinstance(result, Mapping)
    return result


def _host_methods(session: QualityHostSession) -> list[str]:
    return [
        message["method"]
        for message in session.messages
        if isinstance(message.get("method"), str)
    ]


def test_shared_worker_emits_sdk_verified_partial_candidate_batch_and_completes() -> None:
    content = b'Mara has blue eyes. A danger grows in the hall! "Run!"'
    parameters = canonical_bytes(
        {
            "schema": "quality-review-parameters/v1",
            "consistency": [
                {
                    "fact_id": "hero-eyes",
                    "expected_value": "green eyes",
                    "conflicting_phrases": ["blue eyes"],
                    "severity": "error",
                }
            ],
            "tension": {"minimum_score": 2, "signals": ["danger"]},
            "style_drift": {
                "baseline": "Calm.",
                "max_drift_basis_points": 50,
            },
        }
    )
    snapshot = quality_snapshot(content, parameters=parameters)

    session, start = _worker_session(
        snapshot,
        source_content=content,
        parameters=parameters,
    )

    result = _successful_start(session, start)
    assert result == {
        "accepted": True,
        "worker_run_id": "worker-run-1",
        "provenance_receipt_id": f"receipt-{sha256(b'attempt-1').hexdigest()[:48]}",
        "output_streams": [],
    }
    assert len(session.completions) == 1
    completion = session.completions[0]
    assert completion["outcome"] == "partial"
    assert completion["worker_run_id"] == "worker-run-1"
    assert str(completion["candidate_stage_operation_key"]).startswith(
        "quality-candidate-stage-"
    )
    bundle_asset_id = completion["result_bundle_asset_id"]
    assert isinstance(bundle_asset_id, str)
    bundle = json.loads(session.assets[bundle_asset_id])
    assert isinstance(bundle, dict)
    verify_result_bundle(
        bundle,
        snapshot_workspace_id="workspace-1",
        snapshot_hash_value=snapshot["snapshot_hash"],
    )
    assert bundle["contract_id"] == "candidate-batch/v1"
    assert bundle["bundle_type"] == "candidate_batch"
    assert bundle["partial"] is True
    assert bundle["warnings"] == []
    assert len(bundle["items"]) == 3
    for item in bundle["items"]:
        assert item["schema"] == "candidate-item/v1"
        assert item["item_kind"] == "document"
        assert item["status"] == "partial"
        assert item["mutation"] == {
            "mode": "append_text",
            "payload_schema": "core/document-text/v1",
            "payload_hash": item["mutation"]["payload_hash"],
        }

    methods = _host_methods(session)
    assert methods.count("host.asset.create/v1") == 4
    assert methods.count("host.job.complete/v1") == 1
    assert "host.candidate.stage/v1" not in methods
    assert not any("publish" in method for method in methods)


def test_worker_preserves_failed_domain_details_in_partial_candidate_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_consistency(_source, _request):
        raise RuntimeError("injected consistency failure")

    faulted_runtime = QualityRuntime(analyzers={"consistency": fail_consistency})
    monkeypatch.setattr(
        quality_runtime_module,
        "QualityRuntime",
        lambda: faulted_runtime,
    )
    content = b"The room is quiet."
    snapshot = quality_snapshot(content)

    session, start = _worker_session(snapshot, source_content=content)

    _successful_start(session, start)
    assert len(session.completions) == 1
    bundle_asset_id = session.completions[0]["result_bundle_asset_id"]
    assert isinstance(bundle_asset_id, str)
    bundle = json.loads(session.assets[bundle_asset_id])
    assert isinstance(bundle, dict)
    verify_result_bundle(
        bundle,
        snapshot_workspace_id="workspace-1",
        snapshot_hash_value=snapshot["snapshot_hash"],
    )
    failed_items = [item for item in bundle["items"] if item["status"] == "failed"]
    assert len(failed_items) == 1
    assert failed_items[0]["item_id"].startswith("quality-consistency-")
    assert bundle["partial"] is True
    assert {item["status"] for item in bundle["items"]} == {"failed", "partial"}
    assert bundle["warnings"] == [
        {
            "code": "quality.consistency.failed",
            "message": "RuntimeError: injected consistency failure",
            "details_asset_id": failed_items[0]["payload_asset_id"],
        }
    ]
    report = json.loads(session.assets[failed_items[0]["payload_asset_id"]])
    assert report["domain"] == "consistency"
    assert report["state"] == "failed"
    assert report["failure"] == "RuntimeError: injected consistency failure"
    methods = _host_methods(session)
    assert "host.candidate.stage/v1" not in methods
    assert not any("publish" in method for method in methods)


def test_worker_keeps_an_all_failed_review_outcome_partial_with_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_domain(_source, _request):
        raise RuntimeError("injected domain failure")

    faulted_runtime = QualityRuntime(
        analyzers={
            "consistency": fail_domain,
            "tension": fail_domain,
            "style_drift": fail_domain,
        }
    )
    monkeypatch.setattr(
        quality_runtime_module,
        "QualityRuntime",
        lambda: faulted_runtime,
    )
    content = b"The room is quiet."
    snapshot = quality_snapshot(content)

    session, start = _worker_session(snapshot, source_content=content)

    _successful_start(session, start)
    completion = session.completions[0]
    assert completion["outcome"] == "partial"
    bundle_asset_id = completion["result_bundle_asset_id"]
    assert isinstance(bundle_asset_id, str)
    bundle = json.loads(session.assets[bundle_asset_id])
    assert isinstance(bundle, dict)
    verify_result_bundle(
        bundle,
        snapshot_workspace_id="workspace-1",
        snapshot_hash_value=snapshot["snapshot_hash"],
    )
    statuses = [item["status"] for item in bundle["items"]]
    assert statuses.count("failed") == 3
    assert statuses.count("partial") == 1
    summary_item = next(
        item for item in bundle["items"] if item["item_id"].startswith("quality-summary-")
    )
    summary = json.loads(session.assets[summary_item["payload_asset_id"]])
    assert summary["domain"] == "summary"
    assert summary["failed_domains"] == ["consistency", "tension", "style_drift"]
    assert [warning["details_asset_id"] for warning in bundle["warnings"]] == [
        item["payload_asset_id"]
        for item in bundle["items"]
        if item["status"] == "failed"
    ]


def test_worker_rejects_malformed_document_or_source_binding_before_output() -> None:
    content = b"The room is quiet."
    malformed_snapshot = quality_snapshot(content, document_id=None)
    malformed_session, malformed_start = _worker_session(
        malformed_snapshot,
        source_content=content,
    )
    malformed_response = _response_for(malformed_session.messages, malformed_start)
    assert "error" in malformed_response
    assert malformed_session.created_assets == {}
    assert malformed_session.completions == []
    assert _host_methods(malformed_session) == ["host.asset.read/v1"]

    source_snapshot = quality_snapshot(content)
    source_session, source_start = _worker_session(
        source_snapshot,
        source_content=b"tampered source",
    )
    source_response = _response_for(source_session.messages, source_start)
    assert "error" in source_response
    assert source_session.created_assets == {}
    assert source_session.completions == []
    assert _host_methods(source_session) == [
        "host.asset.read/v1",
        "host.asset.read/v1",
    ]


def test_worker_rejects_snapshot_package_release_drift_before_quality_effect() -> None:
    content = b"The room is quiet."
    snapshot = quality_snapshot(
        content,
        package_hash="b" * 64,
        release=QUALITY_RELEASE,
    )

    session, start = _worker_session(snapshot, source_content=content)

    response = _response_for(session.messages, start)
    assert "error" in response
    assert session.created_assets == {}
    assert session.completions == []
    assert _host_methods(session) == ["host.asset.read/v1"]


def test_candidate_stage_operation_key_is_stable_across_attempts() -> None:
    content = b"The room is quiet."
    snapshot = quality_snapshot(content)

    first_session, first_start = _worker_session(
        snapshot,
        source_content=content,
        operation_id="worker-run-1",
        attempt_id="attempt-1",
        lease_epoch=1,
    )
    second_session, second_start = _worker_session(
        snapshot,
        source_content=content,
        operation_id="worker-run-2",
        attempt_id="attempt-2",
        lease_epoch=2,
    )

    _successful_start(first_session, first_start)
    _successful_start(second_session, second_start)
    first_completion = first_session.completions[0]
    second_completion = second_session.completions[0]
    assert (
        first_completion["candidate_stage_operation_key"]
        == second_completion["candidate_stage_operation_key"]
    )
    assert first_completion["operation_key"] != second_completion["operation_key"]


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
    assert descriptor["input_schema"] == "run-snapshot/v1"
    assert descriptor["output_schema"] == "result-bundle/v1"
    assert descriptor["result_contract"] == "candidate-batch/v1"
    assert descriptor["supports"] == ["run"]
    assert descriptor["deterministic"] is True
    with pytest.raises(QualityRuntimeError, match="release_id"):
        capability_descriptor(release_id="not-a-release")
