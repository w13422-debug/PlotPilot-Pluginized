# NW-P5-STORY-STATE-RUNTIME-G3

## Source outcome

Fresh structural Story State generation from exact parent
`8e3d3a7268e0d28559a7212790e3ab9f22c1453a`. This delivery does not use a
rejected Story State commit or worktree and is not a patch on G2.

The source slice is review-ready but remains integration-deferred. It is not a
production executable, installable build, or merge-eligibility decision.

## Structural boundary

- `StatePayload` is the closed `story-state-payload/v1` discriminated union for
  Bible, character, relationship, world, location, organization, rule, item,
  foreshadowing and story evolution.
- `StoryStateRuntime` finishes payload/reference, public ResultBundle,
  provenance and Skill-chain preflight before the sole terminal port call.
- Skill-chain semantics have exactly one authority call:
  `plotpilot_prompt_skill_runtime.verify_chain`. Story State does not parse,
  sort, re-hash or independently validate receipts, indexes, previous hashes,
  anchors, claims, patches, chain hashes or sequential inputs.
- `TerminalCommand` carries an atomic Asset/Bundle/receipt/Candidate plan.
  `TerminalCompletion` is closed and must return exact per-item Candidate
  mappings plus the committed Core receipt; the returned runtime value is that
  committed receipt, including a Core rewrite of `created_at/receipt_hash`.
- `PublishedStateRecord` accepts only a closed Publication, published Candidate,
  current Revision pointer, complete Asset and execution/Publication receipt
  lineage. `rebuild_projection` validates all records before creating a
  disposable projection.
- Retry selection folds continuous terminal history by stable operation/item and
  only schedules a unique latest failure.

## Decisive acceptance probes

- A two-step chain with distinct H0, H1 and H2 passes the public SDK verifier,
  Prompt Runtime and Story Runtime, and invokes terminal completion once.
- Replacing the second receipt input with H0, then recomputing a valid receipt
  hash and public chain hash, still passes the public SDK but is rejected by
  Prompt Runtime and Story Runtime before the terminal port is called.
- Rehashed unrelated `receipt_id`, `job_id` and `attempt_id` substitutions are
  rejected before a Publication anchor exists.
- Mixed success/failure materializes a public-valid partial ResultBundle and
  stages only complete items.

## Integration deferral

The exact terminal and Publication seams, owners, dependencies and stop
conditions are recorded in
`coordination/PPA-05/story-state-runtime/runtime-integration-delta-v1.json`.
No public contract, SDK, Prompt Runtime, Core, HTTP, app entrypoint, root
dependency or lock file is modified by this source delivery.

## Verification

Raw command outputs are committed under
`coordination/PPA-05/story-state-runtime/evidence/raw/`. The final Git identity
is intentionally reported outside this self-contained source commit to avoid a
self-referential evidence hash.
