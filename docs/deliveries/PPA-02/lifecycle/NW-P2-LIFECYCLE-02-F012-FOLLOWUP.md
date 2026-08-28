# NW-P2-LIFECYCLE-02 — F-012 formal migration follow-up

## Fixed identity

- Parent: `09f1120062b15e0d55fad58bdf9cfbbeee44abb0`
- Replacement Sol review SHA-256:
  `d92e06e5c4b1f5a3ac6fdb545d6718dce937fca6d283a5bd8f4f4a7a0f10eedf`
- Sole scope: `NW-P2-LC-F-012`

## Source response

The exact formal `LIFECYCLE_MIGRATIONS` SQL now performs
`INSERT OR IGNORE INTO p2_plugin_generation_pointer(singleton) VALUES(1)`
immediately after creating the pointer table.  No second initializer or public
contract was introduced; the existing test bootstrap continues to consume the
same idempotent schema statements.

`test_f012_formal_migration_initializes_pointer_once` applies the exported
migration to an empty SQLite database through the production `MigrationRunner`
without calling `initialize_standalone_schema_for_tests()`.  It then constructs
`LifecycleRepository`, calls `generation_state()`, reapplies the exact migration
and proves that the singleton row count, values and migration-ledger row remain
stable.

This delivery records the source response only.  It does not mark the Finding
closed and does not claim merge eligibility; the replacement Sol reviewer owns
that decision.
