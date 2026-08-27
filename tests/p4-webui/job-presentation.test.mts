import assert from 'node:assert/strict'
import test from 'node:test'
import {
  canCancelJob,
  canResumeJob,
  jobProgress,
  presentCapabilityStatus,
  presentJobState,
  type JobDrawerItem,
} from '../../frontend/src/core/jobPresentation.ts'

test('calculates progress from the Host-only drawer view model', () => {
  const item: JobDrawerItem = { job_id: 'job-1', workspace_id: 'ws-1', job_state: 'queued', job_revision: 1,
    steps: [{ step_id: 'step-1', state: 'pending', revision: 1 }], attempts: [], job_event_high_water: 0 }
  assert.deepEqual(jobProgress(item), { completed: 0, total: 1 })
  assert.equal(presentJobState(item.job_state).label, '等待中')
  assert.equal(canCancelJob(item.job_state), true)
})

test('presents every fixed capability state explicitly', () => {
  const states = ['ready', 'running', 'disabled', 'missing', 'error'] as const
  assert.deepEqual(states.map(state => presentCapabilityStatus(state).label), [
    'Ready', 'Running', 'Disabled', 'Missing', 'Error',
  ])
})

test('only exposes contract-valid resume and cancel affordances', () => {
  assert.equal(canResumeJob('needs_attention'), true)
  assert.equal(canResumeJob('succeeded'), false)
  assert.equal(canCancelJob('running'), true)
  assert.equal(canCancelJob('cancelling'), false)
})
