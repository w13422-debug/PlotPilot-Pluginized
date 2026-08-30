# NW-P4-CORE-FLOWS-02 source delivery

## Identity and scope

- Node: `NW-P4-CORE-FLOWS-02`
- Exact base: `8e3d3a7268e0d28559a7212790e3ab9f22c1453a`
- Exact base tree: `eab64b7d5b1e3ad652aeaf48bd0085d930beca5d`
- Branch: `codex/nw-p4-core-flows-02`
- Readiness: `source_ready_integration_deferred`
- Runtime dependencies: `NW-P1-AUTHORITY-PUBLICATION-SEAM-02`, `NW-P1-CORE-HTTP-G2`, `NW-P0-RUNTIME-COMPOSITION-02`

This record describes a source candidate. It is not central acceptance, Finding closure, merge eligibility, or a live-runtime claim.

## Reuse decision

The local knowledge router returned `kb-plotpilot-pluginized-nw-p1-core-http-runtime-seam-blocker-20260828` (draft, SHA-256 `14936673ecdd5696d01f520780ebdc47a29b59dcb9bc39a7374a15bc9581a52d`) at:

`C:\Users\Administrator\Documents\Local-KnowledgeBase\20_项目档案\PlotPilot-Pluginized\交付记录\2026-08-28-NW-P1-CORE-HTTP-01-runtime-seam-blocker.md`

It confirms that this node must remain source/fixture-only until the P1/P0 HTTP and composition seams exist. The repository's frozen `frontend/src/contracts/core-api.ts`, `frontend/src/contracts/types.ts`, Core method matrix and contract fake were therefore reused through a thin adapter. The local source-tool selector returned only zero-stack-match utilities, so none was copied. GitHub search was skipped because the accepted repository already contains the exact closed schemas, method matrix, validators and deterministic fake; an external helper would add no reuse benefit and would risk contract drift.

## Delivered behavior

- A route-keyed `CoreFlowRouteMap` binds each used route to its frozen request and response type.
- The HTTP gateway derives methods/paths/query/body from the frozen matrix, validates ingress/egress, and enforces exact success and failure pairs.
- Project list/create/delete use Workspace identity and CAS revision; uncertain retries reuse the same mutation identity.
- Home opens a project directly without a setup wizard. Rich planning fields remain visibly disabled because Core Workspace does not own them.
- Workbench uses authoritative `document_id` for routes and selection; numeric display indices are explicitly non-authoritative.
- Chapter open validates Workspace/Document/Revision identity and consumes paged content at the frozen 65,536-character bound.
- Edit state is local and per-document. Save creates a Core document Revision with injected `created_by`, stable retry identities, base-revision binding and the frozen content limit.
- The runtime registry has no fake, legacy adapter, or implicit live HTTP mount. P0 must install the authoritative gateway and actor binding later.
- Owned Home/Workbench components preserve the Home card/sidebar and Workbench header/three-pane/editor structure without mounting legacy project/chapter authority components.

## Validation evidence

| Command | Raw result |
|---|---|
| `node --experimental-strip-types --test tests/p4-webui/core-flows/*.test.mts` | `18 tests, 18 pass, 0 fail` |
| `node --experimental-strip-types --test tests/p4-webui/*.test.mts` | `13 tests, 13 pass, 0 fail` |
| exact local `frontend/node_modules/.bin/vue-tsc.cmd --noEmit -p frontend/tsconfig.app.json --incremental false` | Not runnable: this isolated worktree has no `frontend/node_modules` |
| read-only dependency reuse: main-worktree `vue-tsc` + temporary equivalent strict config targeting this worktree | `exit 0` |

No browser, GUI, Tauri, desktop, EXE, installer or final-artifact gate was run.

## Known integration limits

- The source runtime is intentionally uninstalled here; live requests remain blocked on the three named integration dependencies.
- Core Workspace has no premise/genre/target-length fields. Those existing visual inputs are disabled rather than silently stored elsewhere or discarded.
- Core Document has no authoritative chapter number. Navigation and deep links use `document_id`; display indices are presentation-only.
- `created_by` must be supplied by the later authoritative runtime composition; this slice does not invent an actor.
