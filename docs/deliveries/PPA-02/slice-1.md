# PPA-02 dependency-ready slice 1

## Identity

- Base: `42123d1a5126bb2bef31304b0498e2e7def9183e` (`M0-OPEN-R4`)
- Branch: `codex/ppa-02-plugin-platform`
- Design: v1.2 / `e70f450b75cfa753148cf620d13855d0073f7d61294e7599ea9cc98f3e612b7b`

## Delivered

- Offline folder/ZIP package reading, frozen Windows path/casefold rules, manifest/hash/compatibility/type gates and finite archive limits.
- Content-addressed nonreplace package store with `(plugin_id, version) -> package_hash` conflict rejection.
- Install staging snapshots and re-verifies bytes before immutable publication.
- Pure Generation CAS, current/LKG/safe-mode and rollback-once domain rules.
- Immutable Settings draft/receipt/activation binding rules.
- `com.plotpilot.prompt-skill-runtime` deterministic Skill identity, ordered chain/receipt/patch verification, active-version protection, legacy read-only history, and explicit real P1/P3 port gates.

## Real integration gates retained

- P1: immutable Asset read/create and Candidate staging.
- P3: real Job/Attempt start/poll/cancel and durable receipt aggregation.
- No fake Provider, Candidate repository, Job adapter, external schema, network installation, or online pip was added.

## Verification

```text
$env:PYTHONPATH='backend;.'; python -m pytest tests/p2-plugin-platform -q
9 passed in 0.48s
```

P0 contract-corpus materialization gap is recorded in `coordination/PPA-02/P0-GOLDEN-MATERIALIZATION-BLOCKER.md`; P2 did not change P0-owned files.
