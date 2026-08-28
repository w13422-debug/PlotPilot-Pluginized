import { jobActionAvailability } from '../jobPresentation.ts'
import { parseJobEventCursor, parseJobSseRecovery } from './ingress.ts'
import { JobDrawerStore } from './store.ts'
import {
  isTerminalJobState,
  type JobAction,
  type JobDrawerControllerOptions,
  type JobDrawerGateway,
  type JobDrawerScheduler,
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
  private readonly snapshotReads = new Map<string, Promise<void>>()
  private readonly actions = new Map<string, JobAction>()
  private workspaceId: string | undefined
  private generation = 0
  private started = false

  constructor(gateway: JobDrawerGateway, store = new JobDrawerStore(), options: JobDrawerControllerOptions = {}) {
    this.gateway = gateway
    this.store = store
    this.scheduler = options.scheduler ?? DEFAULT_SCHEDULER
    this.reconnectBaseDelayMs = Math.max(0, options.reconnectBaseDelayMs ?? 250)
    this.reconnectMaxDelayMs = Math.max(this.reconnectBaseDelayMs, options.reconnectMaxDelayMs ?? 8_000)
  }

  pendingAction(jobId: string): JobAction | undefined {
    return this.actions.get(jobId)
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
    const snapshots = await this.gateway.discover(this.workspaceId)
    if (!this.isCurrent(generation)) return
    this.store.replaceWorkspaceSnapshots(this.workspaceId, snapshots)
    this.reconcileConnections(generation)
  }

  stop(): void {
    this.started = false
    this.workspaceId = undefined
    this.generation += 1
    for (const [jobId] of this.slots) this.detach(jobId)
    this.slots.clear()
    this.snapshotReads.clear()
    this.actions.clear()
  }

  async act(action: JobAction, jobId: string): Promise<void> {
    const snapshot = this.store.snapshot(jobId)
    if (snapshot == null) throw new Error(`cannot ${action} unknown job ${jobId}`)
    if (this.actions.has(jobId)) throw new Error(`job ${jobId} already has an action in flight`)
    const availability = jobActionAvailability(snapshot)
    if (!availability[action]) throw new Error(`${action} is not available for job ${jobId}`)
    this.actions.set(jobId, action)
    const generation = this.generation
    try {
      const replacement = await this.gateway.act(action, jobId)
      if (!this.isCurrent(generation)) return
      if (replacement != null) this.requireAppliedSnapshot(replacement)
      else await this.refreshSnapshot(jobId, generation)
      this.reconcileConnections(generation)
    } finally {
      this.actions.delete(jobId)
    }
  }

  private reconcileConnections(generation: number): void {
    const active = new Set(this.store.activeJobIds())
    for (const jobId of this.slots.keys()) {
      if (!active.has(jobId)) this.detach(jobId)
    }
    for (const jobId of active) {
      if (!this.slots.has(jobId)) void this.connect(jobId, generation)
    }
  }

  private async connect(jobId: string, generation: number): Promise<void> {
    if (!this.isCurrent(generation) || isTerminalJobState(this.store.snapshot(jobId)?.job_state ?? 'cancelled')) return
    const slot = this.slots.get(jobId) ?? { generation, reconnectAttempt: 0, serial: 0 }
    slot.generation = generation
    slot.serial += 1
    const serial = slot.serial
    this.slots.set(jobId, slot)
    this.store.setConnection(jobId, slot.reconnectAttempt > 0 ? 'reconnecting' : 'connecting')
    const requestedAfter = this.store.cursor(jobId)
    try {
      const handle = await this.gateway.connect(jobId, requestedAfter, {
        signal: signal => { void this.onSignal(jobId, requestedAfter, generation, serial, signal) },
        closed: error => this.onClosed(jobId, generation, serial, error),
      })
      if (!this.isConnectionCurrent(jobId, generation, serial)) {
        handle.close()
        return
      }
      slot.handle = handle
      slot.reconnectAttempt = 0
      this.store.setConnection(jobId, 'connected')
    } catch (error) {
      this.onClosed(jobId, generation, serial, error)
    }
  }

  private async onSignal(
    jobId: string,
    requestedAfter: number,
    generation: number,
    serial: number,
    signal: JobStreamSignal,
  ): Promise<void> {
    if (!this.isConnectionCurrent(jobId, generation, serial)) return
    try {
      if (signal.type === 'recovery') {
        await this.applyRecovery(jobId, requestedAfter, generation, serial, parseJobSseRecovery(signal.recovery))
        return
      }
      const cursor = parseJobEventCursor(signal.cursor)
      if (cursor.job_id !== jobId) {
        throw new Error('job stream cursor domain mismatch')
      }
      const result = this.store.recordEvent(jobId, cursor.job_event_seq)
      if (result === 'gap') {
        this.store.setConnection(jobId, 'needs_snapshot')
        this.scheduleReconnect(jobId, generation)
        return
      }
      if (result === 'applied') await this.refreshSnapshot(jobId, generation)
    } catch {
      this.store.setConnection(jobId, 'error')
      this.scheduleReconnect(jobId, generation)
    }
  }

  private async applyRecovery(
    jobId: string,
    requestedAfter: number,
    generation: number,
    serial: number,
    recovery: JobSseRecovery,
  ): Promise<void> {
    if (recovery.schema !== 'sse-recovery/v1' || recovery.stream_kind !== 'job_event'
      || recovery.aggregate_id !== jobId || recovery.requested_after_seq !== requestedAfter) {
      throw new Error('invalid job SSE recovery identity')
    }
    if (requestedAfter > recovery.durable_high_water_seq) throw new Error('job SSE cursor is ahead of durable high-water')
    const needsSnapshot = recovery.gap || recovery.snapshot_required
    if (!needsSnapshot) {
      if (recovery.snapshot_schema != null || recovery.snapshot_revision != null
        || recovery.snapshot_asset_id != null || recovery.snapshot_hash != null) {
        throw new Error('non-gap recovery must not carry snapshot metadata')
      }
      return
    }
    if (!recovery.gap || !recovery.snapshot_required || recovery.snapshot_schema !== 'job-snapshot/v1'
      || recovery.snapshot_revision == null || recovery.snapshot_asset_id == null || recovery.snapshot_hash == null) {
      throw new Error('job SSE gap requires a complete matching snapshot reference')
    }
    this.store.setConnection(jobId, 'needs_snapshot')
    const snapshot = await this.gateway.readSnapshot(jobId, { recovery })
    if (!this.isConnectionCurrent(jobId, generation, serial)) return
    if (snapshot.job_id !== jobId || snapshot.job_revision !== recovery.snapshot_revision
      || snapshot.snapshot_hash !== recovery.snapshot_hash
      || snapshot.job_event_high_water !== recovery.durable_high_water_seq) {
      throw new Error('job recovery snapshot does not converge to durable state')
    }
    this.requireAppliedSnapshot(snapshot)
    this.store.setConnection(jobId, 'connected')
    this.reconcileConnections(generation)
  }

  private refreshSnapshot(jobId: string, generation: number): Promise<void> {
    const current = this.snapshotReads.get(jobId)
    if (current != null) return current
    const read = this.gateway.readSnapshot(jobId)
      .then(snapshot => {
        if (!this.isCurrent(generation)) return
        this.requireAppliedSnapshot(snapshot)
        this.reconcileConnections(generation)
      })
      .finally(() => {
        if (this.snapshotReads.get(jobId) === read) this.snapshotReads.delete(jobId)
      })
    this.snapshotReads.set(jobId, read)
    return read
  }

  private requireAppliedSnapshot(snapshot: Readonly<import('../../contracts/types').JobSnapshot>): void {
    const result = this.store.replaceSnapshot(snapshot)
    if (result === 'stale') throw new Error('authoritative snapshot did not cover the observed job event cursor')
  }

  private onClosed(jobId: string, generation: number, serial: number, _error?: unknown): void {
    if (!this.isConnectionCurrent(jobId, generation, serial)
      || isTerminalJobState(this.store.snapshot(jobId)?.job_state ?? 'cancelled')) return
    this.scheduleReconnect(jobId, generation)
  }

  private scheduleReconnect(jobId: string, generation: number): void {
    if (!this.isCurrent(generation) || isTerminalJobState(this.store.snapshot(jobId)?.job_state ?? 'cancelled')) return
    const slot = this.slots.get(jobId)
    if (slot == null || slot.timer != null) return
    slot.serial += 1
    const handle = slot.handle
    slot.handle = undefined
    handle?.close()
    slot.reconnectAttempt += 1
    this.store.setConnection(jobId, 'reconnecting')
    const delay = Math.min(this.reconnectMaxDelayMs, this.reconnectBaseDelayMs * (2 ** (slot.reconnectAttempt - 1)))
    slot.timer = this.scheduler.set(delay, () => {
      slot.timer = undefined
      void this.connect(jobId, generation)
    })
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

  private isCurrent(generation: number): boolean {
    return this.started && generation === this.generation
  }

  private isConnectionCurrent(jobId: string, generation: number, serial: number): boolean {
    const slot = this.slots.get(jobId)
    return this.isCurrent(generation) && slot?.generation === generation && slot.serial === serial
  }
}
