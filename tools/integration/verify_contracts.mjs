#!/usr/bin/env node
/** Independent Node verifier for the M0 cross-language golden vectors. */
import { readFileSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { createHash } from 'node:crypto'
import { fileURLToPath, pathToFileURL } from 'node:url'

const ROOT = resolve(fileURLToPath(new URL('../..', import.meta.url)))
const CONTRACTS = join(ROOT, 'contracts')
const SCHEMAS = join(CONTRACTS, 'json-schema')
const GOLDEN = join(CONTRACTS, 'golden')
const V2_GOLDEN = join(GOLDEN, 'm4-m5-public-surface-v2')
const V2_CORPUS = join(CONTRACTS, 'corpus', 'm4-m5-public-surface-v2')
const PROMPT_GOLDEN = join(GOLDEN, 'prompt-skill-rpc-v2')
const PROMPT_CORPUS = join(CONTRACTS, 'corpus', 'prompt-skill-rpc-v2')
const MACRO_GOLDEN = join(GOLDEN, 'macro-planning-host-v1')
const MACRO_CORPUS = join(CONTRACTS, 'corpus', 'macro-planning-host-v1')
const MODEL_PROVIDER_GOLDEN = join(GOLDEN, 'model-provider-rpc-v2')
const MODEL_PROVIDER_CORPUS = join(CONTRACTS, 'corpus', 'model-provider-rpc-v2')
const SDK_RESOURCES = join(ROOT, 'backend', 'plotpilot_plugin_sdk', 'resources')
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

function jsonEqual(left, right) {
  return canonicalJson(left) === canonicalJson(right)
}

// The Node gate only needs the closed-contract subset used by this source
// wave.  It intentionally mirrors the existing schema ingress rules instead
// of introducing a second generated schema set or a dependency.
function validateClosed(value, schema, label = '$') {
  if (schema === false) throw new Error(`${label}: schema rejects value`)
  if (schema === true) return
  if (schema.$ref) throw new Error(`${label}: unexpected external reference ${schema.$ref}`)
  if (schema.oneOf) {
    const matches = schema.oneOf.filter((branch) => {
      try { validateClosed(value, branch, label); return true } catch (_) { return false }
    })
    if (matches.length !== 1) throw new Error(`${label}: oneOf matched ${matches.length} branches`)
    return
  }
  if (schema.anyOf) {
    if (!schema.anyOf.some((branch) => { try { validateClosed(value, branch, label); return true } catch (_) { return false } })) throw new Error(`${label}: anyOf matched no branch`)
    return
  }
  if (schema.const !== undefined && !jsonEqual(value, schema.const)) throw new Error(`${label}: const mismatch`)
  if (schema.enum !== undefined && !schema.enum.some((item) => jsonEqual(value, item))) throw new Error(`${label}: enum mismatch`)
  if (schema.type === 'object') {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label}: expected object`)
  } else if (schema.type === 'array') {
    if (!Array.isArray(value)) throw new Error(`${label}: expected array`)
  } else if (schema.type === 'string') {
    if (typeof value !== 'string') throw new Error(`${label}: expected string`)
    if (schema.pattern && !(new RegExp(schema.pattern, 'u')).test(value)) throw new Error(`${label}: pattern mismatch`)
    if (schema.minLength !== undefined && [...value].length < schema.minLength) throw new Error(`${label}: minLength`)
    if (schema.maxLength !== undefined && [...value].length > schema.maxLength) throw new Error(`${label}: maxLength`)
  } else if (schema.type === 'integer') {
    if (!Number.isSafeInteger(value)) throw new Error(`${label}: expected integer`)
    if (schema.minimum !== undefined && value < schema.minimum) throw new Error(`${label}: minimum`)
    if (schema.maximum !== undefined && value > schema.maximum) throw new Error(`${label}: maximum`)
  } else if (schema.type === 'number') {
    if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`${label}: expected number`)
    if (schema.minimum !== undefined && value < schema.minimum) throw new Error(`${label}: minimum`)
    if (schema.maximum !== undefined && value > schema.maximum) throw new Error(`${label}: maximum`)
  } else if (schema.type === 'null') {
    if (value !== null) throw new Error(`${label}: expected null`)
  } else if (schema.type === 'boolean' && typeof value !== 'boolean') throw new Error(`${label}: expected boolean`)
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) throw new Error(`${label}: minItems`)
    if (schema.maxItems !== undefined && value.length > schema.maxItems) throw new Error(`${label}: maxItems`)
    if (schema.uniqueItems && new Set(value.map(canonicalJson)).size !== value.length) throw new Error(`${label}: duplicate array item`)
    if (schema.items && !Array.isArray(schema.items)) value.forEach((item, index) => validateClosed(item, schema.items, `${label}[${index}]`))
  }
  if (schema.type === 'object') {
    const properties = schema.properties ?? {}
    for (const key of schema.required ?? []) if (!Object.prototype.hasOwnProperty.call(value, key)) throw new Error(`${label}: missing ${key}`)
    for (const key of Object.keys(value)) {
      if (!Object.prototype.hasOwnProperty.call(properties, key)) {
        if (schema.additionalProperties === false) throw new Error(`${label}: unknown ${key}`)
      } else validateClosed(value[key], properties[key], `${label}.${key}`)
    }
  }
}

async function verifyV2() {
  const matrix = readJson(join(SCHEMAS, 'core-api-method-matrix.v2.json'))
  const macroRouteIds = new Set(['model-secret.put', 'model-profile.revise', 'workspace-plan.select', 'project-planning.get', 'project-planning.start'])
  const routes = matrix.routes.filter((route) => !macroRouteIds.has(route.route_id))
  if (matrix.schema !== 'core-api-method-matrix/v2' || matrix.publication_path !== 'publication.accept' || matrix.publication_owner !== 'core' || matrix.plugin_publication_allowed !== false) throw new Error('v2 Publication ownership drift')
  if (matrix.routes.length !== 24 || routes.length !== 19 || routes.filter((route) => route.route_id === 'publication.accept').length !== 1) throw new Error('v2 route inventory drift')
  if (matrix.routes.some((route) => route.route_id !== 'publication.accept' && route.path_template.includes('publication'))) throw new Error('v2 exposes a second Publication route')
  const schema = (name) => readJson(join(SCHEMAS, name))
  const candidateSchema = schema('candidate-query-result-v2.schema.json')
  const coreSchema = schema('core-authority-command-query-v2.schema.json')
  const reviewSchema = schema('candidate-review-v2.schema.json')
  const projectionSchema = schema('story-state-projection-input-v2.schema.json')
  const jobSchema = schema('job-http-command-query-v2.schema.json')
  const pluginSchema = schema('plugin-api-command-query-v2.schema.json')
  const candidate = readJson(join(V2_GOLDEN, 'candidate.json'))
  const review = readJson(join(V2_GOLDEN, 'review.json'))
  const publication = readJson(join(V2_GOLDEN, 'publication.json'))
  const projection = readJson(join(V2_GOLDEN, 'story-state.json')).projection
  const job = readJson(join(V2_GOLDEN, 'job.json'))
  const plugin = readJson(join(V2_GOLDEN, 'plugin.json'))
  const http = readJson(join(V2_GOLDEN, 'http.json'))
  const tsCore = await import(pathToFileURL(join(ROOT, 'frontend', 'src', 'contracts', 'core-api-v2.ts')).href)
  const tsHttp = await import(pathToFileURL(join(ROOT, 'frontend', 'src', 'contracts', 'm4-m5-http-v2.ts')).href)
  for (const [name, value] of [['candidate', candidate.candidate], ['candidate_text_patch', candidate.candidate_text_patch], ['candidate_replace', candidate.candidate_replace], ['candidate_structure_patch', candidate.candidate_structure_patch], ['candidate_relation_patch', candidate.candidate_relation_patch]]) validateClosed(value, candidateSchema, `candidate.${name}`)
  validateClosed(candidate.candidate_list_result, candidateSchema, 'candidate_list_result')
  validateClosed(candidate.candidate_get_query, candidateSchema, 'candidate_get_query')
  validateClosed(candidate.candidate_get_result, candidateSchema, 'candidate_get_result')
  validateClosed(candidate.candidate_preview_query, candidateSchema, 'candidate_preview_query')
  validateClosed(candidate.candidate_preview_result, candidateSchema, 'candidate_preview_result')
  validateClosed(review.query, reviewSchema, 'review.query')
  validateClosed(review.command, reviewSchema, 'review.command')
  validateClosed(review.result, reviewSchema, 'review.result')
  for (const name of ['command_complete', 'command_partial', 'command_incomplete_stream', 'result_complete', 'result_incomplete_stream', 'command_replace', 'result_replace', 'command_structure_patch', 'result_structure_patch', 'command_relation_patch', 'result_relation_patch']) validateClosed(publication[name], coreSchema, `publication.${name}`)
  validateClosed(projection, projectionSchema, 'story-state.projection')
  for (const name of ['list_query', 'list_result', 'snapshot_query', 'snapshot_result', 'start', 'control', 'command_result', 'event_query', 'event_page', 'sse_replay_query', 'sse_replay', 'sse_gap_query', 'sse_gap']) validateClosed(job[name], jobSchema, `job.${name}`)
  for (const name of ['discovery_query', 'discovery_result', 'discovery_error', 'install', 'upgrade', 'retire', 'rollback', 'lifecycle_result_install', 'lifecycle_result_upgrade', 'lifecycle_result_retire', 'lifecycle_result_rollback']) validateClosed(plugin[name], pluginSchema, `plugin.${name}`)
  if (candidate.candidate.target.workspace_id !== candidate.candidate.workspace_id || candidate.candidate.write_set.some((item) => item.workspace_id !== candidate.candidate.workspace_id)) throw new Error('v2 Candidate Workspace binding drift')
  if (publication.command_partial.candidate_id !== candidate.candidate_partial.candidate_id || candidate.candidate_partial.publication_eligibility !== 'review_only') throw new Error('v2 partial Candidate fixture drift')
  if (projection.publication.candidate_id !== projection.candidate.candidate_id || projection.publication.revision_id !== projection.current_revision.revision_id) throw new Error('v2 projection binding drift')
  if (job.sse_replay.snapshot_required || job.sse_replay.gap || job.sse_gap.snapshot_required !== true || job.sse_gap.gap !== true) throw new Error('v2 SSE recovery fixture drift')
  if (matrix.cursor_domains.candidate === matrix.cursor_domains.job || matrix.cursor_domains.job === matrix.cursor_domains.core) throw new Error('v2 cursor domains are not disjoint')
  if (http.schema !== 'm4-m5-public-surface-http-golden/v2' || http.exchange_count !== http.exchanges.length || http.exchanges.length !== routes.length) throw new Error('v2 HTTP golden exchange inventory drift')
  const routeIds = routes.map((route) => route.route_id)
  const observedRouteIds = http.exchanges.map((exchange) => exchange.route_id)
  if (new Set(observedRouteIds).size !== observedRouteIds.length || new Set(observedRouteIds).size !== new Set(routeIds).size || !routeIds.every((routeId) => observedRouteIds.includes(routeId))) throw new Error('v2 HTTP goldens do not cover every route exactly once')
  const corpusFiles = readdirSync(V2_CORPUS).filter((name) => name.endsWith('.json') && name !== 'manifest.json')
  const corpus = readJson(join(V2_CORPUS, 'manifest.json'))
  const groups = corpusFiles.map((name) => readJson(join(V2_CORPUS, name)))
  const negativeCases = groups.reduce((total, group) => total + group.negative.length, 0)
  if (groups.length !== 5 || corpus.group_count !== 5 || corpus.negative_case_count !== negativeCases) throw new Error('v2 corpus inventory drift')
  const expected = readJson(join(V2_GOLDEN, 'expected.json'))
  for (const [name, digest] of Object.entries(expected.fixture_files)) if (sha256(bytes(join(V2_GOLDEN, name))) !== digest) throw new Error(`v2 golden hash drift ${name}`)

  const candidateById = new Map([candidate.candidate, candidate.candidate_text_patch, candidate.candidate_replace, candidate.candidate_structure_patch, candidate.candidate_relation_patch, candidate.candidate_partial, candidate.candidate_incomplete_stream].map((item) => [item.candidate_id, item]))
  tsCore.parseCandidateQueryResultV2(candidate.candidate)
  tsCore.parseCandidateQueryResultV2(candidate.candidate_list_result)
  tsCore.parseCandidateQueryResultV2(candidate.candidate_get_query)
  tsCore.parseCandidateQueryResultV2(candidate.candidate_get_result)
  tsCore.parseCandidateQueryResultV2(candidate.candidate_preview_query)
  tsCore.parseCandidateQueryResultV2(candidate.candidate_preview_result)
  tsCore.parseCandidateQueryResultV2(candidate.candidate_cross_workspace_source)
  tsCore.parseCandidateQueryResultV2(candidate.candidate_partial)
  tsCore.parseCandidateQueryResultV2(candidate.candidate_incomplete_stream)
  tsCore.parseCandidateReviewV2(review.query)
  tsCore.parseCandidateReviewV2(review.command)
  tsCore.parseCandidateReviewV2(review.result)
  for (const [candidateValue, commandValue, resultValue] of [[candidate.candidate_replace, publication.command_replace, publication.result_replace], [candidate.candidate, publication.command_complete, publication.result_complete], [candidate.candidate_structure_patch, publication.command_structure_patch, publication.result_structure_patch], [candidate.candidate_relation_patch, publication.command_relation_patch, publication.result_relation_patch]]) {
    await tsCore.validatePublicationV2(commandValue, resultValue, candidateValue, 'ws-1')
    if (resultValue.content_hash === candidateValue.mutation.payload_hash) throw new Error('Publication golden collapsed mutation payload hash into final Revision hash')
  }
  tsCore.validatePublicationV2(publication.command_incomplete_stream, publication.result_incomplete_stream, candidate.candidate_incomplete_stream, 'ws-1')
  tsCore.parseStoryStateProjectionInputV2(projection, 'ws-1')
  await tsCore.verifyJobSnapshotHashV2(job.snapshot)
  for (const name of ['list_query', 'list_result', 'snapshot_query', 'snapshot_result', 'start', 'control', 'command_result', 'event_query', 'event_page', 'sse_replay_query', 'sse_replay', 'sse_gap_query', 'sse_gap']) {
    const value = job[name]
    if (name === 'event_page') tsCore.parseJobEventPageV2(value)
    else if (name === 'sse_replay' || name === 'sse_gap') await tsHttp.parseJobSseRecoveryV2(value)
    else if (name === 'list_result') await tsCore.validateJobListResultV2(value)
    else if (name === 'snapshot_result') await tsCore.validateJobSnapshotResultV2(value)
    else if (name === 'command_result') tsCore.validateJobCommandResultV2(value)
    else if (name === 'event_query') tsCore.validateJobEventPageQueryV2(value)
    else if (name === 'sse_replay_query' || name === 'sse_gap_query') tsCore.validateJobSseRecoveryQueryV2(value)
  }
  for (const name of ['discovery_query', 'discovery_result', 'install', 'upgrade', 'retire', 'rollback', 'lifecycle_result_install', 'lifecycle_result_upgrade', 'lifecycle_result_retire', 'lifecycle_result_rollback']) tsCore.parsePluginApiV2(plugin[name])
  tsCore.validatePluginLifecycleV2(plugin.install)
  tsCore.validatePluginLifecycleV2(plugin.upgrade, { currentGenerationId: 'generation-1' })
  tsCore.validatePluginLifecycleV2(plugin.retire, { currentGenerationId: 'generation-2' })
  tsCore.validatePluginLifecycleV2(plugin.rollback, { currentGenerationId: 'generation-2' })

  for (const exchange of http.error_exchanges ?? []) await tsHttp.validateHttpExchangeV2(exchange.route_id, exchange.request, exchange.status, exchange.response)

  for (const exchange of http.exchanges) {
    await tsHttp.validateHttpExchangeV2(exchange.route_id, exchange.request, exchange.status, exchange.response, candidateById.get(exchange.request.candidate_id))
  }

  const fixtures = {
    'candidate.record': candidate.candidate,
    'candidate.text_patch': candidate.candidate_text_patch,
    'candidate.replace': candidate.candidate_replace,
    'candidate.structure_patch': candidate.candidate_structure_patch,
    'candidate.relation_patch': candidate.candidate_relation_patch,
    'candidate.list_result': candidate.candidate_list_result,
    'candidate.cross_workspace_source_ref': candidate.candidate_cross_workspace_source,
    'candidate.get_result': candidate.candidate_get_result,
    'candidate.preview_result': candidate.candidate_preview_result,
    'review.query': review.query,
    'review.command': review.command,
    'review.result': review.result,
    'publication.command.complete': publication.command_complete,
    'publication.result.complete': publication.result_complete,
    'publication.command.replace': publication.command_replace,
    'publication.result.replace': publication.result_replace,
    'publication.command.structure_patch': publication.command_structure_patch,
    'publication.result.structure_patch': publication.result_structure_patch,
    'publication.command.relation_patch': publication.command_relation_patch,
    'publication.result.relation_patch': publication.result_relation_patch,
    'publication.command.partial': publication.command_partial,
    'publication.command.incomplete_stream': publication.command_incomplete_stream,
    'publication.result.incomplete_stream': publication.result_incomplete_stream,
    'story_state.projection': projection,
    'story_state.projection_with_unreachable_receipt': readJson(join(V2_GOLDEN, 'story-state.json')).projection_with_unreachable_receipt,
    'job.snapshot': job.snapshot,
    'job.list_result': job.list_result,
    'job.command.start': job.start,
    'job.event_page': job.event_page,
    'job.sse.replay': job.sse_replay,
    'job.sse.gap': job.sse_gap,
    'plugin.discovery': plugin.discovery_result,
    'plugin.discovery_error': plugin.discovery_error,
    'plugin.lifecycle.install': plugin.install,
    'plugin.lifecycle.upgrade': plugin.upgrade,
    'plugin.lifecycle.retire': plugin.retire,
    'plugin.lifecycle.rollback': plugin.rollback,
    'plugin.lifecycle.result.install': plugin.lifecycle_result_install,
    'plugin.lifecycle.result.upgrade': plugin.lifecycle_result_upgrade,
    'plugin.lifecycle.result.retire': plugin.lifecycle_result_retire,
    'plugin.lifecycle.result.rollback': plugin.lifecycle_result_rollback,
  }
  for (const exchange of http.exchanges) fixtures[`http.${exchange.route_id}`] = exchange
  for (const exchange of http.error_exchanges ?? []) fixtures['http.plugin.discovery.error'] = exchange

  const parseFixture = (fixtureId, value) => {
    if (fixtureId.startsWith('candidate.')) return tsCore.parseCandidateQueryResultV2(value)
    if (fixtureId.startsWith('review.')) return tsCore.parseCandidateReviewV2(value)
    if (fixtureId.startsWith('publication.')) return tsCore.parseCoreAuthorityV2(value)
    if (fixtureId.startsWith('story_state.projection')) return tsCore.parseStoryStateProjectionInputV2(value)
    if (fixtureId === 'job.snapshot') return tsCore.validateJobSnapshotResultV2({ schema: 'job-snapshot-result/v2', workspace_id: value.workspace_id, job_id: value.job_id, snapshot: value, cursor: `job/${value.job_id}/${value.job_event_high_water}` })
    if (fixtureId.startsWith('job.')) {
      if (fixtureId === 'job.event_page') return tsCore.parseJobEventPageV2(value)
      if (fixtureId === 'job.sse.replay' || fixtureId === 'job.sse.gap') return tsHttp.parseJobSseRecoveryV2(value)
      if (fixtureId === 'job.list_result') return tsCore.validateJobListResultV2(value)
      if (fixtureId === 'job.command.start') return tsHttp.parseHttpRequestV2('job.start', value)
      return value
    }
    if (fixtureId.startsWith('plugin.')) return tsCore.parsePluginApiV2(value)
    if (fixtureId.startsWith('http.')) return value
    throw new Error(`unknown v2 fixture ${fixtureId}`)
  }
  const mutate = (input, mutation) => {
    if (mutation.op === 'noop') return structuredClone(input)
    const output = structuredClone(input)
    if (mutation.op === 'cycle') {
      output.parent_candidate_ids = [output.candidate_id]
      return output
    }
    let target = output
    for (const token of mutation.path.slice(0, -1)) target = target[token]
    const key = mutation.path.at(-1)
    if (mutation.op === 'set') target[key] = structuredClone(mutation.value)
    else if (mutation.op === 'delete') Array.isArray(target) ? target.splice(Number(key), 1) : delete target[key]
    else throw new Error(`unsupported v2 mutation ${JSON.stringify(mutation)}`)
    return output
  }
  const rejected = async (action) => {
    try { await action(); return false } catch (_) { return true }
  }
  const executed = []
  for (const group of groups) {
    for (const fixtureId of group.positive) {
      if (!(fixtureId in fixtures)) throw new Error(`v2 corpus has no positive fixture ${fixtureId}`)
      await parseFixture(fixtureId, fixtures[fixtureId])
    }
    for (const testCase of group.negative) {
      const value = mutate(fixtures[testCase.fixture], testCase.mutation)
      const kind = testCase.kind
      let action
      if (kind === 'closed_schema') action = () => parseFixture(testCase.fixture, value)
      else if (kind === 'candidate_semantics') action = () => tsCore.validateCandidateV2(value)
      else if (kind === 'candidate_preview') action = () => tsCore.parseCandidateQueryResultV2(value)
      else if (kind === 'candidate_parent_cycle') action = () => tsCore.validateCandidateV2(value, undefined, { [value.candidate_id]: value })
      else if (kind === 'publication_semantics') {
        if (testCase.fixture === 'publication.command.partial') {
          const partialResult = { ...publication.result_complete, publication_operation_key: value.publication_operation_key, candidate_id: value.candidate_id, content_hash: candidate.candidate_partial.mutation.payload_hash }
          action = () => tsCore.validatePublicationV2(value, partialResult, candidate.candidate_partial, 'ws-1')
        } else if (testCase.fixture === 'publication.command.complete') action = () => tsCore.validatePublicationV2(value, publication.result_complete, candidate.candidate, 'ws-1')
        else action = () => tsCore.validatePublicationV2(publication.command_complete, value, candidate.candidate, 'ws-1')
      } else if (kind === 'publication_write_set') action = () => tsCore.validatePublicationV2(publication.command_complete, publication.result_complete, value, 'ws-1')
      else if (kind === 'projection_semantics') action = () => tsCore.parseStoryStateProjectionInputV2(value)
      else if (kind === 'job_cursor') action = () => testCase.fixture === 'job.event_page' ? tsCore.parseJobEventPageV2(value) : tsHttp.parseJobSseRecoveryV2(value)
      else if (kind === 'job_sse') action = () => tsHttp.parseJobSseRecoveryV2(value)
      else if (kind === 'http_request') action = () => tsHttp.parseHttpRequestV2(testCase.route_id, value.request)
      else if (kind === 'http_exchange') action = () => tsHttp.validateHttpExchangeV2(testCase.route_id, value.request, value.status, value.response, candidateById.get(value.request.candidate_id))
      else if (kind === 'plugin_lifecycle') action = () => tsCore.validatePluginLifecycleV2(value, { currentGenerationId: 'generation-1', activeJob: value.action === 'retire' })
      else if (kind === 'plugin_publication') action = () => tsCore.parsePluginApiV2(value)
      else if (kind === 'operation_key_reuse') {
        const routeId = testCase.fixture.startsWith('publication') ? 'publication.accept' : 'plugin.upgrade'
        const response = routeId === 'publication.accept' ? publication.result_complete : plugin.lifecycle_result_upgrade
        const original = fixtures[testCase.fixture]
        const candidateForExchange = candidateById.get(original.candidate_id)
        const ledger = new tsHttp.OperationKeyLedgerV2()
        await ledger.record(routeId, original, 200, response, candidateForExchange)
        action = () => ledger.record(routeId, value, 200, response, candidateForExchange)
      } else if (kind === 'operation_response_drift') {
        const routeId = testCase.route_id
        const original = fixtures[testCase.fixture]
        const candidateForExchange = candidateById.get(original.request.candidate_id)
        const ledger = new tsHttp.OperationKeyLedgerV2()
        await ledger.record(routeId, original.request, original.status, original.response, candidateForExchange)
        action = () => ledger.record(routeId, value.request, value.status, value.response, candidateForExchange)
      } else throw new Error(`unknown v2 corpus kind ${kind}`)
      if (!(await rejected(action))) throw new Error(`v2 corpus false-accepted ${testCase.case_id}`)
      executed.push(testCase.case_id)
    }
  }
  if (executed.length !== negativeCases) throw new Error('v2 corpus execution count drift')
  const negativeCaseDigest = sha256(Buffer.from(JSON.stringify([...executed].sort()), 'utf8'))
  return { routes: routes.length, schemas: 6, golden_files: Object.keys(expected.fixture_files).length, corpus_groups: groups.length, negative_cases: negativeCases, negative_case_digest: negativeCaseDigest, http_exchanges: http.exchanges.length, publication_path: matrix.publication_path, cursor_domains: Object.keys(matrix.cursor_domains).sort() }
}

function verifyMacroPlanningHost() {
  const schemaFiles = {
    'model-config-command-query-v2': 'model-config-command-query-v2.schema.json',
    'model-profile-revision-v1': 'model-profile-revision-v1.schema.json',
    'model-planning-http-error-v2': 'model-planning-http-error-v2.schema.json',
    'project-planning-command-query-v2': 'project-planning-command-query-v2.schema.json',
    'project-planner-runtime-input-v2': 'project-planner-runtime-input-v2.schema.json',
    'project-planner-model-output-v1': 'project-planner-model-output-v1.schema.json',
  }
  const schemas = Object.fromEntries(Object.entries(schemaFiles).map(([name, file]) => [name, readJson(join(SCHEMAS, file))]))
  const schemaContracts = {
    'model-secret-put-command/v2': 'model-config-command-query-v2',
    'model-secret-put-result/v2': 'model-config-command-query-v2',
    'model-profile-revise-command/v2': 'model-config-command-query-v2',
    'model-profile-revise-result/v2': 'model-config-command-query-v2',
    'workspace-plan-selection-command/v2': 'model-config-command-query-v2',
    'workspace-plan-selection-result/v2': 'model-config-command-query-v2',
    'model-profile-revision/v1': 'model-profile-revision-v1',
    'model-secret-http-error/v2': 'model-planning-http-error-v2',
    'model-profile-http-error/v2': 'model-planning-http-error-v2',
    'workspace-planning-http-error/v2': 'model-planning-http-error-v2',
    'project-planning-query/v2': 'project-planning-command-query-v2',
    'project-planning-availability-result/v2': 'project-planning-command-query-v2',
    'project-planning-start-command/v2': 'project-planning-command-query-v2',
    'project-planning-start-result/v2': 'project-planning-command-query-v2',
    'project-planner-runtime-input/v2': 'project-planner-runtime-input-v2',
    'project-planner-model-output/v1': 'project-planner-model-output-v1',
  }
  const secretRef = /^secret:\/\/[A-Za-z0-9](?:[A-Za-z0-9._~-]{0,127})(?:\/[A-Za-z0-9](?:[A-Za-z0-9._~-]{0,127}))*$/u
  const secretId = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u
  const jsonMaxSafeInteger = 9007199254740991
  const wireWhitespaceCodepoints = Object.freeze([
    ...Array.from({ length: 5 }, (_, offset) => 0x0009 + offset),
    ...Array.from({ length: 5 }, (_, offset) => 0x001c + offset),
    0x0085,
    0x00a0,
    0x1680,
    ...Array.from({ length: 11 }, (_, offset) => 0x2000 + offset),
    0x2028,
    0x2029,
    0x202f,
    0x205f,
    0x3000,
    0xfeff,
  ])
  const wireWhitespaceCharacters = new Set(wireWhitespaceCodepoints.map((codepoint) => String.fromCodePoint(codepoint)))
  const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key)
  const fail = (message) => { throw new Error(`macro planning ${message}`) }
  const rejected = (action) => { try { action(); return false } catch (_) { return true } }

  const isWireWhitespaceCharacter = (value) => (
    typeof value === 'string' && [...value].length === 1 && wireWhitespaceCharacters.has(value)
  )
  const containsWireWhitespace = (value) => (
    typeof value === 'string' && [...value].some((character) => wireWhitespaceCharacters.has(character))
  )
  const hasNonWireWhitespaceCharacter = (value) => (
    typeof value === 'string' && [...value].some((character) => !wireWhitespaceCharacters.has(character))
  )
  const validateJsonSafeInteger = (value, minimum, label) => {
    if (!Number.isSafeInteger(value) || value < minimum || value > jsonMaxSafeInteger) fail(`${label} is outside the JSON safe integer domain`)
  }

  const validateSecretRef = (value) => {
    if (typeof value !== 'string' || !secretRef.test(value)) fail('api_key_ref must be an exact secret reference')
  }
  const validateEndpoint = (value) => {
    if (typeof value !== 'string' || containsWireWhitespace(value)) fail('provider endpoint is malformed')
    let parsed
    try { parsed = new URL(value) } catch (_) { fail('provider endpoint is malformed') }
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password || parsed.search || parsed.hash) fail('provider endpoint is not an allowed origin/path')
  }
  const validateModelName = (value) => {
    const characters = typeof value === 'string' ? [...value] : []
    if (characters.length === 0 || isWireWhitespaceCharacter(characters[0]) || isWireWhitespaceCharacter(characters.at(-1))) fail('provider model name has a wire-whitespace boundary')
  }
  const validateNonblankWireText = (value, label) => {
    if (!hasNonWireWhitespaceCharacter(value)) fail(`${label} is blank under the wire-whitespace policy`)
  }
  const validateModelOptionIntegers = (options) => {
    validateJsonSafeInteger(options.max_output_tokens, 1, 'provider max_output_tokens')
    if (options.max_output_tokens > 10000000) fail('provider max_output_tokens exceeds its field maximum')
    validateJsonSafeInteger(options.timeout_seconds, 1, 'provider timeout_seconds')
    if (options.timeout_seconds > 86400) fail('provider timeout_seconds exceeds its field maximum')
    validateJsonSafeInteger(options.max_retries, 0, 'provider max_retries')
    if (options.max_retries > 16) fail('provider max_retries exceeds its field maximum')
  }
  const validateProvider = (provider) => {
    validateEndpoint(provider.endpoint)
    validateModelName(provider.model_name)
    validateSecretRef(provider.api_key_ref)
    validateModelOptionIntegers(provider.options)
  }
  const profileHash = (value) => {
    validateJsonSafeInteger(value.revision_number, 1, 'profile revision_number')
    validateModelOptionIntegers(value.provider.options)
    return hashJcs('model-profile-revision/v1', {
      profile_id: value.profile_id,
      revision_id: value.revision_id,
      revision_number: value.revision_number,
      parent_revision_id: value.parent_revision_id,
      provider: structuredClone(value.provider),
      created_at: value.created_at,
    })
  }
  const runtimeInputHash = (value) => {
    validateJsonSafeInteger(value.writer_epoch, 1, 'runtime input writer_epoch')
    const unsigned = structuredClone(value)
    delete unsigned.input_hash
    return hashJcs('project-planner-runtime-input/v2', unsigned)
  }
  const validateProfile = (value) => {
    validateProvider(value.provider)
    if ((value.revision_number === 1) !== (value.parent_revision_id === null)) fail('profile append-only parent mismatch')
    if (profileHash(value) !== value.revision_hash) fail('profile revision hash mismatch')
  }
  const validatePlanPair = (value, idField, hashField) => {
    if ((value[idField] === null) !== (value[hashField] === null)) fail(`plan pair mismatch: ${idField}`)
  }
  const validateRuntimeInput = (value) => {
    const refs = [value.project_brief, ...Object.values(value.targets)]
    if (refs.some((ref) => ref.workspace_id !== value.workspace_id)) fail('runtime input crosses workspace')
    const targetIds = Object.values(value.targets).map((ref) => ref.document_id)
    if (new Set(targetIds).size !== targetIds.length || targetIds.includes(value.project_brief.document_id)) fail('runtime input targets are not independent')
    if (runtimeInputHash(value) !== value.input_hash) fail('runtime input hash mismatch')
  }
  const validateModelOutput = (value) => {
    for (const role of ['setting', 'bible', 'outline']) validateNonblankWireText(value[role], `model output ${role}`)
  }
  const parseMacro = (input) => {
    if (input === null || typeof input !== 'object' || Array.isArray(input)) fail('wire value must be an object')
    const contract = schemaContracts[input.schema]
    if (!contract) fail(`unsupported discriminator ${String(input.schema)}`)
    validateClosed(input, schemas[contract], input.schema)
    const value = structuredClone(input)
    if (value.schema === 'model-profile-revision/v1') validateProfile(value)
    else if (value.schema === 'model-secret-put-command/v2') {
      if (!secretId.test(value.secret_id)) fail('invalid secret id')
    } else if (value.schema === 'model-secret-put-result/v2') {
      validateSecretRef(value.api_key_ref)
      if (value.api_key_ref !== `secret://${value.secret_id}`) fail('secret result identity mismatch')
    } else if (value.schema === 'model-profile-revise-command/v2') validateProvider(value.provider)
    else if (value.schema === 'model-profile-revise-result/v2') {
      const revision = parseMacro(value.revision)
      if (revision.profile_id !== value.profile_id) fail('profile result identity mismatch')
    } else if (value.schema === 'workspace-plan-selection-command/v2') {
      validateJsonSafeInteger(value.expected_workspace_revision, 0, 'plan expected_workspace_revision')
      validatePlanPair(value, 'expected_current_plan_revision_id', 'expected_current_plan_revision_hash')
    } else if (value.schema === 'workspace-plan-selection-result/v2') {
      validateJsonSafeInteger(value.workspace_revision, 1, 'plan workspace_revision')
      validatePlanPair(value, 'previous_plan_revision_id', 'previous_plan_revision_hash')
    } else if (['model-secret-http-error/v2', 'model-profile-http-error/v2', 'workspace-planning-http-error/v2'].includes(value.schema)) validateNonblankWireText(value.message, 'HTTP error message')
    else if (value.schema === 'project-planning-availability-result/v2') {
      if (value.available !== (value.reason === 'ready')) fail('availability failed open')
    } else if (value.schema === 'project-planning-start-result/v2') validateJsonSafeInteger(value.writer_epoch, 1, 'planning start writer_epoch')
    else if (value.schema === 'project-planner-runtime-input/v2') validateRuntimeInput(value)
    else if (value.schema === 'project-planner-model-output/v1') validateModelOutput(value)
    return value
  }
  const validateSecretExchange = (commandInput, responseInput) => {
    const command = parseMacro(commandInput)
    if (command.schema !== 'model-secret-put-command/v2') fail('secret exchange command variant mismatch')
    const containsRaw = (value) => {
      if (typeof value === 'string') return value.includes(command.value)
      if (Array.isArray(value)) return value.some(containsRaw)
      if (value !== null && typeof value === 'object') return Object.entries(value).some(([key, child]) => key.includes(command.value) || containsRaw(child))
      return false
    }
    if (containsRaw(responseInput)) fail('secret response contains raw value')
    const response = parseMacro(responseInput)
    if (response.schema === 'model-secret-put-result/v2') {
      if (response.operation_key !== command.operation_key || response.secret_id !== command.secret_id) fail('secret result binding mismatch')
    } else if (response.schema === 'model-secret-http-error/v2') {
      if (response.secret_id !== command.secret_id || (response.operation_key !== null && response.operation_key !== command.operation_key)) fail('secret error binding mismatch')
    } else fail('secret exchange response variant mismatch')
    return [command, response]
  }
  const validateProfileExchange = (commandInput, resultInput) => {
    const command = parseMacro(commandInput)
    const result = parseMacro(resultInput)
    if (command.schema !== 'model-profile-revise-command/v2' || result.schema !== 'model-profile-revise-result/v2') fail('profile exchange variant mismatch')
    const revision = result.revision
    if (result.operation_key !== command.operation_key || result.profile_id !== command.profile_id || revision.profile_id !== command.profile_id || revision.parent_revision_id !== command.expected_parent_revision_id || !jsonEqual(revision.provider, command.provider)) fail('profile exchange binding mismatch')
    return [command, result]
  }
  const validatePlanSelection = (commandInput, resultInput, authority = {}) => {
    const command = parseMacro(commandInput)
    if (command.schema !== 'workspace-plan-selection-command/v2') fail('plan command variant mismatch')
    if (hasOwn(authority, 'workspace_revision') && command.expected_workspace_revision !== authority.workspace_revision) fail('stale workspace plan CAS')
    if (hasOwn(authority, 'plan_revision_id') && command.expected_current_plan_revision_id !== authority.plan_revision_id) fail('stale current plan id')
    if (hasOwn(authority, 'plan_revision_hash') && command.expected_current_plan_revision_hash !== authority.plan_revision_hash) fail('stale current plan hash')
    if (hasOwn(authority, 'generation_id') && command.expected_active_generation_id !== authority.generation_id) fail('active generation mismatch')
    if (resultInput === undefined) return [command, undefined]
    const result = parseMacro(resultInput)
    if (result.schema !== 'workspace-plan-selection-result/v2') fail('plan result variant mismatch')
    const bindings = [['operation_key', 'operation_key'], ['workspace_id', 'workspace_id'], ['expected_current_plan_revision_id', 'previous_plan_revision_id'], ['expected_current_plan_revision_hash', 'previous_plan_revision_hash'], ['selection_mode', 'selection_mode'], ['plan_revision_id', 'plan_revision_id'], ['plan_revision_hash', 'plan_revision_hash'], ['model_profile_revision_id', 'model_profile_revision_id'], ['model_profile_revision_hash', 'model_profile_revision_hash'], ['expected_active_generation_id', 'active_generation_id']]
    for (const [left, right] of bindings) if (command[left] !== result[right]) fail(`plan exchange binding mismatch: ${left}`)
    if (!result.idempotent && result.workspace_revision !== command.expected_workspace_revision + 1) fail('plan selection did not advance workspace once')
    if (result.idempotent && result.workspace_revision < command.expected_workspace_revision) fail('replayed plan selection regressed workspace')
    return [command, result]
  }
  const validatePlanningStart = (commandInput, briefAuthority) => {
    const command = parseMacro(commandInput)
    if (command.schema !== 'project-planning-start-command/v2') fail('planning start variant mismatch')
    if (briefAuthority !== undefined) {
      const expected = { document_id: command.project_brief_document_id, ...command.expected_project_brief }
      if (!jsonEqual(expected, briefAuthority)) fail('stale project brief CAS')
    }
    return command
  }
  const validatePlanningStartExchange = (commandInput, resultInput) => {
    const command = validatePlanningStart(commandInput)
    const result = parseMacro(resultInput)
    if (result.schema !== 'project-planning-start-result/v2' || result.operation_key !== command.operation_key || result.workspace_id !== command.workspace_id) fail('planning start exchange binding mismatch')
    return [command, result]
  }

  const goldenPath = join(MACRO_GOLDEN, 'positive.json')
  const fixturePath = join(CONTRACTS, 'examples', 'fixtures', 'macro-planning-host-positive.json')
  if (!bytes(goldenPath).equals(bytes(fixturePath))) fail('fixture and golden bytes differ')
  const positive = readJson(goldenPath)
  const fixtures = positive.fixtures
  const exchanges = positive.exchanges
  if (positive.schema !== 'macro-planning-host-positive/v1' || positive.fixture_count !== Object.keys(fixtures).length || Object.keys(fixtures).length !== 17 || positive.exchange_count !== exchanges.length || exchanges.length !== 5) fail('positive inventory drift')
  for (const value of Object.values(fixtures)) parseMacro(value)
  const profile = fixtures.model_profile_revision
  const runtimeInput = fixtures.project_planner_runtime_input
  if (profileHash(profile) !== profile.revision_hash || positive.expected.model_profile_revision_hash !== profile.revision_hash) fail('profile hash golden drift')
  if (runtimeInputHash(runtimeInput) !== runtimeInput.input_hash || positive.expected.planner_runtime_input_hash !== runtimeInput.input_hash) fail('runtime input hash golden drift')

  if (jsonMaxSafeInteger !== Number.MAX_SAFE_INTEGER) fail('JSON safe integer maximum drift')
  const maximumProfile = structuredClone(profile)
  maximumProfile.revision_number = jsonMaxSafeInteger
  maximumProfile.parent_revision_id = 'revision-model-profile-parent-max-safe'
  maximumProfile.revision_hash = profileHash(maximumProfile)
  parseMacro(maximumProfile)
  const unsafeProfile = structuredClone(maximumProfile)
  unsafeProfile.revision_number = jsonMaxSafeInteger + 1
  if (!rejected(() => profileHash(unsafeProfile))) fail('unsafe profile revision_number reached hashing')

  const maximumPlanCommand = structuredClone(fixtures.workspace_plan_selection_command)
  maximumPlanCommand.expected_workspace_revision = jsonMaxSafeInteger
  parseMacro(maximumPlanCommand)
  const monotonicPlanCommand = structuredClone(maximumPlanCommand)
  monotonicPlanCommand.expected_workspace_revision = jsonMaxSafeInteger - 1
  const maximumPlanResult = structuredClone(fixtures.workspace_plan_selection_result)
  maximumPlanResult.workspace_revision = jsonMaxSafeInteger
  validatePlanSelection(monotonicPlanCommand, maximumPlanResult)

  const maximumStartResult = structuredClone(fixtures.project_planning_start_result)
  maximumStartResult.writer_epoch = jsonMaxSafeInteger
  parseMacro(maximumStartResult)

  const maximumRuntimeInput = structuredClone(runtimeInput)
  maximumRuntimeInput.writer_epoch = jsonMaxSafeInteger
  maximumRuntimeInput.input_hash = runtimeInputHash(maximumRuntimeInput)
  parseMacro(maximumRuntimeInput)
  const unsafeRuntimeInput = structuredClone(maximumRuntimeInput)
  unsafeRuntimeInput.writer_epoch = jsonMaxSafeInteger + 1
  if (!rejected(() => runtimeInputHash(unsafeRuntimeInput))) fail('unsafe runtime writer_epoch reached hashing')

  if (wireWhitespaceCodepoints.length !== 30 || new Set(wireWhitespaceCodepoints).size !== 30) fail('wire-whitespace set drift')
  const internalSpaceModelName = structuredClone(fixtures.model_profile_revise_command)
  internalSpaceModelName.provider.model_name = 'planner model 1'
  parseMacro(internalSpaceModelName)
  const outputRoles = ['setting', 'bible', 'outline']
  for (const [index, codepoint] of wireWhitespaceCodepoints.entries()) {
    const character = String.fromCodePoint(codepoint)
    if (!isWireWhitespaceCharacter(character) || !containsWireWhitespace(`left${character}right`) || hasNonWireWhitespaceCharacter(character) || !hasNonWireWhitespaceCharacter(`${character}content`)) fail(`wire-whitespace helper drift at U+${codepoint.toString(16).toUpperCase().padStart(4, '0')}`)

    const endpoint = structuredClone(fixtures.model_profile_revise_command)
    endpoint.provider.endpoint = `https://models.example.test/v1${character}suffix`
    if (!rejected(() => parseMacro(endpoint))) fail(`wire-whitespace endpoint accepted U+${codepoint.toString(16)}`)

    for (const modelName of [`${character}planner-model-1`, `planner-model-1${character}`]) {
      const model = structuredClone(fixtures.model_profile_revise_command)
      model.provider.model_name = modelName
      if (!rejected(() => parseMacro(model))) fail(`wire-whitespace model-name boundary accepted U+${codepoint.toString(16)}`)
    }

    const error = structuredClone(fixtures.model_profile_error)
    error.message = character
    if (!rejected(() => parseMacro(error))) fail(`wire-whitespace error message accepted U+${codepoint.toString(16)}`)

    const output = structuredClone(fixtures.project_planner_model_output)
    output[outputRoles[index % outputRoles.length]] = character
    if (!rejected(() => parseMacro(output))) fail(`wire-whitespace model output accepted U+${codepoint.toString(16)}`)
  }

  const macroRouteIds = ['model-secret.put', 'model-profile.revise', 'workspace-plan.select', 'project-planning.get', 'project-planning.start']
  const matrix = readJson(join(SCHEMAS, 'core-api-method-matrix.v2.json'))
  if (!jsonEqual(matrix.macro_planning_routes, macroRouteIds)) fail('route order drift')
  const routes = Object.fromEntries(matrix.routes.filter((route) => macroRouteIds.includes(route.route_id)).map((route) => [route.route_id, route]))
  const expectedRoutes = {
    'model-secret.put': { method: 'PUT', path_template: '/api/v2/core/secrets/{secret_id}', path_identity: ['secret_id'], request_schema: 'model-secret-put-command/v2', result_schema: 'model-secret-put-result/v2', error_schema: 'model-secret-http-error/v2', success_statuses: [200, 201], failure_statuses: [{ status: 400, error_codes: ['malformed_request', 'secret_value_rejected'] }, { status: 404, error_codes: ['unknown_reference'] }, { status: 409, error_codes: ['duplicate_operation'] }], read_only: false, operation_key_required: true, cursor_domain: null, owner: 'host' },
    'model-profile.revise': { method: 'POST', path_template: '/api/v2/core/model-profiles/{profile_id}/revisions', path_identity: ['profile_id'], request_schema: 'model-profile-revise-command/v2', result_schema: 'model-profile-revise-result/v2', error_schema: 'model-profile-http-error/v2', success_statuses: [201], failure_statuses: [{ status: 400, error_codes: ['malformed_request', 'invalid_secret_reference'] }, { status: 404, error_codes: ['unknown_reference'] }, { status: 409, error_codes: ['stale_cas', 'duplicate_operation'] }], read_only: false, operation_key_required: true, cursor_domain: null, owner: 'host' },
    'workspace-plan.select': { method: 'POST', path_template: '/api/v2/core/workspaces/{workspace_id}/plans:select', path_identity: ['workspace_id'], request_schema: 'workspace-plan-selection-command/v2', result_schema: 'workspace-plan-selection-result/v2', error_schema: 'workspace-planning-http-error/v2', success_statuses: [200], failure_statuses: [{ status: 400, error_codes: ['malformed_request'] }, { status: 404, error_codes: ['unknown_reference'] }, { status: 409, error_codes: ['stale_cas', 'generation_conflict', 'duplicate_operation'] }], read_only: false, operation_key_required: true, cursor_domain: null, owner: 'host' },
    'project-planning.get': { method: 'GET', path_template: '/api/v2/core/workspaces/{workspace_id}/project-planning', path_identity: ['workspace_id'], request_schema: 'project-planning-query/v2', result_schema: 'project-planning-availability-result/v2', error_schema: 'workspace-planning-http-error/v2', success_statuses: [200], failure_statuses: [{ status: 400, error_codes: ['malformed_request'] }, { status: 404, error_codes: ['unknown_reference', 'cross_workspace'] }], read_only: true, operation_key_required: false, cursor_domain: null, owner: 'host' },
    'project-planning.start': { method: 'POST', path_template: '/api/v2/core/workspaces/{workspace_id}/project-planning', path_identity: ['workspace_id'], request_schema: 'project-planning-start-command/v2', result_schema: 'project-planning-start-result/v2', error_schema: 'workspace-planning-http-error/v2', success_statuses: [201], failure_statuses: [{ status: 400, error_codes: ['malformed_request', 'planning_unavailable'] }, { status: 404, error_codes: ['unknown_reference', 'cross_workspace'] }, { status: 409, error_codes: ['stale_cas', 'generation_conflict', 'duplicate_operation', 'planning_unavailable'] }], read_only: false, operation_key_required: true, cursor_domain: null, owner: 'host' },
  }
  if (!jsonEqual(Object.keys(routes).sort(), Object.keys(expectedRoutes).sort())) fail('route set drift')
  for (const routeId of macroRouteIds) {
    const observed = { ...routes[routeId] }
    delete observed.route_id
    if (!jsonEqual(observed, expectedRoutes[routeId])) fail(`route metadata drift: ${routeId}`)
  }

  const parseHttpExchange = (exchange) => {
    const route = routes[exchange.route_id]
    if (!route) fail(`unknown HTTP route ${exchange.route_id}`)
    const pathParams = exchange.path_params
    if (pathParams === null || typeof pathParams !== 'object' || Array.isArray(pathParams)) fail('trusted path params required')
    if (!jsonEqual(Object.keys(pathParams).sort(), [...route.path_identity].sort())) fail('trusted path params are not exact')
    for (const name of route.path_identity) if (typeof pathParams[name] !== 'string' || !pathParams[name] || exchange.request[name] !== pathParams[name]) fail(`request trusted path mismatch: ${name}`)
    if (exchange.request.schema !== route.request_schema) fail('HTTP request schema mismatch')
    const request = parseMacro(exchange.request)
    const success = route.success_statuses.includes(exchange.status)
    let response
    if (success) {
      if (exchange.response.schema !== route.result_schema) fail('HTTP result schema mismatch')
      response = parseMacro(exchange.response)
    } else {
      const failure = route.failure_statuses.find((item) => item.status === exchange.status)
      if (!failure || exchange.response.schema !== route.error_schema || !failure.error_codes.includes(exchange.response.error_code)) fail('HTTP error status/code mismatch')
      response = parseMacro(exchange.response)
    }
    for (const name of route.path_identity) if (response[name] !== pathParams[name]) fail(`response trusted path mismatch: ${name}`)
    for (const name of ['workspace_id', 'profile_id', 'secret_id']) if (hasOwn(request, name) && hasOwn(response, name) && request[name] !== response[name]) fail(`HTTP response identity mismatch: ${name}`)
    if (route.operation_key_required) {
      if (success && response.operation_key !== request.operation_key) fail('HTTP success operation key mismatch')
      if (!success && response.operation_key !== null && response.operation_key !== request.operation_key) fail('HTTP error operation key mismatch')
    }
    if (exchange.route_id === 'model-secret.put') validateSecretExchange(request, response)
    else if (exchange.route_id === 'model-profile.revise' && success) validateProfileExchange(request, response)
    else if (exchange.route_id === 'workspace-plan.select' && success) validatePlanSelection(request, response)
    else if (exchange.route_id === 'project-planning.start' && success) validatePlanningStartExchange(request, response)
    return [request, response]
  }
  if (!jsonEqual(exchanges.map((item) => item.route_id), macroRouteIds)) fail('positive HTTP route coverage drift')
  for (const exchange of exchanges) parseHttpExchange(exchange)
  parseHttpExchange({ route_id: 'model-secret.put', path_params: { secret_id: 'provider-main' }, request: fixtures.model_secret_put_command, status: 400, response: fixtures.model_secret_error })
  parseHttpExchange({ route_id: 'model-profile.revise', path_params: { profile_id: 'model-profile-planner' }, request: fixtures.model_profile_revise_command, status: 409, response: fixtures.model_profile_error })
  parseHttpExchange({ route_id: 'project-planning.start', path_params: { workspace_id: 'workspace-1' }, request: fixtures.project_planning_start_command, status: 409, response: fixtures.workspace_planning_error })

  const corpusFixtures = { ...fixtures }
  for (const exchange of exchanges) corpusFixtures[`http.${exchange.route_id}`] = exchange
  corpusFixtures.secret_success_exchange = { command: fixtures.model_secret_put_command, response: fixtures.model_secret_put_result }
  corpusFixtures.secret_error_exchange = { command: fixtures.model_secret_put_command, response: fixtures.model_secret_error }
  corpusFixtures.plan_selection_exchange = { command: fixtures.workspace_plan_selection_command, result: fixtures.workspace_plan_selection_result }
  corpusFixtures.planning_start_exchange = { command: fixtures.project_planning_start_command, result: fixtures.project_planning_start_result }
  corpusFixtures['http.model-profile.revise.error'] = { route_id: 'model-profile.revise', path_params: { profile_id: 'model-profile-planner' }, request: fixtures.model_profile_revise_command, status: 409, response: fixtures.model_profile_error }
  const mutate = (input, mutation) => {
    const output = structuredClone(input)
    let target = output
    for (const token of mutation.path.slice(0, -1)) target = target[token]
    const key = mutation.path.at(-1)
    if (mutation.op === 'set') target[key] = structuredClone(mutation.value)
    else if (mutation.op === 'delete') Array.isArray(target) ? target.splice(Number(key), 1) : delete target[key]
    else fail(`unknown mutation ${mutation.op}`)
    return output
  }
  const integerVectorPath = join(MACRO_CORPUS, 'integer-representations.json')
  const integerVectorBytes = bytes(integerVectorPath)
  const integerVectors = readJson(integerVectorPath)
  const expectedIntegerFields = [
    { field_id: 'model-profile-revision-number', fixture: 'model_profile_revision', path: ['revision_number'], minimum: 1, maximum: jsonMaxSafeInteger },
    { field_id: 'plan-expected-workspace-revision', fixture: 'workspace_plan_selection_command', path: ['expected_workspace_revision'], minimum: 0, maximum: jsonMaxSafeInteger },
    { field_id: 'plan-result-workspace-revision', fixture: 'workspace_plan_selection_result', path: ['workspace_revision'], minimum: 1, maximum: jsonMaxSafeInteger },
    { field_id: 'planning-start-writer-epoch', fixture: 'project_planning_start_result', path: ['writer_epoch'], minimum: 1, maximum: jsonMaxSafeInteger },
    { field_id: 'planner-runtime-writer-epoch', fixture: 'project_planner_runtime_input', path: ['writer_epoch'], minimum: 1, maximum: jsonMaxSafeInteger },
    { field_id: 'model-option-max-output-tokens', fixture: 'model_profile_revision', path: ['provider', 'options', 'max_output_tokens'], minimum: 1, maximum: 10000000 },
    { field_id: 'model-option-timeout-seconds', fixture: 'model_profile_revision', path: ['provider', 'options', 'timeout_seconds'], minimum: 1, maximum: 86400 },
    { field_id: 'model-option-max-retries', fixture: 'model_profile_revision', path: ['provider', 'options', 'max_retries'], minimum: 0, maximum: 16 },
  ]
  const vectors = integerVectors.vectors
  if (
    integerVectors.schema !== 'macro-planning-integer-representations/v1'
    || integerVectors.json_schema_dialect !== 'https://json-schema.org/draft/2020-12/schema'
    || integerVectors.semantic_authority !== 'mathematical-json-integer'
    || !jsonEqual(integerVectors.fields, expectedIntegerFields)
    || integerVectors.field_count !== expectedIntegerFields.length
    || !Array.isArray(vectors)
    || integerVectors.vector_count !== vectors.length
    || vectors.length !== 34
    || integerVectors.accepted_count !== 19
    || integerVectors.rejected_count !== 15
  ) fail('raw integer vector inventory drift')

  const allowedVectorTargets = {
    'model-profile-revision-number': new Set([
      'model_profile_revision:revision_number',
      'model_profile_revise_result:revision.revision_number',
    ]),
    'plan-expected-workspace-revision': new Set(['workspace_plan_selection_command:expected_workspace_revision']),
    'plan-result-workspace-revision': new Set(['workspace_plan_selection_result:workspace_revision']),
    'planning-start-writer-epoch': new Set(['project_planning_start_result:writer_epoch']),
    'planner-runtime-writer-epoch': new Set(['project_planner_runtime_input:writer_epoch']),
    'model-option-max-output-tokens': new Set([
      'model_profile_revision:provider.options.max_output_tokens',
      'model_profile_revise_command:provider.options.max_output_tokens',
    ]),
    'model-option-timeout-seconds': new Set([
      'model_profile_revision:provider.options.timeout_seconds',
      'model_profile_revise_command:provider.options.timeout_seconds',
    ]),
    'model-option-max-retries': new Set([
      'model_profile_revision:provider.options.max_retries',
      'model_profile_revise_command:provider.options.max_retries',
    ]),
  }
  const valueAtPath = (value, path) => path.reduce((current, token) => current[token], value)
  const integerVectorResults = []
  const integerVectorCaseIds = new Set()
  const equivalenceHashes = new Map()
  const rawTokenGrammar = /^(?:true|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)$/u
  for (const vector of vectors) {
    if (integerVectorCaseIds.has(vector.case_id)) fail(`duplicate raw integer vector ${vector.case_id}`)
    integerVectorCaseIds.add(vector.case_id)
    const target = `${vector.fixture}:${vector.path.join('.')}`
    if (!allowedVectorTargets[vector.field_id]?.has(target)) fail(`raw integer vector target drift: ${vector.case_id}`)
    if (typeof vector.raw_token !== 'string' || !rawTokenGrammar.test(vector.raw_token)) fail(`raw integer token grammar drift: ${vector.case_id}`)
    const tokenValue = JSON.parse(vector.raw_token)
    const value = mutate(fixtures[vector.fixture], { op: 'set', path: vector.path, value: tokenValue })
    let accepted = false
    let normalized = null
    let canonicalHash = null
    let callerMutated = false
    try {
      if (vector.fixture === 'model_profile_revision') {
        const hashInput = structuredClone(value)
        canonicalHash = profileHash(value)
        callerMutated ||= !jsonEqual(value, hashInput)
        value.revision_hash = canonicalHash
      } else if (vector.fixture === 'model_profile_revise_result') {
        const hashInput = structuredClone(value.revision)
        canonicalHash = profileHash(value.revision)
        callerMutated ||= !jsonEqual(value.revision, hashInput)
        value.revision.revision_hash = canonicalHash
      } else if (vector.fixture === 'project_planner_runtime_input') {
        const hashInput = structuredClone(value)
        canonicalHash = runtimeInputHash(value)
        callerMutated ||= !jsonEqual(value, hashInput)
        value.input_hash = canonicalHash
      }
      const parseInput = structuredClone(value)
      const parsed = parseMacro(value)
      callerMutated ||= !jsonEqual(value, parseInput)
      normalized = valueAtPath(parsed, vector.path)
      accepted = true
    } catch (_) {
      // The vector outcome is checked against its generated expectation below.
    }
    if (callerMutated) fail(`raw integer helper mutated caller input: ${vector.case_id}`)
    const expectedAccept = vector.expected === 'accept'
    if (accepted !== expectedAccept) fail(`raw integer vector outcome drift: ${vector.case_id}`)
    if (accepted && (!Number.isSafeInteger(normalized) || normalized !== vector.normalized)) fail(`raw integer normalization drift: ${vector.case_id}`)
    if (!accepted && Object.prototype.hasOwnProperty.call(vector, 'normalized')) fail(`rejected raw integer vector declares normalized value: ${vector.case_id}`)
    const groupId = vector.hash_equivalence_group
    if (groupId !== undefined) {
      if (!accepted || canonicalHash === null) fail(`raw integer equivalence vector did not hash: ${vector.case_id}`)
      if (!equivalenceHashes.has(groupId)) equivalenceHashes.set(groupId, new Set())
      equivalenceHashes.get(groupId).add(canonicalHash)
    }
    integerVectorResults.push({
      case_id: vector.case_id,
      field_id: vector.field_id,
      raw_token: vector.raw_token,
      accepted,
      normalized,
      canonical_hash: canonicalHash,
    })
  }
  if (!jsonEqual([...equivalenceHashes.keys()].sort(), integerVectors.hash_equivalence_groups) || [...equivalenceHashes.values()].some((hashes) => hashes.size !== 1)) fail('raw integer hash equivalence drift')
  const integerVectorSourceSha256 = sha256(integerVectorBytes)
  const integerVectorResultDigest = sha256(Buffer.from(JSON.stringify(integerVectorResults), 'utf8'))
  const group = readJson(join(MACRO_CORPUS, 'negative.json'))
  const corpusManifest = readJson(join(MACRO_CORPUS, 'manifest.json'))
  const cases = group.negative
  if (corpusManifest.schema !== 'macro-planning-host-corpus-manifest/v1' || !jsonEqual(corpusManifest.group_ids, [group.group_id]) || corpusManifest.group_count !== 1 || corpusManifest.negative_case_count !== cases.length || cases.length !== 46 || !jsonEqual(corpusManifest.integer_representations, { path: 'contracts/corpus/macro-planning-host-v1/integer-representations.json', sha256: integerVectorSourceSha256, field_count: 8, vector_count: 34, accepted_count: 19, rejected_count: 15 })) fail('corpus inventory drift')
  const seen = new Set()
  const briefAuthority = { document_id: 'document-project-brief', revision_id: 'revision-project-brief-3', content_hash: 'c'.repeat(64) }
  for (const testCase of cases) {
    if (seen.has(testCase.case_id)) fail(`duplicate negative case ${testCase.case_id}`)
    seen.add(testCase.case_id)
    const value = mutate(corpusFixtures[testCase.fixture], testCase.mutation)
    let action
    if (testCase.kind === 'parse') action = () => parseMacro(value)
    else if (testCase.kind === 'secret_exchange') action = () => validateSecretExchange(value.command, value.response)
    else if (testCase.kind === 'http_exchange') action = () => parseHttpExchange(value)
    else if (testCase.kind === 'project_brief_cas') action = () => validatePlanningStart(value, briefAuthority)
    else if (testCase.kind === 'plan_cas') action = () => validatePlanSelection(value, undefined, { workspace_revision: 7, plan_revision_id: null, plan_revision_hash: null, generation_id: 'generation-planning-1' })
    else if (testCase.kind === 'plan_exchange') action = () => validatePlanSelection(value.command, value.result)
    else if (testCase.kind === 'model_profile' || testCase.kind === 'runtime_input' || testCase.kind === 'model_output' || testCase.kind === 'availability') action = () => parseMacro(value)
    else if (testCase.kind === 'planning_start_exchange') action = () => validatePlanningStartExchange(value.command, value.result)
    else fail(`unknown corpus kind ${testCase.kind}`)
    if (!rejected(action)) fail(`corpus false-accepted ${testCase.case_id}`)
  }
  const integerCaseIds = cases.filter((item) => item.expected === 'json_safe_integer').map((item) => item.case_id).sort()
  const expectedIntegerCaseIds = [
    'macro-unsafe-profile-revision-number',
    'macro-unsafe-plan-expected-workspace-revision',
    'macro-unsafe-plan-workspace-revision-result',
    'macro-unsafe-planning-start-writer-epoch',
    'macro-unsafe-runtime-input-writer-epoch',
  ].sort()
  if (!jsonEqual(integerCaseIds, expectedIntegerCaseIds)) fail('shared JSON-safe-integer corpus coverage drift')
  const whitespaceCaseIds = cases.filter((item) => ['wire_whitespace', 'wire_nonblank'].includes(item.expected)).map((item) => item.case_id).sort()
  const expectedWhitespaceCaseIds = [
    ...['endpoint', 'model-name', 'error-message'].flatMap((surface) => ['u0085', 'ufeff', 'u00a0'].map((suffix) => `macro-wire-whitespace-${surface}-${suffix}`)),
    'macro-wire-whitespace-output-setting-u0085',
    'macro-wire-whitespace-output-bible-ufeff',
    'macro-wire-whitespace-output-outline-u00a0',
  ].sort()
  if (!jsonEqual(whitespaceCaseIds, expectedWhitespaceCaseIds)) fail('shared wire-whitespace corpus coverage drift')

  const router = readJson(join(CONTRACTS, 'corpus', 'manifest-v2.json'))
  const additiveRoots = ['m4-m5-public-surface-v2', 'macro-planning-host-v1', 'model-provider-rpc-v2', 'prompt-skill-rpc-v2']
  const actualCorpusPaths = additiveRoots.flatMap((directory) => readdirSync(join(CONTRACTS, 'corpus', directory)).filter((name) => name.endsWith('.json')).map((name) => `${directory}/${name}`)).sort()
  const routerPaths = router.files.map((record) => record.path)
  if (router.schema !== 'contract-corpus/v2' || router.router_self_excluded !== true || router.file_count_excluding_router !== router.files.length || new Set(routerPaths).size !== routerPaths.length || !jsonEqual([...routerPaths].sort(), actualCorpusPaths)) fail('versioned corpus router completeness drift')
  for (const record of router.files) {
    const content = bytes(join(CONTRACTS, 'corpus', record.path))
    if (content.length !== record.bytes || sha256(content) !== record.sha256) fail(`versioned corpus hash drift: ${record.path}`)
  }
  if (!jsonEqual(router.frozen_v1, { manifest: 'manifest.json', sha256: 'bcaafab242980366546c34c824256a0396ed370345d70f3e5b1582090a68ce77' })) fail('frozen corpus identity drift')
  const caseDigest = sha256(Buffer.from(JSON.stringify([...seen].sort()), 'utf8'))
  return { routes: Object.keys(routes).length, schemas: Object.keys(schemas).length, fixtures: Object.keys(fixtures).length, http_exchanges: exchanges.length, negative_cases: seen.size, negative_case_digest: caseDigest, safe_integer_boundary_fields: 5, canonical_integer_fields: 8, integer_vector_count: integerVectorResults.length, integer_vector_accepted: integerVectorResults.filter((result) => result.accepted).length, integer_vector_rejected: integerVectorResults.filter((result) => !result.accepted).length, integer_vector_source_sha256: integerVectorSourceSha256, integer_vector_result_digest: integerVectorResultDigest, wire_whitespace_codepoints: wireWhitespaceCodepoints.length, representative_whitespace_cases: whitespaceCaseIds.length, corpus_files: router.files.length - 2, model_profile_revision_hash: profile.revision_hash, planner_runtime_input_hash: runtimeInput.input_hash }
}

async function verifyPromptSkill() {
  const matrix = readJson(join(SCHEMAS, 'rpc-method-matrix.v2.json'))
  if (matrix.schema !== 'rpc-method-matrix/v2' || matrix.authority !== 'core' || matrix.plugin_authority_allowed !== false) throw new Error('Prompt-Skill matrix authority drift')
  if (matrix.protocol?.jsonrpc !== '2.0' || matrix.protocol?.version !== '1' || matrix.envelope?.exactly_one !== true) throw new Error('Prompt-Skill matrix protocol drift')
  if (JSON.stringify(matrix.envelope?.members) !== JSON.stringify(['request', 'success', 'error'])) throw new Error('Prompt-Skill envelope member drift')
  if (!Array.isArray(matrix.methods) || matrix.methods.length !== 1) throw new Error('Prompt-Skill method inventory drift')
  const method = matrix.methods[0]
  for (const [key, expected] of Object.entries({ method: 'prompt.skill.execute/v2', authority: 'core', consumer: 'P2', direction: 'host-to-worker', endpoint: 'worker', protocol_version: '1', framing: 'content-length-crlf', meta_profile: 'attempt', lease_fenced: true, operation_key_required: true, request_schema: 'prompt-skill-execute-request/v2', result_contract: 'artifact-bundle/v1', result_schema: 'prompt-skill-execute-result/v2', success_schema: 'rpc-method-success/v2', error_schema: 'rpc-error-v1', error_code_registry: 'rpc-method-matrix/v1#error_codes' })) if (method[key] !== expected) throw new Error(`Prompt-Skill method field drift: ${key}`)
  if (canonicalJson(method.secrets) !== canonicalJson({ location: 'params', mode: 'one-shot', response_forbidden: true })) throw new Error('Prompt-Skill secret policy drift')
  if (method.job_mapping?.start?.method !== 'job.start' || method.job_mapping?.resume?.method !== 'job.resume') throw new Error('Prompt-Skill job mapping drift')

  const rpc = await import(pathToFileURL(join(ROOT, 'frontend', 'src', 'contracts', 'prompt-skill-rpc-v2.ts')).href)
  const golden = readJson(join(PROMPT_GOLDEN, 'execute.json'))
  const expected = readJson(join(PROMPT_GOLDEN, 'expected.json'))
  for (const [name, digest] of Object.entries(expected.fixture_files)) if (sha256(bytes(join(PROMPT_GOLDEN, name))) !== digest) throw new Error(`Prompt-Skill golden hash drift ${name}`)
  const request = rpc.parsePromptSkillExecuteRequestV2(golden.request)
  const result = rpc.parsePromptSkillExecuteResultV2(golden.result)
  if (rpc.validateRpcMethodSuccessV2(golden.success, golden.request).result?.operation_key !== result.operation_key) throw new Error('Prompt-Skill success binding drift')
  if (rpc.parsePromptSkillRpcEnvelopeV2(golden.request).params.operation_key !== request.params.operation_key) throw new Error('Prompt-Skill request envelope drift')
  if (rpc.parsePromptSkillRpcEnvelopeV2(golden.success, golden.request).result.operation_key !== result.operation_key) throw new Error('Prompt-Skill success envelope drift')
  if (rpc.parsePromptSkillRpcEnvelopeV2(golden.error, golden.request).error.code !== golden.error.error.code) throw new Error('Prompt-Skill error envelope drift')
  for (const status of ['failed', 'cancelled', 'uncertain']) if (rpc.parsePromptSkillExecuteResultV2(golden[status]).status !== status) throw new Error(`Prompt-Skill terminal fixture drift: ${status}`)
  const operationDigest = await rpc.promptSkillOperationKeyV2(golden.request)
  if (operationDigest !== golden.operation_digest || operationDigest !== expected.operation_digest) throw new Error('Prompt-Skill operation digest drift')
  const operationBytesHex = Buffer.from(canonicalJson(golden.request.params), 'utf8').toString('hex')
  if (operationBytesHex !== golden.operation_canonical_bytes_hex || operationBytesHex !== expected.operation_canonical_bytes_hex) throw new Error('Prompt-Skill canonical operation bytes drift')
  const permuted = structuredClone(golden.request)
  permuted.params.skill_releases.reverse()
  const permutedBytesHex = Buffer.from(canonicalJson(permuted.params), 'utf8').toString('hex')
  const permutedDigest = await rpc.promptSkillOperationKeyV2(permuted)
  if (permutedBytesHex === operationBytesHex || permutedDigest === operationDigest) throw new Error('Prompt-Skill Skill array permutation did not change canonical identity')

  const authority = (value) => Object.fromEntries(['workspace_id', 'generation_id', 'plugin_id', 'plugin_release_id', 'job_id', 'step_id', 'attempt_id', 'lease_epoch', 'operation_key'].map((field) => [field, value.params[field]]))
  const ledger = new rpc.PromptSkillOperationLedgerV2()
  const first = ledger.record(golden.request, golden.result, authority(golden.request))
  if (first.replayed || first.result.operation_key !== result.operation_key) throw new Error('Prompt-Skill ledger record drift')
  if (ledger.replay(golden.request, authority(golden.request)).operation_key !== result.operation_key) throw new Error('Prompt-Skill ledger replay drift')
  const groups = readdirSync(PROMPT_CORPUS).filter((name) => name.endsWith('.json') && name !== 'manifest.json').map((name) => readJson(join(PROMPT_CORPUS, name)))
  const manifest = readJson(join(PROMPT_CORPUS, 'manifest.json'))
  const negativeCases = groups.reduce((total, group) => total + group.negative.length, 0)
  if (groups.length !== 1 || manifest.group_count !== 1 || manifest.negative_case_count !== negativeCases || negativeCases <= 25) throw new Error('Prompt-Skill corpus inventory drift')
  const fixtures = { 'execute.request': golden.request, 'execute.result': golden.result, 'execute.success': golden.success, 'execute.failed': golden.failed, 'execute.cancelled': golden.cancelled, 'execute.uncertain': golden.uncertain, 'execute.error': golden.error, 'execute.envelope': golden.request }
  const setPath = (value, path, replacement) => { let target = value; for (const part of path.slice(0, -1)) target = target[part]; target[path.at(-1)] = structuredClone(replacement); return value }
  const rejected = async (action) => { try { await action(); return false } catch (_) { return true } }
  const observed = new Set()
  for (const group of groups) {
    if (group.schema !== 'prompt-skill-rpc-corpus/v2') throw new Error('Prompt-Skill corpus schema drift')
    for (const fixtureId of group.positive) {
      if (fixtureId === 'execute.request') rpc.parsePromptSkillExecuteRequestV2(fixtures[fixtureId])
      else if (['execute.result', 'execute.failed', 'execute.cancelled', 'execute.uncertain'].includes(fixtureId)) rpc.parsePromptSkillExecuteResultV2(fixtures[fixtureId])
      else if (fixtureId === 'execute.success') rpc.validateRpcMethodSuccessV2(fixtures[fixtureId], golden.request)
      else if (fixtureId === 'execute.error') rpc.parsePromptSkillErrorV2(fixtures[fixtureId])
      else throw new Error(`unknown Prompt-Skill positive fixture ${fixtureId}`)
    }
    for (const item of group.negative) {
      if (observed.has(item.case_id)) throw new Error(`duplicate Prompt-Skill case ${item.case_id}`)
      observed.add(item.case_id)
      let action
      if (item.fixture === 'execute.ledger') {
        action = () => {
          if (item.mutation.op === 'no-authority') return new rpc.PromptSkillOperationLedgerV2().replay(golden.request)
          if (item.mutation.op === 'stale-replay') { const staleLedger = new rpc.PromptSkillOperationLedgerV2(); const auth = authority(golden.request); staleLedger.record(golden.request, golden.result, auth); return staleLedger.replay(golden.request, { ...auth, attempt_id: 'attempt-other' }) }
          if (item.mutation.op === 'future-poison' || item.mutation.op === 'future-epoch-rejected-before-mutation') { const futureRequest = setPath(setPath(structuredClone(golden.request), ['params', 'lease_epoch'], 99), ['meta', 'lease_epoch'], 99); const futureResult = setPath(structuredClone(golden.result), ['lease_epoch'], 99); const futureLedger = new rpc.PromptSkillOperationLedgerV2(); return futureLedger.record(futureRequest, futureResult, authority(golden.request)) }
          if (item.mutation.op === 'authority-missing') { const context = authority(golden.request); delete context[item.mutation.field]; return new rpc.PromptSkillOperationLedgerV2().record(golden.request, golden.result, context) }
          if (item.mutation.op === 'authority-additional') { const context = authority(golden.request); context[item.mutation.field] = structuredClone(item.mutation.value); return new rpc.PromptSkillOperationLedgerV2().record(golden.request, golden.result, context) }
          if (item.mutation.op === 'authority-partial') return new rpc.PromptSkillOperationLedgerV2().record(golden.request, golden.result, { lease_epoch: golden.request.params.lease_epoch })
          throw new Error('unknown Prompt-Skill ledger operation')
        }
      } else {
        const value = setPath(structuredClone(fixtures[item.fixture]), item.mutation.path, item.mutation.value)
        if (item.fixture === 'execute.request') action = () => rpc.parsePromptSkillExecuteRequestV2(value)
        else if (item.fixture === 'execute.result') action = () => rpc.validatePromptSkillExecuteV2(golden.request, value)
        else if (['execute.failed', 'execute.cancelled', 'execute.uncertain'].includes(item.fixture)) action = () => rpc.parsePromptSkillExecuteResultV2(value)
        else if (item.fixture === 'execute.success') action = () => rpc.validatePromptSkillExecuteV2(golden.request, value)
        else if (item.fixture === 'execute.error') action = () => rpc.parsePromptSkillErrorV2(value, golden.request)
        else if (item.fixture === 'execute.envelope') action = () => rpc.parsePromptSkillRpcEnvelopeV2(value)
        else throw new Error(`unknown Prompt-Skill fixture ${item.fixture}`)
      }
      if (!(await rejected(action))) throw new Error(`Prompt-Skill corpus false-accepted: ${item.case_id}`)
    }
  }
  if (observed.size !== negativeCases) throw new Error('Prompt-Skill corpus execution count drift')
  const authorityContext = authority(golden.request)
  for (const field of Object.keys(authorityContext)) {
    const ledgerProbe = new rpc.PromptSkillOperationLedgerV2()
    const missing = { ...authorityContext }
    delete missing[field]
    if (!(await rejected(() => ledgerProbe.record(golden.request, golden.result, missing)))) throw new Error(`missing authority accepted: ${field}`)
    if (ledgerProbe.record(golden.request, golden.result, authorityContext).replayed) throw new Error(`missing authority mutated ledger: ${field}`)
  }
  const additionalProbe = new rpc.PromptSkillOperationLedgerV2()
  if (!(await rejected(() => additionalProbe.record(golden.request, golden.result, { ...authorityContext, unexpected_authority: true })))) throw new Error('additional authority accepted')
  if (additionalProbe.record(golden.request, golden.result, authorityContext).replayed) throw new Error('additional authority mutated ledger')
  const partialProbe = new rpc.PromptSkillOperationLedgerV2()
  if (!(await rejected(() => partialProbe.record(golden.request, golden.result, { lease_epoch: authorityContext.lease_epoch })))) throw new Error('partial authority accepted')
  if (partialProbe.record(golden.request, golden.result, authorityContext).replayed) throw new Error('partial authority mutated ledger')
  const futureProbeRequest = setPath(setPath(structuredClone(golden.request), ['params', 'lease_epoch'], authorityContext.lease_epoch + 1), ['meta', 'lease_epoch'], authorityContext.lease_epoch + 1)
  const futureProbeResult = setPath(structuredClone(golden.result), ['lease_epoch'], authorityContext.lease_epoch + 1)
  const futureProbe = new rpc.PromptSkillOperationLedgerV2()
  if (!(await rejected(() => futureProbe.record(futureProbeRequest, futureProbeResult, authorityContext)))) throw new Error('future lease epoch accepted')
  if (futureProbe.record(golden.request, golden.result, authorityContext).replayed) throw new Error('future epoch mutated ledger')

  const validRequest = structuredClone(golden.request)
  validRequest.params.secrets[0].value = 'astral-😀'
  if (await rejected(() => rpc.parsePromptSkillExecuteRequestV2(validRequest))) throw new Error('valid request astral scalar rejected')
  const validResult = structuredClone(golden.result)
  validResult.warnings = [{ code: 'prompt-skill.warning', message: 'astral-😀' }]
  if (await rejected(() => rpc.parsePromptSkillExecuteResultV2(validResult))) throw new Error('valid result astral scalar rejected')
  const validSuccess = structuredClone(golden.success)
  validSuccess.result.warnings = [{ code: 'prompt-skill.warning', message: 'astral-😀' }]
  if (await rejected(() => rpc.validateRpcMethodSuccessV2(validSuccess, golden.request))) throw new Error('valid success astral scalar rejected')
  const invalidRequest = structuredClone(golden.request)
  invalidRequest.params.secrets[0].value = 'lone-\ud800'
  const invalidResult = structuredClone(golden.result)
  invalidResult.warnings = [{ code: 'prompt-skill.warning', message: 'lone-\ud800' }]
  const invalidSuccess = structuredClone(golden.success)
  invalidSuccess.result.warnings = [{ code: 'prompt-skill.warning', message: 'lone-\ud800' }]
  const invalidError = structuredClone(golden.error)
  invalidError.error.message = 'lone-\ud800'
  if (!(await rejected(() => rpc.parsePromptSkillExecuteRequestV2(invalidRequest)))) throw new Error('lone surrogate request accepted')
  if (!(await rejected(() => rpc.parsePromptSkillExecuteResultV2(invalidResult)))) throw new Error('lone surrogate result accepted')
  if (!(await rejected(() => rpc.validateRpcMethodSuccessV2(invalidSuccess, golden.request)))) throw new Error('lone surrogate success accepted')
  if (!(await rejected(() => rpc.parsePromptSkillErrorV2(invalidError)))) throw new Error('lone surrogate RPC error accepted')
  return { schemas: 3, golden_files: Object.keys(expected.fixture_files).length, corpus_groups: groups.length, negative_cases: negativeCases, operation_digest: operationDigest, operation_bytes_hex: operationBytesHex, package_resources: 7 }
}

function verifyModelProviderRpc() {
  const methodId = 'model.provider.invoke/v1'
  const terminalStates = ['receipted', 'failed', 'cancelled', 'uncertain']
  const plannerFields = [
    'operation_key', 'workspace_id', 'plugin_id', 'plugin_release_id', 'plugin_package_hash',
    'generation_id', 'job_id', 'step_id', 'attempt_id', 'lease_epoch', 'chain_id',
    'chain_asset_id', 'chain_content_hash', 'run_snapshot_id', 'run_snapshot_asset_id',
    'run_snapshot_hash', 'model_profile_revision_id', 'input_asset_id', 'input_content_hash',
  ]
  const anchorFields = ['receipt_id', 'asset_id', 'content_hash', 'receipt_hash', ...plannerFields]
  const hashDomains = [
    'model_request_asset.content_hash',
    'model_receipt_anchor.content_hash',
    'provider_transport_request_hash',
    'provider_transport_response_hash',
    'response_asset.content_hash',
    'model_receipt_anchor.receipt_hash',
  ]
  const matrixPath = join(SCHEMAS, 'model-provider-rpc-method-matrix.v2.json')
  const matrix = readJson(matrixPath)
  if (canonicalJson(Object.keys(matrix).sort()) !== canonicalJson(['authority', 'direction', 'envelope', 'methods', 'plugin_authority_allowed', 'protocol', 'reserved_method_ids', 'schema'])) throw new Error('Provider RPC matrix root shape drift')
  if (matrix.schema !== 'model-provider-rpc-method-matrix/v2' || matrix.authority !== 'core' || matrix.direction !== 'host-to-provider' || matrix.plugin_authority_allowed !== false) throw new Error('Provider RPC matrix authority drift')
  if (!jsonEqual(matrix.reserved_method_ids, [methodId]) || matrix.methods.length !== 1 || matrix.methods[0].method !== methodId || matrix.methods[0].reserved !== true) throw new Error('Provider RPC reserved method set drift')
  if (!jsonEqual(matrix.methods[0].terminal_states, terminalStates) || matrix.methods[0].terminal_states_use_jsonrpc_success !== true || matrix.methods[0].rpc_error_meaning !== 'no-verifiable-terminal-receipt') throw new Error('Provider RPC terminal semantics drift')
  if (!jsonEqual(matrix.envelope, { exactly_one: true, members: ['request', 'success', 'error'] })) throw new Error('Provider RPC envelope registry drift')

  const schemaNames = [
    'model-provider-invoke-request-v2.schema.json',
    'model-provider-invoke-result-v2.schema.json',
    'model-provider-invoke-success-v2.schema.json',
  ]
  const schemas = Object.fromEntries(schemaNames.map((name) => [name, readJson(join(SCHEMAS, name))]))
  const requestSchema = schemas['model-provider-invoke-request-v2.schema.json']
  const resultSchema = schemas['model-provider-invoke-result-v2.schema.json']
  const successSchema = schemas['model-provider-invoke-success-v2.schema.json']
  const errorSchema = readJson(join(SCHEMAS, 'rpc-error-v1.schema.json'))
  if (!jsonEqual(requestSchema.properties.params.properties.planner_context.required, plannerFields)) throw new Error('Provider RPC planner context inventory drift')
  if (!jsonEqual(resultSchema.properties.model_receipt_anchor.required, anchorFields)) throw new Error('Provider RPC receipt anchor inventory drift')
  const resourceNames = ['model-provider-rpc-method-matrix.v2.json', ...schemaNames]
  for (const name of resourceNames) if (!bytes(join(SDK_RESOURCES, name)).equals(bytes(join(SCHEMAS, name)))) throw new Error(`Provider RPC packaged resource drift: ${name}`)

  const golden = readJson(join(MODEL_PROVIDER_GOLDEN, 'invoke.json'))
  const expected = readJson(join(MODEL_PROVIDER_GOLDEN, 'expected.json'))
  for (const [name, digest] of Object.entries(expected.fixture_files)) if (sha256(bytes(join(MODEL_PROVIDER_GOLDEN, name))) !== digest) throw new Error(`Provider RPC golden hash drift: ${name}`)
  for (const [name, digest] of Object.entries(expected.corpus_files)) if (sha256(bytes(join(MODEL_PROVIDER_CORPUS, name))) !== digest) throw new Error(`Provider RPC corpus hash drift: ${name}`)
  if (sha256(bytes(matrixPath)) !== expected.method_matrix_sha256) throw new Error('Provider RPC matrix golden binding drift')

  const clone = (value) => structuredClone(value)
  const parseRequest = (value) => {
    const request = clone(value)
    validateClosed(request, requestSchema, 'provider.request')
    const context = request.params.planner_context
    const bindings = { operation_id: 'operation_key', generation_id: 'generation_id', job_id: 'job_id', step_id: 'step_id', attempt_id: 'attempt_id', lease_epoch: 'lease_epoch' }
    for (const [metaField, contextField] of Object.entries(bindings)) if (!jsonEqual(request.meta[metaField], context[contextField])) throw new Error(`Provider RPC meta binding drift: ${metaField}`)
    return request
  }
  const parseResult = (value) => {
    const result = clone(value)
    validateClosed(result, resultSchema, 'provider.result')
    return result
  }
  const verifyAsset = (identity, role) => {
    const hex = role === 'model_request_asset' ? golden.model_request_asset_bytes_hex : golden.response_asset_bytes_hex
    if (sha256(Buffer.from(hex, 'hex')) !== identity.content_hash) throw new Error(`${role} Asset content hash drift`)
  }
  const validateResult = (requestValue, resultValue, state, evidence = true, verifyAssets = true) => {
    const request = parseRequest(requestValue)
    const result = parseResult(resultValue)
    for (const field of plannerFields) if (!jsonEqual(request.params.planner_context[field], result.model_receipt_anchor[field])) throw new Error(`Provider RPC planner anchor drift: ${field}`)
    if (!jsonEqual(request.params.model_request_asset, result.model_request_asset)) throw new Error('Provider RPC request Asset echo drift')
    if (!evidence) throw new Error('Provider RPC canonical receipt authority missing')
    const receipt = golden.canonical_receipts[state]
    const receiptAsset = golden.receipt_asset_identities[state]
    if (!receipt || Object.keys(receipt).length !== 27) throw new Error('Provider RPC canonical receipt fixture drift')
    if (result.model_receipt_anchor.asset_id !== receiptAsset.asset_id || result.model_receipt_anchor.content_hash !== receiptAsset.content_hash) throw new Error('Provider RPC receipt Asset anchor drift')
    const mirrors = {
      receipt_id: result.model_receipt_anchor.receipt_id,
      receipt_hash: result.model_receipt_anchor.receipt_hash,
      state: result.provider_terminal_state,
      request_hash: result.provider_transport_request_hash,
      response_hash: result.provider_transport_response_hash,
      profile_revision_id: result.model_receipt_anchor.model_profile_revision_id,
    }
    for (const [field, mirror] of Object.entries(mirrors)) if (!jsonEqual(receipt[field], mirror)) throw new Error(`Provider RPC canonical receipt mirror drift: ${field}`)
    if (receipt.response_asset_id === null) {
      if (result.response_asset !== null) throw new Error('Provider RPC null response Asset drift')
    } else if (result.response_asset === null || result.response_asset.asset_id !== receipt.response_asset_id) throw new Error('Provider RPC response Asset ID mirror drift')
    if (verifyAssets) {
      verifyAsset(result.model_request_asset, 'model_request_asset')
      if (result.response_asset !== null) verifyAsset(result.response_asset, 'response_asset')
    }
    return result
  }
  const validateResponse = (requestValue, responseValue, state = null, evidence = true, verifyAssets = true) => {
    const request = parseRequest(requestValue)
    const response = clone(responseValue)
    const kinds = ['result', 'error'].filter((key) => Object.prototype.hasOwnProperty.call(response, key))
    if (kinds.length !== 1 || Object.prototype.hasOwnProperty.call(response, 'method')) throw new Error('Provider RPC response exactly-one drift')
    if (response.id !== request.id) throw new Error('Provider RPC response ID drift')
    if (kinds[0] === 'error') {
      if (evidence) throw new Error('Provider RPC error cannot carry receipt evidence')
      validateClosed(response, errorSchema, 'provider.error')
      return response
    }
    validateClosed(response, successSchema, 'provider.success')
    return validateResult(request, response.result, state ?? response.result.provider_terminal_state, evidence, verifyAssets)
  }
  const parseEnvelope = (value, requestValue = null, state = 'receipted') => {
    const envelope = clone(value)
    const kinds = ['method', 'result', 'error'].filter((key) => Object.prototype.hasOwnProperty.call(envelope, key))
    if (kinds.length !== 1) throw new Error('Provider RPC envelope exactly-one drift')
    if (kinds[0] === 'method') {
      if (requestValue !== null) throw new Error('Provider request parsed as response')
      return parseRequest(envelope)
    }
    if (requestValue === null) throw new Error('Provider response missing parsed request')
    return validateResponse(requestValue, envelope, state, kinds[0] === 'result')
  }

  const request = parseRequest(golden.request)
  parseEnvelope(golden.request)
  for (const state of terminalStates) {
    const terminalResult = validateResponse(request, golden.terminal_successes[state], state, true)
    if (terminalResult.provider_terminal_state !== state) throw new Error(`Provider RPC terminal success drift: ${state}`)
  }
  validateResponse(request, golden.error, null, false)
  if (golden.terminal_results.failed.response_asset !== null || golden.terminal_results.failed.provider_transport_response_hash === null) throw new Error('Provider RPC null Asset/non-null response hash vector drift')
  const operationBytesHex = Buffer.from(canonicalJson(request.params), 'utf8').toString('hex')
  const operationDigest = hashJcs('model-provider-invoke/v2', request.params)
  const bridgeDigest = hashJcs('model-provider-rpc-bridge/v2', golden.bridge_projection)
  if (operationBytesHex !== expected.operation_canonical_bytes_hex || operationBytesHex !== golden.operation_canonical_bytes_hex || operationDigest !== expected.operation_digest || operationDigest !== golden.operation_digest || bridgeDigest !== expected.bridge_digest || bridgeDigest !== golden.bridge_digest) throw new Error('Provider RPC operation/bridge digest drift')
  const result = golden.result
  const hashValues = new Set([result.model_request_asset.content_hash, result.model_receipt_anchor.content_hash, result.provider_transport_request_hash, result.provider_transport_response_hash, result.response_asset.content_hash, result.model_receipt_anchor.receipt_hash])
  if (hashValues.size !== 6 || !jsonEqual(hashDomains, expected.hash_domains)) throw new Error('Provider RPC six hash domains drift')

  const ordinaryPlan = readJson(join(CONTRACTS, 'examples', 'fixtures', 'plugin-plan.json'))
  const ordinaryDescriptor = readJson(join(CONTRACTS, 'examples', 'fixtures', 'capability-provider.json'))
  const ordinaryManifest = readJson(join(CONTRACTS, 'examples', 'fixtures', 'plugin-manifest-code.json'))
  const reservedBinding = clone(ordinaryPlan); reservedBinding.bindings[0].capability_id = methodId
  const reservedSynthesizer = clone(ordinaryPlan); reservedSynthesizer.result_mode = 'synthesize'; reservedSynthesizer.synthesizer = { binding_id: ordinaryPlan.bindings[0].binding_id, capability_id: methodId, plugin_id: ordinaryPlan.bindings[0].plugin_id, release_requirement: ordinaryPlan.bindings[0].release_requirement }
  const reservedDescriptor = clone(ordinaryDescriptor); reservedDescriptor.capability_id = methodId
  const reservedManifest = clone(ordinaryManifest); reservedManifest.capabilities[0].capability_id = methodId
  const reservedFixtures = {
    'ordinary.plan': ordinaryPlan,
    'ordinary.descriptor': ordinaryDescriptor,
    'ordinary.manifest': ordinaryManifest,
    'reserved.plan.binding': reservedBinding,
    'reserved.plan.synthesizer': reservedSynthesizer,
    'reserved.descriptor': reservedDescriptor,
    'reserved.manifest': reservedManifest,
  }
  const validateReserved = (fixtureId, value) => {
    const ids = []
    if (fixtureId.includes('plan')) {
      ids.push(...value.bindings.map((binding) => binding.capability_id))
      if (value.synthesizer !== null) ids.push(value.synthesizer.capability_id)
    } else if (fixtureId.includes('descriptor')) ids.push(value.capability_id)
    else ids.push(...value.capabilities.map((capability) => capability.capability_id))
    if (ids.includes(methodId)) throw new Error('Provider RPC reserved business capability')
    return value
  }
  validateReserved('ordinary.plan', ordinaryPlan)
  validateReserved('ordinary.descriptor', ordinaryDescriptor)
  validateReserved('ordinary.manifest', ordinaryManifest)

  const fixtures = {
    'invoke.request': golden.request,
    'invoke.bare-result': golden.result,
    'invoke.error': golden.error,
    'invoke.exchange.receipted': { request: golden.request, response: golden.success },
    'invoke.ledger': golden.request,
    ...reservedFixtures,
  }
  for (const state of terminalStates) {
    fixtures[`invoke.result.${state}`] = golden.terminal_results[state]
    fixtures[`invoke.success.${state}`] = golden.terminal_successes[state]
  }
  const mutate = (value, mutation) => {
    const changed = clone(value)
    const setPath = (targetValue, path, replacement) => { let target = targetValue; for (const token of path.slice(0, -1)) target = target[token]; target[path.at(-1)] = clone(replacement) }
    if (mutation.op === 'noop') return changed
    if (mutation.op === 'set') { setPath(changed, mutation.path, mutation.value); return changed }
    if (mutation.op === 'delete') { let target = changed; for (const token of mutation.path.slice(0, -1)) target = target[token]; delete target[mutation.path.at(-1)]; return changed }
    if (mutation.op === 'set-many') { for (const change of mutation.changes) setPath(changed, change.path, change.value); return changed }
    throw new Error(`unsupported Provider corpus mutation ${mutation.op}`)
  }
  class ContractLedger {
    constructor() { this.entry = null }
    record(requestValue, resultValue, verifyAssets = false) {
      const resultOnly = Object.prototype.hasOwnProperty.call(resultValue, 'result') ? validateResponse(requestValue, resultValue, 'receipted', true, verifyAssets) : validateResult(requestValue, resultValue, 'receipted', true, verifyAssets)
      const entry = { request: canonicalJson(parseRequest(requestValue)), result: canonicalJson(resultOnly), value: clone(resultOnly) }
      if (this.entry !== null) {
        if (this.entry.request !== entry.request || this.entry.result !== entry.result) throw new Error('Provider exact replay drift')
        return { value: clone(this.entry.value), replayed: true }
      }
      this.entry = entry
      return { value: clone(resultOnly), replayed: false }
    }
    replay(requestValue) {
      if (this.entry === null || this.entry.request !== canonicalJson(parseRequest(requestValue))) throw new Error('Provider replay missing')
      return clone(this.entry.value)
    }
  }

  const group = readJson(join(MODEL_PROVIDER_CORPUS, '01-invoke.json'))
  const corpusManifest = readJson(join(MODEL_PROVIDER_CORPUS, 'manifest.json'))
  const observed = new Set()
  const rejected = (action) => { try { action(); return false } catch (_) { return true } }
  for (const item of group.negative) {
    if (observed.has(item.case_id)) throw new Error(`duplicate Provider RPC case ${item.case_id}`)
    observed.add(item.case_id)
    let action
    if (item.kind === 'ledger') {
      action = () => {
        if (item.mutation.op === 'unknown-replay') return new ContractLedger().replay(request)
        const ledger = new ContractLedger(); ledger.record(request, golden.success)
        if (item.mutation.op === 'request-drift') { const changedRequest = clone(request); const changedSuccess = clone(golden.success); changedRequest.id = '123e4567-e89b-42d3-a456-426614174101'; changedSuccess.id = changedRequest.id; return ledger.record(changedRequest, changedSuccess) }
        if (item.mutation.op === 'result-drift') { const changedResult = clone(golden.result); changedResult.response_asset.content_hash = 'f'.repeat(64); return ledger.record(request, changedResult) }
        throw new Error('unknown Provider ledger operation')
      }
    } else {
      const value = mutate(fixtures[item.fixture], item.mutation)
      if (item.kind === 'request') action = () => parseRequest(value)
      else if (item.kind === 'result') action = () => validateResult(request, value, item.fixture.split('.').at(-1), true, true)
      else if (item.kind === 'response') action = () => item.fixture === 'invoke.error' ? validateResponse(request, value, null, false) : validateResponse(request, value, item.fixture.split('.').at(-1), true, true)
      else if (item.kind === 'response-no-evidence') action = () => validateResponse(request, value, 'receipted', false)
      else if (item.kind === 'error-with-evidence') action = () => validateResponse(request, value, null, true)
      else if (item.kind === 'envelope') action = () => parseEnvelope(value, Object.prototype.hasOwnProperty.call(value, 'method') ? null : request)
      else if (item.kind === 'exchange') action = () => validateResponse(value.request, value.response, 'receipted', true, true)
      else if (item.kind === 'reserved') action = () => validateReserved(item.fixture, value)
      else throw new Error(`unknown Provider corpus kind ${item.kind}`)
    }
    if (!rejected(action)) throw new Error(`Provider RPC corpus false-accepted: ${item.case_id}`)
  }
  const caseDigest = sha256(Buffer.from(JSON.stringify([...observed].sort()), 'utf8'))
  if (observed.size !== 76 || corpusManifest.group_count !== 1 || corpusManifest.negative_case_count !== observed.size || corpusManifest.negative_case_digest !== caseDigest || expected.negative_case_count !== observed.size || expected.negative_case_digest !== caseDigest) throw new Error('Provider RPC corpus inventory/digest drift')
  return {
    schema_count: 3,
    golden_files: Object.keys(expected.fixture_files).length,
    corpus_groups: 1,
    negative_cases: observed.size,
    negative_case_digest: caseDigest,
    operation_digest: operationDigest,
    bridge_digest: bridgeDigest,
    terminal_states: terminalStates,
    hash_domains: hashDomains,
    package_resources: resourceNames.length,
    planner_context_fields: plannerFields.length,
    model_receipt_anchor_fields: anchorFields.length,
    canonical_receipt_fields: 27,
  }
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
  const additiveV1Named = new Set(['model-profile-revision-v1.schema.json', 'project-planner-model-output-v1.schema.json'])
  const v2Schemas = schemaFiles.filter((name) => name.endsWith('-v2.schema.json') || additiveV1Named.has(name))
  const v2Set = new Set(v2Schemas)
  const v1Schemas = schemaFiles.filter((name) => !v2Set.has(name))
  if (v1Schemas.length !== 55 || v2Schemas.length !== 18) throw new Error(`expected 55 v1 + 18 additive schemas, got ${v1Schemas.length} + ${v2Schemas.length}`)
  for (const name of schemaFiles) {
    const schema = readJson(join(SCHEMAS, name))
    if (schema.$schema !== 'https://json-schema.org/draft/2020-12/schema') throw new Error(`schema dialect drift: ${name}`)
  }
  const groups = readdirSync(join(CONTRACTS, 'corpus', 'negative', '84.13')).filter((name) => name.endsWith('.json'))
  if (groups.length !== 14) throw new Error(`expected 14 negative groups, got ${groups.length}`)
  return { schema_count: schemaFiles.length, v1_schema_count: v1Schemas.length, v2_schema_count: v2Schemas.length, negative_group_count: groups.length }
}

if (process.argv.includes('--all')) {
  const result = { inventory: verifyInventory(), v2: await verifyV2(), macro_planning_host: verifyMacroPlanningHost(), model_provider_rpc: verifyModelProviderRpc(), prompt_skill: await verifyPromptSkill(), package: verifyPackage(), skill: verifySkill(), snapshot: verifySnapshot(), backup: verifyBackup() }
  console.log(JSON.stringify(result, null, 2))
} else {
  console.error('pass --all')
  process.exitCode = 2
}
