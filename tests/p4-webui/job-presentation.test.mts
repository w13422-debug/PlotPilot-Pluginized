import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import {
  canCancelJob,
  canResumeJob,
  jobProgress,
  presentCapabilityStatus,
  presentJobState,
  type JobSnapshotV1,
} from '../../frontend/src/core/jobPresentation.ts'

const fixturePath = new URL('../../contracts/examples/fixtures/job-snapshot.json', import.meta.url)

test('consumes the P0 typed job snapshot fixture without reinterpretation', async () => {
  const fixture = JSON.parse(await readFile(fixturePath, 'utf8')) as JobSnapshotV1
  assert.equal(fixture.schema, 'job-snapshot/v1')
  assert.deepEqual(jobProgress(fixture), { completed: 0, total: 1 })
  assert.equal(presentJobState(fixture.job_state).label, '等待中')
  assert.equal(canCancelJob(fixture.job_state), true)
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
