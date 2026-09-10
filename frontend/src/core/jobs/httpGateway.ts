import {
  parseHttpRequestV2,
  parseHttpResponseV2,
  routeForV2,
} from '../../contracts/m4-m5-http-v2.ts'
import type {
  JobAction,
  JobDrawerGateway,
  JobSnapshot,
  JobSnapshotReadContext,
  JobSseRecovery,
  JobStreamHandlers,
  JobStreamSubscription,
} from './types.ts'

type JsonRecord = Record<string, unknown>
type JobRouteId = 'job.list' | 'job.get' | 'job.resume' | 'job.cancel' | 'job.sse-recovery'

const DISCOVERY_PAGE_LIMIT = 200
const MAX_DISCOVERY_PAGES = 100
const DEFAULT_RECOVERY_POLL_DELAY_MS = 1_000
const MIN_RECOVERY_POLL_DELAY_MS = 250

export interface JobHttpGatewayScheduler {
  set(delayMs: number, callback: () => void): unknown
  clear(handle: unknown): void
}

const DEFAULT_RECOVERY_SCHEDULER: JobHttpGatewayScheduler = {
  set: (delayMs, callback) => globalThis.setTimeout(callback, delayMs),
  clear: handle => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
}

export interface JobHttpGatewayOptions {
  /** Empty keeps all requests browser-relative to the WebUI origin. */
  baseUrl?: string
  fetch?: typeof globalThis.fetch
  createOperationKey?: (action: JobAction, jobId: string) => string
  /** Finite recovery responses are polled at this bounded interval. */
  pollDelayMs?: number
  scheduler?: JobHttpGatewayScheduler
}

export class JobHttpGatewayError extends Error {
  readonly routeId: JobRouteId
  readonly status: number
  readonly errorCode: string
  readonly retryable: boolean

  constructor(routeId: JobRouteId, status: number, payload: Readonly<JsonRecord>) {
    super(String(payload.message))
    this.name = 'JobHttpGatewayError'
    this.routeId = routeId
    this.status = status
    this.errorCode = String(payload.error_code)
    this.retryable = Boolean(payload.retryable)
  }
}

interface JobContext {
  workspaceId: string
  revision: number
}

interface RecoveredSnapshot {
  recovery: JobSseRecovery
  snapshot: Readonly<JobSnapshot>
}

function field<T>(source: Readonly<JsonRecord>, name: string): T {
  return source[name] as T
}

function joinUrl(baseUrl: string, path: string): string {
  if (baseUrl.length === 0) return path
  return `${baseUrl.replace(/\/$/, '')}/${path.replace(/^\//, '')}`
}

function buildRequest(
  routeId: JobRouteId,
  request: Readonly<JsonRecord>,
  baseUrl: string,
): [string, RequestInit] {
  const route = routeForV2(routeId)
  let path = route.path_template
  const pathIdentity = new Set(route.path_identity)
  for (const identity of pathIdentity) {
    path = path.replace(`{${identity}}`, encodeURIComponent(field<string>(request, identity)))
  }

  const headers = { Accept: 'application/json' }
  if (route.method === 'GET') {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(request)) {
      if (key === 'schema' || pathIdentity.has(key) || value === null) continue
      query.set(key, String(value))
    }
    const suffix = query.size === 0 ? '' : `?${query.toString()}`
    return [joinUrl(baseUrl, `${path}${suffix}`), { method: route.method, headers }]
  }

  return [joinUrl(baseUrl, path), {
    method: route.method,
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  }]
}

function projectSnapshot(source: Readonly<JsonRecord>): Readonly<JobSnapshot> {
  // Jobs v2 intentionally exposes an execution projection rather than the
  // older per-step/per-attempt payload. Keep those presentation-only arrays
  // empty instead of inventing runtime data. The frozen v2 ingress has already
  // validated every copied wire field before this host projection is made.
  return {
    schema: 'job-snapshot/v1',
    job_id: field<string>(source, 'job_id'),
    workspace_id: field<string>(source, 'workspace_id'),
    job_state: field<JobSnapshot['job_state']>(source, 'state'),
    job_revision: field<number>(source, 'job_revision'),
    steps: [],
    attempts: [],
    candidate_ids: [...field<readonly string[]>(source, 'candidate_ids')],
    current_checkpoint_id: field<string | null>(source, 'checkpoint_id'),
    stream_high_waters: field<JobSnapshot['stream_high_waters']>(source, 'stream_high_waters'),
    core_event_high_water: field<number>(source, 'core_event_high_water'),
    job_event_high_water: field<number>(source, 'job_event_high_water'),
    created_at: field<string>(source, 'created_at'),
    snapshot_hash: field<string>(source, 'snapshot_hash'),
  }
}

function projectRecovery(source: Readonly<JsonRecord>): {
  recovery: JobSseRecovery
  snapshot: Readonly<JobSnapshot> | undefined
  tail: ReadonlyArray<Readonly<JsonRecord>>
} {
  const snapshotSource = field<Readonly<JsonRecord> | null>(source, 'snapshot')
  const snapshot = snapshotSource === null ? undefined : projectSnapshot(snapshotSource)
  const gap = field<boolean>(source, 'gap')

  // The v1 controller expects a gap Snapshot to already cover its declared
  // durable cursor. A v2 recovery may include a snapshot followed by a tail,
  // so expose the supplied snapshot cursor as this recovery's convergence
  // point. The controller immediately reconnects from it for the v2 tail.
  const durableHighWater = snapshot?.job_event_high_water
    ?? field<number>(source, 'durable_high_water_seq')
  const snapshotCursor = field<string>(source, 'snapshot_cursor')
  const recovery: JobSseRecovery = {
    schema: 'sse-recovery/v1',
    stream_kind: 'job_event',
    aggregate_id: field<string>(source, 'job_id'),
    requested_after_seq: field<number>(source, 'requested_after_seq'),
    replay_floor_seq: field<number>(source, 'replay_floor_seq'),
    durable_high_water_seq: durableHighWater,
    gap,
    snapshot_required: gap,
    snapshot_schema: snapshot === undefined ? null : 'job-snapshot/v1',
    snapshot_revision: snapshot?.job_revision ?? null,
    // Jobs v2 carries a job-bound Snapshot cursor, not a v1 Asset identifier.
    // This opaque endpoint-issued locator satisfies the legacy ingress metadata
    // requirement; the actual cached v2 Snapshot remains the sole read source.
    snapshot_asset_id: snapshot === undefined ? null : snapshotCursor,
    snapshot_hash: snapshot?.snapshot_hash ?? null,
  }
  return {
    recovery,
    snapshot,
    tail: field<ReadonlyArray<Readonly<JsonRecord>>>(source, 'tail'),
  }
}

function defaultOperationKey(action: JobAction, _jobId: string): string {
  const uuid = globalThis.crypto?.randomUUID?.()
  if (uuid !== undefined) return `webui-${action}-${uuid}`
  return `webui-${action}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

function recoveryPollDelay(value: number | undefined): number {
  const delay = value ?? DEFAULT_RECOVERY_POLL_DELAY_MS
  if (!Number.isFinite(delay) || delay < MIN_RECOVERY_POLL_DELAY_MS) {
    throw new Error(`Job recovery poll delay must be at least ${MIN_RECOVERY_POLL_DELAY_MS}ms`)
  }
  return Math.floor(delay)
}

/**
 * Browser-relative adapter from frozen Jobs v2 HTTP/recovery responses to the
 * already-accepted Job drawer port. It validates all wire traffic through the
 * shared contract ingress and deliberately owns no second public DTO parser.
 */
export class JobHttpGateway implements JobDrawerGateway {
  private readonly fetchImpl: typeof globalThis.fetch
  private readonly baseUrl: string
  private readonly createOperationKey: (action: JobAction, jobId: string) => string
  private readonly pollDelayMs: number
  private readonly scheduler: JobHttpGatewayScheduler
  private readonly contexts = new Map<string, JobContext>()
  private readonly recoveredSnapshots = new Map<string, RecoveredSnapshot>()

  constructor(options: JobHttpGatewayOptions = {}) {
    const fetchImpl = options.fetch ?? globalThis.fetch
    if (typeof fetchImpl !== 'function') throw new Error('Job HTTP gateway requires a fetch implementation')
    this.fetchImpl = fetchImpl
    this.baseUrl = options.baseUrl ?? ''
    this.createOperationKey = options.createOperationKey ?? defaultOperationKey
    this.pollDelayMs = recoveryPollDelay(options.pollDelayMs)
    this.scheduler = options.scheduler ?? DEFAULT_RECOVERY_SCHEDULER
  }

  async discover(workspaceId: string): Promise<ReadonlyArray<Readonly<JobSnapshot>>> {
    const snapshots: Readonly<JobSnapshot>[] = []
    const contexts = new Map<string, JobContext>()
    let cursor: string | null = null

    for (let page = 0; page < MAX_DISCOVERY_PAGES; page += 1) {
      const result = await this.request('job.list', {
        schema: 'job-list-query/v2',
        workspace_id: workspaceId,
        cursor,
        limit: DISCOVERY_PAGE_LIMIT,
        state: null,
      })
      for (const item of field<ReadonlyArray<Readonly<JsonRecord>>>(result, 'items')) {
        const snapshot = projectSnapshot(item)
        snapshots.push(snapshot)
        contexts.set(snapshot.job_id, { workspaceId: snapshot.workspace_id, revision: snapshot.job_revision })
      }
      cursor = field<string | null>(result, 'next_cursor')
      if (cursor === null) {
        this.contexts.clear()
        for (const [jobId, context] of contexts) this.contexts.set(jobId, context)
        return snapshots
      }
    }
    throw new Error(`Job discovery exceeded ${MAX_DISCOVERY_PAGES} pages for workspace ${workspaceId}`)
  }

  async readSnapshot(jobId: string, context?: JobSnapshotReadContext): Promise<Readonly<JobSnapshot>> {
    if (context?.recovery !== undefined) {
      const recovered = this.recoveredSnapshots.get(jobId)
      if (recovered !== undefined
        && recovered.recovery.snapshot_hash === context.recovery.snapshot_hash
        && recovered.recovery.snapshot_revision === context.recovery.snapshot_revision) {
        return recovered.snapshot
      }
      throw new Error(`Job recovery snapshot is unavailable for ${jobId}`)
    }

    const known = this.contextFor(jobId)
    const result = await this.request('job.get', {
      schema: 'job-snapshot-query/v2',
      workspace_id: known.workspaceId,
      job_id: jobId,
    })
    const snapshot = projectSnapshot(field<Readonly<JsonRecord>>(result, 'snapshot'))
    this.remember(snapshot)
    return snapshot
  }

  async connect(
    jobId: string,
    afterJobEventSeq: number,
    handlers: JobStreamHandlers,
  ): Promise<JobStreamSubscription> {
    const known = this.contextFor(jobId)
    let closed = false
    let timer: unknown
    let timerActive = false

    const clearTimer = (): void => {
      if (!timerActive) return
      this.scheduler.clear(timer)
      timerActive = false
      timer = undefined
    }
    const schedule = (delayMs: number, callback: () => void): void => {
      if (closed) return
      clearTimer()
      timerActive = true
      timer = this.scheduler.set(delayMs, () => {
        timerActive = false
        timer = undefined
        if (!closed) callback()
      })
    }
    const closeWithError = (error: unknown): void => {
      if (closed) return
      closed = true
      clearTimer()
      handlers.closed(error)
    }
    const poll = async (): Promise<void> => {
      let projected: ReturnType<typeof projectRecovery>
      try {
        // Keep the controller's requested cursor fixed for this subscription.
        // Duplicate retained tail entries are safely absorbed by the store.
        const result = await this.request('job.sse-recovery', {
          schema: 'job-sse-recovery-query/v2',
          workspace_id: known.workspaceId,
          job_id: jobId,
          after_seq: afterJobEventSeq,
          last_event_id: `job/${jobId}/${afterJobEventSeq}`,
          requested_cursor_domain: 'job',
        })
        if (closed) return
        projected = projectRecovery(result)
      } catch (error) {
        closeWithError(error)
        return
      }
      if (projected.snapshot !== undefined) {
        this.remember(projected.snapshot)
        this.recoveredSnapshots.set(jobId, {
          recovery: projected.recovery,
          snapshot: projected.snapshot,
        })
      }
      // Defer ingress until the controller has installed this handle. This is
      // a finite JSON recovery transport, never an EventSource impersonation.
      schedule(0, () => {
        handlers.signal({ type: 'recovery', recovery: projected.recovery })
        // A v2 gap is deliberately split at its cached Snapshot boundary; the
        // controller rotates this handle and retrieves the retained tail.
        if (projected.recovery.gap) return
        for (const event of projected.tail) {
          if (closed) return
          handlers.signal({
            type: 'event',
            cursor: {
              stream_kind: 'job_event',
              job_id: field<string>(event, 'job_id'),
              job_event_seq: field<number>(event, 'job_event_seq'),
            },
          })
        }
        schedule(this.pollDelayMs, () => { void poll() })
      })
    }

    const subscription: JobStreamSubscription = {
      close: () => {
        if (closed) return
        closed = true
        clearTimer()
      },
    }
    void poll()
    return subscription
  }

  async act(action: JobAction, jobId: string): Promise<void> {
    const known = this.contextFor(jobId)
    const routeId: 'job.resume' | 'job.cancel' = action === 'cancel' ? 'job.cancel' : 'job.resume'
    const command: 'resume' | 'cancel' = action === 'cancel' ? 'cancel' : 'resume'
    await this.request(routeId, {
      schema: 'job-control-command/v2',
      operation_key: this.createOperationKey(action, jobId),
      workspace_id: known.workspaceId,
      job_id: jobId,
      expected_job_revision: known.revision,
      reason: `WebUI requested ${action}`,
      command,
      resume_intent_id: null,
    })
  }

  private contextFor(jobId: string): JobContext {
    const context = this.contexts.get(jobId)
    if (context === undefined) throw new Error(`Job ${jobId} is not known to the current workspace discovery`)
    return context
  }

  private remember(snapshot: Readonly<JobSnapshot>): void {
    this.contexts.set(snapshot.job_id, {
      workspaceId: snapshot.workspace_id,
      revision: snapshot.job_revision,
    })
  }

  private async request(routeId: JobRouteId, input: JsonRecord): Promise<Readonly<JsonRecord>> {
    const request = parseHttpRequestV2(routeId, input) as Readonly<JsonRecord>
    const [url, init] = buildRequest(routeId, request, this.baseUrl)
    const response = await this.fetchImpl(url, init)
    const contentType = response.headers.get('content-type') ?? ''
    if (!contentType.toLowerCase().includes('application/json')) {
      throw new Error(`Job route ${routeId} returned a non-JSON response`)
    }
    let raw: unknown
    try {
      raw = await response.json()
    } catch (error) {
      const detail = error instanceof Error ? `: ${error.message}` : ''
      throw new Error(`Job route ${routeId} returned invalid JSON${detail}`)
    }
    const parsed = await parseHttpResponseV2(routeId, response.status, raw) as Readonly<JsonRecord>
    if (!response.ok) throw new JobHttpGatewayError(routeId, response.status, parsed)
    return parsed
  }
}

export function createJobHttpGateway(options: JobHttpGatewayOptions = {}): JobDrawerGateway {
  return new JobHttpGateway(options)
}
