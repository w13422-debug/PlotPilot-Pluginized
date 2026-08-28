# NW-P5-PLANNER-RUNTIME-02 source delivery

## Identity and boundary

- Node: `NW-P5-PLANNER-RUNTIME-02`
- Exact base: `8e3d3a7268e0d28559a7212790e3ab9f22c1453a`
- Base tree: `eab64b7d5b1e3ad652aeaf48bd0085d930beca5d`
- Branch: `codex/nw-p5-planner-runtime-02`
- Readiness: `source_ready_integration_deferred`
- Publication authority: Core only; this package has no publish/accept/stage/invoke callable.

## Delivered source-only runtime

`plotpilot_project_planner.runtime` thin-adapts the accepted public SDK rather
than inventing a transport or importing Core internals:

1. `freeze_planner_run` verifies `run-snapshot/v1`, then freezes the exact
   Planner release/package, validated settings revision/schema, designated
   Prompt-as-Skill release, ordered Skill releases, Model Profile revision and
   Plan revision into one deterministic binding fingerprint.
2. `prepare_planner_run` requires exactly `setting`, `bible` and `outline`,
   binds each to a pre-existing Core document/base revision from the snapshot,
   emits deterministic UTF-8/JCS payload bytes, and creates an immutable run
   version. Intentional reruns require a new Attempt, contiguous ordinal and an
   append-only previous-version link; old versions are never modified.
3. `materialize_candidate_batch` binds Host-created Asset IDs, emits the three
   `candidate-item/v1` document replacements in fixed order, attaches explicit
   Core-confirmed parent lineage and Skill chain evidence, and runs the public
   `verify_result_bundle` gate against Workspace and snapshot identity.
4. Legacy domain input validation now rejects bool/float word counts,
   non-string JSON keys and non-finite numbers before identity calculation.

The source contains no plugin manifest, worker Stub, fake authority, database,
active-version pointer, Provider transport or Publication path.

## Reuse decision

- Direct reuse: public SDK `canonical_bytes`, `hash_jcs`, `verify_snapshot` and
  `verify_result_bundle`.
- Thin adaptation: current Project Planner immutable drafts, Prompt/Skill
  frozen release semantics and repository Candidate vocabulary.
- Local tool selector candidates were unrelated; no tool was copied.
- GitHub search was skipped because accepted repository code already supplies
  the exact product-specific canonicalization, snapshot and Candidate rules;
  an external helper would add no reuse value.

Knowledge evidence consulted:

- `kb-novel-agent-na013-skill-runtime-attribution-20260812` (reviewed), SHA-256
  `5acae707a07f457f3b4021a6d503cd2c1baee6376e3cc55af7319a82019cbffd`.
- `kb-novel-agent-na016-workflow-plan-character-skills-20260813` (reviewed),
  SHA-256 `0c5f631bde80e1f457747a92ba8e547288d1004af3be7d2e02764c0d027ea4b7`.
- `kb-plotpilot-pluginized-ppa05-planning-remediation-20260828` (generated),
  SHA-256 `13c4ea62a893ffb9073be2791992001993adf2fd86f4cd7af53548e29151dbde`.

## Stopped composition slice

`coordination/PPA-05/planner-runtime/runtime-composition-contract-delta-v1.json`
records `NW-P5-PLANNER-RUNTIME-CD-001`:

- public SDK has no typed ports equivalent to the accepted Model/Broker/
  Candidate-stage/Job-complete RPC methods;
- the legacy `stage_candidate(operation_key, bundle_id)` signature is not the
  accepted `result_bundle_asset_id + input_snapshot_hash` method;
- Prompt is only indirectly represented as a Skill release in RunSnapshot;
- setting/Bible/outline targets must already exist as Core documents because
  Planner may not create Core truth.

Accordingly this candidate does not claim production execution. Runtime
composition remains deferred to P0/P2/P3 and the accepted Prompt/Skill Runtime.

## Validation

Raw reproducible results are recorded in `validation-evidence.json` and its
`evidence/raw/` command captures. The post-Ruff source state passes 22 focused
Planner Runtime tests, all 41 P5 Planning tests, Ruff and compileall. No final
EXE, installer, GUI, browser or desktop gate was run.
