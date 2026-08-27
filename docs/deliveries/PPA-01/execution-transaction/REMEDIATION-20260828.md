# NW-P1-EXECUTION-TX-01 bounded remediation

## Frozen review input

- Parent: `d1b6f612b1bb330a28328954fc23e2fdf0ce2d1d`
- Parent tree: `16b7c0131f28146c56881cb723e844da288d8f26`
- Finding manifest: `nw-p1-execution-tx-fresh-sol-block-v1.json`
- Manifest SHA-256: `1c47a49309c16af426da5d4c967a0c34ee3e91fea2457bdc8a0e9d54fda2394e`
- Scope: frozen Findings `NW-P1-ETX-F-001..015`; no Finding is marked closed here.

## Implementation evidence

| Finding | Bounded remediation | Decisive counterexample |
|---|---|---|
| F-001 | Durable cancel reservation now precedes execution; non-invoke ledger rows insert-or-verify; retry after response persistence loss recovers from the durable child projection without repeating cancel. | `test_f001_cancel_reservation_precedes_side_effect_and_recovers_record_failure` |
| F-002 | Step owns one active Attempt, monotonic epoch, ordinal, owner and expiry; acquisition serializes and fences expired predecessors; validation reads only persisted authority. | `test_f002_attempt_acquisition_has_one_active_monotonic_fence` |
| F-003 | Parent terminal transaction joins actual child Jobs/receipts, refreshes the Broker projection, blocks nonterminal/unsatisfied required children and derives the exact receipt set. | `test_f003_required_queued_child_blocks_parent_success_without_mutation` |
| F-004 | Step and Attempt persist the expected result contract; terminal Bundle profile must equal both frozen values. | `test_f004_frozen_result_contract_rejects_profile_swap` |
| F-005 | Bundle verification receives RunSnapshot workspace and Candidate staging independently rechecks target/base/write-set workspace. | `test_f005_run_snapshot_workspace_is_rechecked_before_staging` |
| F-006 | Internal `freeze_plan` persists complete membership, dependency DAG, ordinals and one output Step; starts outside the plan fail and final Job anchors come only from the output Step. | `test_f006_explicit_plan_freezes_membership_dag_and_output` |
| F-007 | Core reconstructs lineage, Bundle refs, child refs, staged items, timestamp and hash; unsupported model refs and caller-forged refs fail closed. | `test_f007_core_materializes_receipt_and_rejects_unbound_parent` |
| F-008 | Plugin/direct Candidate staging rejects `incomplete_stream`; Publication no longer treats it as a publishability exception. | `test_f008_plugin_incomplete_stream_is_never_stageable` |
| F-009 | Publication joins and revalidates Job/outcome/Attempt/RunSnapshot/Bundle/receipt/release/package/Candidate evidence before Revision CAS and on replay. | `test_f009_publication_requires_complete_execution_evidence` |
| F-010 | Shared SQLite connection readers and close are serialized by the repository `RLock`, yielding committed-only visibility. | `test_f010_shared_connection_reader_waits_for_rollback` |
| F-011 | Candidate staging keys are scoped by operation context/method and bindings are identified by Attempt/Bundle/item/Candidate. | `test_f011_candidate_identity_is_scoped_by_attempt_and_bundle` |
| F-012 | Existing visible Candidate parents are read in the same transaction and supplied to the existing SDK verifier. | `test_f012_existing_visible_parent_candidate_is_accepted` |
| F-013 | A winning `cancelling` CAS returns code `1003` for success/partial before generic edge validation and before any write. | `test_f013_cancelling_wins_with_cancelled_code_and_zero_writes` |
| F-014 | Plugin Job Events use `plugin.<plugin_id>.job.<outcome>`. | `test_f014_plugin_job_event_uses_current_plugin_namespace` |
| F-015 | Complete replay validates receipt/Event/Core Event/Snapshot/Bundle/bindings/high-water/frame closure; invoke replay validates reservation, Snapshot bytes/binding and child Job/Step/Attempt release/generation/epoch. | `test_f015_complete_replay_fails_closed_when_receipt_is_missing`, `test_f015_invoke_replay_requires_snapshot_bytes_and_child_identity` |

## Composition boundary

P0 still owns runtime wiring of plan freeze/acquisition, worker lease renewal/cancel composition, public endpoint exposure and merge order. This remediation adds no public contract and performs no P0 wiring.

The original fresh reviewer remains the authority for per-Finding disposition and merge eligibility.
