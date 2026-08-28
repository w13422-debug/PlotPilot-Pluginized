# NW-P2-SUPERVISOR-01 bounded remediation v1

## Frozen identity

- Remediation parent: `353e8908cc4f74d9e16b50583914adbfdef29bc6`
- Parent tree: `0be2e18c5a1b72d7db0d2f16d297c2d8a98c715a`
- Frozen Finding Manifest SHA-256: `1deba5f9fc087590c99bb7fe47d7adff98c867d8ba3df492d90d379447f58e34`
- Scope: owned supervisor source/tests/docs/coordination only.
- Review state: submitted for the original reviewer to recheck each frozen ID. This document does not declare any Finding CLOSED, central PASS, or merge eligibility.

## Reuse decision

The remediation thin-adapts the repository's accepted SDK framing/verifier, Core lease/CAS ports, immutable package lookup and Python standard-library process primitives. The local tool selector returned unrelated utilities, so copying them or adding a dependency had no reuse benefit. An external GitHub dependency was not introduced because the exact required implementations already exist in the repository and owned standard-library adapters.

## Finding-by-Finding remediation mapping

| Finding | Remediation supplied for recheck | Decisive evidence |
|---|---|---|
| `NW-P2-SUP-F-001` | Protocol events are accepted only in STARTING/READY; handshake can perform only STARTING -> READY and late callbacks cannot revive termination. | test_late_handshake_cannot_revive_timed_out_lifecycle; test_synchronous_startup_transport_failure_never_writes_handshake_or_returns_ticket |
| `NW-P2-SUP-F-002` | Lifecycle-keyed records remain independently draining; released non-current records are pruned both during reconciliation and the replacement current-swap window. | test_replacement_keeps_old_lifecycle_tracked_through_kill_deadline; test_replacement_prunes_old_record_when_old_process_exits_before_current_swap |
| `NW-P2-SUP-F-003` | All pre-record failures use the exact release-or-enqueue retry path; per-record cleanup and pending retry are serialized. | test_prespawn_release_failure_is_retried_without_masking_original (lookup/start x false/exception); test_concurrent_terminal_cleanup_cannot_recreate_pending_release |
| `NW-P2-SUP-F-004` | Bound Attempt/install fences are busy retains; idle begins only after the exact final unbind, including terminal-commit unbind. | test_attempt_and_install_are_busy_retains_until_last_unbind; terminal Attempt/install ACK tests |
| `NW-P2-SUP-F-005` | Crash, EOF/protocol failure and forced termination reconcile the exact authoritative Attempt before releasing the worker claim; retries preserve ordering. | test_crash_attempt_reconciliation_precedes_claim_release_and_retries; test_termination_barrier_rejects_late_bind_and_unbind_without_losing_attempt; terminal exit-before-ACK test |
| `NW-P2-SUP-F-006` | Each tick rechecks exact Attempt and install authority/epoch and fences only that lifecycle; terminal install release has a bounded ACK drain. | test_revoked_bound_install_is_fenced_without_inbound_rpc; test_terminal_install_release_drains_durable_ack_before_exact_unbind |
| `NW-P2-SUP-F-007` | Canonical first responses remain retryable; exact durable replay covers commit-before-local-stage; terminal ACK drains before terminate, while mismatch and missing callback fail closed/bounded. | test_host_response_survives_terminal_authority_consumption_and_write_retry; terminal ACK/unbind/exit/deadline tests; durable exact replay and payload/method/meta mismatch matrix |
| `NW-P2-SUP-F-008` | Windows workers are suspended until KILL_ON_JOB_CLOSE Job assignment succeeds; terminate/kill cover the tree and EOF drain closes inherited pipes on a deadline. | test_windows_job_assignment_failure_kills_child_fail_closed; test_pipe_drain_deadline_closes_inherited_pipes_and_reports_exit; test_windows_job_close_terminates_real_descendant_and_unblocks_inherited_pipe |
| `NW-P2-SUP-F-009` | Per-process bounded writer queues remove pipe I/O from the global supervisor lock; write/flush deadlines fail only the exact transport and callbacks occur only after flush. | bounded queue/failure/deadline tests; test_writer_calls_delivery_callback_only_after_flush_commits_frame; test_blocked_worker_write_does_not_hold_global_supervisor_lock |
| `NW-P2-SUP-F-010` | Provisioning requires exact probed CPython 3.12 identity before install, after install and after nonreplace publish; an owned bad final is removed. | venv marker/probe/prepublish/postpublish/nonreplace-race failure-window tests |
| `NW-P2-SUP-F-011` | Routes carry absolute resolved immutable package/venv plus disjoint mutable working/private-generation roots; reparse and overlap aliases fail closed; process cwd uses working_root. | lookup root/reparse/overlap/data-generation tests; process cwd assertion |
| `NW-P2-SUP-F-012` | Owned P0 Delta now requires durable single-authority startup reconciliation: Attempt/install terminal reconciliation before exact worker-pin release, idempotent across restart/replacement. | coordination/PPA-02/supervisor/p0-composition-delta-v1.json; production composition remains P0-owned |
| `NW-P2-SUP-F-013` | Every retain gets factory value plus monotonic sequence and is released by live-set identity idempotently. | test_retain_factory_collision_cannot_alias_late_release |
| `NW-P2-SUP-F-014` | Pending/inbound/event queues have hard limits, written inbound state is evictable, heartbeats bypass ordinary events and overflow terminates only the exact lifecycle. | pending/inbound recovery tests; heartbeat capacity and exact lifecycle overflow tests |
| `NW-P2-SUP-F-015` | Outbound IDs are reserved without replacement and every collision, including handshake/shutdown factory collision, is rejected. | test_outbound_duplicate_id_never_overwrites_first_pending_request |
| `NW-P2-SUP-F-016` | Every timeout must be numeric, finite and positive. | test_timeout_configuration_rejects_every_nonfinite_value (5 timeouts x NaN/+Infinity/-Infinity) |

## Raw validation

- Supervisor target: `120 passed in 3.84s`
- Complete `tests/p2-plugin-platform`: `152 passed in 4.09s`
- `compileall`: exit 0, no diagnostics
- Ruff: `All checks passed!`
- Contract manifest: `contract manifest is deterministic (137 files)`
- `git diff --check`: exit 0; emitted only Git LF-to-CRLF advisory lines
- Raw files: `docs/deliveries/PPA-02/supervisor/evidence/remediation-raw/`

## Remaining P0 composition dependencies

Production authority wiring, durable startup reconciliation, exact-request durable Host ledger resolution, trusted CPython 3.12 selection, private `plugin.db` API composition, Windows contained factory composition, immutable worker route mount and response-lifetime pin remain P0-owned. The production path remains fail-closed when these are absent; this node does not claim mount/composition ownership.
