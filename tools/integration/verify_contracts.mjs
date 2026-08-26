#!/usr/bin/env node
/** Independent Node verifier for the M0 cross-language golden vectors. */
import { readFileSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(fileURLToPath(new URL('../..', import.meta.url)))
const CONTRACTS = join(ROOT, 'contracts')
const SCHEMAS = join(CONTRACTS, 'json-schema')
const GOLDEN = join(CONTRACTS, 'golden')
const text = (path) => readFileSync(path, 'utf8')
const bytes = (path) => readFileSync(path)
const sha256 = (value) => createHash('sha256').update(value).digest('hex')
const utf8 = (value) => Buffer.from(value, 'utf8')

function canonicalJson(value) {
  if (value === null) return 'null'
  if (typeof value === 'string') return JSON.stringify(value)
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('non-finite number')
    if (Object.is(value, -0)) return '0'
    return JSON.stringify(value)
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (typeof value === 'object' && value !== undefined) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`
  }
  throw new Error('not JSON')
}

function hashJcs(prefix, value) {
  return sha256(Buffer.concat([utf8(`${prefix}\n`), utf8(canonicalJson(value))]))
}

function readJson(path) {
  const raw = bytes(path)
  if (raw.subarray(0, 3).equals(Buffer.from([0xef, 0xbb, 0xbf]))) throw new Error(`BOM: ${path}`)
  return JSON.parse(raw.toString('utf8'))
}

function utf8Compare(a, b) {
  const left = utf8(a)
  const right = utf8(b)
  const size = Math.min(left.length, right.length)
  for (let i = 0; i < size; i += 1) if (left[i] !== right[i]) return left[i] - right[i]
  return left.length - right.length
}

function filesManifest(fileEntries) {
  return fileEntries
    .sort((a, b) => utf8Compare(a[0], b[0]))
    .map(([path, content]) => `${sha256(content)}  ${path}\n`)
    .join('')
}

function verifyPackage() {
  const dir = join(GOLDEN, 'package')
  const files = [['plugin.json', bytes(join(dir, 'plugin.json'))], ['data/rules.json', bytes(join(dir, 'data', 'rules.json'))]]
  const manifest = filesManifest(files)
  if (manifest !== text(join(dir, 'files.sha256'))) throw new Error('package files.sha256 mismatch')
  const packageHash = sha256(Buffer.concat([utf8('plotpilot-package/v1\n'), utf8(manifest)]))
  const pluginHash = sha256(bytes(join(dir, 'plugin.json')))
  const releaseId = sha256(utf8(`plotpilot-release/v1\ncom.plotpilot.golden.echo\n1.0.0\n${packageHash}\n`))
  const expected = readJson(join(dir, 'expected.json'))
  if (packageHash !== expected.package_hash || releaseId !== expected.release_id) throw new Error('package golden mismatch')
  if (pluginHash !== '5e3188bf60c1e12c38e25f6e39ca11376bd82d3dee285e99cdb981b8e7b4186c') throw new Error('plugin bytes drift')
  return { package_hash: packageHash, release_id: releaseId }
}

function verifySkill() {
  const dir = join(GOLDEN, 'skill')
  const files = [['skill.json', bytes(join(dir, 'skill.json'))], ['prompt.txt', bytes(join(dir, 'prompt.txt'))]]
  const manifest = filesManifest(files)
  if (manifest !== text(join(dir, 'files.sha256'))) throw new Error('Skill files.sha256 mismatch')
  const packageHash = sha256(Buffer.concat([utf8('plotpilot-skill-package/v1\n'), utf8(manifest)]))
  const releaseId = sha256(utf8(`plotpilot-skill-release/v1\ncom.plotpilot.skill.golden\n1.0.0\n${packageHash}\n`))
  const expected = readJson(join(dir, 'expected.json'))
  if (packageHash !== expected.skill_package_hash || releaseId !== expected.skill_release_id) throw new Error('Skill golden mismatch')
  return { skill_package_hash: packageHash, skill_release_id: releaseId }
}

function verifySnapshot() {
  const dir = join(GOLDEN, 'run-snapshot')
  const snapshot = readJson(join(dir, 'snapshot.json'))
  const assets = new Map(snapshot.asset_hashes.map((item) => [item.asset_id, item.sha256]))
  const revisions = [...snapshot.input_revisions].sort((a, b) => utf8Compare(`${a.document_id}\0${a.revision_id}`, `${b.document_id}\0${b.revision_id}`))
  const revisionLine = revisions.map((item) => `${item.document_id}=${item.revision_id}=${item.content_hash}`).join(',')
  const requestBytes = `${['request-key/v1', snapshot.workspace_id, snapshot.scope.operation, snapshot.scope.document_id ?? 'null', snapshot.scope.node_id ?? 'null', revisionLine, snapshot.plan_revision_id, assets.get(snapshot.parameters_asset_id) ?? '-', snapshot.run_intent_id, ''].join('\n')}`
  const requestKey = sha256(utf8(requestBytes))
  const unsigned = { ...snapshot }
  delete unsigned.snapshot_hash
  const setLike = {
    ...unsigned,
    input_revisions: revisions,
    plugin_releases: [...unsigned.plugin_releases].sort((a, b) => utf8Compare(a.plugin_id, b.plugin_id)),
    plugin_settings_revisions: [...unsigned.plugin_settings_revisions].sort((a, b) => utf8Compare(`${a.plugin_id}\0${a.scope}\0${a.scope_id ?? ''}`, `${b.plugin_id}\0${b.scope}\0${b.scope_id ?? ''}`)),
    asset_hashes: [...unsigned.asset_hashes].sort((a, b) => utf8Compare(a.asset_id, b.asset_id)),
  }
  const snapshotHash = hashJcs('run-snapshot/v1', setLike)
  if (requestKey !== snapshot.request_key || snapshotHash !== snapshot.snapshot_hash) throw new Error('RunSnapshot golden mismatch')
  if (!bytes(join(dir, 'request-key.txt')).equals(utf8(requestBytes))) throw new Error('request-key bytes mismatch')
  if (!bytes(join(dir, 'snapshot.jcs')).equals(utf8(canonicalJson(snapshot)))) throw new Error('JCS bytes mismatch')
  return { request_key: requestKey, snapshot_hash: snapshotHash }
}

function verifyBackup() {
  const backup = readJson(join(GOLDEN, 'backup', 'backup.json'))
  const unsigned = { ...backup }
  delete unsigned.bundle_hash
  const bundleHash = hashJcs('plotpilot-backup/v1', unsigned)
  if (bundleHash !== backup.bundle_hash) throw new Error('backup golden mismatch')
  return { backup_hash: bundleHash }
}

function verifyInventory() {
  const schemaFiles = readdirSync(SCHEMAS).filter((name) => name.endsWith('.schema.json')).sort()
  if (schemaFiles.length !== 48) throw new Error(`expected 48 schemas, got ${schemaFiles.length}`)
  for (const name of schemaFiles) {
    const schema = readJson(join(SCHEMAS, name))
    if (schema.$schema !== 'https://json-schema.org/draft/2020-12/schema') throw new Error(`schema dialect drift: ${name}`)
  }
  const groups = readdirSync(join(CONTRACTS, 'corpus', 'negative', '84.13')).filter((name) => name.endsWith('.json'))
  if (groups.length !== 14) throw new Error(`expected 14 negative groups, got ${groups.length}`)
  return { schema_count: schemaFiles.length, negative_group_count: groups.length }
}

if (process.argv.includes('--all')) {
  const result = { inventory: verifyInventory(), package: verifyPackage(), skill: verifySkill(), snapshot: verifySnapshot(), backup: verifyBackup() }
  console.log(JSON.stringify(result, null, 2))
} else {
  console.error('pass --all')
  process.exitCode = 2
}
