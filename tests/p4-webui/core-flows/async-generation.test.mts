import assert from 'node:assert/strict'
import test from 'node:test'
import {
  reconcilePartialDeleteSelection,
  ScopedRequestGeneration,
} from '../../../frontend/src/core/flows/asyncGeneration.ts'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

test('A to B to C transitions reject every older Workspace generation', () => {
  const guard = new ScopedRequestGeneration()
  const workspaceA = guard.begin('workspace-a')
  const workspaceB = guard.begin('workspace-b')
  const workspaceC = guard.begin('workspace-c')

  assert.equal(guard.isCurrent(workspaceA), false)
  assert.equal(guard.isCurrent(workspaceB), false)
  assert.equal(guard.isCurrent(workspaceC), true)
  assert.deepEqual(guard.capture(), workspaceC)
})

test('explicit invalidation makes a pending load, open, or save response stale', () => {
  const guard = new ScopedRequestGeneration()
  const pending = guard.begin('workspace-a')
  guard.invalidate()

  assert.equal(guard.isCurrent(pending), false)
  assert.equal(guard.capture(), null)
})

test('late A and B responses cannot overwrite a completed C Workspace response', async () => {
  const guard = new ScopedRequestGeneration()
  const a = deferred<string>()
  const b = deferred<string>()
  const c = deferred<string>()
  let committed = ''

  async function load(scope: string, response: Promise<string>) {
    const token = guard.begin(scope)
    const value = await response
    if (guard.isCurrent(token)) committed = value
  }

  const pendingA = load('workspace-a', a.promise)
  const pendingB = load('workspace-b', b.promise)
  const pendingC = load('workspace-c', c.promise)
  c.resolve('C')
  await pendingC
  a.resolve('A')
  b.resolve('B')
  await Promise.all([pendingA, pendingB])

  assert.equal(committed, 'C')
})

test('only the latest project-list refresh may commit or report success', async () => {
  const guard = new ScopedRequestGeneration()
  const first = deferred<string>()
  const second = deferred<string>()
  const commits: string[] = []

  async function refresh(response: Promise<string>) {
    const token = guard.begin('project-list')
    const value = await response
    if (!guard.isCurrent(token)) return false
    commits.push(value)
    return true
  }

  const oldRefresh = refresh(first.promise)
  const latestRefresh = refresh(second.promise)
  second.resolve('latest')
  assert.equal(await latestRefresh, true)
  first.resolve('stale')
  assert.equal(await oldRefresh, false)
  assert.deepEqual(commits, ['latest'])
})

test('partial delete reconciliation retains only failed authoritative selections', () => {
  const selected = ['workspace-ok', 'workspace-failed', 'workspace-gone', 'workspace-failed']
  const succeeded = new Set(['workspace-ok'])
  const authoritative = new Set(['workspace-failed', 'workspace-other'])

  assert.deepEqual(
    reconcilePartialDeleteSelection(selected, succeeded, authoritative),
    ['workspace-failed'],
  )
})

test('partial delete reconciliation covers all-success and all-failure boundaries', () => {
  const selected = ['workspace-a', 'workspace-b']
  assert.deepEqual(
    reconcilePartialDeleteSelection(selected, new Set(selected), new Set()),
    [],
  )
  assert.deepEqual(
    reconcilePartialDeleteSelection(selected, new Set(), new Set(selected)),
    selected,
  )
})
