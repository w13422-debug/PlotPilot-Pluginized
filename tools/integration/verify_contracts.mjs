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
  if (matrix.schema !== 'core-api-method-matrix/v2' || matrix.publication_path !== 'publication.accept' || matrix.publication_owner !== 'core' || matrix.plugin_publication_allowed !== false) throw new Error('v2 Publication ownership drift')
  if (matrix.routes.length !== 19 || matrix.routes.filter((route) => route.route_id === 'publication.accept').length !== 1) throw new Error('v2 route inventory drift')
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
  if (http.schema !== 'm4-m5-public-surface-http-golden/v2' || http.exchange_count !== http.exchanges.length || http.exchanges.length !== matrix.routes.length) throw new Error('v2 HTTP golden exchange inventory drift')
  const routeIds = matrix.routes.map((route) => route.route_id)
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
  return { routes: matrix.routes.length, schemas: 6, golden_files: Object.keys(expected.fixture_files).length, corpus_groups: groups.length, negative_cases: negativeCases, negative_case_digest: negativeCaseDigest, http_exchanges: http.exchanges.length, publication_path: matrix.publication_path, cursor_domains: Object.keys(matrix.cursor_domains).sort() }
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
  const v1Schemas = schemaFiles.filter((name) => !name.endsWith('-v2.schema.json'))
  const v2Schemas = schemaFiles.filter((name) => name.endsWith('-v2.schema.json'))
  if (v1Schemas.length !== 55 || v2Schemas.length !== 9) throw new Error(`expected 55 v1 + 9 v2 schemas, got ${v1Schemas.length} + ${v2Schemas.length}`)
  for (const name of schemaFiles) {
    const schema = readJson(join(SCHEMAS, name))
    if (schema.$schema !== 'https://json-schema.org/draft/2020-12/schema') throw new Error(`schema dialect drift: ${name}`)
  }
  const groups = readdirSync(join(CONTRACTS, 'corpus', 'negative', '84.13')).filter((name) => name.endsWith('.json'))
  if (groups.length !== 14) throw new Error(`expected 14 negative groups, got ${groups.length}`)
  return { schema_count: schemaFiles.length, v1_schema_count: v1Schemas.length, v2_schema_count: v2Schemas.length, negative_group_count: groups.length }
}

if (process.argv.includes('--all')) {
  const result = { inventory: verifyInventory(), v2: await verifyV2(), prompt_skill: await verifyPromptSkill(), package: verifyPackage(), skill: verifySkill(), snapshot: verifySnapshot(), backup: verifyBackup() }
  console.log(JSON.stringify(result, null, 2))
} else {
  console.error('pass --all')
  process.exitCode = 2
}
