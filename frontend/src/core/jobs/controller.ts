import { jobActionAvailability } from '../jobPresentation.ts'
import { parseJobEventCursor, parseJobSseRecovery } from './ingress.ts'
import { JobDrawerStore } from './store.ts'
import {
  isTerminalJobState,
  type JobAction,
  type JobDrawerControllerOptions,
  type JobDrawerGateway,
  type JobDrawerScheduler,
  type JobSnapshot,
  type JobSseRecovery,
  type JobStreamSignal,
  type JobStreamSubscription,
} from './types.ts'

interface ConnectionSlot {
  generation: number
  reconnectAttempt: number
  serial: number
  handle?: JobStreamSubscription
  timer?: unknown
}

interface ConnectionToken {
  generation: number
  jobId: string
  serial: number
}

interface ActionEntry {
  action: JobAction
  generation: number
  token: number
}

interface SnapshotContinuation {
  generation: number
  barrier: number
  connection?: ConnectionToken
  actionToken?: number
}

interface SnapshotReadEntry {
  generation: number
  barrier: number
  continuation: SnapshotContinuation
  requiredCoverage: number
  promise: Promise<void>
}

const DEFAULT_SCHEDULER: JobDrawerScheduler = {
  set: (delayMs, callback) => globalThis.setTimeout(callback, delayMs),
  clear: handle => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
}

/**
 * Source-only coordinator. Network/RPC details stay behind JobDrawerGateway so
 * P3/P0 can bind their accepted modules without router or shared-store edits.
 */
export class JobDrawerController {
  readonly store: JobDrawerStore
  private readonly gateway: JobDrawerGateway
  private readonly scheduler: JobDrawerScheduler
  private readonly reconnectBaseDelayMs: number
  private readonly reconnectMaxDelayMs: number
  private readonly slots = new Map<string, ConnectionSlot>()
  private readonly snapshotReads = new Map<string, SnapshotReadEntry>()
  private readonly readBarriers = new Map<string, number>()
  private readonly actions = new Map<string, ActionEntry>()
  private workspaceId: string | undefined
  private generation = 0
  private refreshOrdinal = 0
  private actionOrdinal = 0
  private started = false

  constructor(gateway: JobDrawerGateway, store = new JobDrawerStore(), options: JobDrawerControllerOptions = {}) {
    this.gateway = gateway
    this.store = store
    this.scheduler = options.scheduler ?? DEFAULT_SCHEDULER
    this.reconnectBaseDelayMs = Math.max(0, options.reconnectBaseDelayMs ?? 250)
    this.reconnectMaxDelayMs = Math.max(this.reconnectBaseDelayMs, options.reconnectMaxDelayMs ?? 8_000)
  }

  pendingAction(jobId: string): JobAction | undefined {
    return this.actions.get(jobId)?.action
  }

  async start(workspaceId: string): Promise<void> {
    this.stop()
    this.started = true
    this.workspaceId = workspaceId
    this.store.setWorkspace(workspaceId)
    await this.refresh()
  }

  /** Refresh is the reattachment gate used after page/component reload. */
  async refresh(): Promise<void> {
    if (!this.started || this.workspaceId == null) throw new Error('job drawer controller is not started')
    const generation = this.generation
    const workspaceId = this.workspaceId
    const ordinal = ++this.refreshOrdinal
    const snapshots = await this.gateway.discover(workspaceId)
    if (!this.isCurrent(generation) || this.workspaceId !== workspaceId || ordinal !== this.refreshOrdinal) return
    this.store.replaceWorkspaceSnapshots(workspaceId, snapshots)
    this.reconcileConnections(generation)
  }

  stop(): void {
    this.started = false
    this.workspaceId = undefined
    this.generation += 1
    this.refreshOrdinal += 1
    for (const jobId of [...this.slots.keys()]) this.detach(jobId)
    this.snapshotReads.clear()
    // Actions deliberately survive a stop/start boundary until their own token
    // settles, preserving per-Job serialization across controller generations.
  }

  async act(action: JobAction, jobId: string): Promise<void> {
    const snapshot = this.store.snapshot(jobId)
    if (snapshot == null) throw new Error(`cannot ${action} unknown job ${jobId}`)
    if (this.actions.has(jobId)) throw new Error(`job ${jobId} already has an action in flight`)
    const availability = jobActionAvailability(snapshot)
    if (!availability[action]) throw new Error(`${action} is not available for job ${jobId}`)
    const entry: ActionEntry = { action, generation: this.generation, token: ++this.actionOrdinal }
    this.actions.set(jobId, entry)
    try {
      const replacement = await this.gateway.act(action, jobId)
      if (!this.isActionCurrent(jobId, entry)) return

      // Fence every read that began before the action completed. A returned
      // Snapshot is itself causally post-action; void responses force a fresh
      // authoritative read that cannot reuse an older Promise.
      const barrier = this.advanceReadBarrier(jobId)
      if (replacement != null) {
        this.requireAppliedSnapshot(jobId, replacement, this.store.observedCursor(jobId))
      } else {
        await this.requestConvergence(
          jobId,
          this.store.observedCursor(jobId),
          { generation: entry.generation, barrier, actionToken: entry.token },
          { forceFresh: true },
        )
      }
      if (!this.isActionCurrent(jobId, entry)) return
      this.reconcileConnections(entry.generation)
      this.markProjectionCovered(jobId, entry.generation)
    } finally {
      if (this.actions.get(jobId)?.token === entry.token) this.actions.delete(jobId)
    }
  }

  private reconcileConnections(generation: number): void {
    if (!this.isCurrent(generation)) return
    const active = new Set(this.store.activeJobIds())
    for (const jobId of [...this.slots.keys()]) {
      if (!active.has(jobId)) this.detach(jobId)
    }
    for (const jobId of active) {
      if (!this.slots.has(jobId)) void this.connect(jobId, generation)
    }
  }

  private async connect(jobId: string, generation: number): Promise<void> {
    if (!this.isCurrent(generation) || this.isTerminal(jobId)) return
    const slot = this.slots.get(jobId) ?? { generation, reconnectAttempt: 0, serial: 0 }
    slot.generation = generation
    slot.serial += 1
    const token: ConnectionToken = { generation, jobId, serial: slot.serial }
    this.slots.set(jobId, slot)
    this.store.setConnection(jobId, slot.reconnectAttempt > 0 ? 'reconnecting' : 'connecting')
    const requestedAfter = this.store.projectionCursor(jobId)
    try {
      const handle = await this.gateway.connect(jobId, requestedAfter, {
        signal: signal => { void this.onSignal(token, requestedAfter, signal) },
        closed: error => this.onClosed(token, error),
      })
      if (!this.isConnectionCurrent(token)) {
        handle.close()
        return
      }
      slot.handle = handle
      this.store.setConnection(
        jobId,
        this.store.projectionCursor(jobId) >= this.store.observedCursor(jobId) ? 'connected' : 'needs_snapshot',
      )
    } catch (error) {
      if (this.isConnectionCurrent(token)) this.scheduleReconnect(token, error)
    }
  }

  private async onSignal(token: ConnectionToken, requestedAfter: number, signal: JobStreamSignal): Promise<void> {
    if (!this.isConnectionCurrent(token)) return
    try {
      if (signal.type === 'recovery') {
        await this.applyRecovery(token, requestedAfter, parseJobSseRecovery(signal.recovery))
        return
      }
      const cursor = parseJobEventCursor(signal.cursor)
      if (cursor.job_id !== token.jobId) throw new Error('job stream cursor domain mismatch')
      const result = this.store.recordEvent(token.jobId, cursor.job_event_seq)
      this.markStreamStable(token)
      if (result === 'gap') {
        this.store.setConnection(token.jobId, 'needs_snapshot')
        this.scheduleReconnect(token)
        return
      }
      if (cursor.job_event_seq > this.store.projectionCursor(token.jobId)) {
        this.store.setConnection(token.jobId, 'needs_snapshot')
        await this.requestConvergence(
          token.jobId,
          this.store.observedCursor(token.jobId),
          { generation: token.generation, barrier: this.readBarrier(token.jobId), connection: token },
        )
      } else {
        this.markProjectionCovered(token.jobId, token.generation)
      }
    } catch {
      if (!this.isConnectionCurrent(token)) return
      this.store.setConnection(token.jobId, 'error')
      this.scheduleReconnect(token)
    }
  }

  private async applyRecovery(
    token: ConnectionToken,
    requestedAfter: number,
    recovery: JobSseRecovery,
  ): Promise<void> {
    this.validateRecovery(token.jobId, requestedAfter, recovery)
    this.markStreamStable(token)

    if (!recovery.gap) {
      const requiredCoverage = Math.max(this.store.observedCursor(token.jobId), recovery.durable_high_water_seq)
      if (requiredCoverage > this.store.projectionCursor(token.jobId)) {
        this.store.setConnection(token.jobId, 'needs_snapshot')
        await this.requestConvergence(
          token.jobId,
          requiredCoverage,
          { generation: token.generation, barrier: this.readBarrier(token.jobId), connection: token },
        )
      } else {
        this.markProjectionCovered(token.jobId, token.generation)
      }
      return
    }

    this.store.setConnection(token.jobId, 'needs_snapshot')
    const barrier = this.readBarrier(token.jobId)
    let snapshot: Readonly<JobSnapshot>
    try {
      snapshot = await this.gateway.readSnapshot(token.jobId, { recovery })
    } catch (error) {
      if (!this.isConnectionCurrent(token) || this.readBarrier(token.jobId) !== barrier) return
      throw error
    }
    if (!this.isConnectionCurrent(token) || this.readBarrier(token.jobId) !== barrier) return
    if (snapshot.job_id !== token.jobId || snapshot.job_revision !== recovery.snapshot_revision
      || snapshot.snapshot_hash !== recovery.snapshot_hash
      || snapshot.job_event_high_water !== recovery.durable_high_water_seq) {
      throw new Error('job recovery snapshot does not converge to durable state')
    }
    this.requireAppliedSnapshot(token.jobId, snapshot, recovery.durable_high_water_seq)
    if (!this.isConnectionCurrent(token)) return
    this.reconcileConnections(token.generation)
    if (!this.isConnectionCurrent(token)) return
    this.rotateConnection(token)
  }

  private validateRecovery(jobId: string, requestedAfter: number, recovery: JobSseRecovery): void {
    if (recovery.schema !== 'sse-recovery/v1' || recovery.stream_kind !== 'job_event'
      || recovery.aggregate_id !== jobId || recovery.requested_after_seq !== requestedAfter) {
      throw new Error('invalid job SSE recovery identity')
    }
    if (requestedAfter > recovery.durable_high_water_seq) {
      throw new Error('job SSE cursor is ahead of durable high-water')
    }
    if (recovery.replay_floor_seq > recovery.durable_high_water_seq) {
      throw new Error('job SSE replay floor is ahead of durable high-water')
    }
    const expectedGap = requestedAfter < recovery.replay_floor_seq
    if (recovery.gap !== expectedGap || recovery.snapshot_required !== expectedGap) {
      throw new Error('job SSE replay floor and gap flags are inconsistent')
    }
    const metadata = [
      recovery.snapshot_schema,
      recovery.snapshot_revision,
      recovery.snapshot_asset_id,
      recovery.snapshot_hash,
    ]
    if (!expectedGap) {
      if (metadata.some(value => value != null)) throw new Error('non-gap recovery must not carry snapshot metadata')
      return
    }
    if (recovery.snapshot_schema !== 'job-snapshot/v1'
      || recovery.snapshot_revision == null
      || recovery.snapshot_asset_id == null
      || recovery.snapshot_hash == null) {
      throw new Error('job SSE gap requires a complete matching snapshot reference')
    }
  }

  private requestConvergence(
    jobId: string,
    minimumCoverage: number,
    continuation: SnapshotContinuation,
    options: { forceFresh?: boolean } = {},
  ): Promise<void> {
    if (!this.isContinuationCurrent(jobId, continuation)) return Promise.resolve()
    const current = this.snapshotReads.get(jobId)
    if (!options.forceFresh && current != null && this.canReuseRead(current, continuation)) {
      current.requiredCoverage = Math.max(current.requiredCoverage, minimumCoverage)
      return current.promise
    }

    const entry: SnapshotReadEntry = {
      generation: continuation.generation,
      barrier: continuation.barrier,
      continuation,
      requiredCoverage: minimumCoverage,
      promise: Promise.resolve(),
    }
    entry.promise = this.runConvergence(jobId, entry)
      .finally(() => {
        if (this.snapshotReads.get(jobId) === entry) this.snapshotReads.delete(jobId)
      })
    this.snapshotReads.set(jobId, entry)
    return entry.promise
  }

  private async runConvergence(jobId: string, entry: SnapshotReadEntry): Promise<void> {
    while (this.isContinuationCurrent(jobId, entry.continuation)) {
      const requiredCoverage = Math.max(entry.requiredCoverage, this.store.observedCursor(jobId))
      entry.requiredCoverage = requiredCoverage
      let snapshot: Readonly<JobSnapshot>
      try {
        snapshot = await this.gateway.readSnapshot(jobId)
      } catch (error) {
        if (!this.isContinuationCurrent(jobId, entry.continuation)) return
        throw error
      }
      if (!this.isContinuationCurrent(jobId, entry.continuation)) return
      this.requireAppliedSnapshot(jobId, snapshot, requiredCoverage)
      if (!this.isContinuationCurrent(jobId, entry.continuation)) return
      this.reconcileConnections(entry.generation)
      if (!this.isContinuationCurrent(jobId, entry.continuation)) return
      entry.requiredCoverage = Math.max(entry.requiredCoverage, this.store.observedCursor(jobId))
      if (this.store.projectionCursor(jobId) >= entry.requiredCoverage) {
        this.markProjectionCovered(jobId, entry.generation)
        return
      }
    }
  }

  private canReuseRead(entry: SnapshotReadEntry, continuation: SnapshotContinuation): boolean {
    if (entry.generation !== continuation.generation || entry.barrier !== continuation.barrier) return false
    if (continuation.connection == null) return false
    if (entry.continuation.actionToken != null) {
      return this.isContinuationCurrent(continuation.connection.jobId, entry.continuation)
    }
    const owner = entry.continuation.connection
    return owner != null
      && owner.generation === continuation.connection.generation
      && owner.jobId === continuation.connection.jobId
      && owner.serial === continuation.connection.serial
  }

  private requireAppliedSnapshot(
    expectedJobId: string,
    snapshot: Readonly<JobSnapshot>,
    minimumCoverage: number,
  ): void {
    if (snapshot.job_id !== expectedJobId) {
      throw new Error(`job snapshot identity mismatch: expected ${expectedJobId}, received ${snapshot.job_id}`)
    }
    const result = this.store.replaceSnapshot(snapshot, minimumCoverage)
    if (result === 'stale') throw new Error('authoritative snapshot did not cover the required job event cursor')
  }

  private onClosed(token: ConnectionToken, _error?: unknown): void {
    if (!this.isConnectionCurrent(token) || this.isTerminal(token.jobId)) return
    this.scheduleReconnect(token)
  }

  private scheduleReconnect(token: ConnectionToken, _error?: unknown): void {
    if (!this.isConnectionCurrent(token) || this.isTerminal(token.jobId)) return
    const slot = this.slots.get(token.jobId)
    if (slot == null || slot.timer != null) return
    slot.serial += 1
    const handle = slot.handle
    slot.handle = undefined
    handle?.close()
    slot.reconnectAttempt += 1
    this.store.setConnection(token.jobId, 'reconnecting')
    const delay = Math.min(
      this.reconnectMaxDelayMs,
      this.reconnectBaseDelayMs * (2 ** (slot.reconnectAttempt - 1)),
    )
    let timer: unknown
    timer = this.scheduler.set(delay, () => {
      if (!this.isCurrent(token.generation) || this.slots.get(token.jobId) !== slot || slot.timer !== timer) return
      slot.timer = undefined
      void this.connect(token.jobId, token.generation)
    })
    slot.timer = timer
  }

  private rotateConnection(token: ConnectionToken): void {
    if (!this.isConnectionCurrent(token) || this.isTerminal(token.jobId)) return
    const slot = this.slots.get(token.jobId)
    if (slot == null) return
    slot.serial += 1
    const handle = slot.handle
    slot.handle = undefined
    handle?.close()
    void this.connect(token.jobId, token.generation)
  }

  private markStreamStable(token: ConnectionToken): void {
    if (!this.isConnectionCurrent(token)) return
    const slot = this.slots.get(token.jobId)
    if (slot != null) slot.reconnectAttempt = 0
  }

  private markProjectionCovered(jobId: string, generation: number): void {
    if (!this.isCurrent(generation)
      || this.store.projectionCursor(jobId) < this.store.observedCursor(jobId)) return
    const slot = this.slots.get(jobId)
    if (slot?.generation === generation && slot.handle != null && slot.timer == null) {
      this.store.setConnection(jobId, 'connected')
    }
  }

  private advanceReadBarrier(jobId: string): number {
    const barrier = this.readBarrier(jobId) + 1
    this.readBarriers.set(jobId, barrier)
    return barrier
  }

  private readBarrier(jobId: string): number {
    return this.readBarriers.get(jobId) ?? 0
  }

  private detach(jobId: string): void {
    const slot = this.slots.get(jobId)
    if (slot == null) return
    slot.serial += 1
    const handle = slot.handle
    slot.handle = undefined
    handle?.close()
    if (slot.timer != null) this.scheduler.clear(slot.timer)
    this.slots.delete(jobId)
    this.store.setConnection(jobId, 'detached')
  }

  private isTerminal(jobId: string): boolean {
    return isTerminalJobState(this.store.snapshot(jobId)?.job_state ?? 'cancelled')
  }

  private isActionCurrent(jobId: string, entry: ActionEntry): boolean {
    return this.isCurrent(entry.generation) && this.actions.get(jobId)?.token === entry.token
  }

  private isContinuationCurrent(jobId: string, continuation: SnapshotContinuation): boolean {
    if (!this.isCurrent(continuation.generation) || this.readBarrier(jobId) !== continuation.barrier) return false
    if (continuation.connection != null && !this.isConnectionCurrent(continuation.connection)) return false
    if (continuation.actionToken != null && this.actions.get(jobId)?.token !== continuation.actionToken) return false
    return true
  }

  private isCurrent(generation: number): boolean {
    return this.started && generation === this.generation
  }

  private isConnectionCurrent(token: ConnectionToken): boolean {
    const slot = this.slots.get(token.jobId)
    return this.isCurrent(token.generation)
      && slot?.generation === token.generation
      && slot.serial === token.serial
  }
}
