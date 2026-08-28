# NW-P4-JOB-DRAWER-02 source candidate

## Scope

- Node: `NW-P4-JOB-DRAWER-02`
- Base: `8e3d3a7268e0d28559a7212790e3ab9f22c1453a`
- Baseline tree: `eab64b7d5b1e3ad652aeaf48bd0085d930beca5d`
- Branch: `codex/nw-p4-job-drawer-02`
- Candidate locator: the single enclosing direct-child source commit
- Status: source candidate, review pending; this document does not claim acceptance or merge eligibility

## Delivered source

- `TaskDrawer.vue` now renders Job, every Step and every Attempt, explicit progress, distinct `partial` and `needs_attention` states, connection/recovery state, action errors and pending retry/resume/cancel intents.
- `core/jobs/store.ts` owns one authoritative in-memory projection. Snapshot application replaces complete nested collections, rejects identity/hash conflicts, prevents revision/high-water rollback, detects event gaps and keeps the Job cursor separate from Core cursors.
- `core/jobs/controller.ts` performs refresh discovery/reattachment, active-Job SSE subscription, cursor-based reconnect, matching recovery Snapshot replacement, terminal detach, per-connection late-callback fencing and injected retry/resume/cancel intents.
- `core/jobs/ingress.ts` is a private closed adapter for the frozen `sse-recovery/v1`/Job cursor shapes. Public contracts remain untouched.
- The gateway is injected. No HTTP route, EventSource construction, shared store, router, Publication path or second authority was introduced.

## Contract and lifecycle decisions

- Snapshot input is typed as frozen `JobSnapshot`; the P3/P0 gateway contract requires values already accepted by `parseJobSnapshotV1`.
- Step and Attempt states remain open strings because the frozen Job Snapshot schema does not publish closed enums for them; presentation includes an unknown-state fallback.
- Active reattachment follows `backend/plotpilot_core/jobs/states.py::JOB_ACTIVE`, including `needs_attention` and excluding terminal `partial`/`failed`.
- Resume requires a checkpoint in the host projection. Retry, resume and cancel only emit injected intents; they never mutate Job authority optimistically.
- A gap cannot be incrementally patched. Recovery must identify the same Job, cursor, revision, hash and durable high-water before full Snapshot replacement.

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
| `node --experimental-strip-types --test tests/p4-webui/job-drawer/*.test.mts` | 16 passed, 0 failed |
| `node --experimental-strip-types --test tests/p4-webui/*.test.mts` | 13 passed, 0 failed |
| `frontend/node_modules/.bin/vue-tsc.cmd --noEmit -p frontend/tsconfig.app.json --incremental false` | unavailable: this worktree has no `frontend/node_modules`; no unowned dependency path was created |
| installed `vue-tsc` against an isolated exact-current-source mirror | exit 0 |
| `git diff --check` | exit 0 |

Raw outputs are under `docs/deliveries/PPA-04/job-drawer/evidence/raw/`.

## Integration boundary

Runtime connection remains intentionally deferred to `NW-P3-JOB-RPC-02`, `NW-P3-EVENT-SSE-03` and `NW-P0-RUNTIME-COMPOSITION-02`. P4/P0 assembly must provide a `JobDrawerGateway` whose Job Snapshots have passed the frozen TypeScript ingress verifier. No router/main/store/package/shared-shell file was edited, and no GUI, browser, desktop, Tauri, EXE or installer gate was run.
