# P4-DEP-001 — Frontend test runner integration request

P4 is not allowed to edit root manifests, locks, or shared TypeScript config.
The current frontend has no test script/runner and `frontend/tests/**` is not in
`tsconfig.app.json`. Batch 1 therefore uses Node 24 native `node:test` with
`--experimental-strip-types` from `tests/p4-webui/**`, plus direct `vue-tsc`.

Requested P0 decision: either standardize this zero-dependency command in the
root test matrix, or provide the shared frontend test runner/config. No package
or lock change is required for the current batch.
