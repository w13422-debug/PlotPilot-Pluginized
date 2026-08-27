# P4 Batch 001 — Fixed task drawer shell and state presentation

- Exact base: `42123d1a5126bb2bef31304b0498e2e7def9183e`
- Branch: `codex/ppa-04-webui`
- Scope: Home/Workbench fixed task drawer shell, contract-faithful job presentation, explicit
  Ready/Running/Disabled/Missing/Error capability presentation.

## Contract consumption

- `contracts/examples/fixtures/job-snapshot.json`
- `contracts/json-schema/job-snapshot-v1.schema.json`
- Frozen Job transitions from design v1.2 §84.12

No P0 contract, root dependency, lock, API, store, router, or donor file is
changed. The drawer renders an honest Ready/empty state until P3/P0 publish real
Job HTTP/SSE routes; see `coordination/PPA-04/P4-INT-001-job-drawer-api-gate.md`.

## Targeted verification

```text
node --experimental-strip-types --test tests/p4-webui/job-presentation.test.mts
tests 3; pass 3; fail 0

frontend/node_modules/.bin/vue-tsc.cmd --noEmit -p frontend/tsconfig.app.json
exit 0
```

The attempted workspace-mediated `pnpm --dir frontend exec vue-tsc ...` was
stopped by the repository supply-chain policy (`ERR_PNPM_IGNORED_BUILDS`) after
install resolution. Its unintended `pnpm-workspace.yaml` edit was immediately
reverted; direct use of the installed binary then passed with no output.

## Remaining dependencies

- P3/P0 real Job snapshot, SSE recovery and command routes.
- Shared frontend test-runner decision (`P4-DEP-001`).

