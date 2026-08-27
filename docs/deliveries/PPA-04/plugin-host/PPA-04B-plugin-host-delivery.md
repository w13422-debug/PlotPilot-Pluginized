# PPA-04B — Plugin Host first-batch source delivery

- Project: P4B / PPA-04B-Plugin-Host
- Branch: `codex/ppa-04b-plugin-host`
- Exact base: `1b352be671e70a2ce443b10499060982e93d64b7`
- Delivery kind: source candidate (implementation, tests and delivery evidence
  are kept in one source commit; no evidence-only HEAD)
- Donor-local push: disabled

## Implemented slice

1. `wireIngress.ts` now validates the published
   `plugin-ui-message/v1` closed envelope and its eight
   direction/type/body branches.
2. `ingestPluginUiTree`, `ingestPluginUiIntent` and
   `ingestPluginUiAck` delegate to the P0 parsers
   `parsePluginUiTreeV1`, `parsePluginUiIntentV1` and
   `parsePluginUiAckV1`. `ingestPluginUiMessage(value: unknown)`
   returns only the host-normalized `ValidatedPluginUiMessage` view.
3. The existing renderer/session model receives a host-local projection of the
   P0 tree; it does not introduce a second tree, intent or ACK wire DTO.
4. `workerUrl.ts` accepts only the exact root-relative or same-origin route
   `/__plotpilot/plugin-worker/<64hex release_id>/<64hex bundle_hash>/worker.js`.
   Blob/data URLs, cross-origin URLs, query/fragment, encoding, path
   normalization and case/shape deviations are rejected.
5. `workerHost.ts` creates a module Dedicated Worker, sends formal init and
   dispose envelopes, validates `MessageEvent.data` as `unknown` through
   `ingestPluginUiMessage`, fences identity/message sequence/message IDs,
   routes render and intent results, and ignores late callbacks from released
   workers.
6. `watchdog.ts` is an identity-fenced per-Slot timer. Re-arming a Slot
   cannot allow an old timer to terminate its replacement Worker.

## Authority and boundary

- Tree/intent/ACK parsing remains owned by P0
  (`frontend/src/contracts/ingress.ts`); the P4B normalized view is not a
  replacement public contract.
- Intent handling is an injected future dispatcher port. P4B does not execute
  Core mutations, invent HTTP endpoints, or touch shared contracts.
- No route, shell, view, component, store, manifest, lock or desktop artifact
  was changed.

## Actual source/test paths

- `frontend/src/plugin-host/slotHost.ts`
- `frontend/src/plugin-host/uiSession.ts`
- `frontend/src/plugin-host/wireIngress.ts`
- `frontend/src/plugin-host/workerUrl.ts`
- `frontend/src/plugin-host/watchdog.ts`
- `frontend/src/plugin-host/workerHost.ts`
- `tests/p4-webui/plugin-host/wire-ingress.test.mts`
- `tests/p4-webui/plugin-host/worker-host.test.mts`

## Verification evidence

Raw command output is under
`docs/deliveries/PPA-04/plugin-host/evidence/raw/`:

The final staged-path audit is recorded in
`docs/deliveries/PPA-04/plugin-host/evidence/raw/out-of-set.stdout.txt`.

| Check | Result |
|---|---|
| `node --experimental-strip-types --test tests/p4-webui/plugin-host/*.test.mts` | 10 passed, 0 failed |
| `node --experimental-strip-types --test tests/p4-webui/*.test.mts` | 13 passed, 0 failed |
| Direct existing TypeScript 5.9 compiler check of all `frontend/src/plugin-host/*.ts` and imports | exit 0 |
| `git diff --check` | pass before source commit |
| Prescribed `frontend/node_modules/.bin/vue-tsc.cmd --noEmit -p frontend/tsconfig.app.json` | attempted; exit 1 because this worktree has no `frontend/node_modules` |
| Adjacent `vue-tsc` runtime against this worktree (`--incremental false`) | attempted; exit 2 because the current config cannot resolve `@vue/tsconfig/tsconfig.dom.json` or `vite/client` without local dependency resolution |

The direct compiler check used the already-installed adjacent P4 WebUI
TypeScript runtime only; no package or lock file was installed or changed.
The prescribed and adjacent Vue compiler attempts are retained as raw evidence;
the missing local Vue dependency resolution is an integration-environment
dependency, not a source or contract change.

## Remaining dependencies

- P0/Core must provide the immutable same-origin Worker route and response
  policy before product runtime wiring can start a real plugin bundle.
- Parent P4 retains final Slot mounting, fixed navigation and view/component
  wiring.
- Sol review, Finding adjudication and P0 `no-ff` merge eligibility remain
  outside this Luna implementation candidate.
