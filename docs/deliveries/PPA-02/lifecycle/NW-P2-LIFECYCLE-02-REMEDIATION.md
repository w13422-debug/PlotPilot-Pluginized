# NW-P2-LIFECYCLE-02 bounded remediation

Fresh `gpt-5.6-sol/max` pre-commit review froze Findings
`NW-P2-LC-001..011`.  This source node performed one bounded remediation; the
same reviewer must decide closure and this document does not grant central
acceptance.

| Finding | Remediation evidence |
|---|---|
| LC-001 | Closed transition edge/field allowlists; specialized fail/recovery paths; activation cannot jump to failure. |
| LC-002 | All shadow mutations read/check/update the complete lease fence inside one immediate transaction and require one affected row. |
| LC-003 | Atomic deterministic pending pins cover every target and exact rollback-base member; commit/rollback recheck epoch, installed state and package bytes. |
| LC-004 | Rollback generation change and first retirement CAS now require transaction-local Core Event callbacks; callback failure rolls back authority. |
| LC-005 | Nonterminal rows are independent reconciliation fences; pre-activation restart no longer toggles global safe mode; safe-mode exit refuses unresolved fences. |
| LC-006 | Pre-commit health leaves qualification ID null; `lkg_pending` consumes a typed, authority-returned restart qualification bound to Generation and health Asset. |
| LC-007 | Retirement completion accepts only a typed release-bound Candidate/Publication decision, persists blocker IDs, and runs deletion only after the gate.  Production completion remains stopped by the P1 Delta. |
| LC-008 | Shadow insert checks `env_prepared` and frozen request release before mutation; exact later replay is idempotent. |
| LC-009 | Plan Delta now requires opaque revision IDs/hash, Workspace/current Plan/Generation CAS, fresh resolution and atomic Plan-pin exchange. |
| LC-010 | Plan intent retains canonical bytes and returns recursively frozen views; enabled Data bindings require exact data generation and bundle Asset. |
| LC-011 | Exact already-absent package content is treated as successful retry, preserving tombstone identity. |

Added tests exercise forbidden transition patches, post-activation failure,
multiple reconciliation fences, rollback package unavailability, complete
Generation pins, Event/qualification callback rollback, typed Publication
barriers, stale shadow fail/release, invalid shadow creation, deep Plan
immutability, exact Data bundle binding and deletion-after-crash replay.

## Structural simplification after targeted recheck

The same Sol/max targeted recheck closed seven Findings but kept
`NW-P2-LC-001`, `006`, `008` and `010` open.  Rather than apply a second
point-patch round, the owned architecture was reduced once:

- generic `advance_attempt()` no longer contains any qualification,
  current-pointer, LKG-pending or LKG-promotion edge; repository qualification
  now consumes the typed authority result and executable-pin guard in the same
  transaction;
- shadow creation requires a constructor-injected install-pin guard, and the
  guard rechecks exact release, installed/package state, retire epoch, pin kind
  and install owner before INSERT;
- Plan resolution requires an injected DataBundle Asset resolver, invokes the
  published verifier, binds plugin/release/package/format identities, and
  carries the verified `bundle_hash` into the immutable resolution and intent
  hash.

The refreshed candidate has lifecycle `30 passed` and affected P2 `62 passed`.
Closure still belongs to the same Sol/max reviewer; this source document does
not self-close a Finding or grant merge eligibility.
