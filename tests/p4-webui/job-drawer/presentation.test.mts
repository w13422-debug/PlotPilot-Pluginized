import assert from 'node:assert/strict'
import test from 'node:test'
import {
  canCancelJob,
  canResumeJob,
  canRetryJob,
  jobActionAvailability,
  jobProgress,
  jobProgressPercentage,
  presentAttemptState,
  presentJobState,
  presentStepState,
} from '../../../frontend/src/core/jobPresentation.ts'
import { toJobDrawerItem } from '../../../frontend/src/core/jobs/types.ts'
import { snapshot, withState } from './fixtures.ts'

test('presents all ten frozen Job states without folding partial or needs_attention into failed', () => {
  const states = ['queued', 'running', 'waiting_user', 'paused', 'cancelling', 'succeeded', 'partial', 'failed', 'cancelled', 'needs_attention'] as const
  const labels = states.map(state => presentJobState(state).label)
  assert.equal(new Set(labels).size, states.length)
  assert.equal(presentJobState('partial').label, '部分完成')
  assert.equal(presentJobState('needs_attention').label, '需要处理')
  assert.equal(presentJobState('failed').tone, 'error')
})

test('calculates progress for empty, mixed, partial and unknown Step states', () => {
  assert.deepEqual(jobProgress({ steps: [] }), { completed: 0, total: 0 })
  assert.equal(jobProgressPercentage({ steps: [] }), 0)
  const mixed = { steps: [
    { step_id: '1', state: 'succeeded', revision: 1 },
    { step_id: '2', state: 'partial', revision: 1 },
    { step_id: '3', state: 'needs_attention', revision: 1 },
    { step_id: '4', state: 'future_state', revision: 1 },
  ] }
  assert.deepEqual(jobProgress(mixed), { completed: 2, total: 4 })
  assert.equal(jobProgressPercentage(mixed), 50)
  assert.equal(presentStepState('future_state').label, 'future_state')
  assert.equal(presentAttemptState('future_attempt').label, 'future_attempt')
})

test('exposes independent retry, resume and cancel affordances without optimistic state mutation', () => {
  assert.equal(canCancelJob('running'), true)
  assert.equal(canCancelJob('cancelling'), false)
  assert.equal(canResumeJob('needs_attention'), true)
  assert.equal(canResumeJob('succeeded'), false)
  assert.equal(canRetryJob('failed'), true)
  assert.equal(canRetryJob('running'), false)

  const failed = toJobDrawerItem(withState('failed', { current_checkpoint_id: 'checkpoint-1' }))
  assert.deepEqual(jobActionAvailability(failed), { cancel: false, resume: true, retry: true })
  const pausedWithoutCheckpoint = toJobDrawerItem(withState('paused'))
  assert.deepEqual(jobActionAvailability(pausedWithoutCheckpoint), { cancel: true, resume: false, retry: false })
  assert.equal(snapshot().job_state, 'running')
})
