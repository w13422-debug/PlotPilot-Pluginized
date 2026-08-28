# NW-P3-JOB-RPC-02 source candidate

## Scope

This node adds framework-neutral Job command/query and Host RPC application
adapters over the accepted `ExecutionAuthority`, committed repository read gate,
RPC verifier, and P2 supervisor success-response port. It does not mount public
HTTP routes, mutate shared Job packages, create a ledger, publish candidates, or
access a supervisor private session.

## Reuse decision

- Direct reuse: `ExecutionAuthority`, `CoreAuthorityRepository.read_connection`,
  RPC method matrix/verifiers, `RpcEvent`, `WorkerTicket`, and
  `PluginProcessSupervisor.respond_host_request`.
- Thin adaptation: immutable command results, committed snapshot/event
  projections, and event-to-handler dispatch.
- Explicit composition: checkpoint/stream projections require an injected
  `JobSnapshotExtensionReader`; there is no silent empty production fallback.
- Local source-tool candidates were unrelated, and the repository already held
  the exact accepted contracts and authority, so GitHub offered no additional
  reuse benefit.

## Authority boundary

- Creation and plan freezing delegate to P1 authority. Attempt start now stops
  unless P1 supplies an atomic typed `AttemptStartBinding` port.
- Snapshot and Job event pages read only under the accepted committed read gate,
  then validate the frozen contracts.
- Host RPC dispatch validates ingress and result before the P2 response write.
- Pending duplicates are not re-executed; durable replay remains owned by P2.
- Missing P0/P1/P2/checkpoint-stream seams are recorded in the scoped Delta at
  `coordination/PPA-03/job-rpc/NW-P3-JOB-RPC-02-composition-delta-v1.json`.

## Limited remediation

The sole remediation over candidate `27f3339a` is bounded to
`NW-P3-RPC-SOL-F-001..006`:

- batch dispatch no longer consumes P2's destructive `drain_events`; it requires
  non-destructive peek/dispose and removes only a persisted success;
- every local command identity is validated before an authority write;
- concurrent creation reports `replayed=true` only when proven and otherwise
  reports `null`, pending an atomic P1 disposition;
- checkpoint/stream extensions are typed and bound to Workspace, Job, Step and
  source Attempt, with contract, uniqueness and ordering checks;
- Host handlers return a side-effect-free prepared result; it is validated with
  the exact request before the transaction callback and any P2 ACK;
- Attempt start requires an atomic P1 `AttemptStartBinding`; one-shot secrets and
  method/Attempt/epoch/payload-hash reconciliation remain explicit integration
  dependencies rather than inferred state.

The Finding-ID remediation evidence is recorded at
`coordination/PPA-03/job-rpc/NW-P3-JOB-RPC-02-remediation-closure-v1.json`.

## Knowledge evidence

- ID: `kb-plotpilot-pluginized-nw-p1-core-http-runtime-seam-blocker-20260828`
- Status: `draft`
- SHA-256: `14936673ecdd5696d01f520780ebdc47a29b59dcb9bc39a7374a15bc9581a52d`
- Path: `C:\Users\Administrator\Documents\Local-KnowledgeBase\20_项目档案\PlotPilot-Pluginized\交付记录\2026-08-28-NW-P1-CORE-HTTP-01-runtime-seam-blocker.md`
- Effect: do not fabricate an adapter seam or second authority.

## Validation

Raw results are recorded in
`coordination/PPA-03/job-rpc/NW-P3-JOB-RPC-02-validation-raw.txt`.
The source node does not self-review and does not declare acceptance PASS.
