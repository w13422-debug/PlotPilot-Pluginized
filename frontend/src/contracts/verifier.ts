import { canonicalJson, hashJcs, sha256Hex, utf8 } from './canonical.ts'
import type { BackupBundle, CandidateItem, ResultBundle, RunSnapshot, SkillChainRef } from './types.ts'

type JsonObject = Record<string, any>
type ByteFiles = Record<string, Uint8Array>

const HASH = /^[0-9a-f]{64}$/
const RESERVED = new Set([
  'con', 'prn', 'aux', 'nul', 'clock$',
  ...Array.from({ length: 9 }, (_, index) => `com${index + 1}`),
  ...Array.from({ length: 9 }, (_, index) => `lpt${index + 1}`),
])

/* JavaScript has Unicode lowercase, but not Python's full casefold.  Keep
 * the small set of expanding/special folds explicit so path identity is the
 * same in the browser and in the Python SDK (including Straße/strasse). */
const CASEFOLD_SPECIALS: Record<string, string> = {
  "\u00b5": "\u03bc",
  "\u00df": "ss",
  "\u0149": "\u02bcn",
  "\u017f": "s",
  "\u01f0": "j\u030c",
  "\u0345": "\u03b9",
  "\u0390": "\u03b9\u0308\u0301",
  "\u03b0": "\u03c5\u0308\u0301",
  "\u03c2": "\u03c3",
  "\u03d0": "\u03b2",
  "\u03d1": "\u03b8",
  "\u03d5": "\u03c6",
  "\u03d6": "\u03c0",
  "\u03f0": "\u03ba",
  "\u03f1": "\u03c1",
  "\u03f5": "\u03b5",
  "\u0587": "\u0565\u0582",
  "\u13a0": "\u13a0",
  "\u13a1": "\u13a1",
  "\u13a2": "\u13a2",
  "\u13a3": "\u13a3",
  "\u13a4": "\u13a4",
  "\u13a5": "\u13a5",
  "\u13a6": "\u13a6",
  "\u13a7": "\u13a7",
  "\u13a8": "\u13a8",
  "\u13a9": "\u13a9",
  "\u13aa": "\u13aa",
  "\u13ab": "\u13ab",
  "\u13ac": "\u13ac",
  "\u13ad": "\u13ad",
  "\u13ae": "\u13ae",
  "\u13af": "\u13af",
  "\u13b0": "\u13b0",
  "\u13b1": "\u13b1",
  "\u13b2": "\u13b2",
  "\u13b3": "\u13b3",
  "\u13b4": "\u13b4",
  "\u13b5": "\u13b5",
  "\u13b6": "\u13b6",
  "\u13b7": "\u13b7",
  "\u13b8": "\u13b8",
  "\u13b9": "\u13b9",
  "\u13ba": "\u13ba",
  "\u13bb": "\u13bb",
  "\u13bc": "\u13bc",
  "\u13bd": "\u13bd",
  "\u13be": "\u13be",
  "\u13bf": "\u13bf",
  "\u13c0": "\u13c0",
  "\u13c1": "\u13c1",
  "\u13c2": "\u13c2",
  "\u13c3": "\u13c3",
  "\u13c4": "\u13c4",
  "\u13c5": "\u13c5",
  "\u13c6": "\u13c6",
  "\u13c7": "\u13c7",
  "\u13c8": "\u13c8",
  "\u13c9": "\u13c9",
  "\u13ca": "\u13ca",
  "\u13cb": "\u13cb",
  "\u13cc": "\u13cc",
  "\u13cd": "\u13cd",
  "\u13ce": "\u13ce",
  "\u13cf": "\u13cf",
  "\u13d0": "\u13d0",
  "\u13d1": "\u13d1",
  "\u13d2": "\u13d2",
  "\u13d3": "\u13d3",
  "\u13d4": "\u13d4",
  "\u13d5": "\u13d5",
  "\u13d6": "\u13d6",
  "\u13d7": "\u13d7",
  "\u13d8": "\u13d8",
  "\u13d9": "\u13d9",
  "\u13da": "\u13da",
  "\u13db": "\u13db",
  "\u13dc": "\u13dc",
  "\u13dd": "\u13dd",
  "\u13de": "\u13de",
  "\u13df": "\u13df",
  "\u13e0": "\u13e0",
  "\u13e1": "\u13e1",
  "\u13e2": "\u13e2",
  "\u13e3": "\u13e3",
  "\u13e4": "\u13e4",
  "\u13e5": "\u13e5",
  "\u13e6": "\u13e6",
  "\u13e7": "\u13e7",
  "\u13e8": "\u13e8",
  "\u13e9": "\u13e9",
  "\u13ea": "\u13ea",
  "\u13eb": "\u13eb",
  "\u13ec": "\u13ec",
  "\u13ed": "\u13ed",
  "\u13ee": "\u13ee",
  "\u13ef": "\u13ef",
  "\u13f0": "\u13f0",
  "\u13f1": "\u13f1",
  "\u13f2": "\u13f2",
  "\u13f3": "\u13f3",
  "\u13f4": "\u13f4",
  "\u13f5": "\u13f5",
  "\u13f8": "\u13f0",
  "\u13f9": "\u13f1",
  "\u13fa": "\u13f2",
  "\u13fb": "\u13f3",
  "\u13fc": "\u13f4",
  "\u13fd": "\u13f5",
  "\u1c80": "\u0432",
  "\u1c81": "\u0434",
  "\u1c82": "\u043e",
  "\u1c83": "\u0441",
  "\u1c84": "\u0442",
  "\u1c85": "\u0442",
  "\u1c86": "\u044a",
  "\u1c87": "\u0463",
  "\u1c88": "\ua64b",
  "\u1e96": "h\u0331",
  "\u1e97": "t\u0308",
  "\u1e98": "w\u030a",
  "\u1e99": "y\u030a",
  "\u1e9a": "a\u02be",
  "\u1e9b": "\u1e61",
  "\u1e9e": "ss",
  "\u1f50": "\u03c5\u0313",
  "\u1f52": "\u03c5\u0313\u0300",
  "\u1f54": "\u03c5\u0313\u0301",
  "\u1f56": "\u03c5\u0313\u0342",
  "\u1f80": "\u1f00\u03b9",
  "\u1f81": "\u1f01\u03b9",
  "\u1f82": "\u1f02\u03b9",
  "\u1f83": "\u1f03\u03b9",
  "\u1f84": "\u1f04\u03b9",
  "\u1f85": "\u1f05\u03b9",
  "\u1f86": "\u1f06\u03b9",
  "\u1f87": "\u1f07\u03b9",
  "\u1f88": "\u1f00\u03b9",
  "\u1f89": "\u1f01\u03b9",
  "\u1f8a": "\u1f02\u03b9",
  "\u1f8b": "\u1f03\u03b9",
  "\u1f8c": "\u1f04\u03b9",
  "\u1f8d": "\u1f05\u03b9",
  "\u1f8e": "\u1f06\u03b9",
  "\u1f8f": "\u1f07\u03b9",
  "\u1f90": "\u1f20\u03b9",
  "\u1f91": "\u1f21\u03b9",
  "\u1f92": "\u1f22\u03b9",
  "\u1f93": "\u1f23\u03b9",
  "\u1f94": "\u1f24\u03b9",
  "\u1f95": "\u1f25\u03b9",
  "\u1f96": "\u1f26\u03b9",
  "\u1f97": "\u1f27\u03b9",
  "\u1f98": "\u1f20\u03b9",
  "\u1f99": "\u1f21\u03b9",
  "\u1f9a": "\u1f22\u03b9",
  "\u1f9b": "\u1f23\u03b9",
  "\u1f9c": "\u1f24\u03b9",
  "\u1f9d": "\u1f25\u03b9",
  "\u1f9e": "\u1f26\u03b9",
  "\u1f9f": "\u1f27\u03b9",
  "\u1fa0": "\u1f60\u03b9",
  "\u1fa1": "\u1f61\u03b9",
  "\u1fa2": "\u1f62\u03b9",
  "\u1fa3": "\u1f63\u03b9",
  "\u1fa4": "\u1f64\u03b9",
  "\u1fa5": "\u1f65\u03b9",
  "\u1fa6": "\u1f66\u03b9",
  "\u1fa7": "\u1f67\u03b9",
  "\u1fa8": "\u1f60\u03b9",
  "\u1fa9": "\u1f61\u03b9",
  "\u1faa": "\u1f62\u03b9",
  "\u1fab": "\u1f63\u03b9",
  "\u1fac": "\u1f64\u03b9",
  "\u1fad": "\u1f65\u03b9",
  "\u1fae": "\u1f66\u03b9",
  "\u1faf": "\u1f67\u03b9",
  "\u1fb2": "\u1f70\u03b9",
  "\u1fb3": "\u03b1\u03b9",
  "\u1fb4": "\u03ac\u03b9",
  "\u1fb6": "\u03b1\u0342",
  "\u1fb7": "\u03b1\u0342\u03b9",
  "\u1fbc": "\u03b1\u03b9",
  "\u1fc2": "\u1f74\u03b9",
  "\u1fc3": "\u03b7\u03b9",
  "\u1fc4": "\u03ae\u03b9",
  "\u1fc6": "\u03b7\u0342",
  "\u1fc7": "\u03b7\u0342\u03b9",
  "\u1fcc": "\u03b7\u03b9",
  "\u1fd2": "\u03b9\u0308\u0300",
  "\u1fd6": "\u03b9\u0342",
  "\u1fd7": "\u03b9\u0308\u0342",
  "\u1fe2": "\u03c5\u0308\u0300",
  "\u1fe4": "\u03c1\u0313",
  "\u1fe6": "\u03c5\u0342",
  "\u1fe7": "\u03c5\u0308\u0342",
  "\u1ff2": "\u1f7c\u03b9",
  "\u1ff3": "\u03c9\u03b9",
  "\u1ff4": "\u03ce\u03b9",
  "\u1ff6": "\u03c9\u0342",
  "\u1ff7": "\u03c9\u0342\u03b9",
  "\u1ffc": "\u03c9\u03b9",
  "\uab70": "\u13a0",
  "\uab71": "\u13a1",
  "\uab72": "\u13a2",
  "\uab73": "\u13a3",
  "\uab74": "\u13a4",
  "\uab75": "\u13a5",
  "\uab76": "\u13a6",
  "\uab77": "\u13a7",
  "\uab78": "\u13a8",
  "\uab79": "\u13a9",
  "\uab7a": "\u13aa",
  "\uab7b": "\u13ab",
  "\uab7c": "\u13ac",
  "\uab7d": "\u13ad",
  "\uab7e": "\u13ae",
  "\uab7f": "\u13af",
  "\uab80": "\u13b0",
  "\uab81": "\u13b1",
  "\uab82": "\u13b2",
  "\uab83": "\u13b3",
  "\uab84": "\u13b4",
  "\uab85": "\u13b5",
  "\uab86": "\u13b6",
  "\uab87": "\u13b7",
  "\uab88": "\u13b8",
  "\uab89": "\u13b9",
  "\uab8a": "\u13ba",
  "\uab8b": "\u13bb",
  "\uab8c": "\u13bc",
  "\uab8d": "\u13bd",
  "\uab8e": "\u13be",
  "\uab8f": "\u13bf",
  "\uab90": "\u13c0",
  "\uab91": "\u13c1",
  "\uab92": "\u13c2",
  "\uab93": "\u13c3",
  "\uab94": "\u13c4",
  "\uab95": "\u13c5",
  "\uab96": "\u13c6",
  "\uab97": "\u13c7",
  "\uab98": "\u13c8",
  "\uab99": "\u13c9",
  "\uab9a": "\u13ca",
  "\uab9b": "\u13cb",
  "\uab9c": "\u13cc",
  "\uab9d": "\u13cd",
  "\uab9e": "\u13ce",
  "\uab9f": "\u13cf",
  "\uaba0": "\u13d0",
  "\uaba1": "\u13d1",
  "\uaba2": "\u13d2",
  "\uaba3": "\u13d3",
  "\uaba4": "\u13d4",
  "\uaba5": "\u13d5",
  "\uaba6": "\u13d6",
  "\uaba7": "\u13d7",
  "\uaba8": "\u13d8",
  "\uaba9": "\u13d9",
  "\uabaa": "\u13da",
  "\uabab": "\u13db",
  "\uabac": "\u13dc",
  "\uabad": "\u13dd",
  "\uabae": "\u13de",
  "\uabaf": "\u13df",
  "\uabb0": "\u13e0",
  "\uabb1": "\u13e1",
  "\uabb2": "\u13e2",
  "\uabb3": "\u13e3",
  "\uabb4": "\u13e4",
  "\uabb5": "\u13e5",
  "\uabb6": "\u13e6",
  "\uabb7": "\u13e7",
  "\uabb8": "\u13e8",
  "\uabb9": "\u13e9",
  "\uabba": "\u13ea",
  "\uabbb": "\u13eb",
  "\uabbc": "\u13ec",
  "\uabbd": "\u13ed",
  "\uabbe": "\u13ee",
  "\uabbf": "\u13ef",
  "\ufb00": "ff",
  "\ufb01": "fi",
  "\ufb02": "fl",
  "\ufb03": "ffi",
  "\ufb04": "ffl",
  "\ufb05": "st",
  "\ufb06": "st",
  "\ufb13": "\u0574\u0576",
  "\ufb14": "\u0574\u0565",
  "\ufb15": "\u0574\u056b",
  "\ufb16": "\u057e\u0576",
  "\ufb17": "\u0574\u056d",
}

function fail(message: string): never {
  throw new Error(message)
}

function asObject(value: unknown, label: string): JsonObject {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return fail(`${label} must be an object`)
  return value as JsonObject
}

function asArray(value: unknown, label: string): any[] {
  if (!Array.isArray(value)) return fail(`${label} must be an array`)
  return value
}

function assertSchema(value: JsonObject, schema: string): void {
  if (value.schema !== schema) fail(`${schema} schema mismatch`)
}

function assertExactKeys(value: JsonObject, fields: readonly string[], label: string): void {
  const expected = new Set(fields)
  const actual = new Set(Object.keys(value))
  if (actual.size !== expected.size || [...expected].some(field => !actual.has(field))) {
    const missing = fields.filter(field => !actual.has(field))
    const extra = Object.keys(value).filter(field => !expected.has(field))
    fail(`${label} fields are not bound to the method schema: missing=${missing.join(',')}, extra=${extra.join(',')}`)
  }
}

function assertUnique(values: Iterable<unknown>, label: string): void {
  const list = [...values]
  if (new Set(list).size !== list.length) fail(label)
}

function compareUtf8(left: string, right: string): number {
  const a = utf8(left)
  const b = utf8(right)
  const length = Math.min(a.byteLength, b.byteLength)
  for (let index = 0; index < length; index += 1) {
    const leftByte = a[index] ?? 0
    const rightByte = b[index] ?? 0
    if (leftByte !== rightByte) return leftByte - rightByte
  }
  return a.byteLength - b.byteLength
}

function equalBytes(left: Uint8Array, right: Uint8Array): boolean {
  if (left.byteLength !== right.byteLength) return false
  for (let index = 0; index < left.byteLength; index += 1) {
    if (left[index] !== right[index]) return false
  }
  return true
}

function concatBytes(...parts: Uint8Array[]): Uint8Array {
  const result = new Uint8Array(parts.reduce((total, part) => total + part.byteLength, 0))
  let offset = 0
  for (const part of parts) {
    result.set(part, offset)
    offset += part.byteLength
  }
  return result
}

export function unicodeNfcCasefold(value: string): string {
  if (typeof value !== 'string') return fail('casefold identity requires a string')
  const normalized = value.normalize('NFC')
  let folded = ''
  for (const character of normalized) folded += CASEFOLD_SPECIALS[character] ?? character.toLowerCase()
  return folded
}

export function assertHash(value: unknown, label: string): asserts value is string {
  if (typeof value !== 'string' || !HASH.test(value)) fail(`${label} must be lowercase SHA-256`)
}

export function normalizeWindowsPath(path: string): string {
  if (
    typeof path !== 'string' || !path || path.includes('\\') || path.includes('\0') ||
    path.startsWith('/') || /^[A-Za-z]:/.test(path)
  ) return fail('invalid package path')
  const normalized = path.normalize('NFC')
  const parts = normalized.split('/')
  if (parts.some(part => !part || part === '.' || part === '..' || /[<>"|?*:\u0000-\u001f]/.test(part) || /[. ]$/.test(part))) {
    return fail('invalid Windows path segment')
  }
  if (parts.some(part => RESERVED.has(unicodeNfcCasefold(part.split('.', 1)[0] ?? '')))) return fail('reserved Windows path')
  if ([...normalized].length > 240) return fail('path exceeds 240 Unicode scalar values')
  return normalized
}

function normalizedFileMap(files: ByteFiles): Map<string, Uint8Array> {
  const normalized = new Map<string, Uint8Array>()
  const identities = new Set<string>()
  for (const [rawPath, content] of Object.entries(files)) {
    const path = normalizeWindowsPath(rawPath)
    if (path === 'files.sha256') fail('files.sha256 is generated')
    if (!(content instanceof Uint8Array)) fail('package content must be raw bytes')
    const identity = unicodeNfcCasefold(path)
    if (identities.has(identity)) fail('casefold path collision')
    identities.add(identity)
    normalized.set(path, content)
  }
  return normalized
}

export async function buildFilesSha256(files: ByteFiles): Promise<Uint8Array> {
  const normalized = normalizedFileMap(files)
  const rows: string[] = []
  for (const path of [...normalized.keys()].sort(compareUtf8)) {
    rows.push(`${await sha256Hex(normalized.get(path) as Uint8Array)}  ${path}\n`)
  }
  return utf8(rows.join(''))
}

export async function packageDigest(files: ByteFiles, pluginId: string, version: string): Promise<{ filesSha256: Uint8Array; packageHash: string; releaseId: string }> {
  const filesSha256 = await buildFilesSha256(files)
  const packageHash = await sha256Hex(concatBytes(utf8('plotpilot-package/v1\n'), filesSha256))
  const releaseId = await sha256Hex(utf8(`plotpilot-release/v1\n${pluginId}\n${version}\n${packageHash}\n`))
  return { filesSha256, packageHash, releaseId }
}

export async function skillPackageDigest(files: ByteFiles, skillId: string, version: string): Promise<{ filesSha256: Uint8Array; skillPackageHash: string; skillReleaseId: string }> {
  const filesSha256 = await buildFilesSha256(files)
  const skillPackageHash = await sha256Hex(concatBytes(utf8('plotpilot-skill-package/v1\n'), filesSha256))
  const skillReleaseId = await sha256Hex(utf8(`plotpilot-skill-release/v1\n${skillId}\n${version}\n${skillPackageHash}\n`))
  return { filesSha256, skillPackageHash, skillReleaseId }
}

export function requestKeyBytes(snapshot: RunSnapshot): Uint8Array {
  const value = asObject(snapshot, 'RunSnapshot')
  const assets = asArray(value.asset_hashes, 'asset_hashes')
  const parametersId = value.parameters_asset_id as string | null
  const parametersHash = parametersId == null
    ? '-'
    : (assets.find(asset => asObject(asset, 'asset_hash').asset_id === parametersId)?.sha256 ?? '-')
  const revisions = asArray(value.input_revisions, 'input_revisions').slice().sort((left, right) => compareUtf8(`${left.document_id}\0${left.revision_id}`, `${right.document_id}\0${right.revision_id}`))
  const revisionLine = revisions.map(item => `${item.document_id}=${item.revision_id}=${item.content_hash}`).join(',')
  return utf8([
    'request-key/v1', value.workspace_id, value.scope.operation,
    value.scope.document_id ?? 'null', value.scope.node_id ?? 'null',
    revisionLine, value.plan_revision_id, parametersHash, value.run_intent_id, '',
  ].join('\n'))
}

export async function requestKey(snapshot: RunSnapshot): Promise<string> {
  return sha256Hex(requestKeyBytes(snapshot))
}

export async function verifySnapshot(snapshot: RunSnapshot): Promise<void> {
  const value = asObject(snapshot, 'RunSnapshot')
  assertSchema(value, 'run-snapshot/v1')
  assertHash(value.request_key, 'request_key')
  assertHash(value.snapshot_hash, 'snapshot_hash')
  const assets = asArray(value.asset_hashes, 'asset_hashes')
  assertUnique(assets.map(asset => asObject(asset, 'asset_hash').asset_id), 'asset_hashes contains duplicate asset IDs')
  for (const asset of assets) assertHash(asObject(asset, 'asset_hash').sha256, 'asset_hashes.sha256')
  if (value.parameters_asset_id != null && !assets.some(asset => asObject(asset, 'asset_hash').asset_id === value.parameters_asset_id)) {
    fail('parameters_asset_id is absent from asset_hashes')
  }

  const identityChecks: Array<[string, string[]]> = [
    ['input_revisions', ['document_id', 'revision_id']],
    ['plugin_releases', ['plugin_id']],
    ['plugin_settings_revisions', ['plugin_id', 'scope', 'scope_id']],
    ['asset_hashes', ['asset_id']],
    ['data_bindings', ['order']],
    ['skill_releases', ['order']],
  ]
  for (const [field, fields] of identityChecks) {
    assertUnique(asArray(value[field], field).map(item => JSON.stringify(fields.map(fieldName => item[fieldName] ?? ''))), `${field} contains duplicate identity`)
  }
  for (const field of ['data_bindings', 'skill_releases']) {
    const orders = asArray(value[field], field).map(item => item.order)
    if (orders.some((order, index) => index > 0 && orders[index - 1] > order)) fail(`${field} order is not ascending`)
  }
  const assetMap = new Map(assets.map(asset => [asObject(asset, 'asset_hash').asset_id, asObject(asset, 'asset_hash').sha256]))
  for (const binding of asArray(value.data_bindings, 'data_bindings')) {
    if (assetMap.get(binding.bundle_asset_id) !== binding.bundle_hash) fail('data binding bundle hash is not backed by asset_hashes')
  }
  for (const skill of asArray(value.skill_releases, 'skill_releases')) {
    if (skill.parameters_asset_id != null && !assetMap.has(skill.parameters_asset_id)) fail('Skill parameter asset is absent from asset_hashes')
  }

  if (await requestKey(snapshot) !== value.request_key) fail('request_key mismatch')
  const unsigned = { ...value }
  delete unsigned.snapshot_hash
  const setLike = {
    ...unsigned,
    input_revisions: asArray(value.input_revisions, 'input_revisions').slice().sort((left, right) => compareUtf8(`${left.document_id}\0${left.revision_id}`, `${right.document_id}\0${right.revision_id}`)),
    plugin_releases: asArray(value.plugin_releases, 'plugin_releases').slice().sort((left, right) => compareUtf8(left.plugin_id, right.plugin_id)),
    plugin_settings_revisions: asArray(value.plugin_settings_revisions, 'plugin_settings_revisions').slice().sort((left, right) => compareUtf8(`${left.plugin_id}\0${left.scope}\0${left.scope_id ?? ''}`, `${right.plugin_id}\0${right.scope}\0${right.scope_id ?? ''}`)),
    asset_hashes: assets.slice().sort((left, right) => compareUtf8(left.asset_id, right.asset_id)),
  }
  if (await hashJcs('run-snapshot/v1', setLike) !== value.snapshot_hash) fail('snapshot_hash mismatch')
}

export async function verifyDataBundle(bundle: JsonObject): Promise<void> {
  assertSchema(bundle, 'plugin-data-bundle/v1')
  assertHash(bundle.bundle_hash, 'bundle_hash')
  const files = asArray(bundle.files, 'data bundle files')
  const paths = files.map(file => normalizeWindowsPath(asObject(file, 'data bundle file').path))
  const sorted = [...paths].sort(compareUtf8)
  if (paths.some((path, index) => path !== sorted[index])) fail('data bundle files must be sorted by normalized UTF-8 path')
  assertUnique(paths.map(unicodeNfcCasefold), 'data bundle paths must be NFC/casefold-unique')
  assertUnique(files.map(file => asObject(file, 'data bundle file').asset_id), 'data bundle asset IDs must be unique')
  if (!paths.includes(normalizeWindowsPath(bundle.root_path))) fail('data bundle root_path is absent from files')
  await verifySelfHash(bundle, 'bundle_hash', 'plugin-data-bundle/v1')
}

export async function verifyCoreSnapshot(snapshot: JsonObject): Promise<void> {
  assertSchema(snapshot, 'core-snapshot/v1')
  if (snapshot.coverage_complete !== true) fail('core snapshot must be coverage-complete')
  const eventTypes = asArray(asObject(snapshot.subscription_scope, 'subscription_scope').event_types, 'event_types') as string[]
  if (eventTypes.some((item, index) => index > 0 && compareUtf8(eventTypes[index - 1], item) >= 0)) fail('core snapshot event_types must be a sorted set')
  const aggregates = asArray(snapshot.covered_aggregates, 'covered_aggregates')
  const keys = aggregates.map(item => JSON.stringify([item.aggregate_type, item.aggregate_id]))
  assertUnique(keys, 'covered aggregates must be unique')
  if (keys.some((key, index) => index > 0 && keys[index - 1] > key)) fail('covered aggregates must be sorted')
  assertHash(snapshot.snapshot_hash, 'snapshot_hash')
  await verifySelfHash(snapshot, 'snapshot_hash', 'core-snapshot/v1')
}

export async function verifyJobSnapshot(snapshot: JsonObject): Promise<void> {
  assertSchema(snapshot, 'job-snapshot/v1')
  assertUnique(asArray(snapshot.steps, 'steps').map(step => step.step_id), 'job snapshot step IDs must be unique')
  assertUnique(asArray(snapshot.attempts, 'attempts').map(attempt => attempt.attempt_id), 'job snapshot attempt IDs must be unique')
  await verifySelfHash(snapshot, 'snapshot_hash', 'job-snapshot/v1')
}

export async function verifyCheckpoint(checkpoint: JsonObject, expectedSnapshotHash?: string, previousSeq?: number): Promise<void> {
  assertSchema(checkpoint, 'checkpoint/v1')
  if (expectedSnapshotHash != null && checkpoint.run_snapshot_hash !== expectedSnapshotHash) fail('checkpoint belongs to another RunSnapshot')
  if (previousSeq != null && checkpoint.checkpoint_seq <= previousSeq) fail('checkpoint sequence moved backwards')
  await verifySelfHash(checkpoint, 'checkpoint_hash', 'checkpoint/v1')
}

export async function verifySettingsValidationReceipt(receipt: JsonObject): Promise<void> {
  assertSchema(receipt, 'settings-validation-receipt/v1')
  await verifySelfHash(receipt, 'receipt_hash', 'settings-validation-receipt/v1')
}

export function verifyCapabilityDescriptor(descriptor: JsonObject, expectedCapabilityId?: string, allowedCapabilityIds?: Set<string>, expectedProvider?: JsonObject): void {
  assertSchema(descriptor, 'capability-provider/v1')
  if (expectedCapabilityId != null && descriptor.capability_id !== expectedCapabilityId) fail('capability descriptor ID does not match the request')
  if (allowedCapabilityIds != null && !allowedCapabilityIds.has(descriptor.capability_id)) fail('capability descriptor references an unknown capability')
  if (expectedProvider != null && canonicalJson(descriptor.provider) !== canonicalJson(expectedProvider)) fail('capability descriptor provider does not match the installed release')
  assertUnique(asArray(descriptor.supports, 'descriptor.supports'), 'capability descriptor supports must be unique')
  assertUnique(asArray(descriptor.accepted_data_formats, 'descriptor.accepted_data_formats'), 'capability descriptor data formats must be unique')
}

function verifyChainRef(ref: SkillChainRef, options: { bundleId?: string | null; itemIds?: Set<string>; allowStream?: boolean; allowBundleless?: boolean } = {}): void {
  const value = asObject(ref, 'Skill chain ref')
  assertSchema(value, 'skill-chain-ref/v1')
  const assetIdPresent = value.asset_id !== null
  const assetHashPresent = value.asset_hash !== null
  if (assetIdPresent !== assetHashPresent) fail('Skill chain asset ID/hash must be all-null or all-present')
  if (assetHashPresent) assertHash(value.asset_hash, 'Skill chain asset_hash')
  const bundlePair = value.result_bundle_id !== null && value.result_item_id !== null
  const streamPair = value.stream_id !== null && value.acked_prefix_hash !== null
  if ((value.result_bundle_id === null) !== (value.result_item_id === null)) fail('bundle anchor must be all-null or all-present')
  if ((value.stream_id === null) !== (value.acked_prefix_hash === null)) fail('stream anchor must be all-null or all-present')
  if (bundlePair && streamPair) fail('Skill chain reference must have exactly one anchor profile')
  if (!bundlePair && !streamPair && options.allowBundleless !== true) fail('bundleless Skill reference is not allowed here')
  if (streamPair && options.allowStream === false) fail('result Bundle refs must be bundle-backed')
  if (bundlePair) {
    if (options.bundleId != null && value.result_bundle_id !== options.bundleId) fail('Skill reference points at another Bundle')
    if (options.itemIds != null && !options.itemIds.has(value.result_item_id)) fail('Skill reference points at an unknown item')
  }
}

function verifyParentGraph(items: CandidateItem[], knownParentIds?: Set<string>): void {
  const graph = new Map<string, Set<string>>()
  for (const item of items) graph.set(item.item_id, new Set(item.parent_candidate_ids))
  const known = new Set([...graph.keys(), ...(knownParentIds ?? new Set<string>())])
  const visiting = new Set<string>()
  const visited = new Set<string>()
  const visit = (node: string): void => {
    if (visiting.has(node)) fail('candidate parent cycle detected')
    if (visited.has(node) || !graph.has(node)) return
    visiting.add(node)
    for (const parent of graph.get(node) as Set<string>) {
      if (!known.has(parent)) fail('candidate parent does not exist')
      visit(parent)
    }
    visiting.delete(node)
    visited.add(node)
  }
  for (const node of graph.keys()) visit(node)
}

export function verifyCandidate(item: CandidateItem, snapshotWorkspaceId: string): void {
  const value = asObject(item, 'Candidate')
  if (value.target.workspace_id !== snapshotWorkspaceId) fail('candidate target is outside snapshot workspace')
  const target = value.target
  const targetKey = JSON.stringify([target.workspace_id, target.entity_kind, target.entity_id])
  const writeEntries = asArray(value.write_set, 'candidate write_set')
  const writeKeys: string[] = []
  let targetWrite: JsonObject | undefined
  for (const entry of writeEntries) {
    const write = asObject(entry, 'candidate write_set entry')
    if (write.workspace_id !== snapshotWorkspaceId) fail('candidate write_set crosses the snapshot workspace')
    const key = JSON.stringify([write.workspace_id, write.entity_kind, write.entity_id])
    if (writeKeys.includes(key)) fail('candidate write_set contains duplicate target identity')
    writeKeys.push(key)
    if (key === targetKey) targetWrite = write
  }
  if (targetWrite == null) fail('candidate target is not present in write_set')
  if (targetWrite.revision_id !== value.base.revision_id || targetWrite.content_hash !== value.base.content_hash) fail('candidate base does not match target write_set entry')
  if (value.item_kind === 'incomplete_stream') {
    if (value.status !== 'partial' || target.entity_kind !== 'document' || value.mutation.mode !== 'replace' || value.mutation.payload_schema !== 'core/document-text/v1') fail('invalid incomplete stream Candidate')
    return
  }
  const allowed: Record<string, { kind: string; modes: string[] }> = {
    document: { kind: 'document', modes: ['replace', 'text_patch', 'append_text'] },
    node_structure: { kind: 'node_structure', modes: ['structure_patch'] },
    relation_set: { kind: 'relation_set', modes: ['relation_patch'] },
  }
  const rule = allowed[target.entity_kind]
  if (rule == null || value.item_kind !== rule.kind || !rule.modes.includes(value.mutation.mode)) fail('candidate item/mutation mismatch')
}

export function verifyResultProfile(bundle: ResultBundle, snapshotWorkspaceId: string, knownParentIds?: Set<string>): void {
  const value = asObject(bundle, 'ResultBundle')
  assertSchema(value, 'result-bundle/v1')
  const profiles: Record<string, [string, string]> = {
    'candidate-batch/v1': ['candidate_batch', 'candidate-item/v1'],
    'artifact-bundle/v1': ['artifact', 'artifact-item/v1'],
    'diagnostic-bundle/v1': ['diagnostic', 'diagnostic-item/v1'],
  }
  const profile = profiles[value.contract_id]
  if (profile == null || value.bundle_type !== profile[0]) fail('result profile mismatch')
  const items = asArray(value.items, 'result bundle items')
  const itemIds = items.map(item => asObject(item, 'result item').item_id)
  assertUnique(itemIds, 'result bundle item IDs must be unique')
  const itemIdSet = new Set(itemIds)
  const incompleteTargets = new Set<string>()
  for (const item of items) {
    const candidate = asObject(item, 'result item')
    if (candidate.schema !== profile[1]) fail('result bundle item profile does not match contract')
    if (profile[1] === 'candidate-item/v1') {
      verifyCandidate(candidate as CandidateItem, snapshotWorkspaceId)
      if (candidate.item_kind === 'incomplete_stream') {
        const targetKey = JSON.stringify([candidate.target.workspace_id, candidate.target.entity_kind, candidate.target.entity_id])
        if (incompleteTargets.has(targetKey)) fail('only one incomplete stream Candidate is allowed per target')
        incompleteTargets.add(targetKey)
      }
    }
  }
  if (profile[1] === 'candidate-item/v1') verifyParentGraph(items as CandidateItem[], knownParentIds)
  for (const ref of asArray(value.skill_chain_result_refs, 'skill_chain_result_refs')) verifyChainRef(ref as SkillChainRef, { bundleId: value.bundle_id, itemIds: itemIdSet, allowStream: false })
  const statuses = items.map(item => item.status)
  if (value.partial && !statuses.some(status => ['partial', 'failed', 'skipped'].includes(status))) fail('partial result bundle must expose a non-complete item')
  if (!value.partial && statuses.some(status => status !== 'complete')) fail('complete result bundle cannot contain partial/failed/skipped items')
}

export async function verifySelfHash(value: JsonObject, field: string, prefix: string): Promise<void> {
  assertHash(value[field], field)
  const unsigned = { ...value }
  delete unsigned[field]
  if (await hashJcs(prefix, unsigned) !== value[field]) fail(`${field} mismatch`)
}

export async function verifyBackup(bundle: BackupBundle): Promise<void> {
  const value = asObject(bundle, 'BackupBundle')
  assertSchema(value, 'backup-bundle/v1')
  await verifySelfHash(value, 'bundle_hash', 'plotpilot-backup/v1')
  const files = asArray(value.files, 'backup files')
  const paths = files.map(file => normalizeWindowsPath(asObject(file, 'backup file').path))
  const sorted = [...paths].sort(compareUtf8)
  if (paths.some((path, index) => path !== sorted[index])) fail('backup files are not sorted by normalized UTF-8 path')
  assertUnique(paths.map(unicodeNfcCasefold), 'backup files are not NFC/casefold-unique')
  const roles = new Set(files.map(file => file.role))
  if ((value.mode === 'workspace' && (roles.has('plugin_db') || roles.has('package'))) || (value.mode === 'data' && roles.has('package'))) fail('backup mode includes a forbidden role')
  if (!Object.values(asObject(value.verification, 'backup verification')).every(Boolean)) fail('backup verification flags must all be true for a publishable bundle')
}

export async function verifyPackageManifest(files: ByteFiles, manifestBytes: Uint8Array): Promise<void> {
  if (manifestBytes[0] === 0xef && manifestBytes[1] === 0xbb && manifestBytes[2] === 0xbf) fail('files.sha256 must not contain a UTF-8 BOM')
  try {
    const text = new TextDecoder('utf-8', { fatal: true }).decode(manifestBytes)
    if (!text || text.includes('\r') || !text.endsWith('\n')) fail('files.sha256 must use LF and end with a newline')
  } catch (error) {
    if (error instanceof Error && error.message.includes('files.sha256')) throw error
    fail('files.sha256 must be UTF-8')
  }
  const expected = await buildFilesSha256(files)
  if (!equalBytes(expected, manifestBytes)) fail('files.sha256 does not exactly describe package files')
}

export async function verifyPackageIdentity(files: ByteFiles, pluginId: string, version: string, expectedPackageHash: string, expectedReleaseId: string, expectedFilesSha256?: Uint8Array): Promise<void> {
  const manifest = await buildFilesSha256(files)
  if (expectedFilesSha256 != null) {
    await verifyPackageManifest(files, expectedFilesSha256)
    if (!equalBytes(manifest, expectedFilesSha256)) fail('files.sha256 bytes mismatch')
  }
  const packageHash = await sha256Hex(concatBytes(utf8('plotpilot-package/v1\n'), manifest))
  if (packageHash !== expectedPackageHash) fail('package_hash mismatch')
  const releaseId = await sha256Hex(utf8(`plotpilot-release/v1\n${pluginId}\n${version}\n${packageHash}\n`))
  if (releaseId !== expectedReleaseId) fail('release_id mismatch')
}

export async function verifySkillIdentity(files: ByteFiles, skillId: string, version: string, expectedPackageHash: string, expectedReleaseId: string, expectedFilesSha256?: Uint8Array): Promise<void> {
  const manifest = await buildFilesSha256(files)
  if (expectedFilesSha256 != null) await verifyPackageManifest(files, expectedFilesSha256)
  const packageHash = await sha256Hex(concatBytes(utf8('plotpilot-skill-package/v1\n'), manifest))
  if (packageHash !== expectedPackageHash) fail('skill_package_hash mismatch')
  const releaseId = await sha256Hex(utf8(`plotpilot-skill-release/v1\n${skillId}\n${version}\n${packageHash}\n`))
  if (releaseId !== expectedReleaseId) fail('skill_release_id mismatch')
}

export async function verifyProvenanceReceipt(receipt: JsonObject): Promise<void> {
  assertSchema(receipt, 'provenance-receipt/v1')
  if ((receipt.bundle_id === null) !== (receipt.bundle_hash === null)) fail('provenance Bundle ID/hash must be all-null or all-present')
  assertUnique(asArray(receipt.staged_items, 'staged_items').map(item => item.item_id), 'provenance staged item IDs must be unique')
  for (const ref of asArray(receipt.skill_chain_result_refs, 'skill_chain_result_refs')) verifyChainRef(ref as SkillChainRef, { bundleId: receipt.bundle_id, allowBundleless: true })
  await verifySelfHash(receipt, 'receipt_hash', 'provenance-receipt/v1')
}

export async function verifySkillReceipt(receipt: JsonObject): Promise<void> {
  assertSchema(receipt, 'skill-run-receipt/v1')
  verifyChainRef({
    schema: 'skill-chain-ref/v1',
    chain_result_id: receipt.chain_id,
    asset_id: null,
    asset_hash: null,
    result_bundle_id: receipt.result_bundle_id,
    result_item_id: receipt.result_item_id,
    stream_id: receipt.stream_id,
    acked_prefix_hash: receipt.acked_prefix_hash,
  }, { allowBundleless: true })
  if (receipt.result_bundle_id === null && receipt.stream_id === null && !['failed', 'skipped'].includes(receipt.step_state)) fail('only a failed/skipped Skill receipt may be bundleless')
  if (receipt.frozen !== true) fail('Skill receipt must be frozen before chain aggregation')
  if (receipt.step_state === 'skipped' && (receipt.participated || receipt.verified_patch)) fail('skipped Skill step cannot be participated or verified')
  if (receipt.model_claimed && receipt.claim_evidence_asset_id == null) fail('model_claimed requires claim evidence')
  if (receipt.verified_patch && !asArray(receipt.patches, 'patches').some(patch => patch.verified)) fail('verified_patch requires a verified patch')
  for (const patch of asArray(receipt.patches, 'patches')) if (patch.end_codepoint < patch.start_codepoint) fail('Skill patch range is inverted')
  if ((receipt.output_asset_id === null) !== (receipt.output_hash === null)) fail('Skill output Asset ID/hash must be all-null or all-present')
  await verifySelfHash(receipt, 'receipt_hash', 'skill-run-receipt/v1')
}

export async function verifySkillChain(chain: JsonObject, receipts: JsonObject[]): Promise<void> {
  assertSchema(chain, 'skill-chain-result/v1')
  const receiptList = receipts.slice().sort((left, right) => left.chain_index - right.chain_index)
  assertUnique(receiptList.map(receipt => receipt.receipt_id), 'Skill chain receipt IDs must be unique')
  assertUnique(receiptList.map(receipt => receipt.chain_index), 'Skill chain receipt indexes must be unique')
  if (receiptList.length !== asArray(chain.receipt_ids, 'receipt_ids').length || receiptList.some((receipt, index) => receipt.receipt_id !== chain.receipt_ids[index] || receipt.chain_index !== index)) fail('Skill chain receipt IDs/indexes are not continuous')
  verifyChainRef({
    schema: 'skill-chain-ref/v1',
    chain_result_id: chain.chain_id,
    asset_id: null,
    asset_hash: null,
    result_bundle_id: chain.result_bundle_id,
    result_item_id: chain.result_item_id,
    stream_id: chain.stream_id,
    acked_prefix_hash: chain.acked_prefix_hash,
  }, { allowBundleless: true })
  if (chain.result_bundle_id === null && chain.stream_id === null && !['failed', 'cancelled'].includes(chain.chain_status)) fail('only a failed/cancelled Skill chain may be bundleless')
  for (const receipt of receiptList) {
    await verifySkillReceipt(receipt)
    if (receipt.chain_id !== chain.chain_id || receipt.run_snapshot_hash !== chain.run_snapshot_hash) fail('Skill chain receipt identity mismatch')
    const receiptRef = [receipt.result_bundle_id, receipt.result_item_id, receipt.stream_id, receipt.acked_prefix_hash]
    const chainRef = [chain.result_bundle_id, chain.result_item_id, chain.stream_id, chain.acked_prefix_hash]
    if (canonicalJson(receiptRef) !== canonicalJson(chainRef)) fail('Skill receipt anchor does not match its chain')
  }
  if (canonicalJson(chain.receipt_hashes) !== canonicalJson(receiptList.map(receipt => receipt.receipt_hash))) fail('Skill chain receipt hashes do not match')
  if ((chain.final_output_asset_id === null) !== (chain.final_output_hash === null)) fail('final output asset/hash must be all-null or all-present')
  const finalHash = chain.final_output_hash ?? '-'
  const expected = await sha256Hex(utf8(`skill-chain/v1\n${receiptList.map(receipt => receipt.receipt_hash).join('\n')}\n${finalHash}\n`))
  if (expected !== chain.chain_hash) fail('chain_hash mismatch')
}

export function verifyManifest(manifest: JsonObject): void {
  if (manifest.schema !== 'plotpilot-plugin/v1') fail('plugin manifest schema mismatch')
  const capabilities = asArray(manifest.capabilities, 'manifest capabilities')
  const capabilityIds = capabilities.map(capability => capability.capability_id)
  assertUnique(capabilityIds, 'manifest capability IDs must be unique')
  for (const capability of capabilities) assertUnique(asArray(capability.operations, 'capability operations'), 'manifest capability operations must be unique')
  const compatibility = asObject(manifest.compatibility, 'manifest compatibility')
  if (compatibility.core_api !== '>=1.0 <2.0' || compatibility.plugin_rpc !== '1' || compatibility.ui_host !== '1') fail('manifest compatibility is outside the v1 matrix')
  if (manifest.kind === 'data' && asArray(manifest.needs, 'manifest needs').length !== 0) fail('data plugin needs must be empty')
  if (manifest.kind === 'data' && ['backend', 'storage', 'ui'].some(field => field in manifest && manifest[field] != null)) fail('data plugin cannot declare backend/storage/UI fields')
  if (manifest.ui != null) {
    const ui = asObject(manifest.ui, 'manifest ui')
    const contributions = asArray(ui.contributions, 'UI contributions')
    assertUnique(contributions.map(item => JSON.stringify([item.contribution_id, item.slot, item.capability_id])), 'UI contribution triples must be unique')
    const capabilitySet = new Set(capabilityIds)
    for (const contribution of contributions) if (!capabilitySet.has(contribution.capability_id)) fail('UI contribution references an unknown capability')
  }
}

export const RPC_METHOD_MATRIX: Record<string, { meta: string; params: string[]; result: string[] }> = {
  'runtime.handshake': { meta: 'control/install', params: ['host_protocol', 'generation_id', 'plugin_release_id', 'data_generation_id'], result: ['plugin_protocol', 'plugin_id', 'release_id', 'capabilities', 'worker_instance_id'] },
  'runtime.health': { meta: 'control/install', params: ['probe_id', 'db_lease_id', 'db_lease_epoch', 'owner_instance_id'], result: ['status', 'details_asset_id', 'checked_at'] },
  'runtime.heartbeat': { meta: 'attempt', params: ['worker_instance_id', 'observed_at', 'local_seq'], result: [] },
  'capability.describe': { meta: 'control', params: ['capability_id'], result: ['descriptor'] },
  'settings.validate': { meta: 'control/install', params: ['settings_revision_id', 'plugin_release_id', 'schema_hash', 'payload_asset_id'], result: ['valid', 'evaluated_payload_hash', 'details_asset_id', 'errors'] },
  'migration.plan': { meta: 'install', params: ['from_schema', 'to_schema', 'migration_manifest_hash'], result: ['plan_id', 'steps', 'backward_compatible', 'requires_verified_backup'] },
  'migration.apply': { meta: 'install', params: ['plan_id', 'db_lease_id', 'db_lease_epoch', 'owner_instance_id'], result: ['applied_schema', 'receipt_hash'] },
  'migration.verify': { meta: 'install', params: ['db_lease_id', 'db_lease_epoch', 'owner_instance_id', 'expected_schema'], result: ['valid', 'schema_hash', 'errors'] },
  'job.start': { meta: 'attempt', params: ['capability_id', 'run_snapshot_asset_id', 'checkpoint_asset_id', 'secrets'], result: ['accepted', 'worker_run_id', 'provenance_receipt_id', 'output_streams'] },
  'job.resume': { meta: 'attempt', params: ['capability_id', 'run_snapshot_asset_id', 'resume_of_attempt_id', 'checkpoint_asset_id', 'resume_intent_id', 'resume_reason', 'secrets'], result: ['accepted', 'worker_run_id', 'provenance_receipt_id', 'output_streams'] },
  'job.pause': { meta: 'attempt', params: ['worker_run_id', 'reason'], result: ['accepted', 'checkpoint_asset_id'] },
  'job.cancel': { meta: 'attempt', params: ['worker_run_id', 'reason'], result: ['accepted', 'terminal_known', 'attempt_state'] },
  'runtime.shutdown': { meta: 'control/install/attempt', params: ['reason', 'deadline_at'], result: ['accepted'] },
  'host.asset.read/v1': { meta: 'control/install/attempt', params: ['asset_id', 'offset', 'length'], result: ['base64_chunk', 'next_offset', 'content_hash'] },
  'host.asset.create/v1': { meta: 'install/attempt', params: ['operation_key', 'upload_id', 'offset', 'mime', 'total_size', 'expected_hash', 'chunk_hash', 'base64_chunk', 'final'], result: ['upload_id', 'accepted_bytes', 'completed', 'asset_id'] },
  'host.asset.upload.status/v1': { meta: 'install/attempt', params: ['upload_id', 'expected_hash'], result: ['accepted_bytes', 'completed', 'asset_id'] },
  'host.model.invoke/v1': { meta: 'attempt', params: ['operation_key', 'invocation_id', 'invocation_key', 'model_profile_revision_id', 'request_asset_id', 'replay_policy'], result: ['state', 'response_asset_id', 'receipt_id', 'uncertainty'] },
  'host.capability.invoke/v1': { meta: 'attempt', params: ['operation_key', 'binding_id', 'input_asset_id', 'parameters_asset_id', 'expected_result_contract', 'propagate_cancel'], result: ['accepted', 'child_job_id', 'child_step_id', 'child_run_snapshot_asset_id', 'child_run_snapshot_hash', 'child_result_contract', 'child_job_event_seq'] },
  'host.capability.poll/v1': { meta: 'attempt', params: ['child_job_id', 'after_job_event_seq'], result: ['job_snapshot_asset_id', 'job_event_page_asset_id', 'next_job_event_seq', 'terminal', 'result_bundle_asset_id', 'provenance_receipt_id'] },
  'host.capability.cancel/v1': { meta: 'attempt', params: ['operation_key', 'child_job_id', 'reason'], result: ['accepted', 'terminal_known', 'child_state', 'child_job_event_seq'] },
  'host.candidate.stage/v1': { meta: 'attempt', params: ['operation_key', 'result_bundle_asset_id', 'input_snapshot_hash'], result: ['accepted', 'staged_items', 'job_event_seq'] },
  'host.checkpoint.commit/v1': { meta: 'attempt', params: ['operation_key', 'checkpoint_asset_id'], result: ['accepted', 'checkpoint_id', 'completed_units', 'total_units', 'job_event_seq'] },
  'host.stream.commit/v1': { meta: 'attempt', params: ['operation_key', 'stream_prefix_asset_id'], result: ['accepted', 'stream_id', 'acked_prefix_seq', 'acked_bytes', 'acked_prefix_hash', 'job_event_seq'] },
  'host.job.event/v1': { meta: 'attempt', params: ['operation_key', 'event_type', 'payload_asset_id', 'local_seq'], result: ['accepted', 'job_event_seq'] },
  'host.job.await_user/v1': { meta: 'attempt', params: ['operation_key', 'worker_run_id', 'checkpoint_asset_id', 'prompt_asset_id', 'reason'], result: ['accepted', 'attempt_state', 'step_state', 'job_state', 'job_event_seq'] },
  'host.job.complete/v1': { meta: 'attempt', params: ['operation_key', 'worker_run_id', 'outcome', 'result_bundle_asset_id', 'candidate_stage_operation_key', 'terminal_detail_asset_id', 'local_seq'], result: ['accepted', 'attempt_state', 'step_state', 'job_state', 'provenance_receipt_id', 'job_event_seq', 'core_event_high_water'] },
  'host.log/v1': { meta: 'control/install/attempt', params: ['level', 'message', 'fields_asset_id', 'local_seq'], result: ['accepted', 'dropped'] },
  'host.migration.lease.renew/v1': { meta: 'install', params: ['operation_key', 'db_lease_id', 'db_lease_epoch', 'owner_instance_id', 'requested_expires_at'], result: ['accepted', 'db_lease_epoch', 'expires_at'] },
  'host.migration.lease.release/v1': { meta: 'install', params: ['operation_key', 'db_lease_id', 'db_lease_epoch', 'owner_instance_id', 'reason'], result: ['accepted', 'state'] },
}

const OPERATION_METHODS = new Set([
  'host.asset.create/v1', 'host.model.invoke/v1', 'host.capability.invoke/v1', 'host.capability.cancel/v1',
  'host.candidate.stage/v1', 'host.checkpoint.commit/v1', 'host.stream.commit/v1', 'host.job.event/v1',
  'host.job.await_user/v1', 'host.job.complete/v1', 'host.migration.lease.renew/v1', 'host.migration.lease.release/v1',
])

function validateMeta(meta: JsonObject, context: string): void {
  const base = ['protocol_version', 'generation_id', 'plugin_release_id', 'deadline_at', 'context', 'operation_id']
  const fields = context === 'control'
    ? base
    : context === 'install'
      ? [...base, 'install_operation_id', 'install_lease_epoch']
      : [...base, 'job_id', 'step_id', 'attempt_id', 'lease_epoch']
  assertExactKeys(meta, fields, `${context} meta`)
  if (meta.protocol_version !== '1' || meta.context !== context) fail('RPC meta protocol/context mismatch')
}

export function validateRpcRequest(request: JsonObject, expectedLeaseEpoch?: number): void {
  const method = request.method
  const definition = RPC_METHOD_MATRIX[method]
  const heartbeat = method === 'runtime.heartbeat' && !Object.prototype.hasOwnProperty.call(request, 'id')
  if (heartbeat) {
    assertExactKeys(request, ['jsonrpc', 'method', 'meta', 'params'], 'heartbeat')
    const meta = asObject(request.meta, 'RPC meta')
    validateMeta(meta, 'attempt')
    assertExactKeys(asObject(request.params, 'heartbeat params'), RPC_METHOD_MATRIX['runtime.heartbeat'].params, 'runtime.heartbeat params')
    if (expectedLeaseEpoch != null && meta.lease_epoch !== expectedLeaseEpoch) fail('heartbeat lease epoch is stale')
    return
  }
  if (definition == null || method === 'runtime.heartbeat') fail('unknown or notification-only RPC method')
  assertExactKeys(request, ['jsonrpc', 'id', 'method', 'meta', 'params'], `${method} request`)
  const meta = asObject(request.meta, 'RPC meta')
  const context = meta.context
  if (typeof context !== 'string' || !definition.meta.split('/').includes(context)) fail(`${method} cannot use ${String(context)} meta profile`)
  validateMeta(meta, context)
  assertExactKeys(asObject(request.params, `${method} params`), definition.params, `${method} params`)
  if (expectedLeaseEpoch != null && (context === 'install' || context === 'attempt')) {
    const actual = context === 'install' ? meta.install_lease_epoch : meta.lease_epoch
    if (actual !== expectedLeaseEpoch) fail('RPC lease epoch is stale')
  }
  if (OPERATION_METHODS.has(method) && !Object.prototype.hasOwnProperty.call(request.params, 'operation_key')) fail(`${method} requires operation_key`)
  if (method === 'runtime.health') {
    const params = asObject(request.params, 'runtime.health params')
    const leaseFields = ['db_lease_id', 'db_lease_epoch', 'owner_instance_id']
    if (context === 'install' && leaseFields.some(field => params[field] == null)) fail('install health requires all shadow DB lease fields')
    if (context === 'control' && leaseFields.some(field => params[field] != null)) fail('control health cannot carry shadow DB lease fields')
  }
}

export function validateRpcResult(method: string, result: JsonObject, request?: JsonObject): void {
  const definition = RPC_METHOD_MATRIX[method]
  if (definition == null || method === 'runtime.heartbeat') fail('unknown or notification-only RPC method')
  if (request != null && request.method !== method) fail('RPC result method does not match its request')
  assertExactKeys(result, definition.result, `${method} result`)
  const params = request == null ? {} : asObject(request.params, `${method} params`)
  if (method === 'capability.describe') verifyCapabilityDescriptor(asObject(result.descriptor, 'capability descriptor'), params.capability_id)
  if (method === 'runtime.handshake') {
    if (result.plugin_protocol !== '1') fail('handshake attempted a protocol downgrade or upgrade')
    if (request != null && result.release_id !== asObject(request.meta, 'RPC meta').plugin_release_id) fail('handshake release does not match request meta')
  }
  if (method === 'host.capability.invoke/v1' && params.expected_result_contract != null && result.child_result_contract !== params.expected_result_contract) fail('child result contract does not match the binding request')
  if (method === 'job.pause' && result.accepted === true && result.checkpoint_asset_id == null) fail('accepted job.pause must return a checkpoint Asset')
}

export const ERROR_CODES: Record<string, number> = {
  incompatible_generation: 1001, stale_lease: 1002, cancelled: 1003, deadline_exceeded: 1004,
  asset_error: 1005, settings_invalid: 1006, migration_failed: 1007, duplicate_request: 1008,
  uncertain_external_effect: 1009, invalid_transition: 1010, result_contract_mismatch: 1011,
  data_interpreter_unavailable: 1012, release_retiring: 1013, checkpoint_invalid: 1014,
}

export function validateRpcResponse(response: JsonObject, method?: string, request?: JsonObject): void {
  if (request != null) {
    if (method == null) method = request.method
    else if (method !== request.method) fail('RPC response method does not match its request')
  }
  if (Object.prototype.hasOwnProperty.call(response, 'error')) {
    assertExactKeys(response, ['jsonrpc', 'id', 'error'], 'RPC error response')
    const error = asObject(response.error, 'RPC error')
    if (!Object.values(ERROR_CODES).includes(error.code)) fail('RPC error code is not in the v1 registry')
    return
  }
  assertExactKeys(response, ['jsonrpc', 'id', 'result'], 'RPC success response')
  if (method != null) validateRpcResult(method, asObject(response.result, 'RPC result'), request)
}

export function canonicalBytes(value: unknown): Uint8Array {
  return utf8(canonicalJson(value))
}

export { hashJcs, sha256Hex, utf8 }
