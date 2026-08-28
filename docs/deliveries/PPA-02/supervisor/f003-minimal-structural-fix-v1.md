# NW-P2-SUP-F-003 minimal structural correction

## Identity

- Exact parent: c31640d5826d3f24b4e606442062fd6736d55cf6
- Parent tree: d3d5a55f663127b0bc2d0370aec4155dd8022c0a
- Replacement review SHA-256: 416759bc7fc03c4733d33773b622eb9b91b352c9f004debb0877ff1413b11ef3
- Original Manifest SHA-256: 1deba5f9fc087590c99bb7fe47d7adff98c867d8ba3df492d90d379447f58e34
- Review state: submitted to replacement reviewer 01a0460d-1572-74b0-877d-d7268d127221 for the single-ID recheck. This source task does not declare Sol acceptance or merge eligibility.

## Minimal structural change

The claim cleanup guard now covers route/session/handshake setup, initial monotonic clock sampling, retain identity, lifecycle key, callback gate and dispatch closure. processes.start() is called only after those pre-spawn values succeed. Any exception through start() uses the existing exact _release_or_enqueue() path and then re-raises the original exception.

No public contract, SDK, P0 composition, process lifecycle policy, dependency or unrelated Finding was changed.

## Decisive failure window

test_retain_id_failure_retries_exact_claim_before_any_spawn covers:

1. retain_id_factory raises the exact sentinel;
2. authority release returns false or raises;
3. the original sentinel object is preserved;
4. no process starts and no record/current status exists;
5. the exact fence/lifecycle is present in the pending release queue;
6. one tick() retries that same identity and clears the authoritative claim.

## Raw validation

- Focused test: 2 passed in 0.60s
- Supervisor suite: 122 passed in 4.69s
- Complete P2 suite: 154 passed in 4.76s
- Compileall: exit 0, no diagnostics
- Ruff: All checks passed!
- Contract manifest: contract manifest is deterministic (137 files)
- git diff --check: exit 0; only LF-to-CRLF advisories
- Raw directory: docs/deliveries/PPA-02/supervisor/evidence/f003-raw/
