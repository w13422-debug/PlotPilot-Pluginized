# P0 Delta — authoritative Workspace Plan switch CAS

## Identity

- Node: `NW-P2-LIFECYCLE-02`
- Base: `7805be2c2566aa1e63d3cdc6784a84f1aea8f463`
- Owner required: P0 public contract / Core authority
- P2 status: **affected commit slice stopped**

## Gap

`plugin-plan/v1` and `Workspace.current_plan_revision_id` are already public,
but the published Core authority command surface can update only Workspace
`title/status`.  P2 cannot durably commit a Plan switch without either:

1. writing a second Workspace Plan pointer in P2, which would be a forbidden
   second truth source; or
2. changing the P0-owned public command/SDK surface outside this node's write
   authority.

Evidence:

- `contracts/json-schema/core-authority-command-query-v1.schema.json`
  (Workspace update command fields around baseline lines 1694–1744)
- `backend/plotpilot_plugin_sdk/core_api.py` (published Workspace mutation
  helpers around baseline lines 30–39 and 235–264)
- formal design §§17.3/84.7: Plan selection is a user-explicit Workspace
  mutation; Plan revisions are immutable and exact-release conflicts fail
  closed.

## Requested P0 decision

Publish one authoritative, operation-keyed Workspace Plan switch command with
at least:

- `workspace_id`
- `expected_workspace_revision`
- `expected_current_plan_revision_id` (opaque ID or null)
- `expected_current_generation_id`
- `target_plan_revision_id` (the opaque Core revision identity actually stored
  by Workspace)
- canonical target Plan bytes/hash, with `plan_id + revision` retained only as
  content metadata rather than pointer identity
- stable `operation_key`
- deterministic replay response and conflict result

The P1-owned transaction must reload the exact target Plan revision/Asset,
verify its canonical hash, reload the fresh current Generation, resolve every
enabled binding again, and reject any release whose retirement epoch/state has
changed since preparation.  It must acquire deterministic `plan_binding` pins
for every new release before the Workspace CAS, then release the old Plan pins
in the same transaction.  Reusing a release across both Plans must never create
an unpinned window.  Workspace CAS, new/old pin exchange and operation replay
receipt are all-or-nothing.  Existing RunSnapshots remain unchanged.

## Delivered locally without crossing the gap

P2 provides `prepare_plan_switch`, which:

- requires explicit user confirmation;
- invokes the published complete Plan verifier;
- resolves all enabled bindings in exact user order;
- permits multiple different plugins for one capability;
- checks exact SemVer through the verified release catalog;
- rejects capability/Data interpreter/release conflicts;
- emits an immutable deterministic `PlanSwitchIntent`.

It deliberately does **not** persist an active Plan pointer or claim a switch
succeeded.  P0 must accept/reject this Delta before the real commit adapter is
implemented.

## No other Delta

- No dependency/manifest/lock change is required.
- Retirement, lifecycle reconciliation and qualification remain host-internal
  over already published v1 contracts; no SDK or wire expansion was made.
- Generation exact version is resolved through verified
  `release_id -> manifest.version`; `plugin-generation/v1` was not changed.
