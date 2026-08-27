# P4-INT-003 — PPA-01-CD-001 Core API contract version gate

P1 raised and the construction orchestrator confirmed `PPA-01-CD-001`: M0 did
not freeze Workspace/Document/Node/Revision Core HTTP command/query DTOs,
Core-native typed Publication accept/result DTOs, or Asset metadata DTOs.

P4 consequences:

- stop every slice that would guess those requests, responses, endpoints, or
  error mappings;
- do not create P4-private endpoint or DTO compatibility types;
- never interpret a Plugin UI intent as direct Publication or Core mutation;
- keep `PluginUiSession.decideIntent` as validation/deduplication only. A valid
  Core-bound intent is returned to a future versioned dispatcher and is not
  executed by the Slot Host;
- resume real HTTP integration only after P0 publishes the versioned public
  Core API contract and that contract reaches P4 through the integration head
  using the prescribed ff-only synchronization.

This is an integration dependency record, not a competing Contract Delta.
