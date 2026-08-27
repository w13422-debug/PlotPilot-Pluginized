# P4-INT-004 — P0 binding adjudication acknowledged

P0 reports that the adjudication for `PPA-01-CD-001` is recorded in
`coordination/PPA-00/contract-decision-PPA-01-CD-001.json` and
`docs/contracts/adr-041-core-api-contract-v1.md`.

Those records have not yet arrived on P4's current integration ancestry. P4
therefore acknowledges the decision without copying it, importing types from a
different worktree, or implementing endpoints from prose. Core authority,
Publication result, and Asset metadata/range consumption remains stopped until:

1. P0 publishes the closed v1 contract families;
2. P0 provides the new integration head;
3. P4 has a clean working tree and successfully performs the instructed
   `git merge --ff-only <integration-head>`;
4. the public generated TypeScript contracts are then consumed read-only.

Batch 4 keeps asset-backed renderers and Candidate preview behind explicit
authority boundaries and contains no wire DTO.
