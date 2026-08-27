# P2–P6 integration candidate — P2P6-SOL-FINAL-001 evidence remediation — 2026-08-27

Status: **evidence-only remediation candidate prepared; same-Sol targeted re-review required**.

This candidate addresses only `P2P6-SOL-FINAL-001`: P2's central closure is
copied byte-for-byte from the pinned Novel-Agent Git blob into the P0-owned
candidate directory, and the recursive verifier is updated to validate the
candidate locator, committed bytes, SHA-256, and source Git blob. The Finding
is not declared closed by the integration writer.

- Candidate branch: `codex/ppa-00-integration-candidate-r2`
- Required sole parent: `63f8a0e863afe77e70aef3d0f6687039c6cfb7ec`
- Preserved old branch/candidate: `codex/ppa-00-integration` /
  `8646812681e7b9af89a86070a7ec875b603a1b72`
- Central closure: `evidence/closures/P2-central-closure-active-work.json`
- Central closure bytes/SHA-256: `70403` /
  `fd4aeacd934fd0e6e7cfd06fb8c1d8325db6a813b95eed4be81429123c105f84`
- Source Git blob: `87158ac51355e5e072c8d000c6fdd6a2be58d72e`
- Product implementation and five merge commits: unchanged
- Implementation/browser/Node/TS regression suites: not rerun by this
  evidence-only remediation

- Accepted P0 publication head: `745ba10c7c714b29ef43289a9096d2e64b2eb5bd`
- Tested implementation head: `63f8a0e863afe77e70aef3d0f6687039c6cfb7ec`
- Contract manifest SHA-256: `cbe9d02fc42409332151cd7905e387a46537b330b039a2d3d6798f48cbfcc024`
- Matrix SHA-256: `076541384c643d879e8552737f94a9ce2ed9134dba3e0d448f2c4a0b58297656`

## No-ff batches

| Project | Exact source | Merge commit | Parents | Paths |
|---|---|---|---|---:|
| P2 | `5daece82633a700876474b6468f9948f62d466d7` | `e753506c94b5310d0ec3f70c5dbd7a95ca5f761a` | `745ba10c7c714b29ef43289a9096d2e64b2eb5bd`, `5daece82633a700876474b6468f9948f62d466d7` | 20 |
| P3 | `3196aa7cccc9af279107885cc4cea8c26a785020` | `9979fa544932d9c947f8fa9e163c79a1b502c132` | `e753506c94b5310d0ec3f70c5dbd7a95ca5f761a`, `3196aa7cccc9af279107885cc4cea8c26a785020` | 16 |
| P4 | `86208cc2b6bcd421767eea206532f62cdff36800` | `4f49279108122d8a9f2450894aff32f12a4936d6` | `9979fa544932d9c947f8fa9e163c79a1b502c132`, `86208cc2b6bcd421767eea206532f62cdff36800` | 26 |
| P5 | `0d9de43acef1ba4d83ed37dbc0e0b6eb44c1ad15` | `8825337bb25b20a3567184892bb4de80259b7f8e` | `4f49279108122d8a9f2450894aff32f12a4936d6`, `0d9de43acef1ba4d83ed37dbc0e0b6eb44c1ad15` | 8 |
| P6 | `b603b40635f174b984efade18add7afd40ed3db9` | `63f8a0e863afe77e70aef3d0f6687039c6cfb7ec` | `8825337bb25b20a3567184892bb4de80259b7f8e`, `b603b40635f174b984efade18add7afd40ed3db9` | 25 |

Each batch was merged with `--no-ff --no-commit`, tested before commit, and has its own content-addressed receipt/raw output.

## Integrated regression

- Python P1/P2/P3/P5/P6 + public-contract tests: **85 passed**, four retained P6 dependency deprecation warnings.
- P4 Node tests: **13 passed**, Vue TypeScript check: no diagnostics.
- Contract manifest: deterministic, 137 files; Python/Node/cross-language verifiers: exit 0.
- Object/write-set gate: 95-path source union exact, 0 overlaps, 0 out-of-set paths, all five merge parent pairs exact.
- P1 merge appears once; all four forbidden evidence-only commits are absent from the candidate ancestry; M0 tags and donor push remain unchanged.

## Evidence identity note

The P3/P4/P5/P6 source-era submissions predate the current public-contract publication and P2 lacks an exact-source submission file. Their stale head/manifest fields were not rewritten or treated as current proof. The P0 receipts instead bind the exact Git objects, central Sol closure records, current manifest, real source diffs and fresh integration tests. See `candidate-manifest.json` and `receipts/*.json`.

This summary is not an independent review or merge-eligibility decision.

After the single evidence-only candidate commit, run
`python -B coordination/integration-queue/P2-P6-INTEGRATION-CANDIDATE-20260827/evidence/verify-candidate-descendant.py`
to verify its exact parent, P0-only evidence diff, content hashes, clean tree,
candidate-local central closure plus the pinned source Git blob, unchanged
tags/donor and forbidden-evidence exclusion. The integration writer does not
declare `P2P6-SOL-FINAL-001` closed; the original Sol reviewer must perform
the targeted re-review.
