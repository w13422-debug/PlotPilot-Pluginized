# P4-INT-001 — Job Drawer API integration gate

- Baseline: `42123d1a5126bb2bef31304b0498e2e7def9183e` (`M0-OPEN-R4`)
- Consumer: P4 WebUI
- Required provider: P3 Execution, assembled by P0

P0 v1 currently provides typed `job-snapshot/v1`, `job-event-page/v1`, and
`sse-recovery/v1` schemas/fixtures, but no product HTTP/SSE routes or mutation
endpoint matrix for listing/re-attaching jobs and issuing cancel/resume/retry
intents. P4 therefore implements the fixed drawer and exact presentation model,
but intentionally leaves it empty at runtime rather than inventing an endpoint,
permanent mock, or product data.

Integration needs:

1. list/snapshot route for active jobs, including refresh re-attachment;
2. SSE URL, cursor transport, gap signal and snapshot-replace response;
3. durable cancel/resume/retry operation endpoint and operation-key placement;
4. HTTP error/status mapping for stale revision and terminal races.

Once P3/P0 publish these routes, P4 will add the API adapter and bind the current
`TaskDrawer` events without changing the frozen job state semantics.
