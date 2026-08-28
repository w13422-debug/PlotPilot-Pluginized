# NW-P2-LIFECYCLE-02 bounded remediation

Fresh `gpt-5.6-sol/max` pre-commit review froze Findings
`NW-P2-LC-001..011`.  This source node performed one bounded remediation; the
same reviewer must decide closure and this document does not grant central
acceptance.

## Frozen 2026-08-28 Finding mapping (review status unchanged)

This maps the bounded source response to frozen Manifest SHA-256
`d73e2755674987b4e14043684560e0e33772e40732d52dd825d452a0212b5288`.
It does not mark a Finding closed or grant merge eligibility.

| Frozen Finding | Source response | Decisive test |
|---|---|---|
| `NW-P2-LC-F-001` | Qualification binds frozen request/member/package/base delta/settings/exact shadow and freezes canonical Generation payload; commit accepts only that payload. | `test_f001_frozen_generation_binding_and_payload_are_immutable` |
| `NW-P2-LC-F-002` | Qualification releases the Shadow lease atomically; terminal mutations recheck Attempt/release/pin and activation rechecks exact qualified/released row. | `test_f002_qualified_shadow_is_terminal_and_releases_lease` |
| `NW-P2-LC-F-003` | One post-activation owner fences successor commit; pointer-advanced rollback consumes its token into replay-stable `superseded`. | `test_f003_post_activation_owner_and_rollback_replay_converge` |
| `NW-P2-LC-F-004` | Only exact `PACKAGE_PUBLISHED` plus `require_release` advances; FAILED/SUPERSEDED terminates without registration. | `test_f004_failed_staging_marker_never_becomes_published` |
| `NW-P2-LC-F-005` | Tombstones are checked before stage/adopt and all terminal Attempts, including `lkg_promoted`, bypass staging. | `test_f005_retired_release_cannot_be_rematerialized` |
| `NW-P2-LC-F-006` | Caller Maps were removed; a transaction-local P1 reader supplies plugin-bound revision and receipt; absence stops the slice. | `test_f006_settings_validation_requires_p1_authority` |
| `NW-P2-LC-F-007` | Stage/begin enforce exact Settings namespace binding; public verifier work is recorded as P0 Delta. | `test_f007_manifest_settings_namespace_is_bound_before_staging` |
| `NW-P2-LC-F-008` | Installed release pin acquisition is atomic with Attempt creation; retirement also scans every nonterminal frozen request. | `test_f008_selected_attempt_blocks_retirement_without_a_pin` |
| `NW-P2-LC-F-009` | One semantic validator enforces cross-field postconditions and current commit requires `pending_apply`. | `test_f009_semantic_postconditions_reject_empty_stage_and_direct_commit` |
| `NW-P2-LC-F-010` | P1 operation authority binds method/release/operation/request hash/result; P2 adds no second ledger. | `test_f010_retirement_replay_is_operation_and_payload_bound` |
| `NW-P2-LC-F-011` | Full public operation ID remains durable while a SHA-256-derived Windows-safe component addresses staging. | `test_f011_public_operation_id_uses_stable_hashed_path` |
| `NW-P2-LC-F-012` | Constructors execute no DDL; a hashed migration bundle and injectable shared transaction factory replace unsafe composition. | `test_f012_foreign_transaction_is_rejected_and_constructors_do_not_ddl` |
| `NW-P2-LC-F-013` | Decision invariants are validated before any package remover call. | `test_f013_contradictory_publication_barrier_never_calls_remover` |
| `NW-P2-LC-F-014` | One canonical ASCII decimal parser rejects Python-only indices and unauthorized `-` append. | `test_f014_array_indices_are_canonical_and_append_is_not_authorized` |

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
