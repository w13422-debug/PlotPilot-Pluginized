import { canonicalJson, hashJcs, sha256Hex, utf8 } from './canonical'
import type { BackupBundle, CandidateItem, ResultBundle, RunSnapshot, SkillChainRef } from './types'

const HASH = /^[0-9a-f]{64}$/
const RESERVED = new Set(['con', 'prn', 'aux', 'nul', 'clock$', ...Array.from({ length: 9 }, (_, index) => `com${index + 1}`), ...Array.from({ length: 9 }, (_, index) => `lpt${index + 1}`)])

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

export function assertHash(value: string, label: string): void {
  if (!HASH.test(value)) throw new Error(`${label} must be lowercase SHA-256`)
}

export function normalizeWindowsPath(path: string): string {
  if (!path || path.includes('\\') || path.includes('\0') || path.startsWith('/') || /^[A-Za-z]:/.test(path)) throw new Error('invalid package path')
  const normalized = path.normalize('NFC')
  const parts = normalized.split('/')
  if (parts.some(part => !part || part === '.' || part === '..' || /[<>"|?*:\u0000-\u001f]/.test(part) || /[. ]$/.test(part))) throw new Error('invalid Windows path segment')
  if (parts.some(part => RESERVED.has(part.split('.', 1)[0]?.toLowerCase() ?? ''))) throw new Error('reserved Windows path')
  if ([...normalized].length > 240) throw new Error('path exceeds 240 Unicode scalar values')
  return normalized
}

export async function buildFilesSha256(files: Record<string, Uint8Array>): Promise<Uint8Array> {
  const normalized = new Map<string, Uint8Array>()
  for (const [rawPath, content] of Object.entries(files)) {
    const path = normalizeWindowsPath(rawPath)
    if (path === 'files.sha256') throw new Error('files.sha256 is generated')
    const key = path.toLowerCase()
    if ([...normalized.keys()].some(existing => existing.toLowerCase() === key)) throw new Error('casefold path collision')
    normalized.set(path, content)
  }
  const rows: string[] = []
  for (const path of [...normalized.keys()].sort(compareUtf8)) rows.push(`${await sha256Hex(normalized.get(path) as Uint8Array)}  ${path}\n`)
  return utf8(rows.join(''))
}

export async function packageDigest(files: Record<string, Uint8Array>, pluginId: string, version: string): Promise<{ filesSha256: Uint8Array; packageHash: string; releaseId: string }> {
  const filesSha256 = await buildFilesSha256(files)
  const packageHash = await sha256Hex(new Uint8Array([...utf8('plotpilot-package/v1\n'), ...filesSha256]))
  const releaseId = await sha256Hex(utf8(`plotpilot-release/v1\n${pluginId}\n${version}\n${packageHash}\n`))
  return { filesSha256, packageHash, releaseId }
}

export async function skillPackageDigest(files: Record<string, Uint8Array>, skillId: string, version: string): Promise<{ filesSha256: Uint8Array; skillPackageHash: string; skillReleaseId: string }> {
  const filesSha256 = await buildFilesSha256(files)
  const skillPackageHash = await sha256Hex(new Uint8Array([...utf8('plotpilot-skill-package/v1\n'), ...filesSha256]))
  const skillReleaseId = await sha256Hex(utf8(`plotpilot-skill-release/v1\n${skillId}\n${version}\n${skillPackageHash}\n`))
  return { filesSha256, skillPackageHash, skillReleaseId }
}

export function requestKeyBytes(snapshot: RunSnapshot): Uint8Array {
  const paramsHash = snapshot.parameters_asset_id == null ? '-' : snapshot.asset_hashes.find(asset => asset.asset_id === snapshot.parameters_asset_id)?.sha256
  if (paramsHash == null) throw new Error('parameters asset is absent from asset_hashes')
  const revisions = [...snapshot.input_revisions].sort((a, b) => compareUtf8(`${a.document_id}\0${a.revision_id}`, `${b.document_id}\0${b.revision_id}`))
  const revisionLine = revisions.map(item => `${item.document_id}=${item.revision_id}=${item.content_hash}`).join(',')
  return utf8(['request-key/v1', snapshot.workspace_id, snapshot.scope.operation, snapshot.scope.document_id ?? 'null', snapshot.scope.node_id ?? 'null', revisionLine, snapshot.plan_revision_id, paramsHash, snapshot.run_intent_id, ''].join('\n'))
}

export async function verifySnapshot(snapshot: RunSnapshot): Promise<void> {
  assertHash(snapshot.request_key, 'request_key')
  assertHash(snapshot.snapshot_hash, 'snapshot_hash')
  const expectedRequest = await sha256Hex(requestKeyBytes(snapshot))
  if (expectedRequest !== snapshot.request_key) throw new Error('request_key mismatch')
  const unsigned = { ...snapshot }
  delete (unsigned as Partial<RunSnapshot>).snapshot_hash
  const setLike = {
    ...unsigned,
    input_revisions: [...snapshot.input_revisions].sort((a, b) => compareUtf8(`${a.document_id}\0${a.revision_id}`, `${b.document_id}\0${b.revision_id}`)),
    plugin_releases: [...snapshot.plugin_releases].sort((a, b) => compareUtf8(a.plugin_id, b.plugin_id)),
    plugin_settings_revisions: [...snapshot.plugin_settings_revisions].sort((a, b) => compareUtf8(`${a.plugin_id}\0${a.scope}\0${a.scope_id ?? ''}`, `${b.plugin_id}\0${b.scope}\0${b.scope_id ?? ''}`)),
    asset_hashes: [...snapshot.asset_hashes].sort((a, b) => compareUtf8(a.asset_id, b.asset_id)),
  }
  const expectedSnapshot = await hashJcs('run-snapshot/v1', setLike)
  if (expectedSnapshot !== snapshot.snapshot_hash) throw new Error('snapshot_hash mismatch')
}

export function verifyCandidate(item: CandidateItem, snapshotWorkspaceId: string): void {
  if (item.target.workspace_id !== snapshotWorkspaceId) throw new Error('candidate target is outside snapshot workspace')
  const targetKey = `${item.target.workspace_id}/${item.target.entity_kind}/${item.target.entity_id}`
  const writeKeys = new Set(item.write_set.map(entry => `${entry.workspace_id}/${entry.entity_kind}/${entry.entity_id}`))
  if (!writeKeys.has(targetKey) || item.write_set.some(entry => entry.workspace_id !== snapshotWorkspaceId)) throw new Error('candidate write_set mismatch')
  if (item.item_kind === 'incomplete_stream') {
    if (item.status !== 'partial' || item.target.entity_kind !== 'document' || item.mutation.mode !== 'replace' || item.mutation.payload_schema !== 'core/document-text/v1') throw new Error('invalid incomplete stream Candidate')
    return
  }
  const expected: Record<string, { kind: string; modes: string[] }> = {
    document: { kind: 'document', modes: ['replace', 'text_patch', 'append_text'] },
    node_structure: { kind: 'node_structure', modes: ['structure_patch'] },
    relation_set: { kind: 'relation_set', modes: ['relation_patch'] },
  }
  const rule = expected[item.target.entity_kind]
  if (rule == null || item.item_kind !== rule.kind || !rule.modes.includes(item.mutation.mode)) throw new Error('candidate item/mutation mismatch')
}

function verifyBundleRef(ref: SkillChainRef, bundleId: string, itemIds: Set<string>): void {
  const bundlePair = ref.result_bundle_id !== null && ref.result_item_id !== null
  const streamPair = ref.stream_id !== null && ref.acked_prefix_hash !== null
  if ((ref.result_bundle_id === null) !== (ref.result_item_id === null) || (ref.stream_id === null) !== (ref.acked_prefix_hash === null) || bundlePair === streamPair) throw new Error('invalid Skill chain anchor profile')
  if (!bundlePair || ref.result_bundle_id !== bundleId || !itemIds.has(ref.result_item_id as string)) throw new Error('Bundle Skill ref must point at this Bundle item')
}

export function verifyResultProfile(bundle: ResultBundle): void {
  const expected: Record<string, [string, string]> = { 'candidate-batch/v1': ['candidate_batch', 'candidate-item/v1'], 'artifact-bundle/v1': ['artifact', 'artifact-item/v1'], 'diagnostic-bundle/v1': ['diagnostic', 'diagnostic-item/v1'] }
  const profile = expected[bundle.contract_id]
  if (profile == null || bundle.bundle_type !== profile[0] || bundle.items.some(item => item.schema !== profile[1])) throw new Error('result profile mismatch')
  if (bundle.partial === false && bundle.items.some(item => item.status !== 'complete')) throw new Error('complete bundle contains partial item')
  const itemIds = new Set(bundle.items.map(item => item.item_id))
  if (bundle.contract_id === 'candidate-batch/v1') for (const item of bundle.items) verifyCandidate(item as CandidateItem, bundle.input_snapshot_hash)
  for (const ref of bundle.skill_chain_result_refs) verifyBundleRef(ref, bundle.bundle_id, itemIds)
}

export async function verifySelfHash(value: Record<string, unknown>, field: string, prefix: string): Promise<void> {
  const unsigned = { ...value }
  delete unsigned[field]
  const actual = await hashJcs(prefix, unsigned)
  if (actual !== value[field]) throw new Error(`${field} mismatch`)
}

export async function verifyBackup(bundle: BackupBundle): Promise<void> {
  const unsigned = { ...bundle }
  delete (unsigned as Partial<BackupBundle>).bundle_hash
  const expected = await hashJcs('plotpilot-backup/v1', unsigned)
  if (expected !== bundle.bundle_hash) throw new Error('bundle_hash mismatch')
  const paths = bundle.files.map(file => normalizeWindowsPath(file.path))
  const sorted = [...paths].sort(compareUtf8)
  if (paths.some((path, index) => path !== sorted[index]) || new Set(paths.map(path => path.toLowerCase())).size !== paths.length) throw new Error('backup files are not sorted/casefold-unique')
  const roles = new Set(bundle.files.map(file => file.role))
  if ((bundle.mode === 'workspace' && (roles.has('plugin_db') || roles.has('package'))) || (bundle.mode === 'data' && roles.has('package'))) throw new Error('backup mode includes a forbidden role')
}

export function canonicalBytes(value: unknown): Uint8Array {
  return utf8(canonicalJson(value))
}
