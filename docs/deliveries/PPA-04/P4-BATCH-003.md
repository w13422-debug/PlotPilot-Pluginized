# P4 Batch 003 — Plugin UI session and intent gate

- Exact parent: `ff7566c2f6d7d30c07db978ad64311f79d3a9c6b`
- Original accepted base: `42123d1a5126bb2bef31304b0498e2e7def9183e`
- Branch: `codex/ppa-04-webui`

## Delivered

- session identity bound to fixed Slot, Generation, Release, Workspace and Plan;
- tree-before-intent and exact installed `render_seq` gate;
- freshness equality gate;
- monotonic new-intent sequence gate;
- installed-tree action/event gate;
- identical duplicate intent returns the recorded ACK;
- same ID with changed canonical payload is rejected;
- ACK cannot be recorded for a different or undispatched intent.

The implementation consumes P0 `plugin-ui-tree.json` and
`plugin-ui-intent.json` fixtures. It only returns a validated intent to a future
dispatcher. It contains no Core endpoint, request/response guess, Publication
accept operation, Asset fetch, or product mock.

`PPA-01-CD-001` is explicitly tracked in
`coordination/PPA-04/P4-INT-003-core-api-contract-version-gate.md`.

## Targeted verification

```text
node --experimental-strip-types --test tests/p4-webui/*.test.mts
tests 9; pass 9; fail 0

frontend/node_modules/.bin/vue-tsc.cmd --noEmit -p frontend/tsconfig.app.json
exit 0
```

Changed paths are confined to `frontend/src/plugin-host/**`,
`tests/p4-webui/**`, `docs/deliveries/PPA-04/**`, and
`coordination/PPA-04/**`.
