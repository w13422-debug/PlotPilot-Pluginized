# PlotPilot Quality Suite

`com.plotpilot.quality-suite` provides a read-only, deterministic domain
adapter for the existing language-style `Finding` scanner.  The public
builders consume a `FrozenSource` (or an equivalent provenance mapping):

```python
from hashlib import sha256
from plotpilot_quality_suite import build_quality_bundles, freeze_source

body = "首先是震惊，其次是愤怒，最后是释然。"
digest = sha256(body.encode("utf-8")).hexdigest()
source = freeze_source(
    body,
    workspace_id="ws-1",
    source_revision="rev-1",
    asset_id="asset-content-1",
    content_hash=digest,
)
bundles = build_quality_bundles(source)
assert bundles.candidate.status == "proposed"
assert bundles.diagnostic.json_bytes == bundles.diagnostic.to_bytes()
assert bundles.diagnostic.sha256 == bundles.diagnostic.digest
```

Public builders have these signatures:

```text
freeze_source(content, *, workspace_id=None, revision_id=None,
              source_revision=None, asset_id=None, content_hash=None,
              asset_hash=None, asset_sha256=None, asset_provenance=None)
build_diagnostic_bundle(source, findings=None) -> QualityBundle
build_finding_bundle(source, findings=None) -> QualityBundle
build_candidate_bundle(source, findings=None, *, suggestions=None) -> QualityBundle
build_quality_bundles(source, findings=None, *, suggestions=None) -> QualityBundleSet
```

The `quality-bundle/v1` envelope is a **private, in-memory domain projection**;
it is not a published Core contract and must not be sent to `stage_candidate`
or Publication.  It carries `workspace_id`, `revision_id`, `asset_id`,
`content_hash`, and the matching immutable Asset hash.  Diagnostic items use
`diagnostic-item/v1`; Finding items additionally retain deterministic offsets
and excerpts; candidate items are `quality-candidate-item/v1` with
`status="proposed"` and `publication_eligibility="none"`.  Bundle JSON is
canonical UTF-8 (sorted keys, no whitespace or trailing newline), and its
`json_bytes`/`bytes` and `sha256` are derived solely from the frozen input and
findings.

Missing or mismatched workspace, revision, Asset, content hash, Asset hash, BOM,
encoding, or Finding provenance raises `ProvenanceError`/`ValueError`.  The
module has no `publish` callable and does not write a Revision, Asset,
Candidate, Publication, database, or file; a candidate entry is only a review
suggestion.
The plugin remains domain-only and does not claim a `plugin.json`, wheel,
worker, or runtime integration.
