# PPA-04B — Plugin Host integration handoff

## Candidate identity

- Branch: `codex/ppa-04b-plugin-host`
- Exact base: `1b352be671e70a2ce443b10499060982e93d64b7`
- Remediation parent: `5a85cc4801d245f81a5d3a970ac471cde1bd6895`
- Source candidate head: the single source-only commit produced with this
  handoff; the exact hash is returned with the delivery status
- Donor-local push: `DISABLED`
- Required integration mode: P0-owned `no-ff` merge after Sol review

## Review boundary

This handoff records implementation evidence only. The remediation author has
not closed a Finding or granted merge eligibility. The frozen manifest remains
subject to per-ID re-review by the same reviewer task
`01a0436d-6d77-7a83-8f8b-fe54fb8bcf51` before any P0 integration decision.

## P0 contract use

- `ingestPluginUiTree` → `parsePluginUiTreeV1`
- `ingestPluginUiIntent` → `parsePluginUiIntentV1`
- `ingestPluginUiAck` → `parsePluginUiAckV1`
- `ingestPluginUiMessage(value: unknown)` → published
  `plugin-ui-message-v1.schema.json` plus the three P0 body parsers
- `ValidatedPluginUiMessage` is an internal host-normalized envelope view;
  it is not a public contract or a copied P0 DTO

No Contract Delta was required. No path outside the P4B exclusive write set
was changed.

## Evidence

- Targeted raw output:
  `docs/deliveries/PPA-04/plugin-host/evidence/raw/plugin-host-tests.stdout.txt`
- Existing P4 regression raw output:
  `docs/deliveries/PPA-04/plugin-host/evidence/raw/p4-regression.stdout.txt`
- Direct source compiler raw output:
  `docs/deliveries/PPA-04/plugin-host/evidence/raw/plugin-host-source-tsc.stdout.txt`
- Prescribed Vue compiler attempt:
  `docs/deliveries/PPA-04/plugin-host/evidence/raw/vue-tsc.stdout.txt`
- Adjacent Vue compiler attempt (same current worktree config):
  `docs/deliveries/PPA-04/plugin-host/evidence/raw/vue-tsc-adjacent.stdout.txt`
- Final staged exclusive-write-set audit:
  `docs/deliveries/PPA-04/plugin-host/evidence/raw/out-of-set.stdout.txt`

The source candidate contains implementation and evidence together. Do not
append a separate evidence-only commit.

## Integration prerequisites

1. Confirm the candidate remains based on
   `1b352be671e70a2ce443b10499060982e93d64b7`.
2. Have reviewer task `01a0436d-6d77-7a83-8f8b-fe54fb8bcf51` re-review
   `P4B-SOL-F-001` through `P4B-SOL-F-004` and re-run the applicable gates.
3. If accepted, P0 performs the prescribed `no-ff` merge. Do not force-push,
   move tags, or modify donor-local.
4. Keep Core route publication and parent P4 final UI wiring as explicit
   follow-on dependencies; P4B does not invent either one.
