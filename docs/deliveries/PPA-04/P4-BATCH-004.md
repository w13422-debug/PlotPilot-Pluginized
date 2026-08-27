# P4 Batch 004 — Typed renderer and Slot-local failure isolation

- Exact parent: `88ae83c08f176446ce5030e28e780851ec85a496`
- Original accepted base: `42123d1a5126bb2bef31304b0498e2e7def9183e`
- Branch: `codex/ppa-04-webui`

## Delivered

- typed renderer classification for every frozen Plugin UI component;
- direct safe rendering for stack, text, input, textarea, button and progress;
- interactions remain disabled unless the Host explicitly marks the validated
  session interactive;
- asset-backed select/table/tabs/diff/tree/graph stay behind the unpublished
  versioned Asset resolver boundary and display no invented product data;
- Candidate preview remains behind a Core-native control boundary and never
  becomes direct Publication;
- `PluginSlotFrame` presents Ready/Running/Disabled/Missing/Error independently;
- Vue render errors are captured by the Slot frame and replace only that Slot
  with an isolated error message, leaving route/navigation ownership untouched.

The P0 binding adjudication wait is recorded in P4-INT-004. No change was made
to `frontend/src/contracts`, routers, root manifests, locks, or Core APIs.

## Targeted verification

```text
node --experimental-strip-types --test tests/p4-webui/*.test.mts
tests 13; pass 13; fail 0

frontend/node_modules/.bin/vue-tsc.cmd --noEmit -p frontend/tsconfig.app.json
exit 0
```

Changed paths are confined to `frontend/src/plugin-host/**`,
`frontend/src/components/plugin-host/**`, `tests/p4-webui/**`,
`docs/deliveries/PPA-04/**`, and `coordination/PPA-04/**`.

