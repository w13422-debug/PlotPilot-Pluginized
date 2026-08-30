# NW-P4-JOB-DRAWER-02 source candidate

## Scope

- Node: `NW-P4-JOB-DRAWER-02`
- Base: `8e3d3a7268e0d28559a7212790e3ab9f22c1453a`
- Baseline tree: `eab64b7d5b1e3ad652aeaf48bd0085d930beca5d`
- Reviewed candidate / remediation parent: `e0c46f94c45dfb0dd41eb418347974a21f1e91ec`
- Reviewed candidate tree: `494b8f8b669ea9ad2d3e5445edb77b961ba252eb`
- Branch: `codex/nw-p4-job-drawer-02`
- Candidate locator: the single enclosing direct-child remediation commit
- Frozen Finding Manifest SHA-256: `6699dfbf837ec42104eb9faae4a337757c6a40ee1daf4fe8d2a3c3b8b49ed8a7`
- Status: bounded remediation candidate for same-reviewer targeted recheck; this document does not claim acceptance or merge eligibility

## Delivered source

- `TaskDrawer.vue` now renders Job, every Step and every Attempt, explicit progress, distinct `partial` and `needs_attention` states, connection/recovery state, action errors and pending retry/resume/cancel intents.
- `core/jobs/store.ts` owns one authoritative in-memory projection. Snapshot application replaces complete nested collections, uses one position/hash classifier for single and workspace paths, and separates the observed event cursor from the Snapshot-covered projection cursor.
- `core/jobs/controller.ts` performs ordered refresh discovery, projection-cursor reconnect, Snapshot convergence pumping, recovery-handle rotation, post-await generation/serial/barrier fencing, tokenized actions, causal post-action reads and signal-stabilized capped backoff.
- `core/jobs/ingress.ts` is a private closed adapter for the frozen `sse-recovery/v1`/Job cursor shapes. Public contracts remain untouched.
- The gateway is injected. No HTTP route, EventSource construction, shared store, router, Publication path or second authority was introduced.

## Contract and lifecycle decisions

- Snapshot input is typed as frozen `JobSnapshot`; the P3/P0 gateway contract requires values already accepted by `parseJobSnapshotV1`.
- Step and Attempt states remain open strings because the frozen Job Snapshot schema does not publish closed enums for them; presentation includes an unknown-state fallback.
- Active reattachment follows `backend/plotpilot_core/jobs/states.py::JOB_ACTIVE`, including `needs_attention` and excluding terminal `partial`/`failed`.
- Resume requires a checkpoint and is limited to `waiting_user | paused | needs_attention`; retry is limited to `partial | failed`; cancel is limited to `queued | running | waiting_user | paused`. Intents never mutate Job authority optimistically.
- A gap cannot be incrementally patched. Recovery must have floor-consistent gap flags and identify the same Job, cursor, revision, hash and durable high-water before full Snapshot replacement; the ended recovery handle is then replaced by a live subscription from the Snapshot cursor.
- All accepted Step and Attempt states have explicit presentation. Future unknown states retain their raw-value fallback.

## Frozen Finding remediation

All twelve existing IDs have implementation and directed-test evidence in
`docs/deliveries/PPA-04/job-drawer/remediation-closure-v1.json`. Entries are
marked `candidate_addressed` only; the same Sol reviewer remains responsible
for targeted closure decisions. The frozen Manifest was not modified.

## Reuse evidence

- Direct reuse: existing fixed `TaskDrawer.vue` shell and Job/capability presentation.
- Thin adaptation: repository reconnect/timer lifecycle pattern, strengthened with cursor-domain checks, Snapshot convergence and old-subscription fencing.
- Local knowledge:
  - `kb-novel-agent-phase1-chapter-lifecycle-20260811`, reviewed, SHA-256 `a1615b62add208d6279957de6c2d8de2796204eaf3577a4df0c822ed0ca2267d` — durable partial, refresh reattachment and terminal hydration.
  - `kb-novel-agent-cd2-character-distillation-jobs-20260811`, reviewed, SHA-256 `771e9cc9da1dec7a825f142abaabad65e0ac1f2a16b5dfc95bd4212ada0dcd1c` — resume creates a new Attempt and authority remains server-side.
  - `kb-plotpilot-pluginized-nw-p1-core-http-runtime-seam-blocker-20260828`, draft, SHA-256 `14936673ecdd5696d01f520780ebdc47a29b59dcb9bc39a7374a15bc9581a52d` — do not invent missing routes or runtime authority seams.
- Local tool selector returned no stack/capability match. Repository code and frozen fixtures were exact; GitHub offered no additional reuse benefit, so no dependency or third-party source was added.

## Validation summary

| Command | Result |
|---|---|
| `node --experimental-strip-types --test tests/p4-webui/job-drawer/*.test.mts` | 29 passed, 0 failed |
| `node --experimental-strip-types --test tests/p4-webui/*.test.mts` | 13 passed, 0 failed |
| exact-source mirror + `node --experimental-strip-types --test frontend/tests/job-drawer/*.test.mts` | 3 passed, 0 failed; 263 source files, 0 hash differences |
| `frontend/node_modules/.bin/vue-tsc.cmd --noEmit -p frontend/tsconfig.app.json --incremental false` | unavailable: this worktree has no `frontend/node_modules`; no unowned dependency path was created |
| installed `vue-tsc` against an isolated exact-current-source mirror | 263 source files, 0 hash differences; exit 0 |
| `git diff --check` | exit 0 |

Raw outputs are under `docs/deliveries/PPA-04/job-drawer/evidence/raw/`.

## Integration boundary

Runtime connection remains intentionally deferred to `NW-P3-JOB-RPC-02`, `NW-P3-EVENT-SSE-03` and `NW-P0-RUNTIME-COMPOSITION-02`. P4/P0 assembly must provide a `JobDrawerGateway` whose Job Snapshots have passed the frozen TypeScript ingress verifier. No router/main/store/package/shared-shell file was edited, and no GUI, browser, desktop, Tauri, EXE or installer gate was run.
