import type { JobSnapshot } from '../../contracts/types'
import { isTerminalJobState, toJobDrawerItem, type JobConnectionState, type JobDrawerItem } from './types.ts'

export type SnapshotApplyResult = 'inserted' | 'replaced' | 'duplicate' | 'stale'
export type EventApplyResult = 'applied' | 'duplicate' | 'gap'

type Listener = () => void

function assertSnapshotIdentity(snapshot: Readonly<JobSnapshot>, workspaceId?: string): void {
  if (workspaceId != null && snapshot.workspace_id !== workspaceId) {
    throw new Error(`job snapshot workspace mismatch: expected ${workspaceId}, received ${snapshot.workspace_id}`)
  }
  if (!Number.isInteger(snapshot.job_revision) || snapshot.job_revision < 1) {
    throw new Error('job snapshot revision must be a positive integer')
  }
  if (!Number.isInteger(snapshot.job_event_high_water) || snapshot.job_event_high_water < 0) {
    throw new Error('job event high-water must be a non-negative integer')
  }
  if (new Set(snapshot.steps.map(step => step.step_id)).size !== snapshot.steps.length) {
    throw new Error('job snapshot step IDs must be unique')
  }
  if (new Set(snapshot.attempts.map(attempt => attempt.attempt_id)).size !== snapshot.attempts.length) {
    throw new Error('job snapshot attempt IDs must be unique')
  }
}

function cloneSnapshot(snapshot: Readonly<JobSnapshot>): Readonly<JobSnapshot> {
  return Object.freeze({
    ...snapshot,
    steps: Object.freeze(snapshot.steps.map(step => Object.freeze({ ...step }))),
    attempts: Object.freeze(snapshot.attempts.map(attempt => Object.freeze({ ...attempt }))),
    candidate_ids: Object.freeze([...snapshot.candidate_ids]),
    stream_high_waters: Object.freeze(snapshot.stream_high_waters.map(stream => Object.freeze({
      ...stream,
      target: Object.freeze({ ...stream.target }),
    }))),
  }) as Readonly<JobSnapshot>
}

function classifySnapshotPosition(
  current: Readonly<JobSnapshot> | undefined,
  incoming: Readonly<JobSnapshot>,
  minimumCoveredCursor: number,
): SnapshotApplyResult {
  if (current == null) return incoming.job_event_high_water < minimumCoveredCursor ? 'stale' : 'inserted'
  if (incoming.job_revision < current.job_revision
    || incoming.job_event_high_water < current.job_event_high_water) return 'stale'
  if (incoming.job_revision === current.job_revision
    && incoming.job_event_high_water === current.job_event_high_water) {
    if (incoming.snapshot_hash !== current.snapshot_hash) {
      throw new Error('conflicting job snapshots share the same revision and event high-water')
    }
    return incoming.job_event_high_water < minimumCoveredCursor ? 'stale' : 'duplicate'
  }
  return incoming.job_event_high_water < minimumCoveredCursor ? 'stale' : 'replaced'
}

/** Authoritative in-memory projection. Snapshot application always replaces, never patches. */
export class JobDrawerStore {
  private workspaceId: string | undefined
  private readonly snapshots = new Map<string, Readonly<JobSnapshot>>()
  private readonly observedCursors = new Map<string, number>()
  private readonly connections = new Map<string, JobConnectionState>()
  private readonly listeners = new Set<Listener>()

  setWorkspace(workspaceId: string): void {
    if (this.workspaceId != null && this.workspaceId !== workspaceId) this.clear()
    this.workspaceId = workspaceId
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  items(): JobDrawerItem[] {
    return [...this.snapshots.values()]
      .map(toJobDrawerItem)
      .sort((left, right) => right.created_at.localeCompare(left.created_at) || left.job_id.localeCompare(right.job_id))
  }

  activeJobIds(): string[] {
    return [...this.snapshots.values()]
      .filter(snapshot => !isTerminalJobState(snapshot.job_state))
      .map(snapshot => snapshot.job_id)
      .sort()
  }

  snapshot(jobId: string): Readonly<JobSnapshot> | undefined {
    return this.snapshots.get(jobId)
  }

  /** Highest contiguous event observed, whether or not a Snapshot covers it yet. */
  observedCursor(jobId: string): number {
    return this.observedCursors.get(jobId) ?? this.projectionCursor(jobId)
  }

  /** Durable cursor covered by the currently rendered authoritative Snapshot. */
  projectionCursor(jobId: string): number {
    return this.snapshots.get(jobId)?.job_event_high_water ?? 0
  }

  /** Compatibility alias for callers that need the observed stream position. */
  cursor(jobId: string): number {
    return this.observedCursor(jobId)
  }

  connection(jobId: string): JobConnectionState {
    return this.connections.get(jobId) ?? 'detached'
  }

  connectionStates(): Readonly<Record<string, JobConnectionState>> {
    return Object.freeze(Object.fromEntries(this.connections))
  }

  replaceSnapshot(snapshot: Readonly<JobSnapshot>, minimumCoveredCursor = this.observedCursor(snapshot.job_id)): SnapshotApplyResult {
    assertSnapshotIdentity(snapshot, this.workspaceId)
    const current = this.snapshots.get(snapshot.job_id)
    const result = classifySnapshotPosition(current, snapshot, minimumCoveredCursor)
    if (result === 'duplicate' || result === 'stale') return result
    this.snapshots.set(snapshot.job_id, cloneSnapshot(snapshot))
    this.observedCursors.set(
      snapshot.job_id,
      Math.max(this.observedCursor(snapshot.job_id), snapshot.job_event_high_water),
    )
    this.emit()
    return result
  }

  /** Replace one workspace listing atomically; absent records are intentionally removed. */
  replaceWorkspaceSnapshots(workspaceId: string, snapshots: ReadonlyArray<Readonly<JobSnapshot>>): void {
    const ids = new Set<string>()
    const nextSnapshots = new Map<string, Readonly<JobSnapshot>>()
    const nextObservedCursors = new Map<string, number>()
    for (const snapshot of snapshots) {
      assertSnapshotIdentity(snapshot, workspaceId)
      if (ids.has(snapshot.job_id)) throw new Error('job discovery returned duplicate job IDs')
      ids.add(snapshot.job_id)
      const current = this.snapshots.get(snapshot.job_id)
      const observedCursor = this.observedCursor(snapshot.job_id)
      if (classifySnapshotPosition(current, snapshot, observedCursor) === 'stale') {
        throw new Error(`job discovery returned stale snapshot for ${snapshot.job_id}`)
      }
      nextSnapshots.set(snapshot.job_id, cloneSnapshot(snapshot))
      nextObservedCursors.set(snapshot.job_id, Math.max(observedCursor, snapshot.job_event_high_water))
    }
    this.workspaceId = workspaceId
    this.snapshots.clear()
    this.observedCursors.clear()
    for (const [jobId, snapshot] of nextSnapshots) this.snapshots.set(jobId, snapshot)
    for (const [jobId, cursor] of nextObservedCursors) this.observedCursors.set(jobId, cursor)
    for (const jobId of this.connections.keys()) {
      if (!ids.has(jobId)) this.connections.delete(jobId)
    }
    this.emit()
  }

  recordEvent(jobId: string, sequence: number): EventApplyResult {
    if (!this.snapshots.has(jobId)) throw new Error(`event references unknown job ${jobId}`)
    if (!Number.isInteger(sequence) || sequence < 1) throw new Error('job event sequence must be a positive integer')
    const cursor = this.observedCursor(jobId)
    if (sequence <= cursor) return 'duplicate'
    if (sequence !== cursor + 1) return 'gap'
    this.observedCursors.set(jobId, sequence)
    this.emit()
    return 'applied'
  }

  setConnection(jobId: string, state: JobConnectionState): void {
    if (!this.snapshots.has(jobId) && state !== 'detached') return
    if (this.connections.get(jobId) === state) return
    if (state === 'detached') this.connections.delete(jobId)
    else this.connections.set(jobId, state)
    this.emit()
  }

  clear(): void {
    this.snapshots.clear()
    this.observedCursors.clear()
    this.connections.clear()
    this.emit()
  }

  private emit(): void {
    for (const listener of this.listeners) listener()
  }
}
