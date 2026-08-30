import assert from 'node:assert/strict'
import test from 'node:test'
import { createCoreFlows, editCoreChapter, type CoreOpenedChapter } from '../../../frontend/src/core/flows/coreFlows.ts'
import type { CoreFlowGateway, CoreFlowRouteId } from '../../../frontend/src/core/flows/gateway.ts'
import type { CoreAuthorityCommandQuery } from '../../../frontend/src/contracts/types.ts'

const NOW = '2026-08-28T00:00:00Z'

function ids() {
  let value = 0
  return { next: (kind: 'workspace' | 'operation' | 'revision') => `${kind}-${++value}` }
}

function gateway(handler: (routeId: CoreFlowRouteId, request: CoreAuthorityCommandQuery) => unknown): CoreFlowGateway {
  return {
    async request(routeId, request) {
      return structuredClone(handler(routeId, request)) as never
    },
  }
}

function workspace(id: string, title = id, revision = 1) {
  return {
    schema: 'core-workspace/v1' as const, workspace_id: id, workspace_kind: 'WritingProject', title,
    status: 'active', current_plan_revision_id: null, created_at: NOW, updated_at: NOW, revision,
  }
}

function document(id: string, workspaceId: string, title: string, currentRevisionId: string | null = null) {
  return {
    schema: 'core-document/v1' as const, document_id: id, workspace_id: workspaceId,
    document_type: 'core.chapter', title, current_revision_id: currentRevisionId,
    created_at: NOW, updated_at: NOW, revision: currentRevisionId === null ? 0 : 1,
  }
}

test('lists every WritingProject page and keeps the frozen query cursor advancing', async () => {
  const calls: CoreAuthorityCommandQuery[] = []
  const flows = createCoreFlows({ gateway: gateway((_route, request) => {
    calls.push(request)
    const offset = request.schema === 'core-workspace-query/v1' ? request.offset : -1
    return {
      schema: 'core-workspace-page/v1',
      items: offset === 0
        ? [workspace('ws-1'), { ...workspace('ignored'), workspace_kind: 'PluginWorkspace' }]
        : [workspace('ws-2')],
      offset, limit: 100, total: 3, next_offset: offset === 0 ? 2 : null,
    }
  }) })

  const projects = await flows.listProjects()
  assert.deepEqual(projects.map(project => project.workspaceId), ['ws-1', 'ws-2'])
  assert.deepEqual(calls.map(call => call.schema === 'core-workspace-query/v1' ? call.offset : -1), [0, 2])
})

test('creates and CAS-deletes a project with fresh typed operation identities', async () => {
  const calls: Array<[CoreFlowRouteId, CoreAuthorityCommandQuery]> = []
  const flows = createCoreFlows({ ids: ids(), gateway: gateway((route, request) => {
    calls.push([route, request])
    if (request.schema === 'core-workspace-create-command/v1') return workspace(request.workspace_id, request.title, 0)
    if (request.schema === 'core-workspace-delete-command/v1') {
      return { schema: 'core-delete-result/v1', operation_key: request.operation_key, workspace_id: request.workspace_id,
        entity_kind: 'workspace', entity_id: request.workspace_id, previous_revision: request.expected_revision,
        deleted: true, idempotent: false }
    }
    throw new Error('unexpected request')
  }) })

  const created = await flows.createProject('  新项目  ')
  await flows.deleteProject(created)
  assert.equal(created.title, '新项目')
  assert.deepEqual(calls.map(([route]) => route), ['workspace.create', 'workspace.delete'])
  assert.equal(calls[1]?.[1].schema, 'core-workspace-delete-command/v1')
  if (calls[1]?.[1].schema === 'core-workspace-delete-command/v1') assert.equal(calls[1][1].expected_revision, 0)
})

test('loads chapter navigation, opens paged content, edits and saves a new Core revision', async () => {
  const requested: Array<[CoreFlowRouteId, CoreAuthorityCommandQuery]> = []
  const flows = createCoreFlows({ ids: ids(), createdBy: 'user-a', gateway: gateway((route, request) => {
    requested.push([route, request])
    if (route === 'workspace.get') return workspace('ws-1', 'Project')
    if (route === 'document.list') return { schema: 'core-document-page/v1', items: [
      document('doc-10', 'ws-1', '第10章', 'rev-old'), document('doc-2', 'ws-1', '第2章'),
      { ...document('notes', 'ws-1', 'Notes'), document_type: 'core.notes' },
    ], offset: 0, limit: 100, total: 3, next_offset: null }
    if (route === 'document.get') return document('doc-10', 'ws-1', '第10章', 'rev-old')
    if (route === 'revision.get') return { schema: 'core-revision/v1', revision_id: 'rev-old', workspace_id: 'ws-1',
      document_id: 'doc-10', node_id: null, parent_revision_id: null, content_hash: '1'.repeat(64), created_by: 'user-a',
      source_candidate_id: null, created_at: NOW, revision_number: 1, payload_schema: 'core.document-text/v1' }
    if (route === 'revision.content') {
      if (request.schema !== 'core-revision-content-query/v1') throw new Error('wrong query')
      return request.offset === 0
        ? { schema: 'core-revision-content-page/v1', revision_id: 'rev-old', offset: 0, length: 2, total_length: 4, text: '旧章', next_offset: 2 }
        : { schema: 'core-revision-content-page/v1', revision_id: 'rev-old', offset: 2, length: 2, total_length: 4, text: '正文', next_offset: null }
    }
    if (route === 'document.revision.create') {
      if (request.schema !== 'core-document-revision-create-command/v1') throw new Error('wrong command')
      return { schema: 'core-revision/v1', revision_id: request.revision_id, workspace_id: request.workspace_id,
        document_id: request.document_id, node_id: null, parent_revision_id: request.base_revision_id,
        content_hash: '2'.repeat(64), created_by: request.created_by, source_candidate_id: null,
        created_at: NOW, revision_number: 2, payload_schema: request.payload_schema }
    }
    throw new Error(`unexpected ${route}`)
  }) })

  const desk = await flows.loadWorkbench('ws-1')
  assert.deepEqual(desk.chapters.map(chapter => [chapter.documentId, chapter.displayIndex]), [['doc-10', 1], ['doc-2', 2]])
  const opened = await flows.openChapter(desk.chapters[0]!)
  assert.equal(opened.savedContent, '旧章正文')
  const edited = editCoreChapter(opened, '新章正文')
  assert.equal(edited.dirty, true)
  const saved = await flows.saveChapter(edited)
  assert.equal(saved.savedContent, '新章正文')
  assert.equal(saved.dirty, false)
  const saveCall = requested.find(([route]) => route === 'document.revision.create')?.[1]
  assert.equal(saveCall?.schema, 'core-document-revision-create-command/v1')
  if (saveCall?.schema === 'core-document-revision-create-command/v1') {
    assert.equal(saveCall.base_revision_id, 'rev-old')
    assert.equal(saveCall.content, '新章正文')
  }
})

test('opens a never-revised chapter without inventing authority or requesting content', async () => {
  const routes: CoreFlowRouteId[] = []
  const chapter = { documentId: 'doc-empty', workspaceId: 'ws-1', title: '空白章', displayIndex: 1, revision: 0,
    currentRevisionId: null, createdAt: NOW, updatedAt: NOW }
  const flows = createCoreFlows({ gateway: gateway((route) => {
    routes.push(route)
    if (route === 'document.get') return document('doc-empty', 'ws-1', '空白章')
    throw new Error(`unexpected ${route}`)
  }) })
  const opened = await flows.openChapter(chapter)
  assert.equal(opened.savedContent, '')
  assert.deepEqual(routes, ['document.get'])
})

test('rejects a cross-Workspace document page instead of projecting a second truth', async () => {
  const flows = createCoreFlows({ gateway: gateway((route) => {
    if (route === 'workspace.get') return workspace('ws-1')
    if (route === 'document.list') return { schema: 'core-document-page/v1', items: [document('doc-x', 'ws-other', '越界章')],
      offset: 0, limit: 100, total: 1, next_offset: null }
    throw new Error(`unexpected ${route}`)
  }) })
  await assert.rejects(() => flows.loadWorkbench('ws-1'), /crossed Workspace/)
})

test('rejects Workspace page offset, limit, and cursor drift', async (t) => {
  const cases = [
    { name: 'offset', page: { offset: 1, limit: 100, total: 0, next_offset: null }, error: /requested offset or limit/ },
    { name: 'limit', page: { offset: 0, limit: 99, total: 0, next_offset: null }, error: /requested offset or limit/ },
    { name: 'cursor', page: { offset: 0, limit: 100, total: 2, next_offset: 2 }, error: /cursor drifted/ },
  ] as const

  for (const entry of cases) {
    await t.test(entry.name, async () => {
      const flows = createCoreFlows({ gateway: gateway(() => ({
        schema: 'core-workspace-page/v1', items: entry.name === 'cursor' ? [workspace('ws-1')] : [], ...entry.page,
      })) })
      await assert.rejects(() => flows.listProjects(), entry.error)
    })
  }
})

test('rejects a Workspace entity repeated across pages', async () => {
  const flows = createCoreFlows({ gateway: gateway((_route, request) => {
    if (request.schema !== 'core-workspace-query/v1') throw new Error('wrong query')
    return { schema: 'core-workspace-page/v1', items: [workspace('ws-1')], offset: request.offset, limit: 100,
      total: 2, next_offset: request.offset === 0 ? 1 : null }
  }) })
  await assert.rejects(() => flows.listProjects(), /pagination repeated ws-1/)
})

test('rejects Document page offset, limit, and cursor drift', async (t) => {
  const cases = [
    { name: 'offset', page: { items: [], offset: 1, limit: 100, total: 0, next_offset: null }, error: /requested offset or limit/ },
    { name: 'limit', page: { items: [], offset: 0, limit: 99, total: 0, next_offset: null }, error: /requested offset or limit/ },
    { name: 'cursor', page: { items: [document('doc-1', 'ws-1', '第一章')], offset: 0, limit: 100, total: 2, next_offset: 2 }, error: /cursor drifted/ },
  ] as const
  for (const entry of cases) {
    await t.test(entry.name, async () => {
      const flows = createCoreFlows({ gateway: gateway((route) => {
        if (route === 'workspace.get') return workspace('ws-1')
        return { schema: 'core-document-page/v1', ...entry.page }
      }) })
      await assert.rejects(() => flows.loadWorkbench('ws-1'), entry.error)
    })
  }
})

test('rejects a Document entity repeated across pages', async () => {
  const flows = createCoreFlows({ gateway: gateway((route, request) => {
    if (route === 'workspace.get') return workspace('ws-1')
    if (request.schema !== 'core-document-query/v1') throw new Error('wrong query')
    return { schema: 'core-document-page/v1', items: [document('doc-1', 'ws-1', '第一章')], offset: request.offset,
      limit: 100, total: 2, next_offset: request.offset === 0 ? 1 : null }
  }) })
  await assert.rejects(() => flows.loadWorkbench('ws-1'), /pagination repeated doc-1/)
})

test('rejects a revision.get response that drifts from the Document current Revision', async () => {
  const routes: CoreFlowRouteId[] = []
  const flows = createCoreFlows({ gateway: gateway((route) => {
    routes.push(route)
    if (route === 'document.get') return document('doc-1', 'ws-1', '第一章', 'rev-current')
    if (route === 'revision.get') return { schema: 'core-revision/v1', revision_id: 'rev-other', workspace_id: 'ws-1',
      document_id: 'doc-1', node_id: null, parent_revision_id: null, content_hash: '1'.repeat(64), created_by: 'user-a',
      source_candidate_id: null, created_at: NOW, revision_number: 1, payload_schema: 'core.document-text/v1' }
    throw new Error(`unexpected ${route}`)
  }) })
  const chapter = { documentId: 'doc-1', workspaceId: 'ws-1', title: '第一章', displayIndex: 1, revision: 1,
    currentRevisionId: 'rev-current', createdAt: NOW, updatedAt: NOW }

  await assert.rejects(() => flows.openChapter(chapter), /exact current Revision identity/)
  assert.deepEqual(routes, ['document.get', 'revision.get'])
})

test('rejects a non-text current Revision before requesting its content', async () => {
  const routes: CoreFlowRouteId[] = []
  const flows = createCoreFlows({ gateway: gateway((route) => {
    routes.push(route)
    if (route === 'document.get') return document('doc-1', 'ws-1', '第一章', 'rev-current')
    if (route === 'revision.get') return { schema: 'core-revision/v1', revision_id: 'rev-current', workspace_id: 'ws-1',
      document_id: 'doc-1', node_id: null, parent_revision_id: null, content_hash: '1'.repeat(64), created_by: 'user-a',
      source_candidate_id: null, created_at: NOW, revision_number: 1, payload_schema: 'core.other/v1' }
    throw new Error(`unexpected ${route}`)
  }) })
  const chapter = { documentId: 'doc-1', workspaceId: 'ws-1', title: '第一章', displayIndex: 1, revision: 1,
    currentRevisionId: 'rev-current', createdAt: NOW, updatedAt: NOW }

  await assert.rejects(() => flows.openChapter(chapter), /text payload/)
  assert.deepEqual(routes, ['document.get', 'revision.get'])
})

test('edit is pure and preserves the opened chapter snapshot', () => {
  const opened: CoreOpenedChapter = { chapter: { documentId: 'doc-1', workspaceId: 'ws-1', title: '第一章', displayIndex: 1,
    revision: 1, currentRevisionId: 'rev-1', createdAt: NOW, updatedAt: NOW }, savedContent: 'a', draftContent: 'a',
    revisionId: 'rev-1', dirty: false }
  const edited = editCoreChapter(opened, 'b')
  assert.equal(opened.draftContent, 'a')
  assert.deepEqual({ draft: edited.draftContent, dirty: edited.dirty }, { draft: 'b', dirty: true })
})

test('reuses the exact create command after an uncertain transport failure', async () => {
  const commands: CoreAuthorityCommandQuery[] = []
  let attempt = 0
  const flows = createCoreFlows({ ids: ids(), gateway: gateway((_route, request) => {
    commands.push(request)
    attempt += 1
    if (attempt === 1) throw new Error('response lost')
    if (request.schema !== 'core-workspace-create-command/v1') throw new Error('wrong command')
    return workspace(request.workspace_id, request.title, 0)
  }) })
  await assert.rejects(() => flows.createProject('Retry Project'), /response lost/)
  await flows.createProject('Retry Project')
  assert.deepEqual(commands[0], commands[1])
})

test('reuses save identities across retry and rejects a mismatched parent revision', async () => {
  const commands: CoreAuthorityCommandQuery[] = []
  let attempt = 0
  const flows = createCoreFlows({ ids: ids(), createdBy: 'user-a', gateway: gateway((_route, request) => {
    commands.push(request)
    attempt += 1
    if (attempt === 1) throw new Error('timeout')
    if (request.schema !== 'core-document-revision-create-command/v1') throw new Error('wrong command')
    return { schema: 'core-revision/v1', revision_id: request.revision_id, workspace_id: request.workspace_id,
      document_id: request.document_id, node_id: null, parent_revision_id: 'wrong-parent', content_hash: '3'.repeat(64),
      created_by: request.created_by, source_candidate_id: null, created_at: NOW, revision_number: 2,
      payload_schema: request.payload_schema }
  }) })
  const opened: CoreOpenedChapter = { chapter: { documentId: 'doc-1', workspaceId: 'ws-1', title: '第一章', displayIndex: 1,
    revision: 1, currentRevisionId: 'rev-1', createdAt: NOW, updatedAt: NOW }, savedContent: 'a', draftContent: 'b',
    revisionId: 'rev-1', dirty: true }
  await assert.rejects(() => flows.saveChapter(opened), /timeout/)
  await assert.rejects(() => flows.saveChapter(opened), /crossed its chapter identity/)
  assert.deepEqual(commands[0], commands[1])
})

test('binds a saved Revision result to actor, source Candidate, and payload schema', async (t) => {
  const drifts = [
    { name: 'created_by', patch: { created_by: 'other-user' } },
    { name: 'source_candidate_id', patch: { source_candidate_id: 'candidate-other' } },
    { name: 'payload_schema', patch: { payload_schema: 'core.other/v1' } },
  ] as const
  for (const drift of drifts) {
    await t.test(drift.name, async () => {
      const flows = createCoreFlows({ ids: ids(), createdBy: 'user-a', gateway: gateway((_route, request) => {
        if (request.schema !== 'core-document-revision-create-command/v1') throw new Error('wrong command')
        return { schema: 'core-revision/v1', revision_id: request.revision_id, workspace_id: request.workspace_id,
          document_id: request.document_id, node_id: null, parent_revision_id: request.base_revision_id,
          content_hash: '3'.repeat(64), created_by: request.created_by, source_candidate_id: request.source_candidate_id,
          created_at: NOW, revision_number: 2, payload_schema: request.payload_schema, ...drift.patch }
      }) })
      const opened: CoreOpenedChapter = { chapter: { documentId: 'doc-1', workspaceId: 'ws-1', title: '第一章', displayIndex: 1,
        revision: 1, currentRevisionId: 'rev-1', createdAt: NOW, updatedAt: NOW }, savedContent: 'a', draftContent: 'b',
        revisionId: 'rev-1', dirty: true }
      await assert.rejects(() => flows.saveChapter(opened), /crossed its chapter identity/)
    })
  }
})

test('rejects a delete result whose CAS receipt is not exact', async () => {
  const flows = createCoreFlows({ ids: ids(), gateway: gateway((_route, request) => {
    if (request.schema !== 'core-workspace-delete-command/v1') throw new Error('wrong command')
    return { schema: 'core-delete-result/v1', operation_key: request.operation_key, workspace_id: request.workspace_id,
      entity_kind: 'workspace', entity_id: request.workspace_id, previous_revision: request.expected_revision + 1,
      deleted: true, idempotent: false }
  }) })
  await assert.rejects(() => flows.deleteProject({ workspaceId: 'ws-1', title: 'x', status: 'active', revision: 2,
    createdAt: NOW, updatedAt: NOW }), /not bound/)
})
