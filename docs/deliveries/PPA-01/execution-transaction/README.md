# NW-P1-EXECUTION-TX-01 delivery

## Scope delivered

- One P1-owned SQLite transaction now validates the authoritative Attempt fence, canonical RPC operation context, immutable result Bundle, Candidate mapping, provenance receipt, terminal detail Asset, Attempt/Step projection, Job aggregation, Job Event, Core Event(s), outcome row and exact Host operation response.
- `job.state.changed` is always written for the aggregate revision; `job.terminal` is written only when all Job Steps aggregate terminal. Multi-Step completion remains running until the last Step and keeps Job result/receipt anchors unset until terminal.
- Candidate rows are prepared and promoted inside the terminal transaction. Publication remains a separate author action; it atomically writes Revision, receipt, execution binding and `revision.published`, and replay fails closed if the committed lineage is incomplete.
- The P3 Job ledger migration is loaded from the accepted P3 manifest/SQL and verified by SHA-256 before P1 registers it.
- P3B production ports are implemented by `ExecutionAuthority`: authoritative Attempt fencing, durable operation reservation, child record store, and atomic `create_or_recover_child`. One `BEGIN IMMEDIATE` commits child Job/Step/Attempt, reservation child identity, child record and exact invoke response.
- Child Snapshot uses the already accepted `broker-child-snapshot-binding/v1` attestation profile. Immutable Asset publication occurs before SQLite reference publication; rollback may leave only an unreachable content-addressed orphan.

## Decisive evidence

Raw command transcripts are under `evidence/raw/`.

- Owned transaction suite: **47 passed**.
- Existing P3/P3B regression: **39 passed**.
- P1 Core regression including the new suite: **54 passed**.
- Targeted `compileall`: exit 0.
- Final `git diff --check`: recorded separately after the evidence set is staged.

Coverage includes terminal success/partial/failed/cancelled, canonical context and stale fence before replay, byte-exact restart replay, different-payload rejection, multi-Step Job aggregation, every authoritative terminal write failure, receipt/Event/Publication binding rollback, missing committed lineage, reservation/envelope restart, six child factory SQL failure windows, set-once drift, two-connection child creation and committed child replay.

## Reuse decision

The implementation thin-adapts the accepted repository SQLite UoW, P3 Job ledger and P3B Broker contracts. Reviewed local evidence `kb-novel-agent-phase1-chapter-lifecycle-20260811` (SHA-256 `a1615b62add208d6279957de6c2d8de2796204eaf3577a4df0c822ed0ca2267d`) supports one durable authority for Candidate/terminal/event recovery. The local source-tool selector returned no relevant helper; an external dependency or GitHub copy offered no additional reuse benefit.

## Acceptance boundary

This source task does **not** claim central PASS or merge eligibility. It does not add a public contract or perform P0 runtime wiring. The containing source commit must receive the required fresh Sol/max central review.
