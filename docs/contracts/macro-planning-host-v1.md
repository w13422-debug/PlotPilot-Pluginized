# Macro-planning host contracts v1

## Boundary

This slice freezes configuration and Project-planning data at the Host SDK
boundary. It is validation-only: it adds no route mount, runtime handler,
database object, plugin implementation, UI, service lifecycle, or build step.
Execution transport and execution-result authority remain outside this slice.

The authoritative sources are `generate_contract_schemas.py`,
`generate_corpus.py`, and `generate_contract_manifest.py`. Checked-in JSON is
their deterministic output and all three generators support drift-detecting
`--check` operation.

## HTTP surface

| Route ID | Method and path | Request | Success | Error |
| --- | --- | --- | --- | --- |
| `model-secret.put` | `PUT /api/v2/core/secrets/{secret_id}` | `model-secret-put-command/v2` | `model-secret-put-result/v2` (200, 201) | `model-secret-http-error/v2` |
| `model-profile.revise` | `POST /api/v2/core/model-profiles/{profile_id}/revisions` | `model-profile-revise-command/v2` | `model-profile-revise-result/v2` (201) | `model-profile-http-error/v2` |
| `workspace-plan.select` | `POST /api/v2/core/workspaces/{workspace_id}/plans:select` | `workspace-plan-selection-command/v2` | `workspace-plan-selection-result/v2` (200) | `workspace-planning-http-error/v2` |
| `project-planning.get` | `GET /api/v2/core/workspaces/{workspace_id}/project-planning` | `project-planning-query/v2` | `project-planning-availability-result/v2` (200) | `workspace-planning-http-error/v2` |
| `project-planning.start` | `POST /api/v2/core/workspaces/{workspace_id}/project-planning` | `project-planning-start-command/v2` | `project-planning-start-result/v2` (201) | `workspace-planning-http-error/v2` |

Every route is Host-owned. The Python HTTP adapter requires a trusted parsed
`path_params` mapping for all five routes. Its keys must exactly equal the
route's `path_identity`, and each value must equal the identity represented by
both request and response. Missing, additional, empty, or mismatched values are
rejected before an exchange can be accepted.

## Six schema documents

All objects in these Draft 2020-12 documents are recursively closed, and every
top-level variant has a unique `schema` discriminator:

1. `model-config-command-query-v2`
2. `model-profile-revision-v1`
3. `model-planning-http-error-v2`
4. `project-planning-command-query-v2`
5. `project-planner-runtime-input-v2`
6. `project-planner-model-output-v1`

### Secrets and model profiles

Only `model-secret-put-command/v2` has a raw `value`. Secret PUT output uses a
structural no-reflection rule: the closed success object contains only its fixed
discriminator, request-bound operation/Secret identities, server booleans, and
the exact `secret://{secret_id}` reference. Error identity and operation key are
request/path-bound, `retryable` is false, and every error code has one fixed
message. Additional/nested echo fields, altered references or identities, and
dynamic error text are rejected. Coincidental equality between a submitted
value and legitimate fixed/identity text remains valid because wire objects do
not carry runtime string provenance. Fixed-message provenance is an exchange
check with route/status context; shape-only parsing remains compatible with the
frozen positive fixture bytes.

All `api_key_ref` values use the Host opaque-reference grammar rooted at
`secret://`. Raw key text, aliases, leading/trailing whitespace, empty path
segments, query strings, and fragments are invalid. A model endpoint must be a
whitespace-free HTTP(S) URL without user information, query, or fragment.

P0A freezes wire whitespace independently of language/runtime character
classes. The exact set is U+0009..U+000D, U+001C..U+0020, U+0085, U+00A0,
U+1680, U+2000..U+200A, U+2028, U+2029, U+202F, U+205F, U+3000, and U+FEFF
(30 code points). Endpoints reject every member anywhere. Model names reject a
member at either boundary while retaining allowed internal content. HTTP error
messages and every Planner output role must contain at least one code point
outside this set. Schema, Python, and Node use this list directly rather than
runtime-dependent whitespace helpers.

`model-profile-revision/v1` is append-only shaped: revision 1 has a null
parent, and every later revision names a parent. `revision_hash` is the JCS
SHA-256 identity of the exact profile ID, revision ID/number/parent, complete
provider configuration, and creation timestamp under the
`model-profile-revision/v1` domain prefix. No raw secret contributes to that
payload.

P0A follows the Draft 2020-12 mathematical definition of `integer`, not Python
native-type identity and not a token-spelling rule after JSON parsing. One SDK
normalizer accepts an `int` or finite integral `float` (never `bool`), enforces
the field bounds, and writes a canonical Python `int` into a defensive copy
before semantic comparison, JCS hashing, or return. Thus `1`, `1.0`, and `1e0`
are equivalent, as are `9007199254740991` and `9007199254740991.0` where that
maximum is allowed; caller-owned objects retain their original representation.

The five authority fields share the JSON safe-integer ceiling
`9007199254740991`. `revision_number`, Plan-result `workspace_revision`, and
both `writer_epoch` fields range from 1 through that ceiling;
`expected_workspace_revision` ranges from 0 through that ceiling. The three
integer model options retain their narrower bounds: `max_output_tokens` is
1..10,000,000, `timeout_seconds` is 1..86,400, and `max_retries` is 0..16.
Booleans, non-finite or non-integral values, values below the field minimum,
and overflows are rejected before hashing or semantic use. Node applies the
same field bounds with `Number.isSafeInteger`; it does not attempt to recover
or reject an equivalent lexical spelling after `JSON.parse`.

`contracts/corpus/macro-planning-host-v1/integer-representations.json` is the
single generated raw-token source. Its 34 vectors (19 accepted, 15 rejected)
cover all eight fields, equivalent integer/decimal/exponent spellings, zero and
field minima, booleans, non-integral values, non-finite exponent overflow, and
field/safe-domain overflow. Python and Node execute that same file. Its source
SHA-256 is `2d9c72efdf8f593deb399567557229f6bcbccad1c95e961d01e819809bbbf88b`;
the shared normalized-result/hash digest is
`d4cfe7ec27c052badd2f67723fa5a4123becd60b6c1be11ce37b07b58f00f13d`.

### Plan selection and planning start

Plan selection is always `selection_mode: "explicit"`. Its command carries
Workspace revision CAS, the current Plan revision ID/hash pair, active
Generation identity, selected Plan revision ID/hash, and selected model-profile
revision ID/hash. A successful non-replayed result advances the Workspace
revision exactly once and binds every selected and previous value. The
contracts neither rank Plans nor switch one automatically.

Planning availability is fail-closed: `available` is true if and only if
`reason` is `ready`. Other reasons name the missing Host fact without embedding
secret values or runtime authority.

A browser planning-start command contains only:

- discriminator, operation key, and Workspace ID;
- `requested_mode: "plugin"`;
- Project Brief document ID;
- nested expected Project Brief revision ID and content hash.

The Brief tuple is an exact CAS. Caller-supplied Job, snapshot, model-profile,
Generation, writer, attempt, or worker authority is rejected by closure. The
result contains only server-derived operation/Workspace, Job, snapshot
ID/asset/hash, writer epoch, and idempotence fields, and it is bound to the
request operation and Workspace.

### Planner input and output

`project-planner-runtime-input/v2` is a Host-frozen value containing the
Workspace and writer epoch, exact Project Brief revision, exact independent
setting/bible/outline target revisions, selected Plan/model profile, pinned
planner and prompt-skill releases/package hashes/settings, and `input_hash`.
The hash is JCS SHA-256 over every field except itself under the
`project-planner-runtime-input/v2` domain prefix. Cross-Workspace references,
duplicate target documents, legacy generated/source-reference/snapshot/
execution fields, and caller attempt/worker identity are rejected.

`project-planner-model-output/v1` contains exactly `setting`, `bible`, and
`outline` in addition to its discriminator. Each is an independent non-blank
string. The value grants no acceptance, publication, merge, or Plan-selection
authority.

## Publication and packaging evidence

The positive fixture and golden file are byte-identical. The versioned corpus
retains 46 named object-level rejection cases and their stable digest, executed
independently by Python and Node; the 34 raw integer vectors are counted and
hash-bound separately.
`contracts/corpus/manifest-v2.json` lists every additive corpus byte while
retaining the frozen v1 router identity. `contracts/manifest-v2.json` contains
one normalized, unique record for every actual classified contract file and
verifies every size and SHA-256 digest. The SDK wheel includes the authoritative
schemas and exports validators that return defensive deep copies.
