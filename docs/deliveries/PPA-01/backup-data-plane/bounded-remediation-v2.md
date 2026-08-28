# NW-P1-BACKUP-DATA-01 bounded remediation v2

This is the single bounded source remediation on parent
`e05892c0285b23d3c17e8d077ea30d00a685f8ba`. It responds one-for-one to the
frozen IDs `NW-P1-BDP-F-001..015`. The source task does not mark any Finding
closed and does not claim central PASS; the same authoritative reviewer must
decide each ID.

## Authority reuse

- `SqliteCoreSnapshotAdapter` reads only the frozen online-backup image of the
  existing Core SQLite authority. It serializes deterministic workspace
  aggregate state and publishes that state through the existing `AssetStore`.
- The Core event high-water is not guessed from `backup_epoch`. It is an
  independent durable value supplied by `BackupBarrierPort`, copied into the
  snapshot, and checked for exact equality on capture.
- `PackageStoreArchiveAdapter` reads verified `PackageStore` entries and emits a
  fixed-member-order, fixed-time, fixed-mode, `ZIP_STORED` archive. SDK semantic
  `package_hash`/`release_id` remain distinct from the archive SHA-256 recorded
  in the bundle file list.
- P2 and P3 remain injected ports. Their full runtime composition is P0-owned.

## Verification and restore boundary

Creation and replay rebuild the workspace set from `core/core.db`, validate the
CoreSnapshot scope, state Asset binding, strict Asset column/JSON scanning,
closed `AssetMetadata`, exact package semantics, a unique Core database role,
and a closed internal receipt. Ephemeral barrier tokens do not enter the
receipt.

Restore compares the request source identity with the verified manifest before
copy or replay. Lexical root and component checks reject reparse points before
resolution and before publication. Marker v2 binds the full target parent,
operation, generation, binding, and `staging`/`published` state. Cleanup accepts
only the exact hidden generation path. The `verified` callback is followed by a
complete bundle/report/marker revalidation before non-replacing rename.

Backup now has symmetric interrupted-generation cleanup. If publication
completed but acknowledgement was interrupted, an identical request reuses the
fully verified existing bundle; a different request fails closed.

## Finding evidence

The exact implementation/test mapping is recorded in
`coordination/PPA-01/backup-data-plane/remediation-mapping-v2.json`. Tests cover
the production Core adapter, high-water mismatch in both directions, semantic
package identity versus archive hash, P3 role forgery, state Asset mismatch,
scope reconstruction, scanner and metadata closed schemas, restore source and
cleanup boundaries, control/target reparse points, callback mutation,
random-token determinism, receipt extensions/time drift, and interrupted backup
cleanup/publication recovery.

## Remaining composition dependencies

1. P0/P3 must provide the durable, monotonic barrier implementation and hold it
   across all contributors.
2. P0/P2 must compose the durable current/LKG/release/projection provider.
3. P0/P3 must compose quiesced plugin-data SQLite/files and declared Asset roots.

No public contract, SDK, root manifest, lock file, or non-owned Core authority
file is changed by this remediation.
