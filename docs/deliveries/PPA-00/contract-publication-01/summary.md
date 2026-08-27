# PPA-M1 Contract Publication 01

- Reviewed implementation commit: `c99937d64ad4b34b6b4e071a10eaf3484028f5fa`
- Reviewed tree: `5a7052549d780d54d65d18aad4cdc58d51f1b8fd`
- Base: `63ee2b89b719734dd2f9d50bab12cbe0fe10e37e`
- Contract version: `1.2.0`
- Contract manifest SHA-256: `cbe9d02fc42409332151cd7905e387a46537b330b039a2d3d6798f48cbfcc024`
- Sol gate: `PASS` (`P0-CP-F-001..005` all closed)

## Published contract families

- `core-authority-command-query/v1`
- `publication-command-result/v1`
- `asset-metadata/v1`
- `plugin-ui-ingress-validator/v1`
- `operation-context-identity/v1`
- `export-current-revisions/v1`

`PPA-01-CD-001` publishes only the scoped Core HTTP/Publication/Asset public surface; P1 internal migration receipts remain internal and plugin `migration.apply` stays unchanged. `PPA-04-CD-001` is closed by P0-owned executable unknown-ingress validators without private P4 wire DTO. `P3-CD-CONTEXT-IDENTITY-001` is closed by one canonical projection/hash with pre-derivation fencing. `P6-CD-EXPORT-CURRENT-REVISIONS-001` uses the accepted immutable-Asset simplification and does not add a Host RPC.

## Golden portability

`contracts/.gitattributes` freezes byte-sensitive contract/golden JSON/TXT/SHA files to LF. `contracts/golden/package/data/rules.json` is tracked at 20 bytes with SHA-256 `54bfa55d6557dcf1a11f3e845e6492e4fe34f35a0d6751f45e0e8ae77df36e78`. A fresh temporary worktree under global `core.autocrlf=true` passed byte/hash, manifest, Python, real TypeScript, Node and cross-language gates and was removed.

## Resume boundary

P1/P3/P4/P6 may consume these contracts only after ff-only synchronization to the accepted P0 publication commit above. This batch does not grant any P2-P6 merge eligibility. P6 Export runtime remains stopped until real P1 Asset/current-pointer snapshot construction and P2/P3 framed runtime are integrated.
