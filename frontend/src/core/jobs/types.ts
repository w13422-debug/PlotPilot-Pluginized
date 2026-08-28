import type { JobSnapshot, JobState } from '../../contracts/types'

export type { JobSnapshot, JobState }

export type JobAction = 'retry' | 'resume' | 'cancel'

export type JobConnectionState =
  | 'detached'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'needs_snapshot'
  | 'error'

/**
 * Host-only drawer model derived from a verified job-snapshot/v1 value.
 * It deliberately does not masquerade as a public wire contract.
 */
export interface JobDrawerItem {
  job_id: string
  workspace_id: string
  job_state: JobState
  job_revision: number
  steps: ReadonlyArray<{ step_id: string; state: string; revision: number }>
  attempts: ReadonlyArray<{ attempt_id: string; state: string; lease_epoch: number }>
  candidate_ids: ReadonlyArray<string>
  current_checkpoint_id: string | null
  core_event_high_water: number
  job_event_high_water: number
  created_at: string
  source_snapshot_hash: string
}

export interface JobEventCursor {
  stream_kind: 'job_event'
  job_id: string
  job_event_seq: number
}

/** Frozen sse-recovery/v1 fields consumed by the drawer adapter boundary. */
export interface JobSseRecovery {
  schema: 'sse-recovery/v1'
  stream_kind: 'job_event'
  aggregate_id: string | null
  requested_after_seq: number
  replay_floor_seq: number
  durable_high_water_seq: number
  gap: boolean
  snapshot_required: boolean
  snapshot_schema: string | null
  snapshot_revision: number | null
  snapshot_asset_id: string | null
  snapshot_hash: string | null
}

export type JobStreamSignal =
  | { type: 'event'; cursor: JobEventCursor }
  | { type: 'recovery'; recovery: JobSseRecovery }

export interface JobStreamSubscription {
  close(): void
}

export interface JobStreamHandlers {
  signal(signal: JobStreamSignal): void
  closed(error?: unknown): void
}

export interface JobSnapshotReadContext {
  recovery?: JobSseRecovery
}

/**
 * Integration port only. P3/P0 assembly must return values already accepted by
 * the frozen TypeScript ingress verifier; this slice creates no second parser.
 */
export interface JobDrawerGateway {
  discover(workspaceId: string): Promise<ReadonlyArray<Readonly<JobSnapshot>>>
  readSnapshot(jobId: string, context?: JobSnapshotReadContext): Promise<Readonly<JobSnapshot>>
  connect(
    jobId: string,
    afterJobEventSeq: number,
    handlers: JobStreamHandlers,
  ): JobStreamSubscription | Promise<JobStreamSubscription>
  act(action: JobAction, jobId: string): Promise<Readonly<JobSnapshot> | void>
}

export interface JobDrawerScheduler {
  set(delayMs: number, callback: () => void): unknown
  clear(handle: unknown): void
}

export interface JobDrawerControllerOptions {
  reconnectBaseDelayMs?: number
  reconnectMaxDelayMs?: number
  scheduler?: JobDrawerScheduler
}

export function toJobDrawerItem(snapshot: Readonly<JobSnapshot>): JobDrawerItem {
  return {
    job_id: snapshot.job_id,
    workspace_id: snapshot.workspace_id,
    job_state: snapshot.job_state,
    job_revision: snapshot.job_revision,
    steps: snapshot.steps.map(step => ({ ...step })),
    attempts: snapshot.attempts.map(attempt => ({ ...attempt })),
    candidate_ids: [...snapshot.candidate_ids],
    current_checkpoint_id: snapshot.current_checkpoint_id,
    core_event_high_water: snapshot.core_event_high_water,
    job_event_high_water: snapshot.job_event_high_water,
    created_at: snapshot.created_at,
    source_snapshot_hash: snapshot.snapshot_hash,
  }
}

export function isTerminalJobState(state: JobState): boolean {
  return state === 'succeeded' || state === 'failed' || state === 'cancelled' || state === 'partial'
}
