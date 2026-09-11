# Model Provider RPC overlay v2

Status: additive P0B contract freeze
Reserved method: `model.provider.invoke/v1`
Registry: `model-provider-rpc-method-matrix/v2`

## 1. Scope and authority

This contract is a Core-to-Provider transport overlay. It does not implement
Provider dispatch, persistence, retry, recovery, secret resolution, database
state, HTTP/stdio routing, plugin discovery, UI, or publication.

The existing Provider `model-receipt/v1` is the only complete model receipt.
Its existing 27-field producer and the accepted Prompt-Skill decoder/Asset
gate remain authoritative and unchanged. This overlay adds no
`model-receipt/v2`, no standalone full receipt schema, and no second receipt
decoder.

The complete receipt stays in a Host Asset. The overlay carries only:

1. the accepted 23-field Prompt-Skill receipt anchor;
2. exact request/result identity mirrors;
3. separately named Provider transport hashes; and
4. an optional Host response Asset identity.

Nullable Provider identities remain nullable inside the canonical receipt.
The overlay never invents a profile, Provider plugin, or Provider release ID.

## 2. Reserved-method registry

`contracts/json-schema/model-provider-rpc-method-matrix.v2.json` contains one
and only one method:

```text
model.provider.invoke/v1
```

The registry fixes:

```text
authority                  = core
direction                  = host-to-provider
plugin_authority_allowed   = false
reserved_method_ids        = [model.provider.invoke/v1]
meta_profile               = attempt
terminal_states            = receipted | failed | cancelled | uncertain
terminal_states_use_jsonrpc_success = true
rpc_error_meaning          = no-verifiable-terminal-receipt
```

The method is deliberately absent from the frozen generic
`rpc-method-matrix.v1`, Prompt-Skill `rpc-method-matrix.v2`, generic `rpc.py`,
stdio worker, and discovery dispatch.

The SDK verifier rejects the reserved ID in Plugin Plan bindings, a Plan
synthesizer, capability descriptors, and plugin manifest business
capabilities. Ordinary capability IDs continue to validate.

## 3. Request

`model-provider-invoke-request/v2` is a recursively closed JSON-RPC request:

```json
{
  "jsonrpc": "2.0",
  "id": "123e4567-e89b-42d3-a456-426614174100",
  "method": "model.provider.invoke/v1",
  "meta": {
    "protocol_version": "1",
    "context": "attempt",
    "operation_id": "execute-op-1",
    "generation_id": "generation-1",
    "job_id": "job-1",
    "step_id": "step-1",
    "attempt_id": "attempt-1",
    "lease_epoch": 1
  },
  "params": {
    "schema": "model-provider-invoke-request/v2",
    "planner_context": {
      "operation_key": "execute-op-1",
      "workspace_id": "ws-1",
      "plugin_id": "com.plotpilot.prompt-skill",
      "plugin_release_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "plugin_package_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "generation_id": "generation-1",
      "job_id": "job-1",
      "step_id": "step-1",
      "attempt_id": "attempt-1",
      "lease_epoch": 1,
      "chain_id": "chain-1",
      "chain_asset_id": "asset-chain-1",
      "chain_content_hash": "1111111111111111111111111111111111111111111111111111111111111111",
      "run_snapshot_id": "snapshot-1",
      "run_snapshot_asset_id": "asset-snapshot-1",
      "run_snapshot_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "model_profile_revision_id": "model-profile-rev-1",
      "input_asset_id": "asset-input-1",
      "input_content_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    },
    "model_request_asset": {
      "asset_id": "asset-model-request-1",
      "content_hash": "<lowercase sha256 of Host Asset bytes>"
    }
  }
}
```

The 19 `planner_context` fields are exactly the non-locator fields of the
accepted Prompt-Skill `ModelReceiptIdentity`. The meta identity must equal:

```text
meta.operation_id  == planner_context.operation_key
meta.generation_id == planner_context.generation_id
meta.job_id        == planner_context.job_id
meta.step_id       == planner_context.step_id
meta.attempt_id    == planner_context.attempt_id
meta.lease_epoch   == planner_context.lease_epoch
```

`lease_epoch` uses the accepted positive IEEE-754 safe-integer domain.
`model_request_asset` is not the Prompt-Skill input Asset and no equality
between those two identities is authorized by this contract.

## 4. Result and success envelope

`model-provider-invoke-result/v2` is closed and contains exactly:

```text
schema
model_receipt_anchor
model_request_asset
provider_transport_request_hash
response_asset
provider_transport_response_hash
provider_terminal_state
```

`model_receipt_anchor` is the exact accepted 23-field Prompt-Skill shape:

```text
receipt_id, asset_id, content_hash, receipt_hash,
operation_key, workspace_id, plugin_id, plugin_release_id,
plugin_package_hash, generation_id, job_id, step_id, attempt_id,
lease_epoch, chain_id, chain_asset_id, chain_content_hash,
run_snapshot_id, run_snapshot_asset_id, run_snapshot_hash,
model_profile_revision_id, input_asset_id, input_content_hash
```

The last 19 fields must equal request `planner_context`, and
`model_request_asset` must be an exact request/result echo.

A terminal response is always the closed
`model-provider-invoke-success/v2` envelope:

```json
{
  "jsonrpc": "2.0",
  "id": "<exact parsed request UUID>",
  "result": {
    "schema": "model-provider-invoke-result/v2",
    "model_receipt_anchor": {},
    "model_request_asset": {},
    "provider_transport_request_hash": "<sha256>",
    "response_asset": null,
    "provider_transport_response_hash": "<sha256 or null>",
    "provider_terminal_state": "failed"
  }
}
```

`receipted`, `failed`, `cancelled`, and `uncertain` all represent a verifiable
terminal receipt and therefore all use JSON-RPC success. A JSON-RPC error means
that no verifiable terminal receipt formed. It reuses `rpc-error/v1`; once the
request has parsed, its `id` must be the exact request UUID and cannot be null.
Bare results, missing IDs, mismatched IDs, and mixed result/error envelopes are
invalid.

## 5. Six hash domains

These names are different semantic domains. Equality of digest values is not
used as a type system; authority and mirror checks define each domain.

| Field | Authority |
|---|---|
| `model_request_asset.content_hash` | SHA-256 of Host model-request Asset bytes |
| `model_receipt_anchor.content_hash` | SHA-256 of Host Asset bytes containing the complete canonical receipt |
| `provider_transport_request_hash` | Provider `hash_jcs("openai-compatible-request/v1", transport request)`; equals canonical receipt `request_hash` |
| `provider_transport_response_hash` | Provider `hash_jcs("model-response/v1", normalized response/error)`; equals canonical receipt `response_hash` |
| `response_asset.content_hash` | SHA-256 of Host response Asset bytes |
| `model_receipt_anchor.receipt_hash` | canonical receipt self-hash over its other 26 fields |

Host Asset hashes are never substituted for Provider transport hashes or the
receipt self-hash. The golden intentionally assigns six different values so
substitution tests cannot pass accidentally.

`response_asset` is either null or a closed `{asset_id, content_hash}` pair.
Its ID mirrors canonical receipt `response_asset_id`. A null response Asset
with a non-null Provider transport response hash is valid, notably for an HTTP
error receipt.

## 6. Canonical receipt gate

The SDK result validator requires one of:

- `canonical_receipt`: an already-authoritatively-decoded receipt view; or
- `receipt_verifier(anchor)`: a callback returning that view or the accepted
  Prompt-Skill `AttributionProof`-like Asset proof.

When the proof exposes `asset_id` and `asset_hash`, both must equal the receipt
anchor. The validator mirrors only receipt ID, receipt self-hash, terminal
state, Provider request/response hashes, profile revision ID, and response
Asset ID. It does not enumerate or decode all 27 fields.

An optional `asset_verifier(identity, role)` callback validates actual Host
bytes for `model_request_asset` and a non-null `response_asset`. This keeps
storage authority outside the overlay while preventing cross-domain hash
substitution at a composed boundary.

## 7. Exact replay

`ModelProviderOperationLedgerV2` is an in-memory contract proof only. It owns no
database or dispatch behavior. The key is `(workspace_id, operation_key)`, and
both the complete request bytes and validated result bytes must replay exactly.
Different request IDs, planner facts, Asset identities, terminal anchors, or
hashes under the same key fail with the existing duplicate-request error.
Returned values are defensive copies.

## 8. Generated and packaged artifacts

The existing generators own all new bytes:

- `generate_contract_schemas.py` emits the matrix and three schemas, then
  publishes byte-identical SDK resource mirrors;
- `generate_corpus.py` emits the golden, one 76-case Python/Node corpus group,
  its manifest, and the additive corpus router;
- `generate_contract_manifest.py` records the complete v2 file/hash closure;
- the SDK wheel includes both authoritative schema data files and four package
  resources.

Current deterministic evidence:

```text
negative cases: 76
negative case digest: 2210b996607ec5ee2f5471c28786b9293423c7f1467bc0b9666e60ec1ae49fbf
operation digest: fe7da5a2964ce063c99c692b4a9df92af7133cf1d7a5980a0e8bb6065b7eefcc
bridge digest: e724d3b7ef9ff310dd3e97c251721a6c25d1e914351dba4aae22990fd0cc6dca
```

## 9. Proof map

| Proof | Evidence |
|---|---|
| P0B-01 | exact Git base/parent/path gate |
| P0B-02 | frozen Provider/Prompt-Skill bytes plus existing 27-field decoder tests; no new full schema/decoder |
| P0B-03 | separate exact reserved-method matrix |
| P0B-04 | closed request/meta schema and six meta mirrors |
| P0B-05 | 19-field planner context equality with the accepted 23-field anchor minus four locator fields |
| P0B-06 | exact seven-field result schema |
| P0B-07 | normalized schema equality with the accepted P2 anchor |
| P0B-08 | request/success/error UUID and exactly-one envelope negatives |
| P0B-09 | four terminal success positives and RPC-error-with-receipt negative |
| P0B-10 | six-domain golden and substitution negatives |
| P0B-11 | response Asset pair/null positives and mirror negatives |
| P0B-12 | existing decoder/Asset gate cross-layer test and rehashed-drift negatives |
| P0B-13 | all 19 anchor drift cases, request Asset echo, and exact replay negatives |
| P0B-14 | Plan binding/synthesizer/descriptor/manifest reserved negatives plus ordinary positives |
| P0B-15 | deterministic generator, resource, golden, corpus, manifest and docs gates |
| P0B-16 | identical Python/Node case count/digest and Provider-to-Prompt-Skill bridge digest |
| P0B-17 | source and installed-wheel parser/registry/resource/defensive-copy probes |
| P0B-18 | frozen hash and forbidden-path/surface gates |
