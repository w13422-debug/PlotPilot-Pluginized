import assert from 'node:assert/strict'
import test from 'node:test'
import { CoreFlowHttpError, createCoreHttpFlowGateway } from '../../../frontend/src/core/flows/gateway.ts'

const NOW = '2026-08-28T00:00:00Z'

test('derives GET path/query from the frozen method matrix and validates the response schema', async () => {
  let capturedUrl = ''
  let capturedInit: RequestInit | undefined
  const gateway = createCoreHttpFlowGateway({ baseUrl: '/root/', fetch: async (input, init) => {
    capturedUrl = String(input); capturedInit = init
    return Response.json({ schema: 'core-document-page/v1', items: [], offset: 0, limit: 100, total: 0, next_offset: null })
  } })
  const result = await gateway.request('document.list', {
    schema: 'core-document-query/v1', workspace_id: 'ws-1', document_id: null, offset: 0, limit: 100,
  })
  assert.equal(result.schema, 'core-document-page/v1')
  assert.equal(capturedUrl, '/root/api/v1/core/workspaces/ws-1/documents?offset=0&limit=100')
  assert.equal(capturedInit?.method, 'GET')
})

test('sends the complete typed revision command and rejects result-schema drift', async () => {
  let body = ''
  const gateway = createCoreHttpFlowGateway({ fetch: async (_input, init) => {
    body = String(init?.body)
    return Response.json({ schema: 'core-document/v1', document_id: 'doc-1', workspace_id: 'ws-1', document_type: 'core.chapter',
      title: 'Chapter', current_revision_id: null, created_at: NOW, updated_at: NOW, revision: 0 }, { status: 201 })
  } })
  const command = { schema: 'core-document-revision-create-command/v1' as const, operation_key: 'op-1', revision_id: 'rev-1',
    workspace_id: 'ws-1', document_id: 'doc-1', base_revision_id: null, content: '正文', created_by: 'user-a',
    source_candidate_id: null, payload_schema: 'core.document-text/v1' }
  await assert.rejects(() => gateway.request('document.revision.create', command), /expected core-revision\/v1/)
  assert.deepEqual(JSON.parse(body), command)
})

test('turns a frozen Core error response into a typed flow error', async () => {
  const gateway = createCoreHttpFlowGateway({ fetch: async () => Response.json({ schema: 'core-http-error/v1',
    error_code: 'stale_cas', message: 'stale', retryable: false }, { status: 409 }) })
  await assert.rejects(
    () => gateway.request('workspace.delete', { schema: 'core-workspace-delete-command/v1', operation_key: 'op-1',
      workspace_id: 'ws-1', expected_revision: 2 }),
    (error: unknown) => error instanceof CoreFlowHttpError && error.status === 409 && error.errorCode === 'stale_cas',
  )
})

test('rejects a request whose schema does not match the selected route', async () => {
  const gateway = createCoreHttpFlowGateway({ fetch: async () => { throw new Error('must not fetch') } })
  await assert.rejects(() => gateway.request('workspace.get', {
    schema: 'core-workspace-query/v1', workspace_id: null, offset: 0, limit: 100,
  }), /requires core-workspace-get-query\/v1/)
})

test('requires the exact success status declared by the frozen matrix', async () => {
  const gateway = createCoreHttpFlowGateway({ fetch: async () => Response.json({ schema: 'core-workspace/v1',
    workspace_id: 'ws-1', workspace_kind: 'WritingProject', title: 'Project', status: 'active', current_plan_revision_id: null,
    created_at: NOW, updated_at: NOW, revision: 0 }) })
  await assert.rejects(() => gateway.request('workspace.create', { schema: 'core-workspace-create-command/v1',
    operation_key: 'op-1', workspace_id: 'ws-1', workspace_kind: 'WritingProject', title: 'Project' }),
  /undeclared success status 200/)
})

test('rejects an undeclared failure status/error-code pair', async () => {
  const gateway = createCoreHttpFlowGateway({ fetch: async () => Response.json({ schema: 'core-http-error/v1',
    error_code: 'stale_cas', message: 'stale', retryable: false }, { status: 418 }) })
  await assert.rejects(() => gateway.request('workspace.delete', { schema: 'core-workspace-delete-command/v1',
    operation_key: 'op-1', workspace_id: 'ws-1', expected_revision: 1 }), /undeclared failure/)
})
