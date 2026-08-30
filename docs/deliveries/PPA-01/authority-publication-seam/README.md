# NW-P1-AUTHORITY-PUBLICATION-SEAM-G2 finding-scoped remediation

## Scope delivered

This direct-child remediation payload is based on reviewed HEAD `6c0f7920d1968ded03d0f6eb704e070ac1345918` and is limited to `NW-P1-APS-SOL-F-004`, `NW-P1-APS-SOL-F-005`, and `NW-P1-APS-SOL-F-006`.

- F-004: Core Authority source-Candidate mutation reuses `PublicationService.verify_candidate_closure`; the full durable parent/source/Asset closure and authoritative base Revision content/hash are validated inside the same Core transaction snapshot before any durable write.
- F-005: the scoped Delta reports only implemented-and-tested behavior, keeps F-004/F-006 and Core HTTP G2 closure dependencies explicit, keeps all stopped capabilities inactive, and leaves `merge_eligible=false`.
- F-006: committed operation receipt replay is consulted before new-operation Candidate fingerprint CAS; overlapping identical operation-key/payload requests converge on the first result, while different payload reuse remains a typed conflict.

No public HTTP/SDK/schema, `app.py`, root manifest/lock, Core HTTP G2, second authority/database/ledger, or final Windows artifact was changed or activated.

## Decisive validation

The current remediation transcript is `evidence/remediation-raw-validation.txt`; its structured index is `evidence/remediation-verification-summary.json`. The older `evidence/raw-validation.txt` and `evidence/verification-summary.json` describe the original G2 source commit and are historical only, not Finding-remediation evidence.

Manifest-required labels:

- `aps-source-candidate-full-closure`: **1 passed in 0.66s**.
- `aps-zero-side-effects-on-source-drift`: **6 passed in 0.78s**.
- `aps-concurrent-identical-operation-replay`: **1 passed in 0.54s**.
- `aps-operation-key-payload-conflict`: **1 passed in 0.64s**.
- `aps-delta-claim-consistency`: **1 passed in 0.49s**.
- `aps-publication-regression`: **32 passed in 1.57s**.
- `aps-p1-regression`: **191 passed in 18.32s**.

Additional gates:

- Ruff on the three affected Python files: **All checks passed**.
- Targeted repository/publication `compileall`: **exit 0**.
- Contract manifest: **deterministic (141 files)**.
- `git diff --check`: **exit 0**; only Git LF-to-CRLF working-copy notices were emitted.

## Finding implementation state

- `NW-P1-APS-SOL-F-004`: `implemented_and_tested_pending_same_reviewer_targeted_closure`.
- `NW-P1-APS-SOL-F-005`: `implemented_and_tested_pending_same_reviewer_targeted_closure`.
- `NW-P1-APS-SOL-F-006`: `implemented_and_tested_pending_same_reviewer_targeted_closure`.

This source task does not mark any of those Findings `CLEARED`, does not perform central acceptance, and does not claim merge eligibility. `scope_deviations=[]` and `skips=[]`.

## Dependencies and stopped capabilities

Same-reviewer targeted closure and controller serialization remain required before Core HTTP G2 can proceed. Stopped/deferred capabilities remain: `publication.node_structure`, `publication.relation_set`, `publication.incomplete_stream`, cascade-dependent Workspace/Node deletion, `revision.content offset>total`, HTTP/app/public-contract/SDK changes, automatic Publication, and any second database/ledger.
