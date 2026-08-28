import type { JobSnapshot, JobState } from '../../../frontend/src/contracts/types.ts'

export function snapshot(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  const revision = overrides.job_revision ?? 1
  const highWater = overrides.job_event_high_water ?? 0
  return {
    schema: 'job-snapshot/v1',
    job_id: 'job-1',
    workspace_id: 'ws-1',
    job_state: 'running',
    job_revision: revision,
    steps: [{ step_id: 'step-1', state: 'running', revision: 1 }],
    attempts: [{ attempt_id: 'attempt-1', state: 'running', lease_epoch: 1 }],
    candidate_ids: [],
    current_checkpoint_id: null,
    stream_high_waters: [],
    core_event_high_water: 0,
    job_event_high_water: highWater,
    created_at: '2026-08-28T00:00:00Z',
    snapshot_hash: `${(revision + highWater) % 10}`.repeat(64),
    ...overrides,
  }
}

export function withState(state: JobState, overrides: Partial<JobSnapshot> = {}): JobSnapshot {
  return snapshot({ job_state: state, ...overrides })
}
