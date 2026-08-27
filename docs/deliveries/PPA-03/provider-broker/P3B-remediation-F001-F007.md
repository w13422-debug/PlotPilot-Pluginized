> **Generation 2 note:** This file is retained as source-seed history only. G2 identity, closure matrix and evidence are authoritative in `P3B-generation-2.md`.

# P3B Restricted Remediation — F001–F007

## Identity and scope

- Required parent: `8ccfb47214ee5f720afdec01acc03923bae68b79`
- Branch: `codex/ppa-03b-provider-broker`
- Finding manifest SHA-256: `ad825a242fda0bf1436a27d5516f47f6616434e73bd6a0123d38f524a52dbd96`
- Reviewer to reuse: `01a0438c-f40b-7ec1-b943-f2a9a711b375`
- Remediation scope: F001–F007 only. F008 remains WITHDRAWN and caused no repository change.

## Candidate closure table (for reviewer decision)

This table records implemented counter-evidence. It does not close any Finding and does not grant merge eligibility.

| ID | Restricted remediation | Counterexample regression |
|---|---|---|
| P3B-SOL-F-001 | Production constructor requires injected Attempt context, durable reservation ledger and durable child-record port. In-memory authorities are available only through `CapabilityBroker.for_test`; all port selection uses `is None`, preserving falsey implementations. | `test_production_constructor_requires_authorities_and_preserves_falsey_ports` |
| P3B-SOL-F-002 | Invoke durably reserves the operation before child creation, binds the immutable envelope Asset, requires atomic `create_or_recover_child`, persists full child identity, reads the child Snapshot through Core and verifies its bytes/hash/bindings. Retry after a post-factory store interruption recovers the reserved child without invoking the factory again. | `test_post_factory_failure_recovers_reserved_child_without_second_creation`; `test_unattested_factory_result_is_rejected_instead_of_faking_snapshot` |
| P3B-SOL-F-003 | Terminal projections require receipts. SDK `verify_provenance_receipt` recomputes receipt hash; receipt identity binds actual child job/step/attempt/lease/Snapshot/release/capability and nullable result Bundle ID/hash. Terminal/result/receipt state is monotonic and aggregation rejects terminal children without receipts. | `test_terminal_receipt_hash_lineage_and_monotonicity_are_fail_closed`; terminal-without-receipt branch in `test_required_optional_aggregation_and_receipt_propagation` |
| P3B-SOL-F-004 | `created_at` is included in the canonical `ModelProfileRevision` hash payload. Duplicate revision IDs with timestamp drift are rejected by both in-memory and SQLite repositories. | `test_created_at_is_hash_bound_and_duplicate_revision_id_rejects_timestamp_drift` |
| P3B-SOL-F-005 | Only v1 policies `idempotent_auto`, `manual_if_unknown`, `never_replay` are accepted. Resend is bounded to explicitly transient pre-send failures under `idempotent_auto`, reusing the exact request/provider/model/key and recording actual `retry_count`. Any possible-send uncertainty queries only an explicitly query-capable transport or returns uncertain; it never sends again. | `test_v1_replay_policy_retries_only_explicit_transient_pre_send_failure`; `test_query_capable_recovery_uses_same_key_and_never_sends_a_second_request` |
| P3B-SOL-F-006 | `choices[].delta.content` always appends; complete `message.content` uses full-content semantics. Iterable SSE input is reassembled before UTF-8/blank-frame decoding, covering identical deltas, repeated prefixes, CRLF/LF blank frames and arbitrary fragmentation. | `test_stream_delta_is_always_appended_across_arbitrary_sse_fragments`; `test_non_stream_response_usage_and_response_persister_are_bound` |
| P3B-SOL-F-007 | Receipt sink failure is handled once, non-recursively. A replacement uncertain receipt records `receipt_persistence_failed`, does not call the sink again and does not claim `receipted`. | `test_receipt_sink_failure_is_uncertain_and_hashes_reject_external_drift` |

## Authority boundary / remaining integration Delta

The accepted public contracts and SDK do not expose a production composition adapter for the Broker reservation protocol. This source therefore defines an injected internal Protocol and fails closed in the production constructor. P1/P3 integration still must provide:

1. a durable adapter implementing `lookup/get_reservation/reserve/attach_envelope/attach_child/record` in the existing authoritative transaction domain; and
2. an atomic `create_or_recover_child` adapter keyed by the reserved operation.

No second authority was fabricated, and no read-only Jobs/Events/contracts/SDK path was modified. This is the only remaining real integration Delta.
