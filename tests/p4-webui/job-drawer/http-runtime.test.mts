import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { createJobDrawerRuntime } from '../../../frontend/src/core/jobs/composition.ts'
import { JobHttpGateway, JobHttpGatewayError } from '../../../frontend/src/core/jobs/httpGateway.ts'
import type { JobDrawerGateway, JobStreamSignal } from '../../../frontend/src/core/jobs/types.ts'

const root = new URL('../../../', import.meta.url)

async function golden(): Promise<Record<string, any>> {
  return JSON.parse(await readFile(new URL('contracts/golden/m4-m5-public-surface-v2/job.json', root), 'utf8')) as Record<string, any>
}

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function queuedFetch(bodies: Array<Response | Error>): { fetch: typeof globalThis.fetch; calls: Array<{ url: string; init?: RequestInit }> } {
  const calls: Array<{ url: string; init?: RequestInit }> = []
  const fetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    calls.push({ url: String(input), init })
    const next = bodies.shift()
    if (next === undefined) throw new Error('unexpected HTTP request')
    if (next instanceof Error) throw next
    return next
  }) as typeof globalThis.fetch
  return { fetch, calls }
}

function terminalList(result: Record<string, any>): Record<string, any> {
  return { ...result, items: [], next_cursor: null }
}

async function waitForTimers(): Promise<void> {
  await new Promise<void>(resolve => globalThis.setTimeout(resolve, 0))
}

class TimerHarness {
  private nextId = 1
  private readonly timers = new Map<number, { delayMs: number; callback: () => void }>()
  readonly scheduledDelays: number[] = []

  set(delayMs: number, callback: () => void): number {
    const id = this.nextId++
    this.timers.set(id, { delayMs, callback })
    this.scheduledDelays.push(delayMs)
    return id
  }

  clear(handle: unknown): void {
    this.timers.delete(handle as number)
  }

  activeCount(): number {
    return this.timers.size
  }

  runNext(): number {
    const entry = this.timers.entries().next().value as [number, { delayMs: number; callback: () => void }] | undefined
    if (entry === undefined) throw new Error('no scheduled timer')
    this.timers.delete(entry[0])
    entry[1].callback()
    return entry[1].delayMs
  }
}

async function settle(): Promise<void> {
  for (let index = 0; index < 6; index += 1) await Promise.resolve()
}

async function waitForScheduled(timers: TimerHarness): Promise<void> {
  for (let index = 0; index < 20; index += 1) {
    if (timers.activeCount() > 0) return
    await waitForTimers()
  }
  throw new Error('recovery transport did not schedule its next deterministic callback')
}

function noNewRecovery(fixture: Record<string, any>): Record<string, any> {
  return {
    ...fixture.sse_replay,
    requested_after_seq: 1,
    durable_high_water_seq: 1,
    snapshot_cursor: 'job/job-v2/1',
    tail: [],
  }
}

test('Jobs v2 discovery pages through the shared contract ingress and projects only supplied fields', async () => {
  const fixture = await golden()
  const queue = queuedFetch([response(fixture.list_result), response(terminalList(fixture.list_result))])
  const gateway = new JobHttpGateway({ fetch: queue.fetch })

  const jobs = await gateway.discover('ws-1')

  assert.equal(jobs.length, 1)
  assert.equal(jobs[0]!.job_id, 'job-v2')
  assert.equal(jobs[0]!.job_state, 'running')
  assert.deepEqual(jobs[0]!.steps, [])
  assert.deepEqual(jobs[0]!.attempts, [])
  assert.equal(queue.calls[0]!.url, '/api/v2/jobs/ws-1?limit=200')
  assert.equal(queue.calls[1]!.url, '/api/v2/jobs/ws-1?cursor=job%2Fjob-v2%2F5&limit=200')
})

test('browser-native fetch receives globalThis as its invocation receiver', async () => {
  const fixture = await golden()
  const queue = queuedFetch([response(fixture.list_result), response(terminalList(fixture.list_result))])
  const receivers: unknown[] = []
  const receiverSensitiveFetch = (async function (
    this: unknown,
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> {
    receivers.push(this)
    return queue.fetch(input, init)
  }) as typeof globalThis.fetch
  const gateway = new JobHttpGateway({ fetch: receiverSensitiveFetch })

  await gateway.discover('ws-1')

  assert.deepEqual(receivers, [globalThis, globalThis])
})

test('Jobs v2 recovery maps replay cursors into the accepted Job stream ingress', async () => {
  const fixture = await golden()
  const queue = queuedFetch([
    response(fixture.list_result),
    response(terminalList(fixture.list_result)),
    response(fixture.sse_replay),
  ])
  const timers = new TimerHarness()
  const gateway = new JobHttpGateway({ fetch: queue.fetch, scheduler: timers })
  await gateway.discover('ws-1')
  const signals: JobStreamSignal[] = []

  const subscription = await gateway.connect('job-v2', 1, {
    signal: signal => signals.push(signal),
    closed: error => { throw error ?? new Error('unexpected stream close') },
  })
  await waitForScheduled(timers)
  assert.equal(timers.runNext(), 0)
  subscription.close()

  assert.deepEqual(signals, [
    {
      type: 'recovery',
      recovery: {
        schema: 'sse-recovery/v1',
        stream_kind: 'job_event',
        aggregate_id: 'job-v2',
        requested_after_seq: 1,
        replay_floor_seq: 1,
        durable_high_water_seq: 2,
        gap: false,
        snapshot_required: false,
        snapshot_schema: null,
        snapshot_revision: null,
        snapshot_asset_id: null,
        snapshot_hash: null,
      },
    },
    { type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-v2', job_event_seq: 2 } },
  ])
  assert.equal(
    queue.calls[2]!.url,
    '/api/v2/jobs/ws-1/job-v2/events/stream?after_seq=1&last_event_id=job%2Fjob-v2%2F1&requested_cursor_domain=job',
  )
})

test('Jobs v2 recovery gap uses its accepted Snapshot before the controller reconnects for the tail', async () => {
  const fixture = await golden()
  const queue = queuedFetch([
    response(fixture.list_result),
    response(terminalList(fixture.list_result)),
    response(fixture.sse_gap),
  ])
  const timers = new TimerHarness()
  const gateway = new JobHttpGateway({ fetch: queue.fetch, scheduler: timers })
  await gateway.discover('ws-1')
  const signals: JobStreamSignal[] = []

  const subscription = await gateway.connect('job-v2', 0, {
    signal: signal => signals.push(signal),
    closed: () => assert.fail('recovery request must not close a live controller subscription'),
  })
  await waitForScheduled(timers)
  assert.equal(timers.runNext(), 0)
  subscription.close()

  assert.equal(signals.length, 1)
  const recovery = signals[0]!.type === 'recovery' ? signals[0].recovery : assert.fail('expected recovery ingress')
  assert.equal(recovery.durable_high_water_seq, 4)
  assert.equal(recovery.snapshot_asset_id, 'job/job-v2/4')
  const snapshot = await gateway.readSnapshot('job-v2', { recovery })
  assert.equal(snapshot.job_event_high_water, 4)
  assert.equal(snapshot.snapshot_hash, fixture.sse_gap.snapshot.snapshot_hash)
})

test('Jobs v2 actions use the accepted control route and a fresh controller Snapshot read', async () => {
  const fixture = await golden()
  const commandResult = { ...fixture.command_result, command: 'resume', operation_key: 'webui-retry-op' }
  const queue = queuedFetch([
    response(fixture.list_result),
    response(terminalList(fixture.list_result)),
    response(commandResult),
  ])
  const gateway = new JobHttpGateway({
    fetch: queue.fetch,
    createOperationKey: () => 'webui-retry-op',
  })
  await gateway.discover('ws-1')

  await gateway.act('retry', 'job-v2')

  assert.equal(queue.calls[2]!.url, '/api/v2/jobs/ws-1/job-v2/resume')
  assert.deepEqual(JSON.parse(String(queue.calls[2]!.init?.body)), {
    schema: 'job-control-command/v2',
    operation_key: 'webui-retry-op',
    workspace_id: 'ws-1',
    job_id: 'job-v2',
    expected_job_revision: 3,
    reason: 'WebUI requested retry',
    command: 'resume',
    resume_intent_id: null,
  })
})

test('Jobs v2 HTTP failures and JSON decode failures surface actionable errors', async () => {
  const errorPayload = {
    schema: 'job-http-error/v2',
    error_code: 'malformed_request',
    message: 'workspace query is malformed',
    retryable: false,
    operation_key: null,
    cursor_domain: 'job',
  }
  const errorQueue = queuedFetch([response(errorPayload, 400)])
  const errorGateway = new JobHttpGateway({ fetch: errorQueue.fetch })
  await assert.rejects(
    errorGateway.discover('ws-1'),
    (error: unknown) => error instanceof JobHttpGatewayError
      && error.status === 400
      && error.errorCode === 'malformed_request',
  )

  const brokenFetch = (async (): Promise<Response> => new Response('{', {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })) as typeof globalThis.fetch
  const brokenGateway = new JobHttpGateway({ fetch: brokenFetch })
  await assert.rejects(brokenGateway.discover('ws-1'), /returned invalid JSON/)
})

test('finite recovery transport polls at a bounded cadence and close stops future requests', async () => {
  const fixture = await golden()
  const idle = noNewRecovery(fixture)
  const queue = queuedFetch([
    response(fixture.list_result),
    response(terminalList(fixture.list_result)),
    response(idle),
    response(idle),
  ])
  const timers = new TimerHarness()
  const gateway = new JobHttpGateway({ fetch: queue.fetch, scheduler: timers, pollDelayMs: 250 })
  await gateway.discover('ws-1')
  const signals: JobStreamSignal[] = []
  const subscription = await gateway.connect('job-v2', 1, {
    signal: signal => signals.push(signal),
    closed: error => assert.fail(error ?? 'unexpected transport close'),
  })

  await waitForScheduled(timers)
  assert.equal(queue.calls.length, 3)
  assert.equal(timers.runNext(), 0)
  assert.equal(signals.length, 1)
  assert.equal(timers.activeCount(), 1)
  assert.equal(timers.runNext(), 250)
  await waitForScheduled(timers)
  assert.equal(queue.calls.length, 4)
  assert.equal(timers.runNext(), 0)
  assert.equal(signals.length, 2)
  assert.deepEqual(timers.scheduledDelays, [0, 250, 0, 250])

  subscription.close()
  assert.equal(timers.activeCount(), 0)
  await settle()
  assert.equal(queue.calls.length, 4)
})

test('finite recovery reports transport errors once and close suppresses late ingress', async () => {
  const fixture = await golden()
  const offline = new Error('offline')
  const errorQueue = queuedFetch([
    response(fixture.list_result),
    response(terminalList(fixture.list_result)),
    offline,
  ])
  const errorTimers = new TimerHarness()
  const errorGateway = new JobHttpGateway({ fetch: errorQueue.fetch, scheduler: errorTimers })
  await errorGateway.discover('ws-1')
  const errors: unknown[] = []
  const erroredSubscription = await errorGateway.connect('job-v2', 1, {
    signal: () => assert.fail('failed transport must not signal'),
    closed: error => errors.push(error),
  })
  await settle()
  erroredSubscription.close()
  await settle()
  assert.deepEqual(errors, [offline])
  assert.equal(errorTimers.activeCount(), 0)

  let resolveRecovery: ((value: Response) => void) | undefined
  const lateRecovery = new Promise<Response>(resolve => { resolveRecovery = resolve })
  const calls: string[] = []
  const lateFetch = (async (input: RequestInfo | URL): Promise<Response> => {
    calls.push(String(input))
    if (calls.length === 1) return response(fixture.list_result)
    if (calls.length === 2) return response(terminalList(fixture.list_result))
    return lateRecovery
  }) as typeof globalThis.fetch
  const lateTimers = new TimerHarness()
  const lateGateway = new JobHttpGateway({ fetch: lateFetch, scheduler: lateTimers })
  await lateGateway.discover('ws-1')
  const lateSignals: JobStreamSignal[] = []
  const lateErrors: unknown[] = []
  const lateSubscription = await lateGateway.connect('job-v2', 1, {
    signal: signal => lateSignals.push(signal),
    closed: error => lateErrors.push(error),
  })
  lateSubscription.close()
  resolveRecovery?.(response(noNewRecovery(fixture)))
  await settle()
  assert.deepEqual(lateSignals, [])
  assert.deepEqual(lateErrors, [])
  assert.equal(lateTimers.activeCount(), 0)
})

test('composition creates a controller from the injected accepted gateway', () => {
  const gateway = {} as JobDrawerGateway
  const runtime = createJobDrawerRuntime({}, {
    createGateway: () => gateway,
    createController: received => {
      assert.equal(received, gateway)
      return { marker: 'controller' } as unknown as ReturnType<typeof createJobDrawerRuntime>['controller']
    },
  })
  assert.equal((runtime.controller as unknown as { marker: string }).marker, 'controller')
})

test('TaskDrawerHost owns loading, actions and unmount disposal without changing TaskDrawer', async () => {
  const source = await readFile(new URL('frontend/src/components/jobs/TaskDrawerHost.vue', root), 'utf8')
  for (const needle of ['controller.start(workspaceId)', 'controller.refresh()', 'controller.act(action, jobId)', 'controller.stop()', 'unsubscribe()']) {
    assert.match(source, new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  }
})
