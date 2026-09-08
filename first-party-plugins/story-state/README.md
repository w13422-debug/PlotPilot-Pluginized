# Story State

`com.plotpilot.story-state` packages the reviewed Story-State runtime as an
installable first-party worker. It validates Story-State proposals, emits only
Candidate batches through the Host/Core ports, and never publishes a Candidate.

Post-chapter settlement accepts only the closed
`post-chapter-story-state-request/v1` publication receipt supplied by Chapter
Workflow. It returns source-bound Candidate bundles to the injected Core staging
seam. Duplicate notifications converge; conflicting operation/publication
identities fail closed.

Projection readback uses the frozen `story-state-projection-input/v2` Core query
and an injected Asset reader. The derived projection is disposable and does not
create a database, ledger, publication authority, or in-memory truth source.