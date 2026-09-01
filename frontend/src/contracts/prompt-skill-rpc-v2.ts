import requestSchema from '../../../contracts/json-schema/prompt-skill-execute-request-v2.schema.json' with { type: 'json' }
import resultSchema from '../../../contracts/json-schema/prompt-skill-execute-result-v2.schema.json' with { type: 'json' }
import successSchema from '../../../contracts/json-schema/rpc-method-success-v2.schema.json' with { type: 'json' }
import errorSchema from '../../../contracts/json-schema/rpc-error-v1.schema.json' with { type: 'json' }
import v1MethodMatrix from '../../../contracts/json-schema/rpc-method-matrix.v1.json' with { type: 'json' }
import { parseJsonSchema, type JsonSchema } from './schema-ingress.ts'
import { canonicalJson, hashJcs } from './canonical.ts'

export type JsonRecord = Record<string, unknown>
export type PromptSkillStatus = 'succeeded' | 'failed' | 'cancelled' | 'uncertain'
export const MAX_SAFE_INTEGER_V2 = Number.MAX_SAFE_INTEGER

export const PROMPT_SKILL_EXECUTE_METHOD_V2 = 'prompt.skill.execute/v2' as const
export const PROMPT_SKILL_REQUEST_SCHEMA_V2 = 'prompt-skill-execute-request/v2' as const
export const PROMPT_SKILL_RESULT_SCHEMA_V2 = 'prompt-skill-execute-result/v2' as const
export const PROMPT_SKILL_SUCCESS_SCHEMA_V2 = 'rpc-method-success/v2' as const
export const PROMPT_SKILL_ERROR_SCHEMA_V1 = 'rpc-error-v1' as const
export const PROMPT_SKILL_RESULT_CONTRACT_V1 = 'artifact-bundle/v1' as const

export interface SkillReleaseBindingV2 {
  skill_id: string
  release_id: string
  package_hash: string
  parameters_asset_id: string | null
  parameters_content_hash: string | null
}

export interface AssetIdentityV2 {
  asset_id: string
  content_hash: string
}

export interface ModelReceiptIdentityV2 {
  receipt_id: string
  asset_id: string
  content_hash: string
  receipt_hash: string
  operation_key: string
  workspace_id: string
  plugin_id: string
  plugin_release_id: string
  plugin_package_hash: string
  generation_id: string
  job_id: string
  step_id: string
  attempt_id: string
  lease_epoch: number
  chain_id: string
  chain_asset_id: string
  chain_content_hash: string
  run_snapshot_id: string
  run_snapshot_asset_id: string
  run_snapshot_hash: string
  model_profile_revision_id: string | null
  input_asset_id: string
  input_content_hash: string
}

export interface ResultBundleIdentityV2 {
  contract_id: typeof PROMPT_SKILL_RESULT_CONTRACT_V1
  bundle_id: string
  asset_id: string
  content_hash: string
  result_item_id: string
  workspace_id: string
  plugin_id: string
  plugin_release_id: string
  plugin_package_hash: string
  generation_id: string
  job_id: string
  step_id: string
  attempt_id: string
  lease_epoch: number
  chain_id: string
  chain_asset_id: string
  chain_content_hash: string
  run_snapshot_id: string
  run_snapshot_asset_id: string
  run_snapshot_hash: string
  model_profile_revision_id: string | null
  input_asset_id: string
  input_content_hash: string
  operation_key: string
}

export interface PromptSkillSecretV2 {
  secret_id: string
  value: string
}

export interface PromptSkillExecuteParamsV2 {
  schema: typeof PROMPT_SKILL_REQUEST_SCHEMA_V2
  authority: 'core'
  capability_id: typeof PROMPT_SKILL_EXECUTE_METHOD_V2
  result_contract: typeof PROMPT_SKILL_RESULT_CONTRACT_V1
  operation_key: string
  workspace_id: string
  plugin_id: string
  plugin_release_id: string
  plugin_package_hash: string
  skill_releases: SkillReleaseBindingV2[]
  run_snapshot_id: string
  run_snapshot_asset_id: string
  run_snapshot_hash: string
  generation_id: string
  job_id: string
  step_id: string
  attempt_id: string
  lease_epoch: number
  chain_id: string
  chain_asset_id: string
  chain_content_hash: string
  input_asset_id: string
  input_content_hash: string
  parameters_asset_id: string | null
  parameters_content_hash: string | null
  model_profile_revision_id: string | null
  anchor: ExecutionAnchorV2
  secrets: PromptSkillSecretV2[] | null
}

export interface PromptSkillMetaV2 {
  protocol_version: '1'
  generation_id: string
  plugin_release_id: string
  deadline_at: string
  context: 'attempt'
  operation_id: string
  job_id: string
  step_id: string
  attempt_id: string
  lease_epoch: number
}

export interface PromptSkillExecuteRequestV2 {
  jsonrpc: '2.0'
  id: string
  method: typeof PROMPT_SKILL_EXECUTE_METHOD_V2
  meta: PromptSkillMetaV2
  params: PromptSkillExecuteParamsV2
}

export interface ExecutionAnchorV2 {
  result_bundle_id: string | null
  result_item_id: string | null
  stream_id: string | null
  acked_prefix_hash: string | null
}

export interface PromptSkillExecuteResultV2 {
  schema: typeof PROMPT_SKILL_RESULT_SCHEMA_V2
  authority: 'core'
  capability_id: typeof PROMPT_SKILL_EXECUTE_METHOD_V2
  result_contract: typeof PROMPT_SKILL_RESULT_CONTRACT_V1
  operation_key: string
  workspace_id: string
  plugin_id: string
  plugin_release_id: string
  plugin_package_hash: string
  skill_releases: SkillReleaseBindingV2[]
  run_snapshot_id: string
  run_snapshot_asset_id: string
  run_snapshot_hash: string
  generation_id: string
  job_id: string
  step_id: string
  attempt_id: string
  lease_epoch: number
  chain_id: string
  chain_asset_id: string
  chain_content_hash: string
  input_asset_id: string
  input_content_hash: string
  parameters_asset_id: string | null
  parameters_content_hash: string | null
  model_profile_revision_id: string | null
  anchor: ExecutionAnchorV2
  status: PromptSkillStatus
  output: AssetIdentityV2 | null
  model_receipt: ModelReceiptIdentityV2 | null
  result_bundle: ResultBundleIdentityV2 | null
  idempotent: boolean
  warnings: Array<{ code: string; message: string }>
}

export interface RpcMethodSuccessV2 {
  jsonrpc: '2.0'
  id: string
  result: PromptSkillExecuteResultV2
}

export interface AuthoritativeAttemptContextV2 {
  workspace_id: string
  generation_id: string
  plugin_id: string
  plugin_release_id: string
  job_id: string
  step_id: string
  attempt_id: string
  lease_epoch: number
  operation_key: string
}

export const RPC_METHOD_MATRIX_V2 = {
  [PROMPT_SKILL_EXECUTE_METHOD_V2]: {
    method: PROMPT_SKILL_EXECUTE_METHOD_V2,
    authority: 'core',
    consumer: 'P2',
    direction: 'host-to-worker',
    endpoint: 'worker',
    protocol_version: '1',
    framing: 'content-length-crlf',
    meta_profile: 'attempt',
    lease_fenced: true,
    operation_key_required: true,
    request_schema: PROMPT_SKILL_REQUEST_SCHEMA_V2,
    result_contract: PROMPT_SKILL_RESULT_CONTRACT_V1,
    result_schema: PROMPT_SKILL_RESULT_SCHEMA_V2,
    success_schema: PROMPT_SKILL_SUCCESS_SCHEMA_V2,
    error_schema: PROMPT_SKILL_ERROR_SCHEMA_V1,
    envelope: { exactly_one: true, members: ['request', 'success', 'error'] as const },
    secrets: { location: 'params', mode: 'one-shot', response_forbidden: true },
    job_mapping: {
      start: { method: 'job.start', source: 'attempt', required: ['capability_id', 'run_snapshot_asset_id', 'checkpoint_asset_id', 'secrets'] },
      resume: { method: 'job.resume', source: 'attempt', required: ['capability_id', 'run_snapshot_asset_id', 'resume_of_attempt_id', 'checkpoint_asset_id', 'resume_intent_id', 'resume_reason', 'secrets'] },
    },
  },
} as const

const ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/u
const HASH = /^[0-9a-f]{64}$/u
const UUID = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$/u
const UTC = /^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.(?!000)[0-9]{3}Z)$/u
const V1_ERROR_CODES = new Set(Object.keys((v1MethodMatrix as { error_codes: Record<string, string> }).error_codes).map(Number))

function fail(message: string): never {
  throw new Error(`Prompt/Skill execute v2 validation failed: ${message}`)
}

function assertUnicodeScalars(value: unknown, path = '$', seen = new WeakSet<object>()): void {
  if (typeof value === 'string') {
    for (let index = 0; index < value.length; index += 1) {
      const code = value.charCodeAt(index)
      if (code >= 0xd800 && code <= 0xdbff) {
        const next = index + 1 < value.length ? value.charCodeAt(index + 1) : 0
        if (next < 0xdc00 || next > 0xdfff) fail(`${path} contains a lone UTF-16 surrogate`)
        index += 1
      } else if (code >= 0xdc00 && code <= 0xdfff) {
        fail(`${path} contains a lone UTF-16 surrogate`)
      }
    }
    return
  }
  if (value === null || typeof value !== 'object') {
    if (typeof value === 'number' && (!Number.isSafeInteger(value))) fail(`${path} is not an IEEE-754 safe integer`)
    return
  }
  if (seen.has(value)) fail(`${path} is cyclic`)
  seen.add(value)
  try {
    if (Array.isArray(value)) value.forEach((item, index) => assertUnicodeScalars(item, `${path}[${index}]`, seen))
    else Object.keys(value).forEach(key => { assertUnicodeScalars(key, `${path}.<key>`, seen); assertUnicodeScalars((value as JsonRecord)[key], `${path}.${key}`, seen) })
  } finally {
    seen.delete(value)
  }
}

function id(value: unknown, label: string, nullable = false): string | null {
  if (value === null && nullable) return null
  if (typeof value !== 'string' || !ID.test(value)) fail(`${label} is not a v2 identity`)
  return value
}

function hash(value: unknown, label: string, nullable = false): string | null {
  if (value === null && nullable) return null
  if (typeof value !== 'string' || !HASH.test(value)) fail(`${label} is not a lowercase SHA-256`)
  return value
}

function safeInteger(value: unknown, label: string, minimum = 0): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum) fail(`${label} must be an IEEE-754 safe integer >= ${minimum}`)
  return value
}

function exactPair(left: unknown, right: unknown, label: string): void {
  if ((left === null) !== (right === null)) fail(`${label} must be all-null or all-present`)
}

function validateSecrets(value: PromptSkillSecretV2[] | null): void {
  if (value === null) return
  const seen = new Set<string>()
  for (const [index, secret] of value.entries()) {
    const secretId = id(secret.secret_id, `secrets[${index}].secret_id`)
    if (seen.has(secretId)) fail('secret IDs must be unique')
    seen.add(secretId)
    if (typeof secret.value !== 'string') fail(`secrets[${index}].value must be a string`)
    assertUnicodeScalars(secret.value, `secrets[${index}].value`)
  }
}

function validateAnchor(value: ExecutionAnchorV2): void {
  exactPair(value.result_bundle_id, value.result_item_id, 'Bundle anchor')
  exactPair(value.stream_id, value.acked_prefix_hash, 'stream anchor')
  if (value.result_bundle_id !== null && value.stream_id !== null) fail('an execution anchor cannot be both Bundle-backed and stream-backed')
  id(value.result_bundle_id, 'anchor.result_bundle_id', true)
  id(value.result_item_id, 'anchor.result_item_id', true)
  id(value.stream_id, 'anchor.stream_id', true)
  hash(value.acked_prefix_hash, 'anchor.acked_prefix_hash', true)
}

function validateSkillReleases(value: PromptSkillExecuteParamsV2 | PromptSkillExecuteResultV2): void {
  const skillIds = value.skill_releases.map(item => item.skill_id)
  if (new Set(skillIds).size !== skillIds.length) fail('Skill identities must be unique')
  for (const [index, release] of value.skill_releases.entries()) {
    id(release.skill_id, `skill_releases[${index}].skill_id`)
    hash(release.release_id, `skill_releases[${index}].release_id`)
    hash(release.package_hash, `skill_releases[${index}].package_hash`)
    id(release.parameters_asset_id, `skill_releases[${index}].parameters_asset_id`, true)
    hash(release.parameters_content_hash, `skill_releases[${index}].parameters_content_hash`, true)
    exactPair(release.parameters_asset_id, release.parameters_content_hash, `skill_releases[${index}] parameters identity`)
  }
}

function validateCommon(value: PromptSkillExecuteParamsV2 | PromptSkillExecuteResultV2, label: string): void {
  if (value.authority !== 'core') fail(`${label} is not Core-owned`)
  if (value.capability_id !== PROMPT_SKILL_EXECUTE_METHOD_V2) fail(`${label} capability is not the frozen v2 method`)
  if (value.result_contract !== PROMPT_SKILL_RESULT_CONTRACT_V1) fail(`${label} result contract is not the frozen Core contract`)
  for (const [field, expected] of Object.entries({
    operation_key: value.operation_key,
    workspace_id: value.workspace_id,
    plugin_id: value.plugin_id,
    run_snapshot_id: value.run_snapshot_id,
    run_snapshot_asset_id: value.run_snapshot_asset_id,
    generation_id: value.generation_id,
    job_id: value.job_id,
    step_id: value.step_id,
    attempt_id: value.attempt_id,
    chain_id: value.chain_id,
    chain_asset_id: value.chain_asset_id,
    input_asset_id: value.input_asset_id,
  })) id(expected, field)
  for (const [field, expected] of Object.entries({
    plugin_release_id: value.plugin_release_id,
    plugin_package_hash: value.plugin_package_hash,
    run_snapshot_hash: value.run_snapshot_hash,
    chain_content_hash: value.chain_content_hash,
    input_content_hash: value.input_content_hash,
  })) hash(expected, field)
  id(value.model_profile_revision_id, 'model_profile_revision_id', true)
  safeInteger(value.lease_epoch, 'lease_epoch', 1)
  id(value.parameters_asset_id, 'parameters_asset_id', true)
  hash(value.parameters_content_hash, 'parameters_content_hash', true)
  exactPair(value.parameters_asset_id, value.parameters_content_hash, 'parameters identity')
  validateSkillReleases(value)
  validateAnchor(value.anchor)
}

function validateMeta(meta: PromptSkillMetaV2, params: PromptSkillExecuteParamsV2): void {
  if (meta.protocol_version !== '1' || meta.context !== 'attempt') fail('request meta must use protocol v1 attempt profile')
  id(meta.generation_id, 'meta.generation_id')
  hash(meta.plugin_release_id, 'meta.plugin_release_id')
  if (!UTC.test(meta.deadline_at)) fail('meta.deadline_at must use the frozen UTC format')
  id(meta.operation_id, 'meta.operation_id')
  id(meta.job_id, 'meta.job_id')
  id(meta.step_id, 'meta.step_id')
  id(meta.attempt_id, 'meta.attempt_id')
  safeInteger(meta.lease_epoch, 'meta.lease_epoch', 1)
  for (const field of ['generation_id', 'plugin_release_id', 'job_id', 'step_id', 'attempt_id', 'lease_epoch'] as const) if (meta[field] !== params[field]) fail(`request meta ${field} does not match execute identity`)
  if (meta.operation_id !== params.operation_key) fail('request meta operation_id must equal operation_key')
}

function validateModelReceipt(value: ModelReceiptIdentityV2, expected: PromptSkillExecuteResultV2): void {
  for (const [field, actual] of Object.entries({
    receipt_id: value.receipt_id, asset_id: value.asset_id, operation_key: value.operation_key, workspace_id: value.workspace_id,
    plugin_id: value.plugin_id, generation_id: value.generation_id, job_id: value.job_id, step_id: value.step_id,
    attempt_id: value.attempt_id, chain_id: value.chain_id, chain_asset_id: value.chain_asset_id,
    run_snapshot_id: value.run_snapshot_id, run_snapshot_asset_id: value.run_snapshot_asset_id,
  })) id(actual, `model_receipt.${field}`)
  for (const [field, actual] of Object.entries({
    content_hash: value.content_hash, receipt_hash: value.receipt_hash, plugin_release_id: value.plugin_release_id,
    plugin_package_hash: value.plugin_package_hash, chain_content_hash: value.chain_content_hash,
    run_snapshot_hash: value.run_snapshot_hash, input_content_hash: value.input_content_hash,
  })) hash(actual, `model_receipt.${field}`)
  safeInteger(value.lease_epoch, 'model_receipt.lease_epoch', 1)
  id(value.model_profile_revision_id, 'model_receipt.model_profile_revision_id', true)
  id(value.input_asset_id, 'model_receipt.input_asset_id')
  const expectedFields = ['operation_key', 'workspace_id', 'plugin_id', 'plugin_release_id', 'plugin_package_hash', 'generation_id', 'job_id', 'step_id', 'attempt_id', 'lease_epoch', 'chain_id', 'chain_asset_id', 'chain_content_hash', 'run_snapshot_id', 'run_snapshot_asset_id', 'run_snapshot_hash', 'model_profile_revision_id', 'input_asset_id', 'input_content_hash'] as const
  for (const field of expectedFields) if (value[field] !== expected[field]) fail(`ModelReceipt ${field} does not match the execute identity`)
}

function validateResultBundle(value: ResultBundleIdentityV2, expected: PromptSkillExecuteResultV2): void {
  if (value.contract_id !== PROMPT_SKILL_RESULT_CONTRACT_V1) fail('result Bundle contract is not the frozen Core contract')
  for (const [field, actual] of Object.entries({
    bundle_id: value.bundle_id, asset_id: value.asset_id, result_item_id: value.result_item_id, workspace_id: value.workspace_id,
    plugin_id: value.plugin_id, generation_id: value.generation_id, job_id: value.job_id, step_id: value.step_id,
    attempt_id: value.attempt_id, chain_id: value.chain_id, chain_asset_id: value.chain_asset_id,
    run_snapshot_id: value.run_snapshot_id, run_snapshot_asset_id: value.run_snapshot_asset_id,
    input_asset_id: value.input_asset_id, operation_key: value.operation_key,
  })) id(actual, `result_bundle.${field}`)
  for (const [field, actual] of Object.entries({
    content_hash: value.content_hash, plugin_release_id: value.plugin_release_id, plugin_package_hash: value.plugin_package_hash,
    chain_content_hash: value.chain_content_hash, run_snapshot_hash: value.run_snapshot_hash,
    input_content_hash: value.input_content_hash,
  })) hash(actual, `result_bundle.${field}`)
  id(value.model_profile_revision_id, 'result_bundle.model_profile_revision_id', true)
  safeInteger(value.lease_epoch, 'result_bundle.lease_epoch', 1)
  const expectedFields = ['operation_key', 'workspace_id', 'plugin_id', 'plugin_release_id', 'plugin_package_hash', 'generation_id', 'job_id', 'step_id', 'attempt_id', 'lease_epoch', 'chain_id', 'chain_asset_id', 'chain_content_hash', 'run_snapshot_id', 'run_snapshot_asset_id', 'run_snapshot_hash', 'model_profile_revision_id', 'input_asset_id', 'input_content_hash'] as const
  for (const field of expectedFields) if (value[field] !== expected[field]) fail(`result Bundle ${field} does not match the execute identity`)
}

function validateResultSemantics(value: PromptSkillExecuteResultV2): void {
  validateCommon(value, 'Prompt Skill result')
  if (value.model_receipt !== null) validateModelReceipt(value.model_receipt, value)
  if (value.result_bundle !== null) validateResultBundle(value.result_bundle, value)
  if (value.output !== null) { id(value.output.asset_id, 'output.asset_id'); hash(value.output.content_hash, 'output.content_hash') }
  if (value.status === 'succeeded') {
    if (value.model_receipt === null || value.result_bundle === null || value.output === null) fail('succeeded execution requires output, ModelReceipt and result Bundle')
    if (value.anchor.result_bundle_id !== value.result_bundle.bundle_id || value.anchor.result_item_id !== value.result_bundle.result_item_id) fail('result Bundle identity does not match the execution anchor')
    if (value.anchor.stream_id !== null || value.anchor.acked_prefix_hash !== null) fail('Bundle-backed result cannot carry a stream anchor')
  } else {
    if (value.model_receipt !== null || value.result_bundle !== null || value.output !== null) fail(`${value.status} execution cannot expose success artifacts`)
    if (Object.values(value.anchor).some(item => item !== null)) fail(`${value.status} execution cannot expose an output anchor`)
  }
  if (typeof value.idempotent !== 'boolean') fail('idempotent must be boolean')
  value.warnings.forEach((warning, index) => {
    id(warning.code, `warnings[${index}].code`)
    if (typeof warning.message !== 'string') fail(`warnings[${index}].message must be a string`)
    assertUnicodeScalars(warning.message, `warnings[${index}].message`)
  })
}

export function parsePromptSkillExecuteRequestV2(value: unknown): Readonly<PromptSkillExecuteRequestV2> {
  assertUnicodeScalars(value)
  const parsed = parseJsonSchema<PromptSkillExecuteRequestV2>(value, requestSchema as JsonSchema, 'Prompt Skill request')
  if (parsed.jsonrpc !== '2.0' || parsed.method !== PROMPT_SKILL_EXECUTE_METHOD_V2 || !UUID.test(parsed.id)) fail('request method, version, or RPC id is not the frozen v2 envelope')
  validateCommon(parsed.params, 'Prompt Skill request')
  validateSecrets(parsed.params.secrets)
  validateMeta(parsed.meta, parsed.params)
  return parsed
}

export function parsePromptSkillExecuteResultV2(value: unknown): Readonly<PromptSkillExecuteResultV2> {
  assertUnicodeScalars(value)
  const parsed = parseJsonSchema<PromptSkillExecuteResultV2>(value, resultSchema as JsonSchema, 'Prompt Skill result')
  if (parsed.schema !== PROMPT_SKILL_RESULT_SCHEMA_V2) fail('result schema discriminator is not v2')
  validateResultSemantics(parsed)
  return parsed
}

export function parsePromptSkillErrorV2(value: unknown, request?: unknown): Readonly<JsonRecord> {
  assertUnicodeScalars(value)
  const parsed = parseJsonSchema<JsonRecord>(value, errorSchema as JsonSchema, 'Prompt Skill RPC error')
  if (parsed.jsonrpc !== '2.0') fail('RPC error response must use JSON-RPC 2.0')
  const error = parsed.error as JsonRecord
  assertUnicodeScalars(error.message, 'error.message')
  if (typeof error.code !== 'number' || !Number.isInteger(error.code) || !V1_ERROR_CODES.has(error.code)) fail('RPC error code is not registered in rpc-method-matrix/v1')
  if (parsed.id !== null && (typeof parsed.id !== 'string' || !UUID.test(parsed.id))) fail('RPC error response id is not a UUID')
  if (request !== undefined) {
    const parsedRequest = parsePromptSkillExecuteRequestV2(request)
    if (parsed.id !== parsedRequest.id) fail('RPC error response id does not match its request')
  }
  return parsed
}

export function validateRpcMethodSuccessV2(value: unknown, request?: unknown): Readonly<RpcMethodSuccessV2> {
  assertUnicodeScalars(value)
  const parsed = parseJsonSchema<RpcMethodSuccessV2>(value, successSchema as JsonSchema, 'RPC success response')
  if (parsed.jsonrpc !== '2.0' || !UUID.test(parsed.id)) fail('RPC success response envelope is not JSON-RPC 2.0')
  let parsedRequest: Readonly<PromptSkillExecuteRequestV2> | undefined
  if (request !== undefined) {
    parsedRequest = parsePromptSkillExecuteRequestV2(request)
    if (parsed.id !== parsedRequest.id) fail('RPC response id does not match its request')
  }
  const parsedResult = parsePromptSkillExecuteResultV2(parsed.result)
  if (parsedRequest !== undefined) validatePromptSkillExecuteV2(parsedRequest, parsedResult)
  return parsed
}

export function parsePromptSkillRpcEnvelopeV2(value: unknown, request?: unknown): unknown {
  assertUnicodeScalars(value)
  const record = value as JsonRecord
  if (record === null || typeof record !== 'object' || Array.isArray(record)) fail('Prompt Skill RPC envelope must be an object')
  const kinds = ['method', 'result', 'error'].filter(key => Object.prototype.hasOwnProperty.call(record, key))
  if (kinds.length !== 1) fail('Prompt Skill RPC envelope must contain exactly one request/success/error member')
  if (kinds[0] === 'method') return parsePromptSkillExecuteRequestV2(value)
  if (kinds[0] === 'result') return validateRpcMethodSuccessV2(value, request)
  return parsePromptSkillErrorV2(value, request)
}

function bindingProjection(value: PromptSkillExecuteParamsV2 | PromptSkillExecuteResultV2): JsonRecord {
  const fields = [
    'authority', 'capability_id', 'result_contract', 'operation_key', 'workspace_id', 'plugin_id', 'plugin_release_id', 'plugin_package_hash',
    'skill_releases', 'run_snapshot_id', 'run_snapshot_asset_id', 'run_snapshot_hash', 'generation_id', 'job_id', 'step_id', 'attempt_id', 'lease_epoch',
    'chain_id', 'chain_asset_id', 'chain_content_hash', 'input_asset_id', 'input_content_hash', 'parameters_asset_id', 'parameters_content_hash',
    'model_profile_revision_id', 'anchor',
  ] as const
  const output: JsonRecord = {}
  for (const field of fields) output[field] = value[field]
  return output
}

function authorityProjection(params: PromptSkillExecuteParamsV2, context: AuthoritativeAttemptContextV2): JsonRecord {
  const fields = ['workspace_id', 'generation_id', 'plugin_id', 'plugin_release_id', 'job_id', 'step_id', 'attempt_id', 'lease_epoch', 'operation_key'] as const
  const output: JsonRecord = {}
  const actualKeys = Object.keys(context)
  if (actualKeys.length !== fields.length || fields.some(field => !Object.prototype.hasOwnProperty.call(context, field)) || actualKeys.some(field => !fields.includes(field as typeof fields[number]))) fail('authoritative Attempt context must contain exactly the frozen fields')
  for (const field of fields) {
    const actual = context[field]
    if (actual !== params[field]) throw new Error(`Prompt/Skill execute v2 validation failed: authoritative Attempt ${field} does not match`)
    output[field] = actual
  }
  id(context.workspace_id, 'authoritative_context.workspace_id')
  id(context.generation_id, 'authoritative_context.generation_id')
  id(context.plugin_id, 'authoritative_context.plugin_id')
  hash(context.plugin_release_id, 'authoritative_context.plugin_release_id')
  id(context.job_id, 'authoritative_context.job_id')
  id(context.step_id, 'authoritative_context.step_id')
  id(context.attempt_id, 'authoritative_context.attempt_id')
  id(context.operation_key, 'authoritative_context.operation_key')
  safeInteger(output.lease_epoch, 'authoritative_context.lease_epoch', 1)
  return output
}

function requireAuthority(request: PromptSkillExecuteRequestV2, context: AuthoritativeAttemptContextV2): string {
  return canonicalJson(authorityProjection(request.params, context))
}

export function validatePromptSkillExecuteV2(
  request: unknown,
  resultOrResponse: unknown,
  authoritativeContext?: AuthoritativeAttemptContextV2,
): Readonly<PromptSkillExecuteResultV2> {
  const parsedRequest = parsePromptSkillExecuteRequestV2(request)
  const candidate = resultOrResponse as JsonRecord
  if (candidate === null || typeof candidate !== 'object' || Array.isArray(candidate)) fail('Prompt Skill result or response must be an object')
  let parsedResult: Readonly<PromptSkillExecuteResultV2>
  if (Object.prototype.hasOwnProperty.call(candidate, 'error')) { parsePromptSkillErrorV2(candidate); fail('Prompt Skill execute returned an RPC error') }
  if (candidate.jsonrpc === '2.0' && Object.prototype.hasOwnProperty.call(candidate, 'result')) parsedResult = validateRpcMethodSuccessV2(candidate, parsedRequest).result
  else parsedResult = parsePromptSkillExecuteResultV2(candidate)
  if (canonicalJson(bindingProjection(parsedRequest.params)) !== canonicalJson(bindingProjection(parsedResult))) fail('result identity projection does not match its request')
  if (authoritativeContext !== undefined) requireAuthority(parsedRequest, authoritativeContext)
  return parsedResult
}

export function buildPromptSkillMetaV2(params: PromptSkillExecuteParamsV2, deadlineAt: string): PromptSkillMetaV2 {
  return {
    protocol_version: '1', generation_id: params.generation_id, plugin_release_id: params.plugin_release_id, deadline_at: deadlineAt,
    context: 'attempt', operation_id: params.operation_key, job_id: params.job_id, step_id: params.step_id, attempt_id: params.attempt_id, lease_epoch: params.lease_epoch,
  }
}

export function buildPromptSkillExecuteRequestV2(params: PromptSkillExecuteParamsV2, requestId: string, meta?: PromptSkillMetaV2): Readonly<PromptSkillExecuteRequestV2> {
  return parsePromptSkillExecuteRequestV2({ jsonrpc: '2.0', id: requestId, method: PROMPT_SKILL_EXECUTE_METHOD_V2, meta: meta ?? buildPromptSkillMetaV2(params, '2026-08-31T00:00:00Z'), params })
}

export function buildRpcMethodSuccessV2(request: unknown, result: unknown): Readonly<RpcMethodSuccessV2> {
  const parsedRequest = parsePromptSkillExecuteRequestV2(request)
  const parsedResult = validatePromptSkillExecuteV2(parsedRequest, result)
  return validateRpcMethodSuccessV2({ jsonrpc: '2.0', id: parsedRequest.id, result: parsedResult }, parsedRequest)
}

export async function promptSkillOperationKeyV2(value: unknown): Promise<string> {
  const request = parsePromptSkillExecuteRequestV2(value)
  return hashJcs('prompt-skill-execute/v2', request.params)
}

export class PromptSkillOperationLedgerV2 {
  private readonly entries = new Map<string, { request: string; result: string; authority: string; value: PromptSkillExecuteResultV2 }>()

  record(request: unknown, resultOrResponse: unknown, authoritativeContext: AuthoritativeAttemptContextV2): { result: Readonly<PromptSkillExecuteResultV2>; replayed: boolean } {
    const parsedRequest = parsePromptSkillExecuteRequestV2(request)
    const authority = requireAuthority(parsedRequest, authoritativeContext)
    const parsedResult = validatePromptSkillExecuteV2(parsedRequest, resultOrResponse, authoritativeContext)
    const key = `${parsedRequest.params.workspace_id}\u0000${parsedRequest.params.operation_key}`
    const requestBytes = canonicalJson(parsedRequest)
    const resultBytes = canonicalJson(parsedResult)
    const previous = this.entries.get(key)
    if (previous !== undefined) {
      if (previous.authority !== authority) throw new Error('Prompt/Skill execute v2 validation failed: authority context changed')
      if (previous.request !== requestBytes || previous.result !== resultBytes) throw new Error('Prompt/Skill execute v2 validation failed: operation key reused with a different exchange')
      return { result: previous.value, replayed: true }
    }
    this.entries.set(key, { request: requestBytes, result: resultBytes, authority, value: parsedResult })
    return { result: parsedResult, replayed: false }
  }

  replay(request: unknown, authoritativeContext: AuthoritativeAttemptContextV2): Readonly<PromptSkillExecuteResultV2> {
    const parsedRequest = parsePromptSkillExecuteRequestV2(request)
    const authority = requireAuthority(parsedRequest, authoritativeContext)
    const key = `${parsedRequest.params.workspace_id}\u0000${parsedRequest.params.operation_key}`
    const previous = this.entries.get(key)
    if (previous === undefined || previous.request !== canonicalJson(parsedRequest)) throw new Error('Prompt/Skill execute v2 validation failed: no exact replay')
    if (previous.authority !== authority) throw new Error('Prompt/Skill execute v2 validation failed: stale replay authority')
    return previous.value
  }

  lookup(request: unknown, authoritativeContext: AuthoritativeAttemptContextV2): Readonly<PromptSkillExecuteResultV2> {
    return this.replay(request, authoritativeContext)
  }
}

export const parseRequestV2 = parsePromptSkillExecuteRequestV2
export const parseResultV2 = parsePromptSkillExecuteResultV2
export const parseSuccessV2 = validateRpcMethodSuccessV2
export const parseRpcMethodSuccessV2 = validateRpcMethodSuccessV2
export const parseRpcSuccessV2 = validateRpcMethodSuccessV2
export const parseErrorV2 = parsePromptSkillErrorV2
export const parseEnvelopeV2 = parsePromptSkillRpcEnvelopeV2
export const validateRequestV2 = parsePromptSkillExecuteRequestV2
export const validateResultV2 = parsePromptSkillExecuteResultV2
export const validateRpcSuccessV2 = validateRpcMethodSuccessV2
export const validateExecuteV2 = validatePromptSkillExecuteV2
export const validateExchangeV2 = validatePromptSkillExecuteV2
