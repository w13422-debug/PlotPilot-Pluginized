# NW-P2-PROMPT-SKILL-RUNTIME-G2 source candidate

## Identity and boundary

- Task: `WAVE-D-G2-NW-P2-PSR-SOURCE-01`
- Exact base and parent: `8e3d3a7268e0d28559a7212790e3ab9f22c1453a`
- Branch: `codex/nw-p2-prompt-skill-runtime-g2`
- Frozen rejected head is not used and must not be an ancestor.
- Delivery is one direct-child source commit; donor-local push remains `DISABLED`.

## Delivered behavior

- Closed `model-receipt/v1` decoder rejects missing/extra members, type/nullability drift, non-finite values and invalid self-hashes; JSON values are recursively immutable.
- Provider/Profile/Release, invocation, request/output, lifecycle, token/cost/retry and metadata are bound to the frozen Skill invocation and Job/step context before attribution.
- Skill attribution requires an authoritative immutable ModelReceipt Asset; the former local-success and unsubstantiated `participated`/`model_claimed` paths are stopped.
- SQLite migrations execute in the same SQLite engine as a temporary reference database. `sqlite_master` and table/FK/index PRAGMA snapshots are compared fail-closed inside one transaction; fake ledgers, weak pre-created tables and SQL-text casefold compatibility are rejected.
- Durable release/chain/receipt records retain immutable release identity, composite FKs, SAVEPOINT rollback, restart revalidation, CAS active revision and deterministic retry/reconciliation behavior.

## Finding coverage

- Closes inherited `NW-P2-PSR-SOL-F-004` (authoritative ModelReceipt attribution) and `NW-P2-PSR-SOL-F-009` (engine-level SQLite structural equivalence).
- Preserves CLOSED: F-001, F-002, F-003, F-005, F-006, F-007, F-008, F-010, F-011, F-012 and F-013.

## Validation

Raw outputs and command exit evidence are under `evidence/raw/` in this directory. The final source run used the repository build virtualenv with an isolated temporary dependency path (no root lock or dependency files changed):

1. `python -B -m pytest -p no:cacheprovider tests/p2-plugin-platform/prompt-skill-runtime -q` — 87 passed.
2. `python -B -m pytest -p no:cacheprovider tests/p2-plugin-platform/test_prompt_skill_runtime.py -q` — 4 passed.
3. `python -B -m pytest -p no:cacheprovider tests/p3-execution/test_job_execution.py -q` — 4 passed.
4. `python -B -m pytest -p no:cacheprovider tests/p1-core -q` — 158 passed, 1 skipped.
5. Ruff, compileall and `git diff --check` — passed.

No browser, GUI, Tauri, desktop, EXE, installer or final-product gate was run. P0 public contracts, SDK, `app.py`, root manifests/locks and app mount remain untouched. This source record is not a self-review or merge-eligibility decision.
