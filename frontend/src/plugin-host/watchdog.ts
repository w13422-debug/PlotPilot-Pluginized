export interface WatchdogWorker {
  terminate(): void
}

export interface SlotWatchdogOptions {
  timeoutMs: number
  setTimeout?: (handler: () => void, timeoutMs: number) => unknown
  clearTimeout?: (handle: unknown) => void
  onTimeout?: (worker: WatchdogWorker) => void
}

export interface SlotWatchdogHandle {
  readonly worker: WatchdogWorker
  refresh(): boolean
  cancel(): boolean
}

interface WatchdogEntry {
  readonly worker: WatchdogWorker
  readonly token: object
  timer: unknown
}

const defaultSetTimeout = (handler: () => void, timeoutMs: number): unknown =>
  globalThis.setTimeout(handler, timeoutMs)
const defaultClearTimeout = (handle: unknown): void => {
  globalThis.clearTimeout(handle as ReturnType<typeof globalThis.setTimeout>)
}

/**
 * A watchdog owns exactly one Slot's current Worker lease.
 *
 * Entries are identity-fenced. If a replacement Worker is armed before an old
 * timer callback runs, the old callback observes a different entry and cannot
 * terminate the replacement.
 */
export class PluginSlotWatchdog {
  private readonly timeoutMs: number
  private readonly schedule: (handler: () => void, timeoutMs: number) => unknown
  private readonly cancelTimer: (handle: unknown) => void
  private readonly onTimeout: ((worker: WatchdogWorker) => void) | undefined
  private activeEntry: WatchdogEntry | null = null

  constructor(options: SlotWatchdogOptions) {
    if (!Number.isSafeInteger(options.timeoutMs) || options.timeoutMs <= 0) {
      throw new Error('watchdog_timeout_invalid')
    }
    this.timeoutMs = options.timeoutMs
    this.schedule = options.setTimeout ?? defaultSetTimeout
    this.cancelTimer = options.clearTimeout ?? defaultClearTimeout
    this.onTimeout = options.onTimeout
  }

  get isArmed(): boolean {
    return this.activeEntry !== null
  }

  get currentWorker(): WatchdogWorker | null {
    return this.activeEntry?.worker ?? null
  }

  arm(worker: WatchdogWorker): SlotWatchdogHandle {
    if (worker === null || typeof worker !== 'object' || typeof worker.terminate !== 'function') {
      throw new Error('watchdog_worker_invalid')
    }
    this.disarm()
    const entry: WatchdogEntry = { worker, token: {}, timer: undefined }
    this.activeEntry = entry
    entry.timer = this.schedule(() => this.expire(entry), this.timeoutMs)
    return {
      worker,
      refresh: () => this.refreshEntry(entry),
      cancel: () => this.disarmEntry(entry),
    }
  }

  /** Refresh only the currently armed Worker for this Slot. */
  refresh(worker?: WatchdogWorker | SlotWatchdogHandle): boolean {
    const entry = this.entryFor(worker)
    return entry === null ? false : this.refreshEntry(entry)
  }

  /** Disarm only the supplied current Worker/handle; no argument disarms this Slot. */
  disarm(worker?: WatchdogWorker | SlotWatchdogHandle): boolean {
    if (worker === undefined) {
      if (this.activeEntry === null) return false
      return this.disarmEntry(this.activeEntry)
    }
    const entry = this.entryFor(worker)
    return entry === null ? false : this.disarmEntry(entry)
  }

  isCurrent(worker: WatchdogWorker): boolean {
    return this.activeEntry?.worker === worker
  }

  private entryFor(target: WatchdogWorker | SlotWatchdogHandle | undefined): WatchdogEntry | null {
    const entry = this.activeEntry
    if (entry === null || target === undefined) return entry
    if (target === entry.worker) return entry
    if (target !== null && typeof target === 'object' && 'worker' in target && target.worker === entry.worker) {
      return entry
    }
    return null
  }

  private refreshEntry(entry: WatchdogEntry): boolean {
    if (this.activeEntry !== entry) return false
    this.cancelTimer(entry.timer)
    entry.timer = this.schedule(() => this.expire(entry), this.timeoutMs)
    return true
  }

  private disarmEntry(entry: WatchdogEntry): boolean {
    if (this.activeEntry !== entry) return false
    this.cancelTimer(entry.timer)
    this.activeEntry = null
    return true
  }

  private expire(entry: WatchdogEntry): void {
    if (this.activeEntry !== entry || entry.token === undefined) return
    this.activeEntry = null
    this.cancelTimer(entry.timer)
    try {
      entry.worker.terminate()
    } finally {
      this.onTimeout?.(entry.worker)
    }
  }
}

export { PluginSlotWatchdog as SlotWatchdog }
export const createSlotWatchdog = (options: SlotWatchdogOptions): PluginSlotWatchdog =>
  new PluginSlotWatchdog(options)
