import assert from 'node:assert/strict'
import test from 'node:test'
import { JobDrawerStore } from '../../../frontend/src/core/jobs/store.ts'
import { snapshot, withState } from './fixtures.ts'

test('authoritative snapshots replace nested Step and Attempt collections instead of patching them', () => {
  const store = new JobDrawerStore()
  store.setWorkspace('ws-1')
  const first = snapshot({
    steps: [{ step_id: 'old-step', state: 'running', revision: 1 }],
    attempts: [{ attempt_id: 'old-attempt', state: 'running', lease_epoch: 1 }],
  })
  assert.equal(store.replaceSnapshot(first), 'inserted')
  first.steps[0]!.state = 'mutated-after-insert'
  const replacement = snapshot({
    job_revision: 2,
    job_event_high_water: 1,
    snapshot_hash: '2'.repeat(64),
    steps: [{ step_id: 'new-step', state: 'partial', revision: 2 }],
    attempts: [{ attempt_id: 'new-attempt', state: 'created', lease_epoch: 2 }],
  })
  assert.equal(store.replaceSnapshot(replacement), 'replaced')
  assert.deepEqual(store.items()[0]!.steps.map(step => step.step_id), ['new-step'])
  assert.deepEqual(store.items()[0]!.attempts.map(attempt => attempt.attempt_id), ['new-attempt'])
  assert.equal(Object.isFrozen(store.snapshot('job-1')), true)
  assert.equal(Object.isFrozen(store.snapshot('job-1')!.steps[0]), true)
})

test('enforces monotonic cursor, duplicate idempotence and explicit gap detection', () => {
  const store = new JobDrawerStore()
  store.replaceWorkspaceSnapshots('ws-1', [snapshot({ job_event_high_water: 4 })])
  assert.equal(store.recordEvent('job-1', 4), 'duplicate')
  assert.equal(store.recordEvent('job-1', 5), 'applied')
  assert.equal(store.cursor('job-1'), 5)
  assert.equal(store.observedCursor('job-1'), 5)
  assert.equal(store.projectionCursor('job-1'), 4)
  assert.equal(store.recordEvent('job-1', 7), 'gap')
  assert.equal(store.cursor('job-1'), 5)
  assert.equal(store.replaceSnapshot(snapshot({ job_revision: 2, job_event_high_water: 4 })), 'stale')
  assert.equal(store.replaceSnapshot(snapshot({ job_revision: 2, job_event_high_water: 5, snapshot_hash: '7'.repeat(64) })), 'replaced')
  assert.equal(store.projectionCursor('job-1'), 5)
})

test('keeps observed and projection cursors separate while burst Snapshots advance incrementally', () => {
  const store = new JobDrawerStore()
  store.replaceWorkspaceSnapshots('ws-1', [snapshot({ job_event_high_water: 0 })])
  assert.equal(store.recordEvent('job-1', 1), 'applied')
  assert.equal(store.recordEvent('job-1', 2), 'applied')
  assert.equal(store.observedCursor('job-1'), 2)
  assert.equal(store.projectionCursor('job-1'), 0)

  assert.equal(store.replaceSnapshot(snapshot({
    job_revision: 2,
    job_event_high_water: 1,
    snapshot_hash: '3'.repeat(64),
  }), 1), 'replaced')
  assert.equal(store.observedCursor('job-1'), 2)
  assert.equal(store.projectionCursor('job-1'), 1)
  assert.equal(store.replaceSnapshot(snapshot({
    job_revision: 3,
    job_event_high_water: 2,
    snapshot_hash: '5'.repeat(64),
  }), 2), 'replaced')
  assert.equal(store.projectionCursor('job-1'), 2)
})

test('rejects identity conflicts and same-position hash conflicts', () => {
  const store = new JobDrawerStore()
  store.setWorkspace('ws-1')
  assert.throws(() => store.replaceSnapshot(snapshot({ workspace_id: 'ws-2' })), /workspace mismatch/)
  assert.throws(() => store.replaceSnapshot(snapshot({
    steps: [{ step_id: 'duplicate', state: 'running', revision: 1 }, { step_id: 'duplicate', state: 'pending', revision: 1 }],
  })), /step IDs must be unique/)
  store.replaceSnapshot(snapshot())
  assert.throws(() => store.replaceSnapshot(snapshot({ snapshot_hash: 'f'.repeat(64) })), /conflicting job snapshots/)
})

test('refresh listing is atomic, removes absent jobs and reattaches every backend-active state', () => {
  const store = new JobDrawerStore()
  const activeStates = ['queued', 'running', 'waiting_user', 'paused', 'cancelling', 'needs_attention'] as const
  const jobs = activeStates.map((state, index) => withState(state, {
    job_id: `job-${index}`,
    snapshot_hash: `${index}`.repeat(64),
  }))
  jobs.push(withState('partial', { job_id: 'job-partial', snapshot_hash: 'a'.repeat(64) }))
  jobs.push(withState('failed', { job_id: 'job-failed', snapshot_hash: 'b'.repeat(64) }))
  store.replaceWorkspaceSnapshots('ws-1', jobs)
  assert.deepEqual(store.activeJobIds(), activeStates.map((_, index) => `job-${index}`).sort())
  store.replaceWorkspaceSnapshots('ws-1', [jobs[0]!])
  assert.deepEqual(store.items().map(job => job.job_id), ['job-0'])

  assert.throws(() => store.replaceWorkspaceSnapshots('ws-1', [snapshot(), snapshot()]), /duplicate job IDs/)
  assert.deepEqual(store.items().map(job => job.job_id), ['job-0'])
})

test('workspace listing rejects same-position hash conflict atomically and preserves prior state', () => {
  const store = new JobDrawerStore()
  const original = snapshot({ snapshot_hash: 'a'.repeat(64) })
  const sibling = snapshot({ job_id: 'job-2', snapshot_hash: 'b'.repeat(64) })
  store.replaceWorkspaceSnapshots('ws-1', [original, sibling])
  store.setConnection('job-1', 'connected')
  const beforeItems = store.items()
  const beforeConnections = store.connectionStates()

  assert.throws(() => store.replaceWorkspaceSnapshots('ws-1', [
    snapshot({ snapshot_hash: 'c'.repeat(64) }),
  ]), /conflicting job snapshots/)
  assert.deepEqual(store.items(), beforeItems)
  assert.deepEqual(store.connectionStates(), beforeConnections)
  assert.equal(store.snapshot('job-1')!.snapshot_hash, 'a'.repeat(64))
  assert.equal(store.snapshot('job-2')!.snapshot_hash, 'b'.repeat(64))

  store.replaceWorkspaceSnapshots('ws-1', [snapshot({ snapshot_hash: 'a'.repeat(64) })])
  assert.deepEqual(store.items().map(item => item.job_id), ['job-1'])
})
