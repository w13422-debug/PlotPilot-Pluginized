# NW-P4-CORE-FLOWS-02 bounded remediation delivery

## Identity

- Node: `NW-P4-CORE-FLOWS-02`
- Branch: `codex/nw-p4-core-flows-02`
- Exact remediation parent: `06a42cfaa7b654e9ac9e2004131f64a813f1b8dc`
- Candidate locator: the single direct-child commit containing this record
- Frozen Finding Manifest SHA-256: `9a583db4c6895822d8a98792c462a7f226bc3e534b2df0a56b78f42825ead061`
- Frozen review result SHA-256: `789e9f8e1908b8e81660ceb3150196bde01e149e8c4f1f6c2356e9972afe4ce8`
- Source readiness remains `source_ready_integration_deferred`.

This is one bounded source remediation for `NW-P4-CF-SOL-F-001..005`. It does not declare Finding closure, acceptance, or merge eligibility. Closure is reserved for the original reviewer agent `01a04834-0e24-7042-a80c-a8bbd3e9811f`.

## Finding-oriented changes

| Finding | Source response | Negative/boundary evidence |
|---|---|---|
| `NW-P4-CF-SOL-F-001` | Binds the opened Revision to the just-read Document current Revision and text payload; binds save receipt actor/source/payload fields. | Wrong Revision ID and payload are rejected before content read; each save-result field drift is rejected. |
| `NW-P4-CF-SOL-F-002` | Adds a unified scoped Workspace generation with desk/open/save request sequences, immediate old-state invalidation, and Workspace+Document draft keys. | A→B→C late responses are modeled as ineligible; source wiring checks cover generation, list clearing, sequences, and scoped drafts. |
| `NW-P4-CF-SOL-F-003` | Tracks successful and failed batch deletes separately, removes only successful IDs, retains authoritative failures in selection, reports each failure, then resynchronizes. | Reducer tests cover partial, all-success, and all-failure sets. |
| `NW-P4-CF-SOL-F-004` | Makes project-list fetches latest-request-only, propagates the latest failure, restricts success feedback to committed refreshes, and resynchronizes after deletes. | Deferred response test proves an older refresh cannot commit after the latest response. |
| `NW-P4-CF-SOL-F-005` | Binds Workspace/Document page offset, limit, and internal cursor, with identity de-duplication across the whole traversal. | Both page types reject offset, limit, cursor, and cross-page identity drift. |

## Reuse decision

Reviewed local notes `kb-novel-agent-na008-settlement-lifecycle-20260812` (SHA-256 `ef87964a930480efeeb03ab95622b319f5719f0adfbf0637c4bf004dfc340d1d`) and `kb-novel-agent-na017-independent-consistency-audit-20260813` (SHA-256 `d62a76a67bd347d78f724f69f67771027f133ec317068919bfdbad9bb217698e`) support fail-closed epochs and scoped request identity. The exact implementation reuses the repository's frozen Core contracts and existing flows through a thin owned adapter. The local tool selector returned only zero-stack-match utilities, so no tool was copied. GitHub search was skipped because the accepted repository already contains the exact contract and the needed guard is a small local state primitive; external code would add no reuse benefit.

## Source validation

| Command | Raw result |
|---|---|
| `node --experimental-strip-types --test tests/p4-webui/core-flows/*.test.mts` | exit `0`; `40 tests`, `40 pass`, `0 fail` |
| `node --experimental-strip-types --test tests/p4-webui/*.test.mts` | exit `0`; `13 tests`, `13 pass`, `0 fail` |
| temporary isolated frontend copy + read-only main-worktree dependencies, equivalent `vue-tsc --noEmit -p frontend/tsconfig.app.json --incremental false` | exit `0` |
| `git diff --check` | exit `0` |

The isolated worktree still intentionally has no `frontend/node_modules`; no dependency, package, lock, runtime mount, browser, GUI, Tauri, desktop, EXE, installer, or final-artifact gate was changed or run.

## Deferred boundary

Runtime composition remains deferred on `NW-P1-AUTHORITY-PUBLICATION-SEAM-02`, `NW-P1-CORE-HTTP-G2`, and `NW-P0-RUNTIME-COMPOSITION-02`. The next action is Finding-ID-only closure by the same reviewer; this source task makes no reviewer verdict claim.
