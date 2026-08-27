# PPA-03 first dependency-ready execution slice

- Accepted base: `42123d1a5126bb2bef31304b0498e2e7def9183e` (`M0-OPEN-R4`)
- Implementation commit: `0768c197fd824b5a28556899458a3d3993382020`
- Branch: `codex/ppa-03-execution`
- Contract role: consumer; no public v1 files changed.

## Delivered

- Exact frozen Job/Step/Attempt edge tables from formal design v1.2.
- Strict RunSnapshot verification before `(workspace_id, request_key)` duplicate resolution; snapshot/intent drift is `1008`.
- Durable Host operation ledger keyed by caller-supplied canonical context identity, method and operation key. It stores the first complete response frame as bytes, replays committed ACK-loss operations before lease preflight, rejects payload drift, and fences a new epoch/context.
- Explicit P1 authority gate: no production Job repository, Receipt, Core Event, or `host.job.complete/v1` terminal mutation is fabricated while P1's real transaction port is unavailable.

## Validation evidence

```text
python -m pytest tests/p3-execution/test_job_execution.py tests/contract/test_rpc_and_sdk.py -q
8 passed in 0.49s

git diff --check
(no output; exit 0)
```

Independent focused review after remediation: PASS; F-P3-01..06 CLOSED. The review explicitly does not claim terminal completion is delivered.

A broader pre-existing golden test was also probed and is not used as pass evidence:
`tests/contract/test_goldens.py::test_design_goldens_recompute_exactly` fails because accepted base lacks `contracts/golden/package/data/rules.json`. No P3 file caused or repairs that P0-owned fixture gap.

## Write-set proof

All implementation paths are within the P3 matrix write set:

- `backend/plotpilot_core/jobs/**`
- `tests/p3-execution/**`
- `coordination/PPA-03/**`
- this delivery record under `docs/deliveries/PPA-03/**`

## Open integration dependencies

1. P1 Job repository + aggregate/Event/Receipt/Candidate transaction port is required before any terminal outcome can mutate authority.
2. P0 must clarify canonical `context_identity`; P3 currently accepts it verbatim and does not invent a composition.
3. P2 worker/runtime ownership and lease allocator/CAS are required before dispatch, takeover, heartbeat or resume is implemented.

P0 integration must use a no-ff merge and retain both open gate records in `coordination/PPA-03/`.

## Bounded audit remediation

The earlier review claim was superseded by the authoritative BLOCK manifest SHA-256 `3031dac166ad6936b00d09d746e8e82e506759de50133cd146e971a9be9a86c3`. The single bounded remediation is recorded under `coordination/PPA-03/`; only its same-reviewer result is authoritative.
