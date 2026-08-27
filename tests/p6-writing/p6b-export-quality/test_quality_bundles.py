from __future__ import annotations

from dataclasses import FrozenInstanceError
from hashlib import sha256

import pytest

from plotpilot_quality_suite import (
    Finding,
    ProvenanceError,
    build_quality_bundles,
    freeze_source,
    validate_bundle,
)


def _source() -> tuple[str, str, object]:
    body = "首先是震惊，其次是愤怒，最后是释然。"
    digest = sha256(body.encode("utf-8")).hexdigest()
    return body, digest, freeze_source(
        body,
        workspace_id="ws-1",
        source_revision="rev-1",
        asset_id="asset-content-1",
        content_hash=digest,
        asset_provenance={
            "workspace_id": "ws-1",
            "asset_id": "asset-content-1",
            "sha256": digest,
        },
    )


def test_quality_emits_deterministic_diagnostic_finding_and_proposed_candidate_bundles() -> None:
    body, digest, source = _source()
    first = build_quality_bundles(source)
    second = build_quality_bundles(source)

    assert [bundle.json_bytes for bundle in first] == [bundle.json_bytes for bundle in second]
    assert [bundle.sha256 for bundle in first] == [sha256(bundle.json_bytes).hexdigest() for bundle in first]
    assert first.diagnostic.bundle_type == "diagnostic"
    assert first.finding.bundle_type == "finding"
    assert first.candidate.status == "proposed"
    assert first.candidate["publication_eligibility"] == "none"
    assert first.candidate.provenance["content_hash"] == digest
    assert first.diagnostic.items[0]["schema"] == "diagnostic-item/v1"
    assert first.finding.items[0]["excerpt"] in body
    assert not hasattr(first.candidate, "publish")
    for bundle in first:
        validate_bundle(bundle)


def test_quality_source_is_immutable_and_rejects_missing_or_mismatched_provenance() -> None:
    body, digest, source = _source()
    with pytest.raises(FrozenInstanceError):
        source.revision_id = "changed"  # type: ignore[misc]

    with pytest.raises(ProvenanceError, match="content hash"):
        freeze_source(body, workspace_id="ws-1", revision_id="rev-1", asset_id="asset-1", content_hash="0" * 64)
    with pytest.raises(ProvenanceError, match="required"):
        freeze_source(body, revision_id="rev-1", asset_id="asset-1", content_hash=digest)
    with pytest.raises(ProvenanceError, match="valid UTF-8"):
        freeze_source(b"\xff", workspace_id="ws-1", revision_id="rev-1", asset_id="asset-1", content_hash=digest)
    with pytest.raises(ProvenanceError, match="does not match"):
        freeze_source(
            body,
            workspace_id="ws-1",
            revision_id="rev-1",
            asset_id="asset-1",
            content_hash=digest,
            asset_hash="1" * 64,
        )


def test_quality_rejects_bom_invalid_unicode_and_conflicting_asset_metadata() -> None:
    body = "正文"
    digest = sha256(body.encode("utf-8")).hexdigest()

    with pytest.raises(ProvenanceError, match="BOM"):
        freeze_source(
            b"\xef\xbb\xbf" + body.encode("utf-8"),
            workspace_id="ws-1",
            revision_id="rev-1",
            asset_id="asset-1",
            content_hash=sha256(b"\xef\xbb\xbf" + body.encode("utf-8")).hexdigest(),
        )
    with pytest.raises(ProvenanceError, match="BOM"):
        freeze_source(
            "\ufeff" + body,
            workspace_id="ws-1",
            revision_id="rev-1",
            asset_id="asset-1",
            content_hash=digest,
        )
    with pytest.raises(ProvenanceError, match="valid UTF-8"):
        freeze_source(
            "\ud800",
            workspace_id="ws-1",
            revision_id="rev-1",
            asset_id="asset-1",
            content_hash="0" * 64,
        )
    with pytest.raises(ProvenanceError, match="mapping"):
        freeze_source(
            body,
            workspace_id="ws-1",
            revision_id="rev-1",
            asset_id="asset-1",
            content_hash=digest,
            asset_provenance=[],  # type: ignore[arg-type]
        )
    with pytest.raises(ProvenanceError, match="hash fields"):
        freeze_source(
            body,
            workspace_id="ws-1",
            revision_id="rev-1",
            asset_id="asset-1",
            content_hash=digest,
            asset_provenance={"sha256": digest, "asset_hash": "1" * 64},
        )
    with pytest.raises(ProvenanceError, match="content size"):
        freeze_source(
            body,
            workspace_id="ws-1",
            revision_id="rev-1",
            asset_id="asset-1",
            content_hash=digest,
            asset_provenance={"size": 999},
        )


def test_finding_provenance_and_excerpt_are_fenced_to_frozen_content() -> None:
    body, digest, source = _source()
    bad_hash = Finding("quality.rule", "warning", "message", 0, 2, body[:2], "0" * 64)
    with pytest.raises(ProvenanceError, match="source_hash"):
        build_quality_bundles(source, [bad_hash])
    bad_excerpt = Finding("quality.rule", "warning", "message", 0, 2, "错", digest)
    with pytest.raises(ProvenanceError, match="excerpt"):
        build_quality_bundles(source, [bad_excerpt])
