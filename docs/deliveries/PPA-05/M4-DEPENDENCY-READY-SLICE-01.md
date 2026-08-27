# PPA-05 dependency-ready slice 01

## Identity

- Branch: `codex/ppa-05-planning`
- Accepted base: `42123d1a5126bb2bef31304b0498e2e7def9183e` (`M0-OPEN-R4`)
- Public contract set consumed read-only: `contracts/manifest-v1.json`, contract version `1.2.0`
- First-party plugins: `com.plotpilot.project-planner`, `com.plotpilot.story-state`

## Delivered

- M0-schema-valid plugin manifests and stdlib-only source package layouts.
- Immutable Project Planner input and Prompt/Skill/Model/Profile/Plan/Release binding.
- Deterministic candidate payload drafts for Bible, characters, world, items,
  foreshadowing and story evolution. Reruns have isolated identities; explicit
  synthesis parents are preserved.
- Story State published-fact references, explicit complete/failed proposal
  partitioning, and a deterministic disposable graph/index projection.
- Fail-closed validation for blank inputs, unknown state kinds, duplicate
  identities, unsupported generated sections and orphan projection edges.

The delivered types are internal domain values, not a private external schema.
They stop before Candidate materialization and Core publication. No legacy or
Core database write path exists in either plugin.

## Reuse decision

- Reused: M0 plugin manifest and result/candidate contract vocabulary; the
  legacy PlotPilot semantic coverage established by the baseline audit.
- Thin adaptation later: immutable drafts to P1 `candidate-item/v1` and P3
  `result-bundle/v1` through the real SDK ports.
- Not copied: legacy repositories/writers, unrelated local source tools, fixed
  WebUI code, or Novel-Agent M8+ features.
- GitHub search skipped: this slice is frozen product-specific domain logic and
  the current project plus M0 contracts already provide the exact source of
  truth; third-party reuse has no additional benefit.

## Verification

```text
python -m pytest tests/p5-planning -q
collected 5 items
tests\p5-planning\test_planning_slice.py ..... [100%]
5 passed in 0.60s
exit 0

git diff --check
exit 0
```

Coverage includes manifest contract validation, frozen bindings, rerun
isolation, explicit parents, fail-closed unsupported output, partial proposal
partitioning, deterministic projection rebuild and orphan rejection.

## Integration gates

See `coordination/PPA-05/INTEGRATION-GATES.md`. P1 Candidate/Publication, P2
Release/Plan/Skill runtime, P3 Job/Provider/Broker/receipt, and P4 fixed UI/Core
controls are not present at this base. P6 consumes only future P1-published
Story State revisions. No substitute port or stub was added.

## Changed-path proof

Every delivered path is under the P5 exclusive write set:

- `first-party-plugins/project-planner/**`
- `first-party-plugins/story-state/**`
- `tests/p5-planning/**`
- `docs/deliveries/PPA-05/**`
- `coordination/PPA-05/**`

No root manifest/lock, public contract, SDK, Core, Execution, WebUI or other
project path was changed.
