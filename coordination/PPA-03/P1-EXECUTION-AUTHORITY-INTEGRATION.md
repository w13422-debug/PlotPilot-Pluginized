# P3 → P1 execution authority integration gate

- Status: open integration dependency; bundle/candidate completion slice stopped fail-closed.
- P3 base: `42123d1a5126bb2bef31304b0498e2e7def9183e` (`M0-OPEN-R4`).
- Affected method: `host.job.complete/v1` when `result_bundle_asset_id != null` or
  `candidate_stage_operation_key != null`.

P3 needs the real P1 Job repository and transaction port promised by the task book to atomically validate the
immutable Bundle Asset, RunSnapshot/producer/lease/receipt identity, stage or discard Candidate
rows, materialize provenance, mutate authoritative aggregates, and append the matching Core
Event.  No public v1 contract change is requested.  Until this port is integrated, P3 does not finalize any terminal outcome and exposes no production SQLite Job repository. The dependency-ready pieces are frozen edge tables, strict verified-Snapshot request resolution over a P1-owned port, and the durable byte-preserving Host operation ledger. P3 does not introduce a second authority or a production fallback.
