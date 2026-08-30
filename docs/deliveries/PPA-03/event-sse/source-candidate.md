# NW-P3-EVENT-SSE-03 source candidate

## Identity and boundary

- Node: `NW-P3-EVENT-SSE-03`
- Branch: `codex/nw-p3-event-sse-03`
- Initial source parent: `8e3d3a7268e0d28559a7212790e3ab9f22c1453a`
- Bounded remediation parent: `4790cbcdb83b2e0bbfad9636552e8fedb22b0d2d`
- Frozen Finding Manifest SHA-256: `9df4ca6e1254f228acb82a34c276a679105a68da466bbdbbb0ecbb669490f4f7`
- Frozen review result SHA-256: `a3af596003bbaed500702224496da9679a8352a950ce4be27fd022c7736b253b`
- Source-only: yes
- Public contracts, generated SDKs, shared Job router/state/ledger/ports/migrations and `app.py`: unchanged
- Runtime mount and public HTTP error mapping: deferred to their existing P3 assembly/P0 owners

## Reuse decision

The implementation is a thin adapter over the accepted H_C authority:

- `execution_core_event` remains the only global Core Event log.
- `execution_job_event` and `execution_job.job_event_high_water` remain the only per-Job Plugin Event log/high-water.
- `CoreAuthorityRepository.transaction/read_connection`, `AssetStore`, canonical JCS bytes and the published snapshot/recovery verifiers are reused directly.
- Job checkpoint/stream projection is injected as a closed read-only callback rather than being fabricated as permanent `null`/empty authority.
- Core aggregate snapshot state is injected by the Core projector rather than reconstructed from Event payloads.

The local tool selector returned unrelated utilities. GitHub lookup was intentionally skipped because the accepted repository already supplies the exact schema, transaction, Asset and verifier seams; an external dependency would add no reuse benefit and would increase compatibility risk.

Local evidence consulted:

- draft `kb-plotpilot-pluginized-nw-p1-core-http-runtime-seam-blocker-20260828`, SHA-256 `14936673ecdd5696d01f520780ebdc47a29b59dcb9bc39a7374a15bc9581a52d`;
- reviewed `kb-novel-agent-phase1-chapter-lifecycle-20260811`, SHA-256 `a1615b62add208d6279957de6c2d8de2796204eaf3577a4df0c822ed0ca2267d`.

## Delivered behavior

### Durable stores

- Core Event append requires a caller-owned active authority transaction so aggregate mutation and Event publication cannot split.
- Core high-water is read from `sqlite_sequence`, so prefix retention and reopen cannot rewind it.
- Plugin Job Event append atomically advances the Job-local high-water and binds Job, Step, Attempt, plugin, release and local sequence.
- Supplied Event connections must already be in a transaction; test-only CAS failure injection proves Job Event INSERT and high-water advance roll back together.
- Contract-valid global Core Events fail closed with `MIGRATION_FAILED` until the authority owner accepts nullable Workspace storage; no sentinel Workspace or second log is fabricated.
- Core and Job payload Asset ID/hash pairs fail closed when half-present.
- Plugin Events must use `plugin.<plugin_id>.` and cannot use Core Event types or the reserved `plugin.generation.*` prefix.
- Replay verifies stored JSON identity against every authoritative row identity column.

### Retention and recovery

- Replay floor is `MIN(retained_seq)-1`; an empty retained prefix uses the durable high-water.
- Cursor ahead and cursor-domain mix fail closed.
- A cursor below the floor requires a matching durable snapshot.
- Snapshot high-water must cover the floor, cannot be ahead, and is followed by a retained tail replay, including events committed after snapshot capture.
- Core and Job recovery values keep their snapshot schemas and cursor domains distinct.

### Refresh-safe SSE and Broker projection

- Frozen cursors are `core/<seq>` and `job/<job_id>/<seq>` with canonical ASCII decimal sequences.
- `after_seq` must agree with `Last-Event-ID` when both are supplied.
- Normal replay is HTTP 200 `text/event-stream`; filtered empty pages still advance browser `Last-Event-ID` with a cursor-only field.
- A retention gap is exposed as HTTP 409 JSON `sse-recovery/v1`, carrying the immutable snapshot Asset reference.
- Job snapshots and `job-event-page/v1` are materialized as canonical immutable Assets.
- Event windows use a dedicated connection and explicit SQLite read transaction, pinning high-water, retention floor and retained rows against a second writer.
- `JobPollProjectionStore` shares one pinned read transaction across terminal/result anchors, Job aggregate snapshot and Event page; a second-connection append/prune regression proves generation consistency.
- `job-event-page.next_job_event_seq` is a next-unread cursor and must be converted to exclusive `after_job_event_seq` with `next_job_event_seq - 1`.

## Verification

Machine-readable results are in [`validation-evidence.json`](validation-evidence.json), and Finding-addressed evidence is in [`remediation-closure.json`](remediation-closure.json). These are source validation results, not an independent review or central acceptance claim; Finding closure remains reviewer-owned.

## Integration notes

- P3 assembly must provide the real checkpoint/stream read projection to `JobSnapshotStore` and export/mount the accepted modules.
- `NW-P3-JOB-RPC-02` must use `next_job_event_seq_to_after` at the page/RPC continuation boundary and prove the first unread Event is not skipped.
- The authority/migration owner must adjudicate [`authority-and-cursor-delta.md`](../../../../coordination/PPA-03/event-sse/authority-and-cursor-delta.md) before global Core Event persistence/subscription assembly proceeds.
- Core mutation owners may pass their existing transaction to `CoreEventStore.append`; the store intentionally refuses standalone Core Event writes.
- P0 remains the only owner of public route mounting and public 400/409 error-envelope mapping.
