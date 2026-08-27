# P3B Generation 2 structural source candidate

## Identity

- Branch: `codex/ppa-03b-provider-broker-g2`
- Exact base / required parent: `1b352be671e70a2ce443b10499060982e93d64b7`
- Source seed: `32ebc76273ce8ef5307ff7cb82b0461435b0be87`
- Structural decision SHA-256: `03b7c22551f7567944caf2ad108dcb92014420bd00db067b2d867f0773ff7487`
- Reviewer to reuse: `01a0438c-f40b-7ec1-b943-f2a9a711b375`
- donor-local push: `DISABLED`

This is a new generation materialized from the accepted base. It is not a third patch on either rejected P3B HEAD. Implementation, regressions, coordination Delta and raw evidence are committed together in one source candidate.

## Structural closure matrix for reviewer

The rows below are candidate counter-evidence, not self-closure or merge eligibility.

| ID | G2 structural behavior | Counterexample coverage |
|---|---|---|
| F001 | The production constructor rejects each exported test-only InMemory authority. A private sentinel is supplied only by `CapabilityBroker.for_test`; production still requires explicit authoritative ports and preserves legitimate falsey ports with `is None`. | `test_production_constructor_requires_authorities_and_preserves_falsey_ports` plus all normal `for_test` broker tests |
| F002 | Preserved: durable pre-child operation reservation, immutable envelope binding, atomic `create_or_recover_child`, authoritative Core Snapshot bytes/hash verification and post-factory recovery without duplicate child creation. | `test_post_factory_failure_recovers_reserved_child_without_second_creation`; snapshot/orphan regressions |
| F003 | `BLOCKING_STATES` inference is removed. Aggregation and receipt propagation share exactly one closed rule: optional + terminal + not satisfying. `interrupted`, `fenced`, generic `terminal` and `failed` are recorded as optional failures but do not block required success. SDK receipt hash and exact child lineage binding remain preserved. | expanded `test_required_optional_aggregation_and_receipt_propagation`; receipt lineage/monotonicity regressions |
| F004 | Preserved: `created_at` and complete immutable payload are revision-hash bound; memory and SQLite reject duplicate-ID drift. | `test_created_at_is_hash_bound_and_duplicate_revision_id_rejects_timestamp_drift` |
| F005 | Boolean `sent=False` is never proof of not-sent. Send evidence resolves to explicit `sent/not_sent/unknown`; retry requires exact `not_sent`, explicit transient, policy and bound. Missing/conflicting/positive evidence queries by the same key when supported or returns uncertain, never resend. | `test_v1_replay_policy_retries_only_explicit_transient_pre_send_failure`; `test_unknown_or_conflicting_send_evidence_never_retries`; query recovery tests |
| F006 | Raw chunks are concatenated without inserted bytes. A response `iter_lines()` source is wrapped explicitly and each stripped line has its newline/empty-frame boundary restored before SSE parsing. Delta content always appends. | raw fragmentation test; `test_stripped_iter_lines_restores_blank_sse_frames` (`a` + `a` = `aa`) |
| F007 | Preserved: receipt sink failure is handled once and non-recursively, returning uncertain without a persisted/receipted claim. | `test_receipt_sink_failure_is_uncertain_and_hashes_reject_external_drift` |
| F008 | WITHDRAWN; no feature or evidence-only remediation. | write-set audit records `F008_TOUCHED=0` |

## Authority boundary and activation Delta

Production activation still requires a P1-owned durable reservation-ledger adapter and an atomic `create_or_recover_child` adapter in the existing authority transaction. G2 does not create a second authority and does not modify Jobs, Events, contracts or the public SDK. Until composition supplies those ports, the production Broker fails closed.

## Validation evidence

Raw outputs under `evidence/raw/` are regenerated on this G2 branch and include command, branch/base identity where applicable, result and exit code for targeted pytest, parent P3 regression, compileall, manifest verification, offline injected-transport smoke, `git diff --check`, and write-set proof.
