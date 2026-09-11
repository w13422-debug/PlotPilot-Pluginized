import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'
import {
  PROJECT_BRIEF_DOCUMENT_TYPE,
  PROJECT_BRIEF_MAX_SERIALIZED_BYTES,
  PROJECT_BRIEF_PAYLOAD_SCHEMA,
  bindProjectBriefWorkspace,
  buildProjectBriefContent,
  parseProjectBriefContent,
  parseSerializedProjectBriefContent,
  preflightProjectBriefInput,
  projectBriefDocumentId,
  serializeProjectBriefContent,
} from '../../../frontend/src/core/flows/projectBrief.ts'
import {
  CoreProjectBriefPartialCreateError,
  createCoreFlows,
} from '../../../frontend/src/core/flows/coreFlows.ts'
import { CoreFlowHttpError, type CoreFlowGateway, type CoreFlowRouteId } from '../../../frontend/src/core/flows/gateway.ts'
import type {
  CoreAuthorityCommandQuery,
  ProjectBriefHomeInput,
} from '../../../frontend/src/contracts/types.ts'

const NOW = '2026-09-11T00:00:00Z'

function ids() {
  let value = 0
  return { next: (kind: 'workspace' | 'document' | 'operation' | 'revision') => `${kind}-${++value}` }
}

function input(overrides: Partial<ProjectBriefHomeInput> = {}): ProjectBriefHomeInput {
  return {
    premise: '失忆的星际修复师在边境站发现了能重写帝国历史的航图。',
    genre: '科幻 / 太空歌剧',
    worldPreset: '边境空间站与衰落帝国并存，跃迁航道需要稀缺燃料。',
    storyStructure: '谜团开篇，联盟扩张，帝国反扑，航图真相收束。',
    pacingControl: '每卷解开一个航图坐标，并让代价逐步提高。',
    writingStyle: '冷峻第一视角，战斗和维修细节并重。',
    specialRequirements: '科技设定必须遵守能源守恒，配角均有独立目标。',
    lengthTier: 'standard',
    useCustomLength: false,
    customChapters: 100,
    customWordsPerChapter: 2500,
    ...overrides,
  }
}

type StoredDocument = {
  schema: 'core-document/v1'
  document_id: string
  workspace_id: string
  document_type: string
  title: string
  current_revision_id: string | null
  created_at: string
  updated_at: string
  revision: number
}

type StoredRevision = {
  revision: {
    schema: 'core-revision/v1'
    revision_id: string
    workspace_id: string
    document_id: string | null
    node_id: string | null
    parent_revision_id: string | null
    content_hash: string
    created_by: string
    source_candidate_id: string | null
    created_at: string
    revision_number: number
    payload_schema: string | null
  }
  content: string
}

interface MemoryOptions {
  loseWorkspaceCreateOnce?: boolean
  loseRevisionCreateOnce?: boolean
  staleRevisionCreateOnce?: boolean
}

function memoryCore(options: MemoryOptions = {}) {
  const calls: Array<[CoreFlowRouteId, CoreAuthorityCommandQuery]> = []
  const workspaces = new Map<string, ReturnType<typeof workspace>>()
  const documents = new Map<string, StoredDocument>()
  const revisions = new Map<string, StoredRevision>()
  let loseWorkspace = options.loseWorkspaceCreateOnce === true
  let loseRevision = options.loseRevisionCreateOnce === true
  let staleRevision = options.staleRevisionCreateOnce === true

  const gateway: CoreFlowGateway = {
    async request(routeId, request) {
      calls.push([routeId, structuredClone(request) as CoreAuthorityCommandQuery])
      if (routeId === 'workspace.create') {
        if (loseWorkspace) {
          loseWorkspace = false
          throw new Error('workspace response lost')
        }
        if (request.schema !== 'core-workspace-create-command/v1') throw new Error('wrong workspace command')
        let current = workspaces.get(request.workspace_id)
        if (current === undefined) {
          current = workspace(request.workspace_id, request.title, 0)
          workspaces.set(current.workspace_id, current)
        }
        return structuredClone(current) as never
      }
      if (routeId === 'workspace.get') {
        if (request.schema !== 'core-workspace-get-query/v1') throw new Error('wrong workspace query')
        const current = workspaces.get(request.workspace_id)
        if (current === undefined) throw new Error('missing workspace')
        return structuredClone(current) as never
      }
      if (routeId === 'document.list') {
        if (request.schema !== 'core-document-query/v1') throw new Error('wrong document query')
        const items = [...documents.values()].filter(document => document.workspace_id === request.workspace_id)
        return {
          schema: 'core-document-page/v1', items, offset: request.offset, limit: request.limit,
          total: items.length, next_offset: null,
        } as never
      }
      if (routeId === 'document.create') {
        if (request.schema !== 'core-document-create-command/v1') throw new Error('wrong document command')
        let current = documents.get(request.document_id)
        if (current === undefined) {
          current = {
            schema: 'core-document/v1', document_id: request.document_id, workspace_id: request.workspace_id,
            document_type: request.document_type, title: request.title, current_revision_id: null,
            created_at: NOW, updated_at: NOW, revision: 0,
          }
          documents.set(current.document_id, current)
        }
        return structuredClone(current) as never
      }
      if (routeId === 'document.get') {
        if (request.schema !== 'core-document-get-query/v1') throw new Error('wrong document query')
        const current = documents.get(request.document_id)
        if (current === undefined || current.workspace_id !== request.workspace_id) throw new Error('missing document')
        return structuredClone(current) as never
      }
      if (routeId === 'document.revision.create') {
        if (request.schema !== 'core-document-revision-create-command/v1') throw new Error('wrong revision command')
        if (staleRevision) {
          staleRevision = false
          throw new CoreFlowHttpError(409, {
            schema: 'core-http-error/v1', error_code: 'stale_cas', message: 'stale base', retryable: false,
          })
        }
        if (loseRevision) {
          loseRevision = false
          throw new Error('revision response lost')
        }
        const document = documents.get(request.document_id)
        if (document === undefined || document.workspace_id !== request.workspace_id) throw new Error('missing document')
        if (document.current_revision_id !== request.base_revision_id) {
          throw new CoreFlowHttpError(409, {
            schema: 'core-http-error/v1', error_code: 'stale_cas', message: 'stale base', retryable: false,
          })
        }
        const revision = {
          schema: 'core-revision/v1' as const, revision_id: request.revision_id, workspace_id: request.workspace_id,
          document_id: request.document_id, node_id: null, parent_revision_id: request.base_revision_id,
          content_hash: 'a'.repeat(64), created_by: request.created_by, source_candidate_id: request.source_candidate_id,
          created_at: NOW, revision_number: document.revision + 1, payload_schema: request.payload_schema,
        }
        revisions.set(revision.revision_id, { revision, content: request.content })
        document.current_revision_id = revision.revision_id
        document.revision += 1
        document.updated_at = NOW
        return structuredClone(revision) as never
      }
      if (routeId === 'revision.get') {
        if (request.schema !== 'core-revision-get-query/v1') throw new Error('wrong revision query')
        const current = revisions.get(request.revision_id)
        if (current === undefined || current.revision.workspace_id !== request.workspace_id) throw new Error('missing revision')
        return structuredClone(current.revision) as never
      }
      if (routeId === 'revision.content') {
        if (request.schema !== 'core-revision-content-query/v1') throw new Error('wrong content query')
        const current = revisions.get(request.revision_id)
        if (current === undefined || current.revision.workspace_id !== request.workspace_id) throw new Error('missing revision')
        return {
          schema: 'core-revision-content-page/v1', revision_id: request.revision_id, offset: request.offset,
          length: [...current.content].length, total_length: [...current.content].length,
          text: current.content, next_offset: null,
        } as never
      }
      throw new Error(`unexpected ${routeId}`)
    },
  }

  return { gateway, calls, workspaces, documents, revisions }
}

function workspace(id: string, title = id, revision = 1) {
  return {
    schema: 'core-workspace/v1' as const, workspace_id: id, workspace_kind: 'WritingProject', title,
    status: 'active', current_plan_revision_id: null, created_at: NOW, updated_at: NOW, revision,
  }
}

function commands(calls: readonly Array<[CoreFlowRouteId, CoreAuthorityCommandQuery]>, schema: string): CoreAuthorityCommandQuery[] {
  return calls.map(([, request]) => request).filter(request => request.schema === schema)
}

test('Project Brief codec is closed, stable, and binds the deterministic UTF-8 SHA-256 Document identity', async () => {
  const content = buildProjectBriefContent('ws-1', input())
  assert.deepEqual(Object.keys(content), ['workspace_id', 'premise', 'genres', 'target_words', 'structure', 'market', 'length'])
  assert.deepEqual(content.genres, { genre: '科幻 / 太空歌剧' })
  assert.equal(content.target_words, 1_000_000)
  assert.deepEqual(content.length, {
    tier: 'standard', use_custom: false, custom_chapters: 100, custom_words_per_chapter: 2500,
  })
  assert.deepEqual(parseSerializedProjectBriefContent(serializeProjectBriefContent(content), 'ws-1'), content)
  const expectedHash = createHash('sha256').update('ws-1', 'utf8').digest('hex')
  assert.equal(await projectBriefDocumentId('ws-1'), `project-brief:${expectedHash}`)
})

test('Project Brief rejects malformed, cross-workspace, unknown, non-finite, and oversized values before effects', async () => {
  const content = buildProjectBriefContent('ws-1', input())
  assert.throws(() => parseProjectBriefContent({ ...content, extra: true }), /unknown property/)
  assert.throws(() => parseProjectBriefContent({ ...content, market: { ...content.market, extra: true } }), /unknown property/)
  assert.throws(() => parseProjectBriefContent({ ...content, target_words: Number.NaN }), /expected type/)
  assert.throws(() => parseProjectBriefContent(content, 'ws-other'), /crossed its expected Workspace identity/)
  assert.throws(() => parseSerializedProjectBriefContent('{broken'), /not valid JSON/)
  assert.throws(() => serializeProjectBriefContent({
    ...content,
    market: { world_preset: 'x'.repeat(PROJECT_BRIEF_MAX_SERIALIZED_BYTES) },
  }), /serialized content exceeds/)

  const store = memoryCore()
  const flows = createCoreFlows({ ids: ids(), createdBy: 'plotpilot.webui.local', gateway: store.gateway })
  await assert.rejects(
    () => flows.createProjectWithBrief('invalid before effects', input({ customWordsPerChapter: Number.NaN, useCustomLength: true })),
    /expected type/,
  )
  assert.equal(store.calls.length, 0)
})

test('creates one verified Core Project Brief Document and first Revision, then reloads every Home field', async () => {
  const store = memoryCore()
  const flows = createCoreFlows({ ids: ids(), createdBy: 'plotpilot.webui.local', gateway: store.gateway })
  const created = await flows.createProjectWithBrief('星图遗民', input())
  const documentCommands = commands(store.calls, 'core-document-create-command/v1')
  const revisionCommands = commands(store.calls, 'core-document-revision-create-command/v1')

  assert.equal(created.project.workspaceId, created.workspaceId)
  assert.equal(created.brief.workspaceId, created.workspaceId)
  assert.equal(created.brief.documentId, await projectBriefDocumentId(created.workspaceId))
  assert.equal(documentCommands.length, 1)
  assert.equal(revisionCommands.length, 1)
  const documentCommand = documentCommands[0]
  const revisionCommand = revisionCommands[0]
  assert.equal(documentCommand?.schema, 'core-document-create-command/v1')
  assert.equal(revisionCommand?.schema, 'core-document-revision-create-command/v1')
  if (documentCommand?.schema === 'core-document-create-command/v1') {
    assert.equal(documentCommand.document_id, created.brief.documentId)
    assert.equal(documentCommand.document_type, PROJECT_BRIEF_DOCUMENT_TYPE)
  }
  if (revisionCommand?.schema === 'core-document-revision-create-command/v1') {
    assert.equal(revisionCommand.base_revision_id, null)
    assert.equal(revisionCommand.payload_schema, PROJECT_BRIEF_PAYLOAD_SCHEMA)
    assert.equal(revisionCommand.source_candidate_id, null)
    assert.equal(revisionCommand.created_by, 'plotpilot.webui.local')
  }
  assert.deepEqual(await flows.loadProjectBrief(created.workspaceId), created.brief)
})

test('updates through the current Revision CAS parent chain without changing chapter behavior', async () => {
  const store = memoryCore()
  const flows = createCoreFlows({ ids: ids(), createdBy: 'plotpilot.webui.local', gateway: store.gateway })
  const first = await flows.createProjectWithBrief('星图遗民', input())
  const second = await flows.saveProjectBrief(first.workspaceId, input({ premise: '更新后的梗概。', useCustomLength: true, customChapters: 120, customWordsPerChapter: 3000 }))
  const revisionCommands = commands(store.calls, 'core-document-revision-create-command/v1')
  assert.equal(second.content.premise, '更新后的梗概。')
  assert.equal(second.content.target_words, 360_000)
  assert.equal(revisionCommands.length, 2)
  const update = revisionCommands[1]
  assert.equal(update?.schema, 'core-document-revision-create-command/v1')
  if (update?.schema === 'core-document-revision-create-command/v1') assert.equal(update.base_revision_id, first.brief.revisionId)
})

test('reuses exact Workspace and Revision commands after uncertain response loss and never reports a partial create as success', async () => {
  const store = memoryCore({ loseWorkspaceCreateOnce: true, loseRevisionCreateOnce: true })
  const flows = createCoreFlows({ ids: ids(), createdBy: 'plotpilot.webui.local', gateway: store.gateway })
  await assert.rejects(() => flows.createProjectWithBrief('可恢复建档', input()), /workspace response lost/)
  await assert.rejects(
    () => flows.createProjectWithBrief('可恢复建档', input()),
    error => error instanceof CoreProjectBriefPartialCreateError && /revision response lost/.test(error.message),
  )
  const created = await flows.createProjectWithBrief('可恢复建档', input())
  const workspaceCommands = commands(store.calls, 'core-workspace-create-command/v1')
  const revisionCommands = commands(store.calls, 'core-document-revision-create-command/v1')
  assert.equal(created.brief.content.workspace_id, created.workspaceId)
  assert.deepEqual(workspaceCommands[0], workspaceCommands[1])
  assert.deepEqual(revisionCommands[0], revisionCommands[1])
  assert.equal(store.workspaces.size, 1)
  assert.equal(store.documents.size, 1)
  assert.equal(store.revisions.size, 1)
})

test('stale Project Brief CAS reloads and fails visibly without an overwrite', async () => {
  const store = memoryCore()
  const flows = createCoreFlows({ ids: ids(), createdBy: 'plotpilot.webui.local', gateway: store.gateway })
  const created = await flows.createProjectWithBrief('CAS', input())

  let staleOnce = true
  const originalGateway = store.gateway
  const staleGateway: CoreFlowGateway = {
    async request(routeId, request) {
      if (routeId === 'document.revision.create' && staleOnce) {
        staleOnce = false
        throw new CoreFlowHttpError(409, {
          schema: 'core-http-error/v1', error_code: 'stale_cas', message: 'stale base', retryable: false,
        })
      }
      return originalGateway.request(routeId, request)
    },
  }
  const staleFlows = createCoreFlows({ ids: ids(), createdBy: 'plotpilot.webui.local', gateway: staleGateway })
  await assert.rejects(
    () => staleFlows.saveProjectBrief(created.workspaceId, input({ premise: '不应覆盖。' })),
    /stale CAS.*no overwrite/,
  )
  assert.equal(store.revisions.size, 1)
})

test('rejects a noncanonical duplicate Project Brief Document instead of silently choosing one', async () => {
  const store = memoryCore()
  store.workspaces.set('ws-duplicate', workspace('ws-duplicate', '重复'))
  store.documents.set('other-brief', {
    schema: 'core-document/v1', document_id: 'other-brief', workspace_id: 'ws-duplicate',
    document_type: PROJECT_BRIEF_DOCUMENT_TYPE, title: 'Project Brief', current_revision_id: null,
    created_at: NOW, updated_at: NOW, revision: 0,
  })
  const flows = createCoreFlows({ ids: ids(), createdBy: 'plotpilot.webui.local', gateway: store.gateway })
  await assert.rejects(() => flows.loadProjectBrief('ws-duplicate'), /more than one Project Brief Document/)
})

test('an existing Core Workspace without a Project Brief remains safe to inspect', async () => {
  const store = memoryCore()
  store.workspaces.set('ws-no-brief', workspace('ws-no-brief', '既有 Core 项目'))
  const flows = createCoreFlows({ ids: ids(), gateway: store.gateway })
  assert.equal(await flows.loadProjectBrief('ws-no-brief'), null)
  assert.equal(store.calls.filter(([route]) => route === 'document.create' || route === 'document.revision.create').length, 0)
})

test('preflight intent is immutable and can only be rebound to the verified Workspace identity', () => {
  const preflight = preflightProjectBriefInput(input())
  const rebound = bindProjectBriefWorkspace(preflight.content, 'ws-9')
  assert.equal(preflight.content.workspace_id, 'project-brief-preflight')
  assert.equal(rebound.workspace_id, 'ws-9')
  assert.equal(rebound.structure.writing_style, input().writingStyle)
})
