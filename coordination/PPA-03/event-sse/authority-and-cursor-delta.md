# NW-P3-EVENT-SSE-03 scoped authority/cursor Delta

This Delta is limited to the three frozen remediation Findings. It does not
change an authority schema, public contract, generated SDK or shared Job
module.

## Nullable global Core Event authority dependency (F-002)

`core-event/v1` permits `workspace_id: null`, while the accepted
`execution_core_event.workspace_id` authority column is `TEXT NOT NULL`.
The owned source therefore fails closed before INSERT when a schema-valid
global Core Event has no Workspace. It does not synthesize a Workspace ID or
create a second store.

**Authority-owner Delta:** the migration/authority owner must define nullable
storage for `execution_core_event.workspace_id` and verify every affected
index, repository query, backup, restore and recovery path against global and
Workspace-scoped events.

**Stop condition:** global Core Event persistence and subscription assembly
remain stopped until that authority migration is accepted and this source's
fail-closed guard is replaced by tests against the accepted nullable schema.
Workspace-bound Core Events remain supported.

## Job page cursor conversion dependency (F-003)

`job-event-page.next_job_event_seq` is the **next unread sequence**. The Event
store and SSE adapter accept exclusive `after_job_event_seq` (return rows with
`job_event_seq > after_job_event_seq`). P3 Job RPC/assembly must therefore use:

```text
after_job_event_seq = next_job_event_seq - 1
```

The owned `next_job_event_seq_to_after` helper implements and validates this
conversion. Directly passing `next_job_event_seq` as `after_job_event_seq`
would skip one durable Event.

**Assembly-owner Delta:** `NW-P3-JOB-RPC-02` must call the conversion at the
page/RPC boundary and add a page-follow-up test proving the first unread Event
is not skipped.

**Stop condition:** the page-to-RPC continuation path remains unassembled
until that conversion test is accepted. Event storage, direct exclusive
cursor replay and immutable page creation remain available.

## Shared read seam

No shared repository change is required. The owned Event module opens a
dedicated SQLite connection and explicit read transaction, then shares that
one pinned snapshot across high-water, replay floor, retained Events,
aggregate projection and terminal anchors. The shared repository remains
read-only to this node.
