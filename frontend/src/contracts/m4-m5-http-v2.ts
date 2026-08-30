import matrix from '../../../contracts/json-schema/core-api-method-matrix.v2.json' with { type: 'json' }
import jobSchema from '../../../contracts/json-schema/job-http-command-query-v2.schema.json' with { type: 'json' }
import pluginSchema from '../../../contracts/json-schema/plugin-api-command-query-v2.schema.json' with { type: 'json' }
import { parseJsonSchema, type JsonSchema } from './schema-ingress.ts'
import { canonicalJson } from './canonical.ts'
import {
  parseCandidateQueryResultV2,
  parseCandidateReviewV2,
  parseCoreAuthorityV2,
  parseJobEventPageV2,
  parseStoryStateProjectionInputV2,
  parsePublicationCommandV2,
  parsePublicationResultV2,
  validateJobCommandResultV2,
  validateJobListResultV2,
  validateJobSnapshotResultV2,
  validateJobEventPageQueryV2,
  validateJobSseRecoveryQueryV2,
  validateJobEventV2,
  verifyJobSnapshotHashV2,
  validatePluginLifecycleV2,
  validatePublicationV2,
  type JsonRecord,
} from './core-api-v2.ts'

type Route = {
  route_id: string
  method: string
  path_template: string
  path_identity: string[]
  request_schema: string
  result_schema: string
  error_schema: 'core-http-error/v2' | 'job-http-error/v2' | 'plugin-http-error/v2'
  success_statuses: number[]
  failure_statuses: Array<{ status: number; error_codes: string[] }>
  read_only: boolean
  operation_key_required: boolean
  cursor_domain: 'candidate' | 'job' | 'core' | null
}

export const CORE_API_METHOD_MATRIX_V2 = matrix as unknown as { routes: Route[]; publication_path: string; publication_owner: string; plugin_publication_allowed: boolean }
const ROUTES = new Map(CORE_API_METHOD_MATRIX_V2.routes.map(route => [route.route_id, route]))
const CURSOR = /^(candidate|job|core)\/[A-Za-z0-9][A-Za-z0-9._:/-]*$/

function fail(message: string): never {
  throw new Error(`M4/M5 v2 HTTP contract validation failed: ${message}`)
}

function isRecord(value: unknown): value is JsonRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function operationKey(value: JsonRecord): string | null {
  const candidate = value.operation_key ?? value.publication_operation_key
  return candidate === undefined ? null : typeof candidate === 'string' ? candidate : null
}

function cursor(value: unknown, domain: 'candidate' | 'job' | 'core', jobId?: string): number | null {
  if (value === null) return null
  if (typeof value !== 'string' || !CURSOR.test(value)) fail('cursor is malformed')
  const parts = value.split('/')
  if (parts[0] !== domain) fail(`cursor domain mismatch: expected ${domain}`)
  if (domain === 'job' && (parts.length !== 3 || (jobId !== undefined && parts[1] !== jobId) || !/^\d+$/.test(parts[2]))) fail('job cursor is not bound to the Job')
  if (domain === 'core' && (parts.length !== 2 || !/^\d+$/.test(parts[1]))) fail('core cursor is malformed')
  if (domain === 'job') return Number(parts[2])
  if (domain === 'core') return Number(parts[1])
  return null
}

function parsePayload(value: JsonRecord): JsonRecord {
  switch (value.schema) {
    case 'candidate/v2':
    case 'candidate-list-query/v2':
    case 'candidate-list-result/v2':
    case 'candidate-get-query/v2':
    case 'candidate-get-result/v2':
    case 'candidate-preview-query/v2':
    case 'candidate-preview-result/v2':
      return parseCandidateQueryResultV2(value) as JsonRecord
    case 'candidate-review-query/v2':
    case 'candidate-review-command/v2':
    case 'candidate-review-result/v2':
      return parseCandidateReviewV2(value) as JsonRecord
    case 'core-authority-query/v2':
    case 'core-authority-result/v2':
    case 'publication-command/v2':
    case 'publication-result/v2':
    case 'core-http-error/v2':
      return parseCoreAuthorityV2(value) as JsonRecord
    case 'story-state-projection-input/v2':
      return parseStoryStateProjectionInputV2(value) as JsonRecord
    case 'job-event-page-result/v2':
      return parseJobEventPageV2(value) as JsonRecord
    case 'job-sse-recovery-result/v2':
      return parseJsonSchema<JsonRecord>(value, jobSchema as JsonSchema, 'job-http-command-query-v2')
    case 'job-snapshot-query/v2':
    case 'job-snapshot-result/v2':
      return parseJsonSchema<JsonRecord>(value, jobSchema as JsonSchema, 'job-http-command-query-v2')
    case 'job-list-query/v2':
    case 'job-list-result/v2':
    case 'job-start-command/v2':
    case 'job-control-command/v2':
    case 'job-command-result/v2':
    case 'job-event-page-query/v2':
    case 'job-sse-recovery-query/v2':
    case 'job-http-error/v2':
      return parseJsonSchema<JsonRecord>(value, jobSchema as JsonSchema, 'job-http-command-query-v2')
    case 'plugin-discovery-query/v2':
    case 'plugin-discovery-result/v2':
    case 'plugin-lifecycle-command/v2':
    case 'plugin-lifecycle-result/v2':
    case 'plugin-http-error/v2':
      return parseJsonSchema<JsonRecord>(value, pluginSchema as JsonSchema, 'plugin-api-command-query-v2')
    default:
      fail(`unsupported payload schema ${String(value.schema)}`)
  }
}

function validateRouteVariant(route: Route, parsed: JsonRecord): void {
  if (route.route_id.startsWith('plugin.') && (parsed.schema === 'plugin-lifecycle-command/v2' || parsed.schema === 'plugin-lifecycle-result/v2')) {
    const expectedAction = route.route_id.slice('plugin.'.length)
    if (parsed.action !== expectedAction) fail(`plugin lifecycle action is not bound to ${route.route_id}`)
  }
  if (route.route_id.startsWith('job.') && parsed.schema === 'job-control-command/v2') {
    const expectedCommand = route.route_id.slice('job.'.length)
    if (parsed.command !== expectedCommand) fail(`Job control command is not bound to ${route.route_id}`)
  }
  if (route.route_id.startsWith('job.') && parsed.schema === 'job-command-result/v2') {
    const expectedCommand = route.route_id.slice('job.'.length)
    if (parsed.command !== expectedCommand) fail(`Job command result is not bound to ${route.route_id}`)
  }
}

function validatePluginDiscoveryCursor(parsed: JsonRecord): void {
  if (parsed.schema === 'plugin-discovery-query/v2') cursor(parsed.cursor, 'core')
  if (parsed.schema === 'plugin-discovery-result/v2') cursor(parsed.next_cursor, 'core')
}

export function routeForV2(routeId: string): Readonly<Route> {
  const route = ROUTES.get(routeId)
  if (!route) fail(`unknown route ${routeId}`)
  return route
}

export function parseHttpRequestV2(routeId: string, value: unknown): Readonly<JsonRecord> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) fail('request must be an object')
  const request = value as JsonRecord
  const route = routeForV2(routeId)
  if (request.schema !== route.request_schema) fail(`request schema is not bound to ${routeId}`)
  for (const field of route.path_identity) if (typeof request[field] !== 'string' || request[field].length === 0) fail(`missing path identity ${field}`)
  if (route.operation_key_required && operationKey(request) === null) fail(`${routeId} requires operation_key`)
  const parsed = parsePayload(request)
  validateRouteVariant(route, parsed)
  validatePluginDiscoveryCursor(parsed)
  if (parsed.schema === 'plugin-lifecycle-command/v2') validatePluginLifecycleV2(parsed)
  if (parsed.schema === 'job-event-page-query/v2') validateJobEventPageQueryV2(parsed)
  if (parsed.schema === 'job-sse-recovery-query/v2') validateJobSseRecoveryQueryV2(parsed)
  if (route.cursor_domain) {
    for (const field of ['cursor', 'after_cursor', 'last_event_id']) if (field in parsed && parsed[field] !== null) cursor(parsed[field], route.cursor_domain, route.cursor_domain === 'job' ? parsed.job_id : undefined)
    if (parsed.requested_cursor_domain !== undefined && parsed.requested_cursor_domain !== route.cursor_domain) fail('request cursor domain is not route cursor domain')
  }
  return parsed
}

export async function parseHttpResponseV2(routeId: string, status: number, value: unknown): Promise<Readonly<JsonRecord>> {
  if (!Number.isSafeInteger(status)) fail('status must be an integer')
  if (value === null || typeof value !== 'object' || Array.isArray(value)) fail('response must be an object')
  const response = value as JsonRecord
  const route = routeForV2(routeId)
  const parsed = parsePayload(response)
  validateRouteVariant(route, parsed)
  validatePluginDiscoveryCursor(parsed)
  if (route.success_statuses.includes(status)) {
    if (parsed.schema !== route.result_schema) fail(`success response is not bound to ${routeId}`)
    if (parsed.schema === 'job-list-result/v2') await validateJobListResultV2(parsed)
    if (parsed.schema === 'job-snapshot-result/v2') await validateJobSnapshotResultV2(parsed)
    if (parsed.schema === 'job-command-result/v2') validateJobCommandResultV2(parsed)
    if (parsed.schema === 'job-event-page-result/v2') parseJobEventPageV2(parsed)
    if (parsed.schema === 'job-sse-recovery-result/v2') await parseJobSseRecoveryV2(parsed)
    return parsed
  }
  const failure = route.failure_statuses.find(item => item.status === status)
  if (!failure || parsed.schema !== route.error_schema) fail(`status ${status} is not declared by ${routeId}`)
  if (!failure.error_codes.includes(parsed.error_code)) fail(`error code is not allowed for ${routeId}`)
  return parsed
}

function responseBoundId(response: JsonRecord, field: 'candidate_id' | 'job_id' | 'plugin_id'): unknown {
  if (field in response) return response[field]
  if (field === 'candidate_id' && isRecord(response.candidate)) return response.candidate.candidate_id
  return undefined
}

/** Validate and bind one complete HTTP exchange, not just two independent DTOs. */
export async function validateHttpExchangeV2(
  routeId: string,
  requestValue: unknown,
  status: number,
  responseValue: unknown,
  candidateValue?: unknown,
): Promise<{ request: Readonly<JsonRecord>; response: Readonly<JsonRecord> }> {
  const request = parseHttpRequestV2(routeId, requestValue) as JsonRecord
  const response = await parseHttpResponseV2(routeId, status, responseValue) as JsonRecord
  const route = routeForV2(routeId)

  if ('workspace_id' in request && 'workspace_id' in response && response.workspace_id !== request.workspace_id) {
    fail(`${routeId} response crosses Workspace identity`)
  }

  for (const field of ['candidate_id', 'job_id', 'plugin_id'] as const) {
    const requestValueForField = request[field]
    if (requestValueForField === undefined) continue
    const responseValueForField = responseBoundId(response, field)
    if (responseValueForField !== undefined && responseValueForField !== requestValueForField) {
      fail(`${routeId} response is not bound to request ${field}`)
    }
    const required = ['candidate.get', 'candidate.preview', 'candidate.review', 'publication.accept'].includes(routeId)
      || routeId.startsWith('job.')
      || routeId.startsWith('plugin.')
    if (required && responseValueForField !== requestValueForField) {
      fail(`${routeId} response is missing request ${field} binding`)
    }
  }

  for (const field of ['action', 'command'] as const) {
    if (field in request && field in response && response[field] !== request[field]) {
      fail(`${routeId} response ${field} does not match request`)
    }
  }

  const requestKey = operationKey(request)
  const responseKey = operationKey(response)
  if (route.operation_key_required && route.success_statuses.includes(status)) {
    if (requestKey === null || responseKey !== requestKey) fail(`${routeId} response operation key does not match request`)
  } else if (route.operation_key_required && responseKey !== null && responseKey !== requestKey) {
    fail(`${routeId} error operation key does not match request`)
  }

  if (routeId.startsWith('plugin.') && response.schema === 'plugin-lifecycle-result/v2') {
    const targetGeneration = request.target_generation_id
    if (targetGeneration !== null && response.generation_id !== targetGeneration) {
      fail(`${routeId} response generation does not match requested target`)
    }
  }

  if (routeId === 'publication.accept' && route.success_statuses.includes(status)) {
    validatePublicationV2(request, response, candidateValue, request.workspace_id)
  }
  return { request, response }
}

export async function parseJobSseRecoveryV2(value: unknown): Promise<Readonly<JsonRecord>> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) fail('SSE recovery must be an object')
  const parsed = parsePayload(value as JsonRecord)
  if (parsed.schema !== 'job-sse-recovery-result/v2') fail('SSE recovery result discriminator is required')
  const snapshotSeq = cursor(parsed.snapshot_cursor, 'job', parsed.job_id)
  if (parsed.requested_after_seq > parsed.durable_high_water_seq) fail('SSE cursor is ahead of durable high-water')
  if (snapshotSeq === null || snapshotSeq > parsed.durable_high_water_seq) fail('SSE snapshot cursor is ahead of durable high-water')
  let continuationBaseline = parsed.requested_after_seq
  if (parsed.gap) {
    if (!parsed.snapshot_required || parsed.snapshot === null || parsed.replay_floor_seq <= parsed.requested_after_seq) fail('SSE gap requires snapshot recovery')
    if (parsed.snapshot.workspace_id !== parsed.workspace_id || parsed.snapshot.job_id !== parsed.job_id) fail('SSE recovery snapshot is not bound to its outer Workspace and Job')
    await verifyJobSnapshotHashV2(parsed.snapshot)
    if (snapshotSeq !== parsed.snapshot.job_event_high_water) fail('SSE snapshot cursor is not bound to the recovered snapshot')
    if (parsed.replay_floor_seq > snapshotSeq) fail('SSE replay floor is beyond the recovered snapshot high-water')
    continuationBaseline = snapshotSeq
  } else if (parsed.snapshot_required || parsed.snapshot !== null || parsed.replay_floor_seq > parsed.requested_after_seq + 1) {
    fail('SSE replay has inconsistent gap markers')
  }
  for (let index = 1; index < parsed.tail.length; index += 1) if (parsed.tail[index - 1].job_event_seq >= parsed.tail[index].job_event_seq) fail('SSE tail events are not strictly ordered')
  let expectedSeq = continuationBaseline + 1
  for (const event of parsed.tail) {
    validateJobEventV2(event, parsed.job_id)
    if (event.job_event_seq !== expectedSeq) fail('SSE tail cursor is not continuous after its continuation baseline')
    if (event.job_event_seq > parsed.durable_high_water_seq) fail('SSE tail is outside the durable Job high-water')
    expectedSeq += 1
  }
  if (expectedSeq - 1 !== parsed.durable_high_water_seq) fail('SSE tail does not converge to the durable Job high-water')
  return parsed
}

export class OperationKeyLedgerV2 {
  private readonly entries = new Map<string, { payload: string; status: number; response: JsonRecord }>()

  async record(routeId: string, requestValue: unknown, status: number, responseValue: unknown, candidateValue?: unknown): Promise<{ status: number; response: Readonly<JsonRecord>; idempotent: boolean }> {
    const { request, response } = await validateHttpExchangeV2(routeId, requestValue, status, responseValue, candidateValue)
    const key = operationKey(request)
    if (key === null) fail('operation replay requires an operation key')
    const identity = `${routeId}\0${key}`
    const payload = canonicalJson(request)
    const existing = this.entries.get(identity)
    if (existing) {
      if (existing.payload !== payload) fail('same operation key was reused with a different payload')
      if (existing.status !== status || canonicalJson(existing.response) !== canonicalJson(response)) fail('same operation key was reused with a different response')
      return { status: existing.status, response: JSON.parse(JSON.stringify(existing.response)) as JsonRecord, idempotent: true }
    }
    const stored = { payload, status, response: JSON.parse(JSON.stringify(response)) as JsonRecord }
    this.entries.set(identity, stored)
    return { status, response, idempotent: false }
  }

  async replay(routeId: string, requestValue: unknown): Promise<{ status: number; response: Readonly<JsonRecord>; idempotent: boolean }> {
    const request = parseHttpRequestV2(routeId, requestValue) as JsonRecord
    const key = operationKey(request)
    if (key === null) fail('operation replay requires an operation key')
    const existing = this.entries.get(`${routeId}\0${key}`)
    if (!existing) fail('operation key has no recorded response')
    if (existing.payload !== canonicalJson(request)) fail('same operation key was reused with a different payload')
    return { status: existing.status, response: JSON.parse(JSON.stringify(existing.response)) as JsonRecord, idempotent: true }
  }
}

export const validateHttpRequestV2 = parseHttpRequestV2
export const validateHttpResponseV2 = parseHttpResponseV2
export const validateHttpExchange = validateHttpExchangeV2
export const validateJobSseRecoveryV2 = parseJobSseRecoveryV2
export { parsePublicationCommandV2, parsePublicationResultV2 }
