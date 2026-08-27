# P4 Batch 002 — Slot Host contract foundation

- Exact parent: `aeb115f84f217f231687d74d6094dfa76949de14`
- Original accepted base: `42123d1a5126bb2bef31304b0498e2e7def9183e`
- Branch: `codex/ppa-04-webui`

Implemented the exact eleven v1 Slot names, thirteen typed component rules,
closed prop/event rejection, fixture-backed tree validation, atomic installed
tree state, and monotonic `render_seq` rejection. It does not claim Worker or
renderer completion; the immutable Worker URL gate is recorded as P4-INT-002.

## Targeted verification

```text
node --experimental-strip-types --test tests/p4-webui/*.test.mts
tests 6; pass 6; fail 0

frontend/node_modules/.bin/vue-tsc.cmd --noEmit -p frontend/tsconfig.app.json
exit 0
```

Changed paths are confined to `frontend/src/plugin-host/**`,
`tests/p4-webui/**`, `docs/deliveries/PPA-04/**`, and
`coordination/PPA-04/**`.
