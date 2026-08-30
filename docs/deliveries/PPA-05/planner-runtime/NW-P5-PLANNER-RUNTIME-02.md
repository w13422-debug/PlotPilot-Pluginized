# NW-P5-PLANNER-RUNTIME-02 bounded remediation delivery

## Identity and frozen scope

- Node: `NW-P5-PLANNER-RUNTIME-02`
- Remediation parent: `b1d747957ff21223e10fa61eb45de3d1780b5551`
- Branch: `codex/nw-p5-planner-runtime-02`
- Source readiness: `source_ready_integration_deferred`
- Finding Manifest SHA-256:
  `c1fdf581aade8d71f12301a0a0e08454475a80e001a756f092c8bc4e7e49caaa`
- Review result SHA-256:
  `f3ee5a198872cc78d0d0e520413ebee950d11e3fba0c578d850972722579c7c2`
- Remediation scope: only `NW-P5-PLAN-SOL-F-001..008`.

This source task does not claim an acceptance result. Finding disposition is
owned by reviewer `01a04834-0a4e-7f90-8bf1-80774c803c0e`.

## Remediated source-only surface

`plotpilot_project_planner.runtime` now exposes only frozen verification and
deterministic payload-proposal preparation:

1. `freeze_planner_run` requires the fixed
   `planning.project.generate/v1` operation, checks Workspace/global settings
   scope, fixes the source-local Planner Prompt compatibility designation,
   verifies ordered Skill releases and freezes the Planner release, Model
   Profile and Plan identities.
2. The returned run is an internal opaque value. Every public consumer thaws
   and re-runs the accepted `verify_snapshot` path, reconstructs every derived
   field and recomputes the binding fingerprint. Replaced Workspace, snapshot
   hash, operation or fingerprint values fail closed.
3. `planner_prepare_operation_key` and `prepare_planner_run` require exactly
   `setting`, `bible` and `outline`, distinct pre-existing document targets,
   frozen input bases and only snapshot-backed `core.revision` or `core.asset`
   source references.
4. A proposal binds its exact payload to a stable operation key and operation
   payload hash. Exact retries converge to the same proposal identity; reuse
   of the key with changed output fails before an effect.
5. A proposal is not a Core Candidate or durable rerun version. Parent lineage,
   ordinal reruns and previous-version CAS stop until the P1/P3 durable ledger
   and P0 composition are accepted.

## Removed overclaiming surface

The prior public `materialize_candidate_batch`, `PlannerCandidateBatch` and
`SkillChainAttachment` surface was removed. Therefore a caller cannot supply
free-form producer identity, Asset IDs, receipt IDs or Skill-chain strings and
obtain a Result Bundle.

Final materialization requires all of the following in the owning composition:

- the current typed Attempt and exact producer identity;
- authoritative Asset reads and content-hash verification;
- a complete self-hash-valid provenance receipt anchored to the Bundle;
- authoritative ordered Skill-chain results and receipts;
- Candidate parent lookup, operation replay-or-conflict and previous-version
  CAS in the same durable transaction.

P5 does not implement a substitute memory/file ledger, fake authority,
Candidate repository, Core write or Publication path.

## Runtime integration delta

The sole machine-readable runtime gate is:

`coordination/PPA-05/planner-runtime/runtime-integration-delta-v1.json`

It records `production_integration_claimed=false`, the exact five dependencies,
the accepted Host method shapes including `host.capability.cancel/v1`, and the
stop conditions for Asset/receipt/Skill/CAS/terminal composition. The older
under-specified composition Delta artifact was removed to avoid two competing
runtime gates.

The fixed Prompt Skill ID is only a source-local compatibility designation.
RunSnapshot still lacks an accepted explicit Prompt-role field, so production
Prompt verification remains deferred to P0/P2.

## Reuse evidence

- `kb-novel-agent-na013-skill-runtime-attribution-20260812` (`reviewed`),
  SHA-256 `5acae707a07f457f3b4021a6d503cd2c1baee6376e3cc55af7319a82019cbffd`.
- Accepted repository `verify_snapshot`, RFC 8785 hashing, P1 terminal
  producer/receipt/Asset/Skill validation and P3 operation-ledger semantics
  were used as thin-adaptation evidence; no external helper was copied.

## Validation boundary

Raw command evidence is stored under `evidence/raw/` and indexed by
`validation-evidence.json`. The compileall capture records the exact command,
cwd, interpreter, bytecode prefix, stdout, stderr and exit code. No browser,
GUI, Tauri, desktop, EXE, installer or final product build was run.
