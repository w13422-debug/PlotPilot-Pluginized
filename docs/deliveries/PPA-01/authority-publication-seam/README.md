# NW-P1-AUTHORITY-PUBLICATION-SEAM-G2 delivery

## Scope delivered

- Rebuilt Publication from the accepted H_C authority surface as a complete-preflight then single-transaction flow.
- Traverses the durable Candidate parent graph with `visited`/`active` sets and rejects cycles, missing ancestors, lifecycle-ineligible parents and cross-Workspace ancestry before publication writes.
- Verifies canonical Candidate rows, target/base/write-set/source closure and Asset metadata, byte length, UTF-8 and SHA-256 bindings before opening the publication transaction.
- Decodes durable Candidate/Event/Snapshot/Bundle/Receipt/Outcome records through typed `incomplete_publication` failures and rechecks the immutable Candidate closure under CAS before commit.
- Adds the transport-free Publication and Core Authority application seams, including the sole Core-database 12-command ledger; no HTTP mount, SDK or public contract was changed.

## Decisive validation

Raw command transcripts are recorded in `evidence/raw-validation.txt` and summarized in `evidence/verification-summary.json`.

- Owned authority/publication seam: **22 passed**.
- Existing execution-transaction regression: **78 passed**.
- Complete P1 regression: **180 passed, 1 skipped**.
- Targeted repository/publication `compileall`: **exit 0**.
- Contract manifest check: **deterministic (141 files)**.
- `git diff --check`: **exit 0** (line-ending notices only).

Coverage includes two- and three-node parent cycles, missing grandparent, cross-Workspace parent and ancestor, staged multi-level closure, parent lifecycle, published Receipt/Revision/Event lineage, Asset metadata shape/size/bytes/hash/UTF-8 drift, typed durable decode failures, replay/result drift, and atomic zero-side-effect failure windows.

## Finding disposition

- `NW-P1-APS-SOL-F-001`: source remediation implemented; complete durable parent closure and cycle/cross-Workspace rejection are covered by the owned suite.
- `NW-P1-APS-SOL-F-002`: source remediation implemented; Asset metadata/bytes/hash and decode failures normalize to typed `incomplete_publication`.
- `NW-P1-APS-SOL-F-003`: **CLOSED** and preserved by the replay/result-lineage checks.
- `NW-P1-APS-SOL-F-004`: **CLOSED** and preserved by the source-Candidate Revision binding checks.
- `NW-P1-APS-SOL-F-005`: `implemented_pending_fresh_sol_max_review`; the scoped Delta deliberately does not claim central PASS or merge eligibility.

## Reuse and boundary

The implementation thin-adapts the accepted H_C `CoreAuthorityRepository`, `AssetStore`, Candidate service and execution evidence verifier. No frozen G1 candidate was merged, cherry-picked or copied, and no new dependency or second database was introduced.

The required fresh `gpt-5.6-sol/max` structural review remains outstanding. Stopped/deferred capabilities are `publication.node_structure`, `publication.relation_set`, `publication.incomplete_stream`, cascade-dependent Workspace/Node delete semantics, `revision.content offset>total`, HTTP/app mounting and all public-contract/SDK changes, automatic Publication and any second ledger/database.
