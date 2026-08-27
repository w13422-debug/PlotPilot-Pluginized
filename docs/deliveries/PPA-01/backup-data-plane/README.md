# NW-P1-BACKUP-DATA-01 delivery

## Scope

This slice implements the P1 backup data plane without changing public contracts,
the root dependency set, or P0 composition:

- SQLite online backup into an operation-owned staging generation;
- Asset closure derived from the frozen Core database, CoreSnapshot state
  Assets, explicit Core/P2/P3 contributor roots, and staged P3 SQLite files,
  then verified against the existing content-addressed `AssetStore` layout;
- accepted `core-snapshot/v1` ingestion through a mandatory injected authority
  port, rather than fabricating the currently absent event high-water/state
  Asset authority;
- canonical `backup-bundle/v1` plus a deterministic internal receipt binding the
  barrier, Core snapshot, SQLite bytes, Asset closure, and file manifest;
- restore-to-new-root staging with complete pre/post-copy verification,
  unique-generation interrupted staging, exact idempotent replay, and no root
  switch.

## Consistency boundary

`BackupDataPlane.create_backup()` requires all four ports and has no production
fallback:

1. `BackupBarrierPort` holds the durable epoch across every contributor.
2. `CoreSnapshotPort` returns a valid `core-snapshot/v1` bound out-of-band to the
   frozen database hash, frozen-database Asset roots, exact workspaces, current
   Core `1.2.0` compatibility, additional required Assets, and (for workspace
   mode) that same scoped snapshot's authoritative hash.
3. `GenerationBackupPort` returns compatible P2 current/LKG/release/projection
   state, declared Asset roots, and package files bound to the same barrier and
   Core snapshot. Package descriptors form an exact release/package-hash/file
   bijection.
4. `PluginDataBackupPort` returns compatible P3 declared Asset roots and frozen
   files bound to the same barrier and Core snapshot. Staged plugin SQLite files
   are integrity-checked and rescanned for Asset references before closure.

The coordinator runs SQLite online backup first, closes and fsyncs the copy, and
then captures all contributors before calculating the union of Asset roots.
Every Asset object is fully read and rehashed, and its manifest hash must equal
the digest encoded by its Asset ID/path; ambient AssetStore objects are excluded.
Contributor files are copied only when their declared size/hash matches, and
SQLite contributors also pass `integrity_check` and `foreign_key_check`.

The current accepted repository does not own a durable global backup epoch,
Core event high-water/aggregate state-Asset builder, P2 current/LKG persistence,
or P3 quiesce/composition. Therefore those capabilities remain explicit P0
composition dependencies. Missing or mismatched ports fail before publication.

## Publication and restore

- Every call assembles output in a unique sibling staging generation. The UUID
  is internal and never affects the deterministic manifest or receipt.
- Existing targets are never replaced.
- A canonical marker remains after nonreplace publication, eliminating a
  marker-removal crash window. Explicit cleanup deletes only one interrupted
  generation whose operation ID, generation, target, binding, and self-hash all
  match, and refuses symlink/junction/reparse content.
- A restore first verifies the sealed source bundle, copies into a new staging
  root, verifies the copied file set, hashes, SQLite databases, Core snapshot,
  receipt, package bindings, compatibility evidence, and exact Asset closure
  again, then writes `restore-report/v1` with `state=restore_ready`.
- Completed replay revalidates the report contract and compares the source and
  target bundle hash, full receipt, publication marker, and every derived report
  field before returning `reused=True`.
- No method in this slice changes a current-root pointer or returns `switched`.

## Reuse decision

Thin adaptation was selected:

- Python `sqlite3.Connection.backup` for the online database image;
- the accepted P1 `AssetStore` content-address/path rules;
- existing SDK `canonical_bytes`, `hash_jcs`, `verify_backup`,
  `verify_core_snapshot`, `verify_restore_report`, and path normalization.

The local source-tool selector returned unrelated video/Git helpers. A GitHub
search was skipped because Python stdlib plus current accepted repository code
already provide the exact verified primitives; importing another dependency
would add supply-chain and compatibility cost without reuse benefit, and this
node has no root dependency authority.

## Validation snapshot

The final raw command results are recorded in `validation.json`. This source
node does not claim central PASS or merge eligibility; fresh Sol/max review and
central integration remain separate gates.
