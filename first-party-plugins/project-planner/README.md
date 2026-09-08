# Project Planner

`com.plotpilot.project-planner` packages the reviewed deterministic Planner runtime as
an installable first-party worker. It freezes the selected RunSnapshot, prepares a
source-bound Candidate batch, and sends Assets, Candidate staging and terminal
completion only through the Host RPC ports declared in `plugin.json`.

The worker never opens Core storage, calls a Publication endpoint, or treats its
in-memory replay guard as durable authority. A Host/Core composition supplies the
attempt-bound planner request adapter; absent composition fails closed.

Settings are intentionally empty and versioned (`project-planner-settings/v1`).
The migration manifest has no private schema steps.