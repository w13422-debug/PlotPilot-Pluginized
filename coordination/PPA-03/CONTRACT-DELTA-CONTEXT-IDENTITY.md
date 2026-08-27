# Contract Delta: durable operation `context_identity`

- Status: clarification requested; no public contract edited.
- Affected surface: every durable Host mutation, initially `host.job.complete/v1`.
- Evidence: the v1 SDK operation ledger keys rows by
  `(context_identity, method, operation_key)`, while the JSON schemas and RPC method matrix do not
  freeze the canonical byte/string construction of `context_identity`.

P3 therefore does **not** synthesize a new identity from Job/Step/Attempt/epoch.  Its execution
service requires the RPC Host adapter to supply the already-canonical identity and persists it
verbatim.  P0 must either confirm the composition rule as an existing v1 interpretation or publish
the clarification through the contract-owner process.  Work that invents or serializes this value
inside P3 is stopped.
