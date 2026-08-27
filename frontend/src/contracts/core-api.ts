import coreAuthoritySchema from '../../../contracts/json-schema/core-authority-command-query-v1.schema.json' with { type: 'json' }
import publicationSchema from '../../../contracts/json-schema/publication-command-result-v1.schema.json' with { type: 'json' }
import assetMetadataSchema from '../../../contracts/json-schema/asset-metadata-v1.schema.json' with { type: 'json' }
import operationContextIdentitySchema from '../../../contracts/json-schema/operation-context-identity-v1.schema.json' with { type: 'json' }
import exportCurrentRevisionsSchema from '../../../contracts/json-schema/export-current-revisions-v1.schema.json' with { type: 'json' }
import coreApiMethodMatrix from '../../../contracts/json-schema/core-api-method-matrix.v1.json' with { type: 'json' }
import { parseJsonSchema, type JsonSchema } from './schema-ingress.ts'
import { canonicalJson, sha256Hex, utf8 } from './canonical.ts'
import { verifySnapshot } from './verifier.ts'
import { CoreHttpContractFixture } from './types.ts'
import type {
  AssetMetadataResponse,
  AssetReadRange,
  CoreAuthorityCommandQuery,
  CoreAuthorityValidationOptions,
  ExportCurrentRevisions,
  ExportValidationOptions,
  PublicationCommand,
  PublicationCommandResult,
  PublicationValidationOptions,
  PublicationResult,
  RunSnapshot,
} from './types.ts'

const ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/
const HASH = /^[0-9a-f]{64}$/
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:(?:[A-Za-z0-9+/]{2}==)|(?:[A-Za-z0-9+/]{3}=)|(?:[A-Za-z0-9+/]{2,3}))?$/

type JsonRecord = Record<string, unknown>

function fail(message: string): never {
  throw new Error(`Core contract validation failed: ${message}`)
}

function isRecord(value: unknown): value is JsonRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function hasOwn(value: JsonRecord, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key)
}

function stringField(value: JsonRecord, key: string, label = key): string {
  const candidate = value[key]
  if (typeof candidate !== 'string' || candidate.length === 0) fail(`${label} must be a non-empty string`)
  return candidate
}

function idField(value: JsonRecord, key: string, label = key): string {
  const candidate = stringField(value, key, label)
  if (!ID.test(candidate)) fail(`${label} is not a contract ID`)
  return candidate
}

function hashField(value: JsonRecord, key: string, label = key): string {
  const candidate = stringField(value, key, label)
  if (!HASH.test(candidate)) fail(`${label} is not a lowercase SHA-256`)
  return candidate
}

function nonNegativeInteger(value: unknown, label: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) fail(`${label} must be a non-negative integer`)
  return value
}

function walkWorkspaceBindings(value: unknown, workspaceId: string, path = '$', seen = new WeakSet<object>()): void {
  if (value === null || typeof value !== 'object') return
  if (seen.has(value)) fail(`cyclic workspace binding at ${path}`)
  seen.add(value)
  try {
    if (Array.isArray(value)) {
      value.forEach((item, index) => walkWorkspaceBindings(item, workspaceId, `${path}[${index}]`, seen))
      return
    }
    const record = value as JsonRecord
    if (hasOwn(record, 'workspace_id') && record.workspace_id !== null && record.workspace_id !== workspaceId) fail(`cross-workspace reference at ${path}.workspace_id`)
    for (const key of Object.keys(record)) walkWorkspaceBindings(record[key], workspaceId, `${path}.${key}`, seen)
  } finally {
    seen.delete(value)
  }
}

function validateCoreAuthoritySemantics(value: CoreAuthorityCommandQuery, options?: CoreAuthorityValidationOptions): void {
  const record = value as unknown as JsonRecord
  const schema = stringField(record, 'schema')
  if (hasOwn(record, 'workspace_id')) {
    const workspaceValue = record.workspace_id
    // The collection query deliberately permits null to mean "all
    // workspaces".  It is not an identity and must not be passed through the
    // non-null ID validator.
    if (workspaceValue !== null) {
      const workspaceId = idField(record, 'workspace_id')
      walkWorkspaceBindings(record, workspaceId)
    }
  }
  if (hasOwn(record, 'operation_key')) idField(record, 'operation_key')
  if (options?.expectedWorkspaceId !== undefined) {
    if (!ID.test(options.expectedWorkspaceId)) fail('expected workspace is not a contract ID')
    walkWorkspaceBindings(record, options.expectedWorkspaceId)
  }
  if (schema === 'core-workspace-update-command/v1' && record.title === null && record.status === null) {
    fail('workspace update must change title or status')
  }
  if (schema === 'core-revision/v1' && ((record.document_id === null) === (record.node_id === null))) {
    fail('Core Revision must target exactly one document or node')
  }
  if (schema.endsWith('-page/v1') && schema !== 'core-revision-content-page/v1') {
    const items = record.items
    if (!Array.isArray(items)) fail('Core page items must be an array')
    const offset = nonNegativeInteger(record.offset, 'offset')
    const total = nonNegativeInteger(record.total, 'total')
    const expectedNext = offset + items.length < total ? offset + items.length : null
    if (record.next_offset !== expectedNext) fail('Core page next_offset is not bound to offset/items/total')
  }
  if (schema === 'core-revision-content-page/v1') {
    const offset = nonNegativeInteger(record.offset, 'offset')
    const length = nonNegativeInteger(record.length, 'length')
    const totalLength = nonNegativeInteger(record.total_length, 'total_length')
    if (typeof record.text !== 'string' || [...record.text].length !== length || offset + length > totalLength) {
      fail('revision content page text/range mismatch')
    }
    const expectedNext = offset + length < totalLength ? offset + length : null
    if (record.next_offset !== expectedNext) fail('revision content next_offset mismatch')
  }
}

function revisionReference(value: JsonRecord): JsonRecord | undefined {
  for (const key of ['resulting_revision', 'revision_ref', 'revision']) {
    if (hasOwn(value, key)) {
      const candidate = value[key]
      if (!isRecord(candidate)) fail(`${key} must be a closed revision reference object`)
      return candidate
    }
  }
  return undefined
}

function validateRevisionBinding(value: JsonRecord): void {
  const workspaceId = idField(value, 'workspace_id')
  const entityId = idField(value, 'entity_id')
  const entityKind = stringField(value, 'entity_kind')
  const reference = revisionReference(value)
  if (reference === undefined) fail('Publication result must carry resulting revision identity')
  idField(reference, 'revision_id', 'resulting revision_id')
  if (hasOwn(reference, 'workspace_id') && reference.workspace_id !== workspaceId) fail('resulting revision crosses workspace')
  if (hasOwn(reference, 'entity_kind') && reference.entity_kind !== entityKind) fail('resulting revision entity kind does not match result')
  if (hasOwn(reference, 'entity_id') && reference.entity_id !== entityId) fail('resulting revision entity ID does not match result')
  if (entityKind === 'document' && hasOwn(reference, 'document_id') && reference.document_id !== entityId) fail('document revision reference does not match publication entity')
  if (entityKind === 'node_structure' && hasOwn(reference, 'node_id') && reference.node_id !== entityId) fail('node revision reference does not match publication entity')
}

function validatePublicationSemantics(value: PublicationCommandResult, options?: PublicationValidationOptions): void {
  const record = value as unknown as JsonRecord
  if (hasOwn(record, 'publication_operation_key')) idField(record, 'publication_operation_key')
  if (hasOwn(record, 'workspace_id')) idField(record, 'workspace_id')
  if (hasOwn(record, 'candidate_id')) idField(record, 'candidate_id')
  if (hasOwn(record, 'accepted_by')) idField(record, 'accepted_by')
  if (options?.expectedWorkspaceId !== undefined && record.workspace_id !== options.expectedWorkspaceId) {
    fail('Publication crosses expected workspace')
  }
  if (hasOwn(record, 'publication_id')) {
    idField(record, 'publication_id')
    if (typeof record.idempotent !== 'boolean') fail('Publication result idempotent must be boolean')
    validateRevisionBinding(record)
    walkWorkspaceBindings(record, stringField(record, 'workspace_id'))
    if (options?.command !== undefined) {
      const command = parsePublicationCommandResultV1<PublicationCommand>(options.command)
      if (command.schema !== 'publication-command/v1') fail('Publication binding command has the wrong schema')
      if (command.workspace_id !== record.workspace_id || command.candidate_id !== record.candidate_id) {
        fail('Publication result does not match its command')
      }
    }
  }
}

function decodeBase64(value: string): Uint8Array {
  if (!BASE64.test(value) || value.length % 4 === 1) fail('base64_chunk is not canonical base64')
  const padding = value.endsWith('==') ? 2 : value.endsWith('=') ? 1 : 0
  const compactLength = value.length - padding
  const outputLength = Math.floor(compactLength * 6 / 8)
  const output = new Uint8Array(outputLength)
  let accumulator = 0
  let bits = 0
  let outputIndex = 0
  for (let index = 0; index < compactLength; index += 1) {
    const code = value.charCodeAt(index)
    const sixBits = code >= 65 && code <= 90
      ? code - 65
      : code >= 97 && code <= 122
        ? code - 71
        : code >= 48 && code <= 57
          ? code + 4
          : code === 43 ? 62 : 63
    accumulator = (accumulator << 6) | sixBits
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

/* Synchronous SHA-256 is used only for a complete range page, so a changed
 * content_hash cannot be hidden behind an asynchronous WebCrypto call. */
function sha256Bytes(bytes: Uint8Array): string {
  const constants = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
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

  let h0 = 0x6a09e667
  let h1 = 0xbb67ae85
  let h2 = 0x3c6ef372
  let h3 = 0xa54ff53a
  let h4 = 0x510e527f
  let h5 = 0x9b05688c
  let h6 = 0x1f83d9ab
  let h7 = 0x5be0cd19
  const words = new Uint32Array(64)
  for (let block = 0; block < padded.length; block += 64) {
    for (let index = 0; index < 16; index += 1) {
      const offset = block + index * 4
      words[index] = ((padded[offset] as number) << 24) | ((padded[offset + 1] as number) << 16) | ((padded[offset + 2] as number) << 8) | (padded[offset + 3] as number)
    }
    for (let index = 16; index < 64; index += 1) {
      const x = words[index - 15] as number
      const y = words[index - 2] as number
      const smallSigma0 = rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3)
      const smallSigma1 = rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10)
      words[index] = (words[index - 16] + smallSigma0 + words[index - 7] + smallSigma1) >>> 0
    }
    let a = h0; let b = h1; let c = h2; let d = h3; let e = h4; let f = h5; let g = h6; let h = h7
    for (let index = 0; index < 64; index += 1) {
      const bigSigma1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)
      const choose = (e & f) ^ (~e & g)
      const temp1 = (h + bigSigma1 + choose + (constants[index] as number) + (words[index] as number)) >>> 0
      const bigSigma0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)
      const majority = (a & b) ^ (a & c) ^ (b & c)
      const temp2 = (bigSigma0 + majority) >>> 0
      h = g; g = f; f = e; e = (d + temp1) >>> 0; d = c; c = b; b = a; a = (temp1 + temp2) >>> 0
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0
    h4 = (h4 + e) >>> 0; h5 = (h5 + f) >>> 0; h6 = (h6 + g) >>> 0; h7 = (h7 + h) >>> 0
  }
  return [h0, h1, h2, h3, h4, h5, h6, h7].map(word => word.toString(16).padStart(8, '0')).join('')
}

/** Parse raw JSON without accepting duplicate object members. */
class StrictJsonBytesParser {
  private index = 0
  private readonly text: string

  constructor(text: string) {
    this.text = text
  }

  parse(): unknown {
    this.skipWhitespace()
    const value = this.parseValue()
    this.skipWhitespace()
    if (this.index !== this.text.length) fail('JSON Asset has trailing data')
    return value
  }

  private skipWhitespace(): void {
    while (this.index < this.text.length && /[ \t\r\n]/.test(this.text[this.index] as string)) this.index += 1
  }

  private parseValue(): unknown {
    const current = this.text[this.index]
    if (current === '{') return this.parseObject()
    if (current === '[') return this.parseArray()
    if (current === '"') return this.parseString()
    if (current === 't' && this.text.startsWith('true', this.index)) { this.index += 4; return true }
    if (current === 'f' && this.text.startsWith('false', this.index)) { this.index += 5; return false }
    if (current === 'n' && this.text.startsWith('null', this.index)) { this.index += 4; return null }
    return this.parseNumber()
  }

  private parseObject(): JsonRecord {
    this.index += 1
    const result: JsonRecord = Object.create(null) as JsonRecord
    const keys = new Set<string>()
    this.skipWhitespace()
    if (this.text[this.index] === '}') { this.index += 1; return result }
    while (true) {
      this.skipWhitespace()
      if (this.text[this.index] !== '"') fail('JSON object key must be a string')
      const key = this.parseString()
      if (typeof key !== 'string') fail('JSON object key must be a string')
      if (keys.has(key)) fail(`JSON object contains duplicate key ${JSON.stringify(key)}`)
      keys.add(key)
      this.skipWhitespace()
      if (this.text[this.index] !== ':') fail('JSON object member is missing a colon')
      this.index += 1
      this.skipWhitespace()
      result[key] = this.parseValue()
      this.skipWhitespace()
      if (this.text[this.index] === '}') { this.index += 1; return result }
      if (this.text[this.index] !== ',') fail('JSON object member is missing a comma')
      this.index += 1
    }
  }

  private parseArray(): unknown[] {
    this.index += 1
    const result: unknown[] = []
    this.skipWhitespace()
    if (this.text[this.index] === ']') { this.index += 1; return result }
    while (true) {
      this.skipWhitespace()
      result.push(this.parseValue())
      this.skipWhitespace()
      if (this.text[this.index] === ']') { this.index += 1; return result }
      if (this.text[this.index] !== ',') fail('JSON array item is missing a comma')
      this.index += 1
    }
  }

  private parseString(): string {
    const start = this.index
    this.index += 1
    while (this.index < this.text.length) {
      const code = this.text.charCodeAt(this.index)
      if (code < 0x20) fail('JSON string contains a control character')
      if (this.text[this.index] === '"') {
        this.index += 1
        const token = this.text.slice(start, this.index)
        try {
          const value: unknown = JSON.parse(token)
          if (typeof value !== 'string') fail('JSON string token is invalid')
          return value
        } catch (error) {
          if (error instanceof Error && error.message.startsWith('Core contract validation failed:')) throw error
          fail('JSON string escape is invalid')
        }
      }
      if (this.text[this.index] === '\\') {
        this.index += 1
        const escape = this.text[this.index]
        if (escape === 'u') {
          const hex = this.text.slice(this.index + 1, this.index + 5)
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) fail('JSON Unicode escape is invalid')
          this.index += 5
        } else if (escape !== '"' && escape !== '\\' && escape !== '/' && escape !== 'b' && escape !== 'f' && escape !== 'n' && escape !== 'r' && escape !== 't') {
          fail('JSON escape is invalid')
        } else {
          this.index += 1
        }
      } else {
        this.index += 1
      }
    }
    fail('JSON string is unterminated')
  }

  private parseNumber(): number {
    const match = this.text.slice(this.index).match(/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/)
    if (match === null) fail(`invalid JSON token at byte ${this.index}`)
    this.index += match[0].length
    const value = Number(match[0])
    if (!Number.isFinite(value)) fail('JSON number is not finite')
    return value
  }
}

function parseStrictJsonBytes(bytes: Uint8Array): unknown {
  if (bytes.byteLength >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) fail('UTF-8 BOM is forbidden')
  let text: string
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    fail('Export Asset must be strict UTF-8 JSON')
  }
  return new StrictJsonBytesParser(text).parse()
}

function validateAssetMetadataSemantics(value: AssetMetadataResponse): void {
  const record = value as unknown as JsonRecord
  idField(record, 'asset_id')
  if (record.schema === 'asset-query/v1') return
  if (record.schema === 'asset-read-range-query/v1') {
    nonNegativeInteger(record.offset, 'offset')
    nonNegativeInteger(record.length, 'length')
    return
  }

  if (record.schema === 'asset-metadata/v1') {
    hashField(record, 'sha256')
    nonNegativeInteger(record.size, 'size')
    return
  }

  if (record.schema !== 'asset-read-range/v1') fail('unknown Asset metadata variant')
  const offset = nonNegativeInteger(record.offset, 'offset')
  const length = nonNegativeInteger(record.length, 'length')
  const totalSize = nonNegativeInteger(record.total_size, 'total_size')
  if (offset > totalSize || length > totalSize - offset) fail('Asset range exceeds total_size')
  if (typeof record.base64_chunk !== 'string') fail('base64_chunk must be a string')
  const bytes = decodeBase64(record.base64_chunk)
  if (bytes.byteLength !== length) fail(`base64 byte length ${bytes.byteLength} does not equal declared length ${length}`)
  const contentHash = hashField(record, 'content_hash')
  if (sha256Bytes(bytes) !== contentHash) fail('range content_hash does not match the returned bytes')
  const expectedNext = offset + length < totalSize ? offset + length : null
  if (record.next_offset !== expectedNext) fail('range next_offset is not bound to offset + length')
}

/** Bind the metadata and range forms for one immutable Core Asset. */
export function verifyAssetMetadataRangePair(metadataValue: unknown, rangeValue: unknown): void {
  const metadata = parseAssetMetadataV1(metadataValue) as Readonly<Extract<AssetMetadataResponse, { schema: 'asset-metadata/v1' }>>
  const range = parseAssetReadRangeV1(rangeValue)
  if (metadata.schema !== 'asset-metadata/v1') fail('metadata value has the wrong Asset variant')
  if (metadata.asset_id !== range.asset_id || metadata.size !== range.total_size) fail('Asset metadata/range identity or size mismatch')
  if (range.offset === 0 && range.length === range.total_size && metadata.sha256 !== range.content_hash) {
    fail('complete Asset range hash does not match metadata')
  }
}

function validateExportSemantics(value: ExportCurrentRevisions, options?: ExportValidationOptions): void {
  const rawOptions = options as (ExportValidationOptions & { exportAssetId?: unknown; exportAssetSha256?: unknown }) | undefined
  if (rawOptions?.exportAssetId !== undefined || rawOptions?.exportAssetSha256 !== undefined) {
    fail('object-only Export parsing cannot bind a caller-supplied Asset hash; use verifyExportCurrentRevisionsAssetV1')
  }
  const record = value as unknown as JsonRecord
  const workspaceId = idField(record, 'workspace_id')
  const expectedWorkspace = options?.expectedWorkspaceId
  if (expectedWorkspace !== undefined && workspaceId !== expectedWorkspace) fail('Export workspace does not match expected workspace')
  if (hasOwn(record, 'expected_workspace_id') && record.expected_workspace_id !== workspaceId) fail('embedded expected workspace does not match export workspace')
  const entries = record.ordered_revisions
  if (!Array.isArray(entries)) fail('ordered_revisions must be an array')
  const documents = new Set<string>()
  const revisions = new Set<string>()
  const assets = new Set<string>()
  entries.forEach((entry, index) => {
    if (!isRecord(entry)) fail(`ordered_revisions[${index}] must be an object`)
    if (entry.ordinal !== index) fail(`ordered_revisions[${index}] ordinal is not contiguous from zero`)
    const documentId = idField(entry, 'document_id', `ordered_revisions[${index}].document_id`)
    const revisionId = idField(entry, 'revision_id', `ordered_revisions[${index}].revision_id`)
    const assetId = idField(entry, 'content_asset_id', `ordered_revisions[${index}].content_asset_id`)
    if (documents.has(documentId)) fail('Export repeats a document')
    if (revisions.has(revisionId)) fail('Export repeats a revision')
    if (assets.has(assetId)) fail('Export repeats a content Asset')
    documents.add(documentId); revisions.add(revisionId); assets.add(assetId)
    if (hasOwn(entry, 'workspace_id') && entry.workspace_id !== workspaceId) fail('Export entry crosses workspace')
    hashField(entry, 'content_hash', `ordered_revisions[${index}].content_hash`)
  })

  const snapshot = options?.runSnapshot
  if (snapshot === undefined) return
  if (snapshot.workspace_id !== workspaceId) fail('Export does not match RunSnapshot workspace')
  const inputByDocument = new Map(snapshot.input_revisions.map(item => [item.document_id, item]))
  if (inputByDocument.size !== snapshot.input_revisions.length || entries.length !== snapshot.input_revisions.length) {
    fail('Export revisions are not an exact projection of RunSnapshot input_revisions')
  }
  for (const entry of entries as JsonRecord[]) {
    const input = inputByDocument.get(stringField(entry, 'document_id'))
    if (input === undefined || input.revision_id !== entry.revision_id || input.content_hash !== entry.content_hash) {
      fail('Export revision does not match the RunSnapshot input revision')
    }
  }
}

export function parseCoreAuthorityCommandQueryV1<T extends CoreAuthorityCommandQuery = CoreAuthorityCommandQuery>(value: unknown, options?: CoreAuthorityValidationOptions): Readonly<T> {
  const parsed = parseJsonSchema<T>(value, coreAuthoritySchema as JsonSchema, 'core-authority-command-query/v1')
  validateCoreAuthoritySemantics(parsed as unknown as CoreAuthorityCommandQuery, options)
  return parsed
}

export function verifyCoreAuthorityCommandQueryV1(value: unknown, options?: CoreAuthorityValidationOptions): void {
  parseCoreAuthorityCommandQueryV1(value, options)
}

export const parseCoreAuthorityV1 = parseCoreAuthorityCommandQueryV1

export function parsePublicationCommandResultV1<T extends PublicationCommandResult = PublicationCommandResult>(value: unknown, options?: PublicationValidationOptions): Readonly<T> {
  const parsed = parseJsonSchema<T>(value, publicationSchema as JsonSchema, 'publication-command-result/v1')
  validatePublicationSemantics(parsed as unknown as PublicationCommandResult, options)
  return parsed
}

export function verifyPublicationCommandResultV1(value: unknown, options?: PublicationValidationOptions): void {
  parsePublicationCommandResultV1(value, options)
}

export function parsePublicationResultV1(value: unknown): Readonly<PublicationResult> {
  const parsed = parsePublicationCommandResultV1(value) as Readonly<PublicationResult>
  if (!('publication_id' in parsed)) fail('value is not a Publication result')
  return parsed
}

export function parseAssetMetadataV1<T extends AssetMetadataResponse = AssetMetadataResponse>(value: unknown): Readonly<T> {
  const parsed = parseJsonSchema<T>(value, assetMetadataSchema as JsonSchema, 'asset-metadata/v1')
  validateAssetMetadataSemantics(parsed as unknown as AssetMetadataResponse)
  return parsed
}

export async function verifyAssetMetadataV1(value: unknown): Promise<void> {
  const parsed = parseAssetMetadataV1(value)
  const record = parsed as unknown as JsonRecord
  if (hasOwn(record, 'base64_chunk') && record.offset === 0 && record.length === record.total_size) {
    const bytes = decodeBase64(stringField(record, 'base64_chunk'))
    const expected = await sha256Hex(bytes)
    if (expected !== record.content_hash) fail('complete Asset range hash mismatch')
  }
}

export function parseAssetReadRangeV1(value: unknown): Readonly<AssetReadRange> {
  const parsed = parseAssetMetadataV1(value) as Readonly<AssetReadRange>
  if (!('base64_chunk' in parsed)) fail('value is not an Asset read range')
  return parsed
}

export function parseOperationContextIdentityV1<T = unknown>(value: unknown): Readonly<T> {
  return parseJsonSchema<T>(value, operationContextIdentitySchema as JsonSchema, 'operation-context-identity/v1')
}

export function verifyOperationContextIdentityV1(value: unknown): void {
  parseOperationContextIdentityV1(value)
}

export function parseExportCurrentRevisionsV1<T extends ExportCurrentRevisions = ExportCurrentRevisions>(value: unknown, options?: ExportValidationOptions): Readonly<T> {
  const parsed = parseJsonSchema<T>(value, exportCurrentRevisionsSchema as JsonSchema, 'export-current-revisions/v1')
  validateExportSemantics(parsed as unknown as ExportCurrentRevisions, options)
  return parsed
}

export function verifyExportCurrentRevisionsV1(value: unknown, options?: ExportValidationOptions): void {
  parseExportCurrentRevisionsV1(value, options)
}

function equalBytes(left: Uint8Array, right: Uint8Array): boolean {
  if (left.byteLength !== right.byteLength) return false
  for (let index = 0; index < left.byteLength; index += 1) if (left[index] !== right[index]) return false
  return true
}

/** Verify the Core-created Export Asset from its immutable raw bytes. */
export async function verifyExportCurrentRevisionsAssetV1(
  rawAssetBytes: Uint8Array,
  runSnapshot: RunSnapshot,
  assetIdOrOptions?: string | { assetId?: string },
): Promise<Readonly<ExportCurrentRevisions>> {
  if (!(rawAssetBytes instanceof Uint8Array)) fail('Export Asset must be raw bytes')
  await verifySnapshot(runSnapshot)
  const expectedAssetId = runSnapshot.parameters_asset_id
  if (expectedAssetId === null) fail('RunSnapshot has no export parameters Asset')
  const requestedAssetId = typeof assetIdOrOptions === 'string' ? assetIdOrOptions : assetIdOrOptions?.assetId
  if (requestedAssetId !== undefined && requestedAssetId !== expectedAssetId) fail('Export Asset is not RunSnapshot parameters_asset_id')
  const expectedHash = runSnapshot.asset_hashes.find(asset => asset.asset_id === expectedAssetId)?.sha256
  if (expectedHash === undefined) fail('Export Asset hash is absent from RunSnapshot asset_hashes')
  const raw = new Uint8Array(rawAssetBytes)
  if (await sha256Hex(raw) !== expectedHash) fail('Export Asset bytes do not match the RunSnapshot hash')
  const parsed = parseStrictJsonBytes(raw)
  if (!isRecord(parsed)) fail('Export Asset must contain a JSON object')
  if (!equalBytes(utf8(canonicalJson(parsed)), raw)) fail('Export Asset JSON must be exact RFC 8785 JCS bytes')
  return parseExportCurrentRevisionsV1(parsed, {
    expectedWorkspaceId: runSnapshot.workspace_id,
    runSnapshot,
  })
}

type CoreRoute = (typeof coreApiMethodMatrix.routes)[number]

function parseCoreHttpPayload(value: unknown): Readonly<unknown> {
  if (!isRecord(value) || typeof value.schema !== 'string') fail('Core HTTP payload has no schema discriminator')
  if (value.schema.startsWith('core-')) return parseCoreAuthorityCommandQueryV1(value)
  if (value.schema === 'publication-command/v1' || value.schema === 'publication-result/v1') return parsePublicationCommandResultV1(value)
  if (value.schema === 'asset-query/v1' || value.schema === 'asset-read-range-query/v1' || value.schema === 'asset-metadata/v1' || value.schema === 'asset-read-range/v1') return parseAssetMetadataV1(value)
  fail(`unsupported Core HTTP payload schema ${value.schema}`)
}

/** Deterministic P4-side typed fake for the P0-owned Core HTTP matrix. */
export class CoreHttpContractFake {
  readonly #exchanges = new Map<string, Array<{ requestJcs: string; status: number; response: Readonly<unknown> }>>()

  private static route(routeId: string): CoreRoute {
    const route = coreApiMethodMatrix.routes.find(candidate => candidate.route_id === routeId) as CoreRoute | undefined
    if (route === undefined) fail(`unknown Core HTTP route fixture ${routeId}`)
    return route
  }

  private static parseRequest(route: CoreRoute, request: unknown): Readonly<unknown> {
    const record = request as JsonRecord
    if (!isRecord(request) || request.schema !== route.request_schema) fail('Core HTTP fixture is not bound to the route request schema')
    const routeRecord = route as unknown as Record<string, any>
    const placeholders = [...String(routeRecord.path_template).matchAll(/\{([^{}]+)\}/g)].map(match => match[1])
    const pathIdentity = Array.isArray(routeRecord.path_identity) ? routeRecord.path_identity : []
    if (JSON.stringify(placeholders) !== JSON.stringify(pathIdentity)) fail('Core HTTP route path identity is not bound to its path template')
    for (const field of pathIdentity) {
      if (typeof field !== 'string' || typeof record[field] !== 'string' || record[field].length === 0) fail(`Core HTTP path identity ${String(field)} must be non-empty`)
    }
    return parseCoreHttpPayload(request)
  }

  private static assertEntityKind(route: CoreRoute, response: JsonRecord): void {
    const expected = (route as unknown as Record<string, any>).result_entity_kind
    if (expected == null) return
    const schemaKind: unknown = {
      'core-workspace/v1': 'workspace',
      'core-document/v1': 'document',
      'core-node/v1': 'node',
      'core-relation/v1': 'relation',
      'core-revision/v1': 'revision',
      'core-workspace-page/v1': 'workspace',
      'core-document-page/v1': 'document',
      'core-node-page/v1': 'node',
      'core-relation-page/v1': 'relation',
      'core-revision-page/v1': 'revision',
      'core-revision-content-page/v1': 'revision',
      'asset-metadata/v1': 'asset',
      'asset-read-range/v1': 'asset',
      'core-delete-result/v1': response.entity_kind,
    }[String(response.schema)]
    if (schemaKind !== expected) fail('Core HTTP route entity kind does not match its result schema')
  }

  add(routeId: string, request: unknown, status: number, response: unknown): void {
    const route = CoreHttpContractFake.route(routeId)
    const parsedRequest = CoreHttpContractFake.parseRequest(route, request)
    if (!isRecord(response)) fail('Core HTTP fixture response must be an object')
    const routeRecord = route as unknown as Record<string, any>
    let parsedResponse: Readonly<unknown>
    if ((route.success_statuses as readonly number[]).includes(status)) {
      if (response.schema !== route.result_schema) fail('Core HTTP fixture is not bound to the route result schema')
      parsedResponse = parseCoreHttpPayload(response)
      CoreHttpContractFake.assertEntityKind(route, parsedResponse as JsonRecord)
    } else {
      const failure = (Array.isArray(routeRecord.failure_statuses) ? routeRecord.failure_statuses : []).find((item: any) => item.status === status)
      if (routeRecord.error_schema !== 'core-http-error/v1' || failure === undefined || response.schema !== routeRecord.error_schema) fail('Core HTTP fixture status is not declared by the route')
      parsedResponse = parseCoreHttpPayload(response)
      if (!Array.isArray(failure.error_codes) || !failure.error_codes.includes((parsedResponse as JsonRecord).error_code)) fail('Core HTTP error code is not allowed for this route/status')
    }
    const entries = this.#exchanges.get(routeId) ?? []
    const requestJcs = canonicalJson(parsedRequest)
    if (entries.some(entry => entry.requestJcs === requestJcs)) fail('Core HTTP fixture cannot register the same request twice')
    entries.push({ requestJcs, status, response: parsedResponse })
    this.#exchanges.set(routeId, entries)
  }

  request(routeId: string, request: unknown): readonly [number, Readonly<unknown>] {
    const route = CoreHttpContractFake.route(routeId)
    const entries = this.#exchanges.get(routeId)
    if (entries === undefined) fail(`Core HTTP route has no fixture response ${routeId}`)
    const requestJcs = canonicalJson(CoreHttpContractFake.parseRequest(route, request))
    const exchange = entries.find(entry => entry.requestJcs === requestJcs)
    if (exchange === undefined) fail('Core HTTP fixture request does not match any frozen exchange')
    return [exchange.status, structuredClone(exchange.response)] as const
  }
}

export { CoreHttpContractFixture }
