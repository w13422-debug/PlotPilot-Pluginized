import requestSchema from '../../../contracts/json-schema/prompt-skill-execute-request-v2.schema.json' with { type: 'json' }
import resultSchema from '../../../contracts/json-schema/prompt-skill-execute-result-v2.schema.json' with { type: 'json' }
import successSchema from '../../../contracts/json-schema/rpc-method-success-v2.schema.json' with { type: 'json' }
import { parseJsonSchema, type JsonSchema } from './schema-ingress.ts'
import { canonicalJson, hashJcs } from './canonical.ts'

export type JsonRecord = Record<string, unknown>
export type PromptSkillStatus = 'succeeded' | 'failed' | 'cancelled' | 'uncertain'

export const PROMPT_SKILL_EXECUTE_METHOD_V2 = 'prompt.skill.execute/v2' as const
export const PROMPT_SKILL_REQUEST_SCHEMA_V2 = 'prompt-skill-execute-request/v2' as const
export const PROMPT_SKILL_RESULT_SCHEMA_V2 = 'prompt-skill-execute-result/v2' as const
export const PROMPT_SKILL_SUCCESS_SCHEMA_V2 = 'rpc-method-success/v2' as const
export const PROMPT_SKILL_RESULT_CONTRACT_V1 = 'artifact-bundle/v1' as const

export interface SkillReleaseBindingV2 {
  order: number
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
  run_snapshot_hash: string
  operation_key: string
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
  input_asset_id: string
  input_content_hash: string
  parameters_asset_id: string | null
  parameters_content_hash: string | null
  model_profile_revision_id: string | null
  anchor: ExecutionAnchorV2
}

export interface PromptSkillExecuteRequestV2 {
  jsonrpc: '2.0'
  id: string
  method: typeof PROMPT_SKILL_EXECUTE_METHOD_V2
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

export const RPC_METHOD_MATRIX_V2 = {
  [PROMPT_SKILL_EXECUTE_METHOD_V2]: {
    authority: 'core',
    consumer: 'P2',
    direction: 'request-response',
    lease_fenced: true,
    operation_key_required: true,
    request_schema: PROMPT_SKILL_REQUEST_SCHEMA_V2,
    result_contract: PROMPT_SKILL_RESULT_CONTRACT_V1,
    result_schema: PROMPT_SKILL_RESULT_SCHEMA_V2,
    success_schema: PROMPT_SKILL_SUCCESS_SCHEMA_V2,
  },
} as const

const ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/
const HASH = /^[0-9a-f]{64}$/
const UUID = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$/

function fail(message: string): never {
  throw new Error(`Prompt/Skill execute v2 validation failed: ${message}`)
}

function record(value: unknown, label: string): JsonRecord {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) fail(`${label} must be an object`)
  return value as JsonRecord
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

function exactPair(left: unknown, right: unknown, label: string): void {
  if ((left === null) !== (right === null)) fail(`${label} must be all-null or all-present`)
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
  const orders = value.skill_releases.map(item => item.order)
  const skillIds = value.skill_releases.map(item => item.skill_id)
  if (new Set(orders).size !== orders.length) fail('Skill release order values must be unique')
  if (new Set(skillIds).size !== skillIds.length) fail('Skill identities must be unique')
  if (orders.some((item, index) => item !== index)) fail('Skill releases must be in contiguous stable order')
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
  for (const [field, expected] of [
    ['operation_key', value.operation_key],
    ['workspace_id', value.workspace_id],
    ['plugin_id', value.plugin_id],
    ['run_snapshot_id', value.run_snapshot_id],
    ['run_snapshot_asset_id', value.run_snapshot_asset_id],
    ['generation_id', value.generation_id],
    ['job_id', value.job_id],
    ['step_id', value.step_id],
    ['attempt_id', value.attempt_id],
    ['chain_id', value.chain_id],
    ['input_asset_id', value.input_asset_id],
  ] as const) id(expected, field)
  for (const [field, expected] of [
    ['plugin_release_id', value.plugin_release_id],
    ['plugin_package_hash', value.plugin_package_hash],
    ['run_snapshot_hash', value.run_snapshot_hash],
    ['input_content_hash', value.input_content_hash],
  ] as const) hash(expected, field)
  id(value.model_profile_revision_id, 'model_profile_revision_id', true)
  if (!Number.isSafeInteger(value.lease_epoch) || value.lease_epoch < 1) fail('lease_epoch must be a positive integer')
  id(value.parameters_asset_id, 'parameters_asset_id', true)
  hash(value.parameters_content_hash, 'parameters_content_hash', true)
  exactPair(value.parameters_asset_id, value.parameters_content_hash, 'parameters identity')
  validateSkillReleases(value)
  validateAnchor(value.anchor)
}

function validateModelReceipt(value: ModelReceiptIdentityV2): void {
  id(value.receipt_id, 'model_receipt.receipt_id')
  id(value.asset_id, 'model_receipt.asset_id')
  hash(value.content_hash, 'model_receipt.content_hash')
  hash(value.receipt_hash, 'model_receipt.receipt_hash')
  id(value.operation_key, 'model_receipt.operation_key')
  hash(value.run_snapshot_hash, 'model_receipt.run_snapshot_hash')
  id(value.model_profile_revision_id, 'model_receipt.model_profile_revision_id', true)
  id(value.input_asset_id, 'model_receipt.input_asset_id')
  hash(value.input_content_hash, 'model_receipt.input_content_hash')
}

function validateResultBundle(value: ResultBundleIdentityV2): void {
  if (value.contract_id !== PROMPT_SKILL_RESULT_CONTRACT_V1) fail('result Bundle contract is not the frozen Core contract')
  id(value.bundle_id, 'result_bundle.bundle_id')
  id(value.asset_id, 'result_bundle.asset_id')
  hash(value.content_hash, 'result_bundle.content_hash')
  id(value.result_item_id, 'result_bundle.result_item_id')
  id(value.workspace_id, 'result_bundle.workspace_id')
  hash(value.run_snapshot_hash, 'result_bundle.run_snapshot_hash')
  id(value.operation_key, 'result_bundle.operation_key')
}

function validateOutput(value: AssetIdentityV2 | null): void {
  if (value === null) return
  id(value.asset_id, 'output.asset_id')
  hash(value.content_hash, 'output.content_hash')
}

function validateResultSemantics(value: PromptSkillExecuteResultV2): void {
  validateCommon(value, 'Prompt Skill result')
  if (value.model_receipt !== null) {
    validateModelReceipt(value.model_receipt)
    const bindings: Array<[string, unknown, unknown]> = [
      ['operation_key', value.model_receipt.operation_key, value.operation_key],
      ['run_snapshot_hash', value.model_receipt.run_snapshot_hash, value.run_snapshot_hash],
      ['model_profile_revision_id', value.model_receipt.model_profile_revision_id, value.model_profile_revision_id],
      ['input_asset_id', value.model_receipt.input_asset_id, value.input_asset_id],
      ['input_content_hash', value.model_receipt.input_content_hash, value.input_content_hash],
    ]
    for (const [field, actual, expected] of bindings) if (actual !== expected) fail(`ModelReceipt ${field} does not match the execute identity`)
  }
  if (value.result_bundle !== null) {
    validateResultBundle(value.result_bundle)
    const bindings: Array<[string, unknown, unknown]> = [
      ['operation_key', value.result_bundle.operation_key, value.operation_key],
      ['run_snapshot_hash', value.result_bundle.run_snapshot_hash, value.run_snapshot_hash],
      ['workspace_id', value.result_bundle.workspace_id, value.workspace_id],
    ]
    for (const [field, actual, expected] of bindings) if (actual !== expected) fail(`result Bundle ${field} does not match the execute identity`)
  }
  validateOutput(value.output)
  if (value.status === 'succeeded') {
    if (value.model_receipt === null) fail('succeeded Prompt Skill execution requires a ModelReceipt')
    if (value.result_bundle === null) fail('succeeded Prompt Skill execution requires a result Bundle')
    if (value.anchor.result_bundle_id !== value.result_bundle.bundle_id || value.anchor.result_item_id !== value.result_bundle.result_item_id) fail('result Bundle identity does not match the frozen execution anchor')
    if (value.anchor.stream_id !== null || value.anchor.acked_prefix_hash !== null) fail('Bundle-backed result cannot carry a stream anchor')
  } else if (value.status === 'uncertain') {
    if (value.model_receipt !== null || value.result_bundle !== null || value.output !== null) fail('uncertain execution cannot expose unproven receipt, Bundle, or output')
  } else {
    if (value.result_bundle !== null || value.output !== null) fail('non-success execution cannot expose a result Bundle or output')
  }
  for (const [index, warning] of value.warnings.entries()) {
    id(warning.code, `warnings[${index}].code`)
    if (typeof warning.message !== 'string') fail(`warnings[${index}].message must be a string`)
  }
}

export function parsePromptSkillExecuteRequestV2(value: unknown): Readonly<PromptSkillExecuteRequestV2> {
  const parsed = parseJsonSchema<PromptSkillExecuteRequestV2>(value, requestSchema as JsonSchema, 'Prompt Skill request')
  if (parsed.jsonrpc !== '2.0' || parsed.method !== PROMPT_SKILL_EXECUTE_METHOD_V2 || !UUID.test(parsed.id)) fail('request method, version, or RPC id is not the frozen v2 envelope')
  validateCommon(parsed.params, 'Prompt Skill request')
  return parsed
}

export function parsePromptSkillExecuteResultV2(value: unknown): Readonly<PromptSkillExecuteResultV2> {
  const parsed = parseJsonSchema<PromptSkillExecuteResultV2>(value, resultSchema as JsonSchema, 'Prompt Skill result')
  if (parsed.schema !== PROMPT_SKILL_RESULT_SCHEMA_V2) fail('result schema discriminator is not v2')
  validateResultSemantics(parsed)
  return parsed
}

export function validateRpcMethodSuccessV2(value: unknown, request?: unknown): Readonly<RpcMethodSuccessV2> {
  const parsed = parseJsonSchema<RpcMethodSuccessV2>(value, successSchema as JsonSchema, 'RPC success response')
  if (parsed.jsonrpc !== '2.0' || !UUID.test(parsed.id)) fail('RPC success response envelope is not JSON-RPC 2.0')
  if (request !== undefined) {
    const parsedRequest = parsePromptSkillExecuteRequestV2(request)
    if (parsed.id !== parsedRequest.id) fail('RPC response id does not match its request')
  }
  parsePromptSkillExecuteResultV2(parsed.result)
  return parsed
}

function bindingProjection(value: PromptSkillExecuteParamsV2 | PromptSkillExecuteResultV2): JsonRecord {
  const fields = [
    'authority', 'capability_id', 'result_contract', 'operation_key', 'workspace_id', 'plugin_id',
    'plugin_release_id', 'plugin_package_hash', 'skill_releases', 'run_snapshot_id', 'run_snapshot_asset_id',
    'run_snapshot_hash', 'generation_id', 'job_id', 'step_id', 'attempt_id', 'lease_epoch', 'chain_id',
    'input_asset_id', 'input_content_hash', 'parameters_asset_id', 'parameters_content_hash',
    'model_profile_revision_id', 'anchor',
  ] as const
  const output: JsonRecord = {}
  for (const field of fields) output[field] = value[field]
  return output
}

export function validatePromptSkillExecuteV2(
  request: unknown,
  resultOrResponse: unknown,
  expectedLeaseEpoch?: number,
): Readonly<PromptSkillExecuteResultV2> {
  const parsedRequest = parsePromptSkillExecuteRequestV2(request)
  const candidate = record(resultOrResponse, 'Prompt Skill result or response')
  const parsedResult = candidate.jsonrpc === '2.0' && Object.prototype.hasOwnProperty.call(candidate, 'result')
    ? validateRpcMethodSuccessV2(candidate, parsedRequest).result
    : parsePromptSkillExecuteResultV2(candidate)
  if (canonicalJson(bindingProjection(parsedRequest.params)) !== canonicalJson(bindingProjection(parsedResult))) fail('result identity projection does not match its request')
  if (expectedLeaseEpoch !== undefined) {
    if (!Number.isSafeInteger(expectedLeaseEpoch) || expectedLeaseEpoch < 1) fail('expected lease epoch must be a positive integer')
    if (parsedRequest.params.lease_epoch !== expectedLeaseEpoch || parsedResult.lease_epoch !== expectedLeaseEpoch) throw new Error('Prompt/Skill execute v2 validation failed: stale lease epoch')
  }
  return parsedResult
}

export function buildPromptSkillExecuteRequestV2(params: PromptSkillExecuteParamsV2, requestId: string): Readonly<PromptSkillExecuteRequestV2> {
  return parsePromptSkillExecuteRequestV2({ jsonrpc: '2.0', id: requestId, method: PROMPT_SKILL_EXECUTE_METHOD_V2, params })
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

export const parseRequestV2 = parsePromptSkillExecuteRequestV2
export const parseResultV2 = parsePromptSkillExecuteResultV2
export const parseSuccessV2 = validateRpcMethodSuccessV2
export const parseRpcMethodSuccessV2 = validateRpcMethodSuccessV2
export const parseRpcSuccessV2 = validateRpcMethodSuccessV2
export const validateRequestV2 = parsePromptSkillExecuteRequestV2
export const validateResultV2 = parsePromptSkillExecuteResultV2
export const validateRpcSuccessV2 = validateRpcMethodSuccessV2
export const validateExecuteV2 = validatePromptSkillExecuteV2
export const validateExchangeV2 = validatePromptSkillExecuteV2
