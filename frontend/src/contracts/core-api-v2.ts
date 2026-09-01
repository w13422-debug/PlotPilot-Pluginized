import candidateSchema from '../../../contracts/json-schema/candidate-query-result-v2.schema.json' with { type: 'json' }
import coreAuthoritySchema from '../../../contracts/json-schema/core-authority-command-query-v2.schema.json' with { type: 'json' }
import reviewSchema from '../../../contracts/json-schema/candidate-review-v2.schema.json' with { type: 'json' }
import projectionSchema from '../../../contracts/json-schema/story-state-projection-input-v2.schema.json' with { type: 'json' }
import jobSchema from '../../../contracts/json-schema/job-http-command-query-v2.schema.json' with { type: 'json' }
import pluginSchema from '../../../contracts/json-schema/plugin-api-command-query-v2.schema.json' with { type: 'json' }
import { parseJsonSchema, type JsonSchema } from './schema-ingress.ts'
import { canonicalJson, hashJcs } from './canonical.ts'

export type JsonRecord = Record<string, any>
export type V2EntityKind = 'document' | 'node_structure' | 'relation_set'
export type V2Status = 'complete' | 'partial'

export interface V2Target {
  workspace_id: string
  entity_kind: V2EntityKind
  entity_id: string
}

export interface CandidateV2 {
  schema: 'candidate/v2'
  candidate_id: string
  workspace_id: string
  item_kind: 'document' | 'node_structure' | 'relation_set' | 'incomplete_stream'
  target: V2Target
  mutation: { mode: string; payload_schema: string; payload_hash: string }
  payload_asset_id: string
  base: { revision_id: string; content_hash: string }
  write_set: V2WriteSetEntry[]
  parent_candidate_ids: string[]
  source_refs: JsonRecord[]
  status: V2Status
  publication_eligibility: 'eligible' | 'review_only' | 'none'
  created_at: string
  source_job_id: string | null
}

export type V2WriteSetEntry = V2Target & { revision_id: string; content_hash: string }

export interface V2CasResult {
  base_revision_id: string
  base_content_hash: string
  revision_id: string
  revision_number: number
  content_hash: string
  write_set: V2WriteSetEntry[]
}

export interface PublicationCommandV2 {
  schema: 'publication-command/v2'
  publication_operation_key: string
  workspace_id: string
  candidate_id: string
  accepted_by: string
}

export interface PublicationResultV2 {
  schema: 'publication-result/v2'
  publication_id: string
  publication_operation_key: string
  candidate_id: string
  workspace_id: string
  entity_kind: V2EntityKind
  entity_id: string
  revision_id: string
  revision_number: number
  cas: V2CasResult
  content_hash: string
  provenance_receipt_id: string
  idempotent: boolean
}

const ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/
const HASH = /^[0-9a-f]{64}$/
const CURSOR = /^(candidate|job|core)\/[A-Za-z0-9][A-Za-z0-9._:/-]*$/

const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:(?:[A-Za-z0-9+/]{2}==)|(?:[A-Za-z0-9+/]{3}=))?$/

function base64Value(code: number): number {
  if (code >= 65 && code <= 90) return code - 65
  if (code >= 97 && code <= 122) return code - 71
  if (code >= 48 && code <= 57) return code + 4
  return code === 43 ? 62 : 63
}

function decodeBase64(value: string): Uint8Array {
  if (!BASE64.test(value)) fail('candidate preview base64 is not canonical base64')
  const padding = value.endsWith('==') ? 2 : value.endsWith('=') ? 1 : 0
  const compactLength = value.length - padding
  if (padding === 2 && (base64Value(value.charCodeAt(compactLength - 1)) & 0x0f) !== 0) fail('candidate preview base64 has non-zero padding bits')
  if (padding === 1 && (base64Value(value.charCodeAt(compactLength - 1)) & 0x03) !== 0) fail('candidate preview base64 has non-zero padding bits')
  const outputLength = Math.floor(compactLength * 6 / 8)
  const output = new Uint8Array(outputLength)
  let accumulator = 0
  let bits = 0
  let outputIndex = 0
  for (let index = 0; index < compactLength; index += 1) {
    accumulator = (accumulator << 6) | base64Value(value.charCodeAt(index))
    bits += 6
    if (bits >= 8) {
      bits -= 8
      output[outputIndex] = (accumulator >> bits) & 0xff
      outputIndex += 1
    }
  }
  return output
}

function rotr(value: number, amount: number): number {
  return (value >>> amount) | (value << (32 - amount))
}

// Keep this synchronous for the same reason as the frozen v1 Asset range
// guard: a complete preview must bind its returned bytes before ingress ends.
function sha256Bytes(bytes: Uint8Array): string {
  const constants = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d,
    0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138,
    0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08,
    0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f,
    0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]
  const paddedLength = Math.ceil((bytes.length + 9) / 64) * 64
  const padded = new Uint8Array(paddedLength)
  padded.set(bytes)
  padded[bytes.length] = 0x80
  const bitLength = bytes.length * 8
  const high = Math.floor(bitLength / 0x100000000)
  const low = bitLength >>> 0
  padded[padded.length - 8] = (high >>> 24) & 0xff
  padded[padded.length - 7] = (high >>> 16) & 0xff
  padded[padded.length - 6] = (high >>> 8) & 0xff
  padded[padded.length - 5] = high & 0xff
  padded[padded.length - 4] = (low >>> 24) & 0xff
  padded[padded.length - 3] = (low >>> 16) & 0xff
  padded[padded.length - 2] = (low >>> 8) & 0xff
  padded[padded.length - 1] = low & 0xff
  let h0 = 0x6a09e667; let h1 = 0xbb67ae85; let h2 = 0x3c6ef372; let h3 = 0xa54ff53a
  let h4 = 0x510e527f; let h5 = 0x9b05688c; let h6 = 0x1f83d9ab; let h7 = 0x5be0cd19
  const words = new Uint32Array(64)
  for (let block = 0; block < padded.length; block += 64) {
    for (let index = 0; index < 16; index += 1) {
      const offset = block + index * 4
      words[index] = ((padded[offset] as number) << 24) | ((padded[offset + 1] as number) << 16) | ((padded[offset + 2] as number) << 8) | (padded[offset + 3] as number)
    }
    for (let index = 16; index < 64; index += 1) {
      const x = words[index - 15] as number; const y = words[index - 2] as number
      words[index] = (words[index - 16] + (rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3)) + words[index - 7] + (rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10))) >>> 0
    }
    let a = h0; let b = h1; let c = h2; let d = h3; let e = h4; let f = h5; let g = h6; let h = h7
    for (let index = 0; index < 64; index += 1) {
      const choose = (e & f) ^ (~e & g)
      const temp1 = (h + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + choose + (constants[index] as number) + (words[index] as number)) >>> 0
      const temp2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0
      h = g; g = f; f = e; e = (d + temp1) >>> 0; d = c; c = b; b = a; a = (temp1 + temp2) >>> 0
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0
    h4 = (h4 + e) >>> 0; h5 = (h5 + f) >>> 0; h6 = (h6 + g) >>> 0; h7 = (h7 + h) >>> 0
  }
  return [h0, h1, h2, h3, h4, h5, h6, h7].map(word => word.toString(16).padStart(8, '0')).join('')
}

// Story-State receipts are checked synchronously while the projection is
// crossing the ingress boundary. Keep this helper beside the existing
// synchronous byte hash so the receipt hash covers the exact JCS bytes
// without changing the async WebCrypto API used by snapshots.
function hashJcsSync(prefix: string, value: unknown): string {
  return sha256Bytes(new TextEncoder().encode(`${prefix}\n${canonicalJson(value)}`))
}

function fail(message: string): never {
  throw new Error(`M4/M5 v2 contract validation failed: ${message}`)
}

function isRecord(value: unknown): value is JsonRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function id(value: unknown, label: string): string {
  if (typeof value !== 'string' || !ID.test(value)) fail(`${label} is not a contract ID`)
  return value
}

function cursor(value: unknown, domain: 'candidate' | 'job' | 'core', jobId?: string): number | null {
  if (value === null) return null
  if (typeof value !== 'string' || !CURSOR.test(value)) fail('cursor is malformed')
  const parts = value.split('/')
  if (parts[0] !== domain) fail(`cursor domain is not ${domain}`)
  if (domain === 'job' && (parts.length !== 3 || (jobId !== undefined && parts[1] !== jobId) || !/^\d+$/.test(parts[2]))) fail('job cursor is not bound to the Job')
  if (domain === 'core' && (parts.length !== 2 || !/^\d+$/.test(parts[1]))) fail('core cursor is malformed')
  if (domain === 'job') return Number(parts[2])
  if (domain === 'core') return Number(parts[1])
  return null
}

export function parseCandidateV2(value: unknown, expectedWorkspaceId?: string): Readonly<CandidateV2> {
  const parsed = parseJsonSchema<CandidateV2>(value, candidateSchema as JsonSchema, 'candidate-query-result/v2')
  validateCandidateV2(parsed, expectedWorkspaceId)
  return parsed
}

export function parseCandidateQueryResultV2(value: unknown): Readonly<JsonRecord> {
  const parsed = parseJsonSchema<JsonRecord>(value, candidateSchema as JsonSchema, 'candidate-query-result/v2')
  if (parsed.schema === 'candidate/v2') validateCandidateV2(parsed)
  if (parsed.schema === 'candidate-list-result/v2') {
    cursor(parsed.next_cursor, 'candidate')
    for (const candidate of parsed.items) validateCandidateV2(candidate, parsed.workspace_id)
  }
  if (parsed.schema === 'candidate-list-query/v2') cursor(parsed.cursor, 'candidate')
  if (parsed.schema === 'candidate-get-result/v2') validateCandidateV2(parsed.candidate, parsed.workspace_id)
  if (parsed.schema === 'candidate-preview-result/v2') validateCandidatePreviewV2(parsed)
  return parsed
}

function validateCandidatePreviewV2(value: JsonRecord): void {
  const end = value.offset + value.length
  if (end > value.total_length) fail('candidate preview exceeds payload length')
  const bytes = decodeBase64(value.base64_chunk)
  if (bytes.byteLength !== value.length) fail('candidate preview decoded length does not match length')
  if (sha256Bytes(bytes) !== value.content_hash) fail('candidate preview content_hash does not match returned bytes')
  if (value.next_offset === null && end !== value.total_length) fail('candidate preview ends before total payload length')
  if (value.next_offset !== null && (value.next_offset !== end || value.next_offset > value.total_length)) fail('candidate preview next_offset is not contiguous')
  if (value.offset === 0 && value.length === value.total_length && value.content_hash !== value.payload_hash) fail('complete candidate preview does not match payload_hash')
}

export function validateCandidateV2(value: unknown, expectedWorkspaceId?: string, parentRecords?: Readonly<Record<string, JsonRecord>>): void {
  if (!isRecord(value) || value.schema !== 'candidate/v2') fail('candidate/v2 discriminator is required')
  const candidate = value as CandidateV2
  if (expectedWorkspaceId !== undefined && candidate.workspace_id !== expectedWorkspaceId) fail('Candidate crosses Workspace')
  if (candidate.target.workspace_id !== candidate.workspace_id) fail('Candidate target crosses Workspace')
  if (candidate.write_set.some(item => item.workspace_id !== candidate.workspace_id)) fail('Candidate write_set crosses Workspace')
  assertWriteSetOrder(candidate.write_set, 'Candidate write_set')
  const target = `${candidate.target.entity_kind}\0${candidate.target.entity_id}`
  const matching = candidate.write_set.filter(item => `${item.entity_kind}\0${item.entity_id}` === target)
  if (matching.length !== 1) fail('Candidate target must occur exactly once in write_set')
  if (matching[0].revision_id !== candidate.base.revision_id || matching[0].content_hash !== candidate.base.content_hash) fail('Candidate base is not bound to target write_set')
  const modes: Record<string, string[]> = {
    document: ['replace', 'text_patch', 'append_text'],
    node_structure: ['structure_patch'],
    relation_set: ['relation_patch'],
    incomplete_stream: ['replace'],
  }
  const expectedKind = candidate.item_kind === 'incomplete_stream' ? 'document' : candidate.item_kind
  if (candidate.target.entity_kind !== expectedKind || !modes[candidate.item_kind].includes(candidate.mutation.mode)) fail('Candidate item_kind/mutation/target binding is invalid')
  if (candidate.item_kind === 'incomplete_stream') {
    if (candidate.status !== 'partial' || candidate.publication_eligibility !== 'eligible' || candidate.source_job_id === null || candidate.mutation.payload_schema !== 'core/document-text/v2') fail('incomplete_stream Candidate is not Core-generated and eligible')
  } else if (candidate.status === 'partial' && candidate.publication_eligibility === 'eligible') {
    fail('normal partial Candidate is review-only')
  }
  if (candidate.status !== 'complete' && candidate.status !== 'partial') fail('failed/skipped staging outcome cannot cross the Candidate query/review surface')
  if (parentRecords !== undefined) validateParentClosure(candidate, parentRecords)
}

function assertWriteSetOrder(writeSet: V2WriteSetEntry[], label: string): void {
  const identities = writeSet.map(item => `${item.workspace_id}\0${item.entity_kind}\0${item.entity_id}`)
  if (new Set(identities).size !== identities.length) fail(`${label} contains a duplicate entity identity`)
  const sortedIdentities = [...identities].sort()
  if (identities.some((identity, index) => identity !== sortedIdentities[index])) fail(`${label} is not in stable entity identity order`)
}

function validateParentClosure(candidate: CandidateV2, parentRecords: Readonly<Record<string, JsonRecord>>): void {
  const visiting = new Set<string>()
  const visited = new Set<string>()
  const visit = (candidateId: string): void => {
    if (visiting.has(candidateId)) fail('Candidate parent graph contains a cycle')
    if (visited.has(candidateId)) return
    const node = candidateId === candidate.candidate_id ? candidate as unknown as JsonRecord : parentRecords[candidateId]
    if (!node) fail(`Candidate parent closure is incomplete: ${candidateId}`)
    if (['rejected', 'deleted', 'expired'].includes(node.status)) fail(`Candidate parent is not live: ${candidateId}`)
    visiting.add(candidateId)
    for (const parentId of node.parent_candidate_ids ?? []) visit(parentId)
    visiting.delete(candidateId)
    visited.add(candidateId)
  }
  visit(candidate.candidate_id)
}

export function parseCoreAuthorityV2(value: unknown): Readonly<JsonRecord> {
  return parseJsonSchema<JsonRecord>(value, coreAuthoritySchema as JsonSchema, 'core-authority-command-query-v2')
}

export function parseCandidateReviewV2(value: unknown): Readonly<JsonRecord> {
  return parseJsonSchema<JsonRecord>(value, reviewSchema as JsonSchema, 'candidate-review-v2')
}

export function validatePluginLifecycleV2(value: unknown, options?: { currentGenerationId?: string; activeJob?: boolean }): void {
  const command = parseJsonSchema<JsonRecord>(value, pluginSchema as JsonSchema, 'plugin-api-command-query-v2')
  if (command.schema !== 'plugin-lifecycle-command/v2') fail('plugin lifecycle validation requires a command')
  if (options?.currentGenerationId !== undefined && command.expected_generation_id !== options.currentGenerationId) fail('plugin generation CAS is stale')
  if (options?.activeJob === true && command.action === 'retire') fail('active Job pins the plugin release')
  if (['install', 'upgrade', 'rollback'].includes(command.action) && command.target_generation_id === null) fail('plugin lifecycle command has no target generation')
  if (['install', 'upgrade'].includes(command.action) && (command.release_id === null || command.package_hash === null)) fail('plugin lifecycle install/upgrade must bind release and package')
}

export function parsePluginApiV2(value: unknown): Readonly<JsonRecord> {
  const parsed = parseJsonSchema<JsonRecord>(value, pluginSchema as JsonSchema, 'plugin-api-command-query-v2')
  if (parsed.schema === 'plugin-lifecycle-command/v2') validatePluginLifecycleV2(parsed)
  return parsed
}

export function parsePublicationCommandV2(value: unknown): Readonly<PublicationCommandV2> {
  const parsed = parseCoreAuthorityV2(value)
  if (parsed.schema !== 'publication-command/v2') fail('Publication command discriminator is required')
  return parsed as PublicationCommandV2
}

export function parsePublicationResultV2(value: unknown): Readonly<PublicationResultV2> {
  const parsed = parseCoreAuthorityV2(value)
  if (parsed.schema !== 'publication-result/v2') fail('Publication result discriminator is required')
  return parsed as PublicationResultV2
}

export function validatePublicationV2(commandValue: unknown, resultValue: unknown, candidateValue?: unknown, expectedWorkspaceId?: string): void {
  const command = parsePublicationCommandV2(commandValue)
  const result = parsePublicationResultV2(resultValue)
  if (command.publication_operation_key !== result.publication_operation_key || command.candidate_id !== result.candidate_id || command.workspace_id !== result.workspace_id) fail('Publication result does not match its command')
  if (expectedWorkspaceId !== undefined && command.workspace_id !== expectedWorkspaceId) fail('Publication crosses Workspace')
  if (result.cas.revision_id !== result.revision_id || result.cas.revision_number !== result.revision_number || result.cas.content_hash !== result.content_hash) fail('Publication result is not bound to the Core CAS Revision')
  assertWriteSetOrder(result.cas.write_set, 'Publication CAS write_set')
  if (candidateValue !== undefined) {
    const candidate = parseCandidateV2(candidateValue, command.workspace_id)
    if (candidate.candidate_id !== result.candidate_id || candidate.target.entity_kind !== result.entity_kind || candidate.target.entity_id !== result.entity_id) fail('Publication result is not bound to Candidate target')
    if (candidate.publication_eligibility !== 'eligible' || candidate.status === 'failed' || candidate.status === 'skipped') fail('Candidate is not publishable')
    if (candidate.status === 'partial' && candidate.item_kind !== 'incomplete_stream') fail('normal partial Candidate is review-only')
    const targetWrite = candidate.write_set.find(item => item.entity_kind === candidate.target.entity_kind && item.entity_id === candidate.target.entity_id)
    if (!targetWrite || result.cas.base_revision_id !== targetWrite.revision_id || result.cas.base_content_hash !== targetWrite.content_hash) fail('Publication result CAS base is not bound to Candidate write_set')
    if (result.cas.write_set.length !== candidate.write_set.length || result.cas.write_set.some((entry, index) => {
      const expected = candidate.write_set[index]
      return entry.workspace_id !== expected.workspace_id || entry.entity_kind !== expected.entity_kind || entry.entity_id !== expected.entity_id || entry.revision_id !== expected.revision_id || entry.content_hash !== expected.content_hash
    })) fail('Publication result CAS write_set is not bound to Candidate write_set')
    // The mutation payload Asset/hash is independently validated on Candidate;
    // the result hash is the final Revision hash computed by Core and may differ.
  }
}

export function parseStoryStateProjectionInputV2(value: unknown, expectedWorkspaceId?: string): Readonly<JsonRecord> {
  const parsed = parseJsonSchema<JsonRecord>(value, projectionSchema as JsonSchema, 'story-state-projection-input-v2')
  validateStoryStateProjectionV2(parsed, expectedWorkspaceId)
  return parsed
}

export function validateStoryStateProjectionV2(value: JsonRecord, expectedWorkspaceId?: string): void {
  if (expectedWorkspaceId !== undefined && value.workspace_id !== expectedWorkspaceId) fail('Story-State projection crosses Workspace')
  if (value.publication.candidate_id !== value.candidate.candidate_id || value.publication.revision_id !== value.current_revision.revision_id) fail('projection references are not bound')
  if (value.publication.revision_number !== value.current_revision.revision_number || value.publication.content_hash !== value.current_revision.content_hash) fail('projection Publication/current Revision mismatch')
  if (value.candidate.target.workspace_id !== value.workspace_id) fail('projection Candidate crosses Workspace')
  const assets = new Map<string, JsonRecord>()
  for (const item of value.assets as JsonRecord[]) {
    if (assets.has(item.asset_id)) fail(`projection Asset identity is duplicated: ${item.asset_id}`)
    assets.set(item.asset_id, item)
  }
  const requiredAssetIds = new Set([value.candidate.payload_asset_id, value.current_revision.content_asset_id])
  if (assets.size !== requiredAssetIds.size || [...assets.keys()].some(assetId => !requiredAssetIds.has(assetId))) fail('projection Asset closure is not exactly the reachable Asset set')
  const payloadAsset = assets.get(value.candidate.payload_asset_id)
  const contentAsset = assets.get(value.current_revision.content_asset_id)
  if (!payloadAsset || payloadAsset.role !== 'candidate.payload' || payloadAsset.sha256 !== value.candidate.payload_hash) fail('projection Candidate payload is absent from Asset closure')
  if (!contentAsset || contentAsset.role !== 'revision.content' || contentAsset.sha256 !== value.current_revision.content_hash) fail('projection Revision content is absent from Asset closure')
  const receipts = new Map<string, JsonRecord>()
  for (const item of value.receipt_closure as JsonRecord[]) {
    if (receipts.has(item.receipt_id)) fail(`projection receipt identity is duplicated: ${item.receipt_id}`)
    receipts.set(item.receipt_id, item)
  }
  const root = receipts.get(value.provenance.receipt_id)
  if (!root || root.receipt_hash !== value.provenance.receipt_hash) fail('projection provenance root is absent from receipt closure')
  if (JSON.stringify(root.parent_receipt_ids) !== JSON.stringify(value.provenance.parent_receipt_ids)) fail('projection provenance parent binding is inconsistent')
  const visiting = new Set<string>()
  const visited = new Set<string>()
  const visit = (receiptId: string): void => {
    if (visiting.has(receiptId)) fail('provenance receipt closure contains a cycle')
    if (visited.has(receiptId)) return
    const receipt = receipts.get(receiptId)
    if (!receipt) fail(`provenance receipt parent is missing: ${receiptId}`)
    const unsigned = { ...receipt }
    delete unsigned.receipt_hash
    if (receipt.receipt_hash !== hashJcsSync('story-state-receipt/v2', unsigned)) fail(`provenance receipt hash mismatch: ${receiptId}`)
    visiting.add(receiptId)
    for (const parentId of receipt.parent_receipt_ids) visit(parentId)
    visiting.delete(receiptId)
    visited.add(receiptId)
  }
  visit(value.provenance.receipt_id)
  if (visited.size !== receipts.size || [...receipts.keys()].some(receiptId => !visited.has(receiptId))) fail('projection receipt closure contains an unreachable receipt')
}

export async function verifyJobSnapshotHashV2(snapshot: JsonRecord): Promise<void> {
  if (snapshot.job_event_high_water < 0 || snapshot.core_event_high_water < 0) fail('Job event high-water cannot be negative')
  for (const stream of snapshot.stream_high_waters) {
    if (stream.acked_bytes < 0 || stream.acked_prefix_seq < 0) fail('stream high-water cannot be negative')
    if (stream.target.workspace_id !== snapshot.workspace_id) fail('stream target crosses Job Workspace')
  }
  const unsigned = { ...snapshot }
  delete unsigned.snapshot_hash
  if (snapshot.snapshot_hash !== await hashJcs('job-snapshot/v2', unsigned)) fail('Job snapshot hash mismatch')
}

export function parseJobEventPageV2(value: unknown): Readonly<JsonRecord> {
  const parsed = parseJsonSchema<JsonRecord>(value, jobSchema as JsonSchema, 'job-http-command-query-v2')
  if (parsed.schema !== 'job-event-page-result/v2') fail('Job event page discriminator is required')
  const nextSeq = cursor(parsed.next_cursor, 'job', parsed.job_id)
  if (nextSeq === null || nextSeq > parsed.high_water_seq) fail('Job event next cursor is ahead of its high-water')
  for (const event of parsed.events) validateJobEventV2(event, parsed.job_id)
  if (parsed.events.length > 0 && parsed.events[parsed.events.length - 1].job_event_seq > parsed.high_water_seq) fail('Job event page exceeds high-water')
  for (let index = 1; index < parsed.events.length; index += 1) {
    if (parsed.events[index - 1].job_event_seq >= parsed.events[index].job_event_seq) fail('Job events are not strictly ordered')
  }
  if (parsed.events.some((event: JsonRecord) => event.job_id !== parsed.job_id)) fail('Job event crosses Job identity')
  return parsed
}

export function validateJobEventV2(event: JsonRecord, jobId: string): void {
  if (event.job_id !== jobId) fail('Job event crosses Job identity')
  if ((event.payload_asset_id === null) !== (event.payload_hash === null)) fail('Job event payload Asset ID and hash must be paired')
}

export function validateJobEventPageQueryV2(value: JsonRecord): void {
  const afterSeq = cursor(value.after_cursor, 'job', value.job_id)
  if (afterSeq === null ? value.after_job_event_seq !== 0 : afterSeq !== value.after_job_event_seq) fail('Job event query cursor and sequence are not paired')
}

export function validateJobSseRecoveryQueryV2(value: JsonRecord): void {
  const afterSeq = cursor(value.last_event_id, 'job', value.job_id)
  if (afterSeq === null || afterSeq !== value.after_seq) fail('SSE after_seq and last_event_id are not paired')
}

export async function validateJobSnapshotResultV2(value: JsonRecord): Promise<void> {
  if (value.workspace_id !== value.snapshot.workspace_id || value.job_id !== value.snapshot.job_id) fail('Job snapshot result is not bound to its outer Workspace and Job')
  cursor(value.cursor, 'job', value.job_id)
  await verifyJobSnapshotHashV2(value.snapshot)
}

export async function validateJobListResultV2(value: JsonRecord): Promise<void> {
  cursor(value.next_cursor, 'job')
  for (const snapshot of value.items) {
    if (snapshot.workspace_id !== value.workspace_id) fail('Job list item crosses its Workspace')
    await verifyJobSnapshotHashV2(snapshot)
  }
}

export function validateJobCommandResultV2(value: JsonRecord): void {
  cursor(value.snapshot_cursor, 'job', value.job_id)
}

export const parseCoreAuthorityCommandQueryV2 = parseCoreAuthorityV2
export const parseCandidateReviewCommandQueryV2 = parseCandidateReviewV2
export const parseStoryStateProjectionV2 = parseStoryStateProjectionInputV2
export const parsePublicationV2 = parseCoreAuthorityV2

// Keep a single canonicalizer in the module's public surface for test probes.
export { canonicalJson }
