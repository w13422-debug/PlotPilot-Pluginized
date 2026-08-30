# NW-P6-CHAPTER-WORKFLOW-02 source delivery

## Identity and boundary

- Node: `NW-P6-CHAPTER-WORKFLOW-02`
- Exact base: `8e3d3a7268e0d28559a7212790e3ab9f22c1453a`
- Exact base tree: `eab64b7d5b1e3ad652aeaf48bd0085d930beca5d`
- Bounded-remediation parent: `7be5b7397d116ca46aaaf9a385cb3f8068ce82d9`
- Frozen Finding Manifest SHA-256:
  `7824e3d01d3cfcd03ee17078aa2a3cfb86b9f23229650c7fc9b24695c72152f3`
- Branch: `codex/nw-p6-chapter-workflow-02`
- Readiness: `source_ready_integration_deferred`
- P0 binding: all durable effects use injected SDK/Broker-shaped ports.  This
  source does not import P5, a Core repository, SQL, or a正文 writer.

This is a Wave D source candidate only.  It does not claim P0 composition,
production integration, reviewer acceptance, or merge eligibility.  The
bounded remediation recorded below awaits the same Sol reviewer's directed
recheck against the nine existing Finding IDs.

## Implemented source

1. **Frozen outline/context assembly**
   - Exact UTF-8 source text and SHA-256 are frozen before Broker start.
   - Context JSON is deterministic and preserves the caller's exact source
     order, target base, instruction, Skill order, and Rewrite selection.
   - `generate`, `continue`, and `rewrite` bind distinct capability IDs.

2. **Ordered Skill chain**
   - Raw generation runs first; frozen Skills run in strictly ascending author
     order.
   - Every Skill input Asset is exactly the previous phase's acknowledged
     output.
   - A completed Skill phase must return a closed, bundle-backed
     `skill-chain-ref/v1` anchored to the preallocated chapter ResultBundle and
     item; the final Candidate preserves those refs in execution order.

3. **Exact partial Candidate**
   - `pause` and `cancel` use only contiguous Broker ACK bytes and validate
     `event_seq`, `prefix_size`, and SHA-256 before any terminal side effect.
   - Unacknowledged bytes never enter the Candidate.
   - At the pre-Skill boundary, a zero-byte Skill prefix falls back to the last
     completed raw/Skill phase rather than inventing empty replacement text.
   - Plugin partials are ordinary `document + status=partial` review-only
     Candidates.  The plugin never claims Core-owned `incomplete_stream`.

4. **Typed accept and settlement**
   - Only a complete staged chapter Candidate can produce the closed five-field
     `publication-command/v1`.
   - Publication results are re-bound to Candidate, Workspace, target and
     resulting Revision; operation-key replay is idempotent in the workflow.
   - Post-chapter settlement accepts only a Publication result produced by this
     workflow, calls an injected P5 proposal seam, preflights the entire bundle
     set before persistence, and exposes Candidates only through one injected
     atomic batch operation with a durable operation-key/fingerprint receipt.
     Only same-Workspace non-正文 complete Candidate bundles that cite the
     published chapter Revision can enter that batch.
   - No Story State or正文 mutation method exists in the plugin.

## Single bounded remediation

The child source commit containing this record implements directed evidence
for the frozen Finding IDs without changing their scope or editing the frozen
Manifest:

1. **F-001** — `FrozenContextPlan` now owns tuple/member/uniqueness/order and
   recomputed-fingerprint invariants; `start()` revalidates before port calls.
2. **F-002** — complete document output is aggregated before strict UTF-8 and
   non-blank validation; invalid complete/partial text cannot become a
   Candidate or reach Publication.
3. **F-003** — `FINALIZING` retains a retryable transition with confirmed
   Asset/Bundle/stage or Skill-start progress.  Terminal state, phase and Skill
   index become visible only after the corresponding port sequence succeeds.
4. **F-004** — per-session state/finalizer locks and
   `(invocation_id, phase, epoch, last_event_seq)` tokens discard late poll,
   competing control, old Skill and synchronous-reentry responses.
5. **F-005** — Rewrite range length is measured in Unicode code points and a
   closed, read-only composition receipt binds current/base Revision, base
   hash, total length, exact range and selected text/hash before any write-side
   port call.
6. **F-006** — Publication receives an immutable command; its result is checked
   against the original frozen Candidate and target rather than adapter-visible
   command state.
7. **F-007** — Story State bundles are deep-snapshotted, globally preflighted
   and de-duplicated before persistence, then committed by a single all-or-none
   composition-only Candidate batch receipt.  Retry reuses confirmed progress.
8. **F-008** — every persisted Story State Bundle ID must equal its declared ID
   before the atomic batch port is callable.
9. **F-009** — the runtime delta contains the exact five node dependencies and
   distinguishes `contract_owners` from dependency identity.

Machine-readable per-ID paths and commands are recorded in
`coordination/PPA-06/chapter-workflow/finding-closure-evidence-v1.json`.

## Reuse decision

- **Thin adaptation:** retained and strengthened the accepted
  `freeze_context_plan` implementation; followed Export Suite's injection-only
  port pattern and the P0 `result-bundle/v1` / `publication-command-result/v1`
  contracts.
- **Local knowledge:** used reviewed NA-014/NA-015/NA-016 evidence for ACK-only
  partials, author-ordered Skill chains, immutable Candidate authority, and
  typed acceptance.
- **Local tool library:** the bounded selector returned video, hardware and Git
  rescue tools; none implements workflow orchestration or matches the contract
  schemas, so copying one would add dependencies without reuse value.
- **GitHub:** skipped because the repository already contains the authoritative
  contracts, SDK verifiers, Skill runtime and accepted port pattern.  A generic
  external workflow library would be a less compatible second truth source.

## Validation

Raw machine-readable evidence is in
`coordination/PPA-06/chapter-workflow/source-validation-v1.json`.

- Chapter Workflow targeted: `66 passed in 1.12s`.
- P6 Writing regression: `113 passed, 4 warnings in 5.32s`; the warnings are
  four pre-existing PDF font deprecation
  warnings.
- Chapter Workflow `compileall`: exit 0.
- `git diff --check`: exit 0.

No browser, GUI, Tauri, desktop, EXE, installer, merge, tag or push operation was
run.

## Deferred integration seams

See `coordination/PPA-06/chapter-workflow/runtime-integration-delta-v1.json`.
In particular, P3 cancel-CAS versus partial terminal serialization, the P2
bundle-backed Skill evidence adapter, Core HTTP Publication composition, and
P5 Story State proposal remain integration work.  The local
`RewriteSelectionReceiptPort` and `SettlementCandidateBatchPort` are explicit
composition-only seams, not published SDK contracts.  Their real P0/P3
adapters and all five exact dependencies remain deferred:

- `NW-P2-PROMPT-SKILL-RUNTIME-02`
- `NW-P5-STORY-STATE-RUNTIME-03`
- `NW-P3-JOB-RPC-02`
- `NW-P3-EVENT-SSE-03`
- `NW-P0-RUNTIME-COMPOSITION-02`
