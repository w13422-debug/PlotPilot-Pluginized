# NW-P2-LIFECYCLE-02 delivery

## Identity and authority

- Base: `7805be2c2566aa1e63d3cdc6784a84f1aea8f463`
- Branch: `codex/nw-p2-lifecycle-02`
- H_B acceptance SHA-256:
  `05d45d8fe8edae1359321af412af0184f5db033e2c9736f9be024e4c4a0dfe63`
- Donor-local push: `DISABLED`
- No dependency, root manifest/lock, public contract, public SDK or P0
  integration file was changed.

## Reuse decision

The implementation directly reuses the accepted package verifier,
`InstallStager`, `PackageStore`, complete SDK Plan verifier and Settings
receipt gates.  It thin-adapts the existing P1 `BEGIN IMMEDIATE`/CAS and
canonical request-hash patterns into the owned lifecycle package.  The local
source-tool selector returned unrelated video/Git/device utilities, so copying
one would add dependencies and no lifecycle value.  No external GitHub helper
was searched because the repository already contained exact verified
primitives and the task forbids unnecessary dependency/public-contract drift.

Reviewed local knowledge evidence:

- `C:\Users\Administrator\Documents\Local-KnowledgeBase\20_项目档案\Novel-Agent\交付记录\2026-08-24-Novel-Agent-TXT-EPUB复制文本导入清洗设计.md`
  (`fea52b397845b1ec98d16e42f6c92436d397a14b9616fb6ac381408d9d2161dd`)
- `C:\Users\Administrator\Documents\Local-KnowledgeBase\20_项目档案\Novel-Agent-Model-Factory\交付记录\2026-08-12-Model-Factory-选型Gold审核纵切.md`
  (`9516caaab574e0a86be39b49e8a658318ba1aa2614bf952aa149393a33b179fc`)

## Delivered source behavior

### Install / reconciliation

- One SQLite authoritative lifecycle row per stable
  `install_operation_id`, bound to a canonical full intent hash.
- Strict public transition adjacency, semantic field checks and CAS updates.
- Every nonterminal attempt is an independent durable reconciliation fence.
  Pre-activation crashes preserve the usable current/LKG and do not misuse one
  global safe-mode bit; explicit safe mode is reserved for post-activation
  pointer disagreement or unavailable rollback execution.
- The generic transition API has closed per-edge field allowlists and contains
  no qualification, current-pointer, LKG-pending or LKG-promotion authority
  edge.  Those states are reachable only through dedicated transactional
  methods that consume the required verifier/guard/pointer side effects.
  Frozen base/current/LKG identities and rollback fields cannot be rewritten,
  and an activated attempt cannot jump directly to `failed`/`superseded`.
- Staging markers remain byte evidence only.  Replays cannot downgrade
  `package_published` to `verified`.
- Package rename/registry crash windows adopt only the exact staged identity;
  no directory scan can auto-activate an orphan.
- Registry mutation uses both the existing process lock and a standard-library
  OS file lock to avoid cross-process lost updates.

### Generation / rollback / LKG

- Immutable Generation storage plus atomic current/LKG pointers.
- Commit and rollback CAS freeze both base current and base LKG, recheck every
  member's pending pin/retire epoch/logical and physical package availability,
  and require the P1-owned Event writer in the same transaction.
- One durable rollback token/attempt per install; restart reuses the token and
  cannot mint another.
- Missing base/LKG or failed recovery enters persistent safe mode.
- Pre-commit isolated health does not assign the public qualification ID.  LKG
  promotion occurs only from `lkg_pending`, consumes a release-bound immutable
  result from the required host qualification authority, and binds the restart
  health Asset/stable qualification ID exactly once.

### Shadow / Settings

- Shadow data generations use independent lease ID, owner, epoch and expiry
  fencing inside one `BEGIN IMMEDIATE` transaction.  Every mutation rechecks
  the complete fence and affected-row count; expired/released leases receive a
  new epoch and stale workers are rejected.
- Shadow creation requires `env_prepared`, the exact release in the frozen
  canonical install request, installed/package-present retirement state and
  the active install-owner pin at the same retire epoch; invalid-state or
  pinless creation leaves no orphan row.
- Only apply → verify → health-qualified shadow data can advance the public
  install transition to migrated.
- The closed Settings migration language supports only
  rename/copy/remove/set_default RFC 6901 operations, creates a new value and
  never runs arbitrary migration code.
- Activation rechecks revision/release/schema bindings; receipt binding
  mismatch uses the frozen `1011` contract code.

### Plan and retirement

- Plan resolution uses the complete verifier, exact manifest SemVer,
  capability/Data-interpreter gates and strictly preserved user order.
  Enabled Data bindings require an injected immutable Asset resolver, run the
  published `verify_data_bundle()` hash/schema verifier, bind plugin/release,
  package and format identities, and include `bundle_hash` in the immutable
  resolution/Plan intent hash.
- Multiple different plugins for the same capability remain enabled; there is
  no recommendation, automatic switching or voting.
- `prepare_plan_switch` requires explicit user confirmation and produces a
  deeply immutable deterministic intent bound to opaque target/current Plan
  revision IDs, expected Workspace revision, canonical Plan hash and current
  Generation.
- Release retirement/pin acquisition linearize in one SQLite transaction.
  Current/LKG, every target/rollback-base Generation member, and every active
  published pin block physical deletion; `retiring` rejects every new
  executable pin with `1013`.
- First retirement CAS requires the P1 Core Event writer.  Completion requires
  a release-bound immutable Candidate/Publication barrier decision before the
  package remover runs; blockers persist as internal `needs_attention` while
  public state remains `retiring`.
- Exact package bytes can be removed after the barrier while the immutable
  registry identity/tombstone remains for historical provenance.  An already
  absent exact path is an idempotent successful deletion postcondition.

## Required Deltas

Real persistence of `Workspace.current_plan_revision_id` is stopped because
the P0-owned Core authority command cannot CAS that public field.  P2 did not
create a second pointer.  See:

`coordination/PPA-02/lifecycle/P0-PLAN-SWITCH-CAS-DELTA.md`

P1 must also supply the existing-contract, host-internal Event,
qualification-authority and Candidate/Publication barrier composition ports.
Physical retirement completion and the affected production authority calls
remain stopped until that integration exists:

`coordination/PPA-02/lifecycle/P1-LIFECYCLE-AUTHORITY-PORTS-DELTA.md`

Neither Delta adds a dependency or lets P2 write a second authority.

## Test coverage

Owned tests cover:

- all eleven nonterminal install crash windows and independent restart fences;
- package publish/registry orphan adoption and repeated request conflict;
- two-connection concurrent Generation CAS;
- atomic current/rollback/retirement Event-writer rollback;
- durable rollback token reuse and no-LKG safe mode;
- shadow lease expiry/epoch fencing and qualification;
- pinless shadow-creation rejection;
- explicit ordered multi-plugin Plan intent and exact-version conflicts;
- authoritative DataBundle Asset absence, tampered hash and identity mismatch;
- generic transition rejection for qualification/current/LKG authority edges;
- closed Settings migration;
- full target/base member pins and epoch recheck;
- recoverable Attempt pin barrier, authoritative retirement attention gate,
  deletion-crash replay and post-delete Publication evidence.

Raw final validation outputs are stored under `evidence/raw/` beside this
file.  This source node does not grant central PASS or merge eligibility.
