# NW-P6-CHAPTER-WORKFLOW-02 source delivery

## Identity and boundary

- Node: `NW-P6-CHAPTER-WORKFLOW-02`
- Exact base: `8e3d3a7268e0d28559a7212790e3ab9f22c1453a`
- Exact base tree: `eab64b7d5b1e3ad652aeaf48bd0085d930beca5d`
- Branch: `codex/nw-p6-chapter-workflow-02`
- Readiness: `source_ready_integration_deferred`
- P0 binding: all durable effects use injected SDK/Broker-shaped ports.  This
  source does not import P5, a Core repository, SQL, or a正文 writer.

This is a Wave D source candidate only.  It does not claim P0 composition,
production integration, reviewer acceptance, or merge eligibility.

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
     workflow, calls an injected P5 proposal seam, and stages only same-Workspace
     non-正文 complete Candidate bundles that cite the published chapter Revision.
   - No Story State or正文 mutation method exists in the plugin.

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

- Chapter Workflow targeted: `33 passed`.
- P6 Writing regression: `80 passed`, four pre-existing PDF font deprecation
  warnings.
- Chapter Workflow `compileall`: exit 0.
- `git diff --check`: exit 0.

No browser, GUI, Tauri, desktop, EXE, installer, merge, tag or push operation was
run.

## Deferred integration seams

See `coordination/PPA-06/chapter-workflow/runtime-integration-delta-v1.json`.
In particular, P3 cancel-CAS versus partial terminal serialization, the P2
bundle-backed Skill evidence adapter, Core HTTP Publication composition, and
P5 Story State Candidate staging remain P0-owned integration work.
