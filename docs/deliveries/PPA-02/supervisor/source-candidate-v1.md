# NW-P2-SUPERVISOR-01 source candidate

## Identity and boundary

- Project: `PPA-NW-P2-Supervisor`
- Branch: `codex/nw-p2-supervisor-01`
- Exact base: `7805be2c2566aa1e63d3cdc6784a84f1aea8f463`
- H_B acceptance SHA-256: `05d45d8fe8edae1359321af412af0184f5db033e2c9736f9be024e4c4a0dfe63`
- Donor-local push: `DISABLED`
- Delivery kind: one source candidate containing implementation, tests, coordination Delta and raw evidence.
- P0 retains all public route mount/composition and production-authority adapter ownership.

## Delivered behavior

- Content-addressed per-package venvs with exact release/package/Python marker validation, closed hash-pinned local wheel locks, isolated/no-index pip execution and non-replacing publication.
- Direct `shell=False`, `python -I` worker launch with a restricted `module:function` bootstrap and minimal ambient environment.
- Closed, length-bounded framed RPC using the published SDK decoder/verifier; strict first handshake, request-ID binding, capability identity, EOF residual rejection and unknown-message closure.
- Independent worker pin, Attempt lease and install lease identities. The injected Core authority is checked at bind/unbind and again for every Attempt/install RPC.
- Lazy start, retain/idle shutdown, exact heartbeat watchdog, lifecycle-token replacement fencing, terminate/kill/exit ordering, retryable release accounting and bounded stderr crash tail.
- Process exit becomes observable only after stdout and stderr both reach EOF. Startup callbacks are gated until the worker record exists, preventing early-exit and premature EOF races.
- Immutable worker/UI lookup rechecks the current authority snapshot and exact live owner pin on every query; UI bytes, hash, frozen route and CSP remain content-bound.

## Reuse decision

This slice thin-adapts the repository's accepted `FrameDecoder`, RPC verifier, `PackageStore` identity and release/generation contracts. Local tool-library candidates were unrelated, so no helper or dependency was copied. A GitHub search was skipped because the repository already contains the exact verified contract implementations and external reuse would add compatibility and supply-chain cost without functional gain.

## Finding closure evidence

The fresh `gpt-5.6-sol/max` reviewer froze `NW-P2-SUP-F-001..007`. After one bounded remediation and one structural simplification, the same reviewer returned all seven IDs `CLOSED` and `LOCAL_SOURCE_READINESS: PASS` against Python aggregate SHA-256 `f31be0935aa1d08f585e776d9ec6eceec0a15e264b410bb3d4f2fa95b5c0c599`.

This is local source-readiness evidence only. It is not central acceptance or merge eligibility.

## Validation

Raw outputs and exit codes are stored under `evidence/raw/`. The final candidate runs:

1. `python -B -m pytest -p no:cacheprovider tests/p2-plugin-platform/supervisor -q`
2. `python -m ruff check backend/plotpilot_core/supervisor tests/p2-plugin-platform/supervisor`
3. `python -B -m compileall -q backend/plotpilot_core/supervisor`
4. `python -B tools/integration/generate_contract_manifest.py --check`
5. `git diff --check` and `git diff --cached --check`
6. exact changed-path/write-set and Git identity audits.

## Remaining P0 dependencies

See `coordination/PPA-02/supervisor/p0-composition-delta-v1.json`. Until P0 supplies those seams, the supervisor remains an injected internal component and does not mount a production route.
