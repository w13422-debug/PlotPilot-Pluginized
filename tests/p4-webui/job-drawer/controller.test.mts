import assert from 'node:assert/strict'
import test from 'node:test'
import { JobDrawerController } from '../../../frontend/src/core/jobs/controller.ts'
import { parseJobSseRecovery } from '../../../frontend/src/core/jobs/ingress.ts'
import type {
  JobAction,
  JobDrawerGateway,
  JobDrawerScheduler,
  JobSseRecovery,
  JobStreamHandlers,
  JobStreamSubscription,
} from '../../../frontend/src/core/jobs/types.ts'
import type { JobSnapshot } from '../../../frontend/src/contracts/types.ts'
import { snapshot, withState } from './fixtures.ts'

interface ConnectCall {
  jobId: string
  after: number
  handlers: JobStreamHandlers
  handle: FakeSubscription
}

interface Deferred<T> {
  promise: Promise<T>
  resolve(value: T | PromiseLike<T>): void
  reject(reason?: unknown): void
}

function deferred<T>(): Deferred<T> {
  let resolve!: Deferred<T>['resolve']
  let reject!: Deferred<T>['reject']
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept
    reject = decline
  })
  return { promise, resolve, reject }
}

class FakeSubscription implements JobStreamSubscription {
  closeCount = 0
  close(): void { this.closeCount += 1 }
}

class TimerHarness implements JobDrawerScheduler {
  readonly pending: Array<{ delay: number; callback: () => void; cancelled: boolean }> = []
  set(delayMs: number, callback: () => void): unknown {
    const task = { delay: delayMs, callback, cancelled: false }
    this.pending.push(task)
    return task
  }
  clear(handle: unknown): void { (handle as { cancelled: boolean }).cancelled = true }
  runNext(): number {
    const task = this.pending.find(item => !item.cancelled)
    assert.ok(task)
    task.cancelled = true
    task.callback()
    return task.delay
  }
  activeCount(): number { return this.pending.filter(item => !item.cancelled).length }
}

class FakeGateway implements JobDrawerGateway {
  discovered: ReadonlyArray<Readonly<JobSnapshot>> = []
  nextRead: Readonly<JobSnapshot> = snapshot()
  nextAction: Readonly<JobSnapshot> | void = undefined
  discoverImpl?: (workspaceId: string) => Promise<ReadonlyArray<Readonly<JobSnapshot>>>
  readImpl?: (jobId: string, context?: unknown) => Promise<Readonly<JobSnapshot>>
  actImpl?: (action: JobAction, jobId: string) => Promise<Readonly<JobSnapshot> | void>
  readonly connectCalls: ConnectCall[] = []
  readonly discoverCalls: string[] = []
  readonly readCalls: Array<{ jobId: string; context?: unknown }> = []
  readonly actions: Array<{ action: JobAction; jobId: string }> = []

  async discover(workspaceId: string): Promise<ReadonlyArray<Readonly<JobSnapshot>>> {
    this.discoverCalls.push(workspaceId)
    return this.discoverImpl?.(workspaceId) ?? this.discovered
  }

  async readSnapshot(jobId: string, context?: unknown): Promise<Readonly<JobSnapshot>> {
    this.readCalls.push({ jobId, context })
    return this.readImpl?.(jobId, context) ?? this.nextRead
  }

  connect(jobId: string, after: number, handlers: JobStreamHandlers): JobStreamSubscription {
    const handle = new FakeSubscription()
    this.connectCalls.push({ jobId, after, handlers, handle })
    return handle
  }

  async act(action: JobAction, jobId: string): Promise<Readonly<JobSnapshot> | void> {
    this.actions.push({ action, jobId })
    return this.actImpl?.(action, jobId) ?? this.nextAction
  }
}

async function flush(): Promise<void> {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
}

async function eventually(predicate: () => boolean, message: string): Promise<void> {
  for (let index = 0; index < 40; index += 1) {
    if (predicate()) return
    await flush()
  }
  assert.fail(message)
}

function recovery(overrides: Partial<JobSseRecovery> = {}): JobSseRecovery {
  return {
    schema: 'sse-recovery/v1',
    stream_kind: 'job_event',
    aggregate_id: 'job-1',
    requested_after_seq: 1,
    replay_floor_seq: 2,
    durable_high_water_seq: 3,
    gap: true,
    snapshot_required: true,
    snapshot_schema: 'job-snapshot/v1',
    snapshot_revision: 2,
    snapshot_asset_id: 'asset-job-snapshot-2',
    snapshot_hash: '5'.repeat(64),
    ...overrides,
  }
}

test('refresh hydrates durable snapshots then attaches only backend-active jobs at projection cursors', async () => {
  const gateway = new FakeGateway()
  gateway.discovered = [
    withState('running', { job_id: 'job-running', job_event_high_water: 3 }),
    withState('needs_attention', { job_id: 'job-attention', job_event_high_water: 7 }),
    withState('partial', { job_id: 'job-partial', job_event_high_water: 9 }),
  ]
  const controller = new JobDrawerController(gateway)
  await controller.start('ws-1')
  await flush()
  assert.deepEqual(gateway.connectCalls.map(call => [call.jobId, call.after]).sort(), [
    ['job-attention', 7], ['job-running', 3],
  ])
  assert.equal(controller.store.connection('job-attention'), 'connected')
  controller.stop()
  assert.equal(gateway.connectCalls[0]!.handle.closeCount, 1)
})

test('continuous event refreshes an authoritative snapshot and terminal convergence detaches', async () => {
  const gateway = new FakeGateway()
  gateway.discovered = [snapshot({ job_event_high_water: 0 })]
  gateway.nextRead = withState('succeeded', {
    job_revision: 2,
    job_event_high_water: 1,
    snapshot_hash: '3'.repeat(64),
  })
  const controller = new JobDrawerController(gateway)
  await controller.start('ws-1')
  await flush()
  const call = gateway.connectCalls[0]!
  call.handlers.signal({ type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 1 } })
  await flush()
  assert.equal(controller.store.snapshot('job-1')!.job_state, 'succeeded')
  assert.equal(controller.store.observedCursor('job-1'), 1)
  assert.equal(controller.store.projectionCursor('job-1'), 1)
  assert.equal(call.handle.closeCount, 1)
  assert.equal(controller.store.connection('job-1'), 'detached')
})

test('disconnect reconnects from the projection cursor and fences synchronous late callbacks', async () => {
  const gateway = new FakeGateway()
  const timers = new TimerHarness()
  gateway.discovered = [snapshot({ job_event_high_water: 4 })]
  const controller = new JobDrawerController(gateway, undefined, {
    scheduler: timers,
    reconnectBaseDelayMs: 10,
    reconnectMaxDelayMs: 40,
  })
  await controller.start('ws-1')
  await flush()
  const old = gateway.connectCalls[0]!
  old.handlers.closed(new Error('offline'))
  assert.equal(timers.activeCount(), 1)
  assert.equal(timers.runNext(), 10)
  await flush()
  const replacement = gateway.connectCalls[1]!
  assert.equal(replacement.after, 4)
  assert.equal(controller.store.connection('job-1'), 'connected')

  old.handlers.signal({ type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 5 } })
  old.handlers.closed(new Error('late close'))
  await flush()
  assert.equal(controller.store.observedCursor('job-1'), 4)
  assert.equal(controller.store.connection('job-1'), 'connected')
  assert.equal(timers.activeCount(), 0)
})

test('gap recovery replaces the projection, closes the recovery handle and opens a live stream from the Snapshot cursor', async () => {
  const gateway = new FakeGateway()
  const timers = new TimerHarness()
  gateway.discovered = [snapshot({ job_event_high_water: 1 })]
  gateway.nextRead = snapshot({
    job_revision: 2,
    job_event_high_water: 3,
    snapshot_hash: '5'.repeat(64),
    steps: [{ step_id: 'replacement-step', state: 'running', revision: 2 }],
    attempts: [{ attempt_id: 'replacement-attempt', state: 'running', lease_epoch: 2 }],
  })
  const controller = new JobDrawerController(gateway, undefined, { scheduler: timers, reconnectBaseDelayMs: 1 })
  await controller.start('ws-1')
  await flush()
  gateway.connectCalls[0]!.handlers.signal({
    type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 3 },
  })
  await flush()
  timers.runNext()
  await flush()
  const recoveryCall = gateway.connectCalls[1]!
  recoveryCall.handlers.signal({ type: 'recovery', recovery: recovery() })
  await eventually(() => gateway.connectCalls.length === 3, 'recovery did not rotate into a third live connection')

  assert.deepEqual(gateway.connectCalls.map(call => call.after), [1, 1, 3])
  assert.equal(recoveryCall.handle.closeCount, 1)
  assert.equal(controller.store.projectionCursor('job-1'), 3)
  assert.deepEqual(controller.store.items()[0]!.steps.map(step => step.step_id), ['replacement-step'])
  assert.equal(controller.store.connection('job-1'), 'connected')
  assert.equal((gateway.readCalls[0]!.context as { recovery: JobSseRecovery }).recovery.snapshot_asset_id, 'asset-job-snapshot-2')

  gateway.nextRead = snapshot({
    job_revision: 3,
    job_event_high_water: 4,
    snapshot_hash: '7'.repeat(64),
  })
  gateway.connectCalls[2]!.handlers.signal({
    type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 4 },
  })
  await flush()
  assert.equal(controller.store.projectionCursor('job-1'), 4)
  assert.equal(controller.store.connection('job-1'), 'connected')
})

test('recovery rejects inconsistent replay-floor/gap tuples before reading or applying a Snapshot', async () => {
  const cases: Array<{ highWater: number; value: JobSseRecovery }> = [
    {
      highWater: 1,
      value: recovery({
        gap: false,
        snapshot_required: false,
        snapshot_schema: null,
        snapshot_revision: null,
        snapshot_asset_id: null,
        snapshot_hash: null,
      }),
    },
    {
      highWater: 2,
      value: recovery({ requested_after_seq: 2, replay_floor_seq: 2, durable_high_water_seq: 3 }),
    },
    {
      highWater: 1,
      value: recovery({ replay_floor_seq: 4, durable_high_water_seq: 3 }),
    },
  ]

  for (const [index, item] of cases.entries()) {
    const gateway = new FakeGateway()
    const timers = new TimerHarness()
    gateway.discovered = [snapshot({ job_event_high_water: item.highWater })]
    const controller = new JobDrawerController(gateway, undefined, { scheduler: timers })
    await controller.start('ws-1')
    await flush()
    gateway.connectCalls[0]!.handlers.signal({ type: 'recovery', recovery: item.value })
    await flush()
    assert.equal(gateway.readCalls.length, 0, `case ${index} unexpectedly read a Snapshot`)
    assert.equal(controller.store.projectionCursor('job-1'), item.highWater)
    assert.equal(timers.activeCount(), 1)
    controller.stop()
  }
})

test('invalid recovery metadata never applies and enters bounded reconnect', async () => {
  const gateway = new FakeGateway()
  const timers = new TimerHarness()
  gateway.discovered = [snapshot({ job_event_high_water: 1 })]
  const controller = new JobDrawerController(gateway, undefined, { scheduler: timers })
  await controller.start('ws-1')
  await flush()
  gateway.connectCalls[0]!.handlers.signal({
    type: 'recovery',
    recovery: recovery({ snapshot_hash: null }),
  })
  await flush()
  assert.equal(controller.store.projectionCursor('job-1'), 1)
  assert.equal(timers.activeCount(), 1)
  assert.equal(gateway.readCalls.length, 0)
})

test('a failed final-event read reconnects from projection and duplicate replay still hydrates terminal state', async () => {
  const gateway = new FakeGateway()
  const timers = new TimerHarness()
  gateway.discovered = [snapshot({ job_event_high_water: 0 })]
  let readNumber = 0
  gateway.readImpl = async () => {
    readNumber += 1
    if (readNumber === 1) throw new Error('Snapshot temporarily unavailable')
    return withState('succeeded', {
      job_revision: 2,
      job_event_high_water: 1,
      snapshot_hash: '3'.repeat(64),
    })
  }
  const controller = new JobDrawerController(gateway, undefined, {
    scheduler: timers,
    reconnectBaseDelayMs: 10,
  })
  await controller.start('ws-1')
  await flush()
  gateway.connectCalls[0]!.handlers.signal({
    type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 1 },
  })
  await flush()
  assert.equal(controller.store.observedCursor('job-1'), 1)
  assert.equal(controller.store.projectionCursor('job-1'), 0)
  assert.equal(timers.runNext(), 10)
  await flush()
  assert.equal(gateway.connectCalls[1]!.after, 0)
  assert.equal(controller.store.connection('job-1'), 'needs_snapshot')

  gateway.connectCalls[1]!.handlers.signal({
    type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 1 },
  })
  await flush()
  assert.equal(readNumber, 2)
  assert.equal(controller.store.projectionCursor('job-1'), 1)
  assert.equal(controller.store.snapshot('job-1')!.job_state, 'succeeded')
  assert.equal(controller.store.connection('job-1'), 'detached')
})

test('event bursts serialize Snapshot reads until projection covers the latest observed cursor', async () => {
  const gateway = new FakeGateway()
  const first = deferred<Readonly<JobSnapshot>>()
  const second = deferred<Readonly<JobSnapshot>>()
  gateway.discovered = [snapshot({ job_event_high_water: 0 })]
  gateway.readImpl = async () => gateway.readCalls.length === 1 ? first.promise : second.promise
  const controller = new JobDrawerController(gateway)
  await controller.start('ws-1')
  await flush()
  const call = gateway.connectCalls[0]!
  call.handlers.signal({ type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 1 } })
  await flush()
  call.handlers.signal({ type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 2 } })
  await flush()
  assert.equal(gateway.readCalls.length, 1)
  assert.equal(controller.store.observedCursor('job-1'), 2)
  assert.equal(controller.store.projectionCursor('job-1'), 0)

  first.resolve(snapshot({ job_revision: 2, job_event_high_water: 1, snapshot_hash: '3'.repeat(64) }))
  await eventually(() => gateway.readCalls.length === 2, 'burst did not trigger a second Snapshot read')
  assert.equal(controller.store.projectionCursor('job-1'), 1)
  assert.equal(controller.store.connection('job-1'), 'needs_snapshot')
  second.resolve(snapshot({ job_revision: 3, job_event_high_water: 2, snapshot_hash: '5'.repeat(64) }))
  await flush()
  assert.equal(controller.store.projectionCursor('job-1'), 2)
  assert.equal(controller.store.observedCursor('job-1'), 2)
  assert.equal(controller.store.connection('job-1'), 'connected')
})

test('a deferred read failure from an old subscription cannot close or reschedule its healthy replacement', async () => {
  const gateway = new FakeGateway()
  const timers = new TimerHarness()
  const oldRead = deferred<Readonly<JobSnapshot>>()
  let readNumber = 0
  gateway.discovered = [snapshot({ job_event_high_water: 0 })]
  gateway.readImpl = async () => {
    readNumber += 1
    if (readNumber === 1) return oldRead.promise
    return snapshot({ job_revision: 2, job_event_high_water: 1, snapshot_hash: '3'.repeat(64) })
  }
  const controller = new JobDrawerController(gateway, undefined, {
    scheduler: timers,
    reconnectBaseDelayMs: 10,
  })
  await controller.start('ws-1')
  await flush()
  const old = gateway.connectCalls[0]!
  old.handlers.signal({ type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 1 } })
  await flush()
  old.handlers.closed(new Error('offline'))
  timers.runNext()
  await flush()
  const replacement = gateway.connectCalls[1]!
  replacement.handlers.signal({
    type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 1 },
  })
  await flush()
  assert.equal(controller.store.connection('job-1'), 'connected')

  oldRead.reject(new Error('late old read failure'))
  await flush()
  assert.equal(replacement.handle.closeCount, 0)
  assert.equal(timers.activeCount(), 0)
  assert.equal(controller.store.connection('job-1'), 'connected')
  assert.equal(controller.store.projectionCursor('job-1'), 1)
})

test('an action barrier fences a pre-action recovery read and its late failure side effects', async () => {
  const gateway = new FakeGateway()
  const timers = new TimerHarness()
  const recoveryRead = deferred<Readonly<JobSnapshot>>()
  gateway.discovered = [snapshot({ job_event_high_water: 1 })]
  gateway.readImpl = async () => recoveryRead.promise
  gateway.actImpl = async () => withState('cancelling', {
    job_revision: 2,
    job_event_high_water: 3,
    snapshot_hash: '6'.repeat(64),
  })
  const controller = new JobDrawerController(gateway, undefined, { scheduler: timers })
  await controller.start('ws-1')
  await flush()
  const call = gateway.connectCalls[0]!
  call.handlers.signal({ type: 'recovery', recovery: recovery() })
  await flush()
  assert.equal(gateway.readCalls.length, 1)
  assert.equal(controller.store.connection('job-1'), 'needs_snapshot')

  await controller.act('cancel', 'job-1')
  assert.equal(controller.store.snapshot('job-1')!.job_state, 'cancelling')
  assert.equal(controller.store.connection('job-1'), 'connected')
  recoveryRead.reject(new Error('late pre-action recovery failure'))
  await flush()
  assert.equal(timers.activeCount(), 0)
  assert.equal(call.handle.closeCount, 0)
  assert.equal(controller.store.snapshot('job-1')!.job_state, 'cancelling')
})

test('only the latest refresh ordinal may atomically replace jobs and reconcile connections', async () => {
  const gateway = new FakeGateway()
  const jobA = snapshot({ job_id: 'job-a', snapshot_hash: 'a'.repeat(64) })
  const jobB = snapshot({ job_id: 'job-b', snapshot_hash: 'b'.repeat(64) })
  const jobC = snapshot({ job_id: 'job-c', snapshot_hash: 'c'.repeat(64) })
  gateway.discovered = [jobA]
  const controller = new JobDrawerController(gateway)
  await controller.start('ws-1')
  await flush()

  const older = deferred<ReadonlyArray<Readonly<JobSnapshot>>>()
  const newer = deferred<ReadonlyArray<Readonly<JobSnapshot>>>()
  const queue = [older, newer]
  gateway.discoverImpl = async () => queue.shift()!.promise
  const olderRefresh = controller.refresh()
  const newerRefresh = controller.refresh()
  newer.resolve([jobA, jobB])
  await newerRefresh
  await flush()
  older.resolve([jobA])
  await olderRefresh
  await flush()
  assert.deepEqual(controller.store.items().map(item => item.job_id).sort(), ['job-a', 'job-b'])
  const jobBConnection = gateway.connectCalls.find(call => call.jobId === 'job-b')
  assert.ok(jobBConnection)
  assert.equal(jobBConnection.handle.closeCount, 0)

  const revivingOlder = deferred<ReadonlyArray<Readonly<JobSnapshot>>>()
  const deletingNewer = deferred<ReadonlyArray<Readonly<JobSnapshot>>>()
  queue.push(revivingOlder, deletingNewer)
  const reviveRefresh = controller.refresh()
  const deleteRefresh = controller.refresh()
  deletingNewer.resolve([jobA])
  await deleteRefresh
  revivingOlder.resolve([jobA, jobC])
  await reviveRefresh
  assert.deepEqual(controller.store.items().map(item => item.job_id), ['job-a'])
})

test('event reads and action responses reject cross-Job Snapshots before touching the store', async () => {
  const eventGateway = new FakeGateway()
  const timers = new TimerHarness()
  eventGateway.discovered = [snapshot({ job_event_high_water: 0 })]
  eventGateway.nextRead = snapshot({
    job_id: 'job-2',
    job_revision: 2,
    job_event_high_water: 1,
    snapshot_hash: '9'.repeat(64),
  })
  const eventController = new JobDrawerController(eventGateway, undefined, { scheduler: timers })
  await eventController.start('ws-1')
  await flush()
  eventGateway.connectCalls[0]!.handlers.signal({
    type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 1 },
  })
  await flush()
  assert.deepEqual(eventController.store.items().map(item => item.job_id), ['job-1'])
  assert.equal(eventController.store.snapshot('job-2'), undefined)
  assert.equal(eventController.store.projectionCursor('job-1'), 0)
  assert.equal(eventController.store.observedCursor('job-1'), 1)
  assert.equal(timers.activeCount(), 1)

  const actionGateway = new FakeGateway()
  actionGateway.discovered = [withState('failed')]
  actionGateway.nextAction = withState('running', {
    job_id: 'job-2',
    job_revision: 2,
    snapshot_hash: '8'.repeat(64),
  })
  const actionController = new JobDrawerController(actionGateway)
  await actionController.start('ws-1')
  await assert.rejects(actionController.act('retry', 'job-1'), /identity mismatch/)
  assert.deepEqual(actionController.store.items().map(item => item.job_id), ['job-1'])
  assert.equal(actionController.pendingAction('job-1'), undefined)
})

test('action token preserves serialization across stop/start and clears only its own pending entry', async () => {
  const gateway = new FakeGateway()
  gateway.discovered = [withState('paused', { current_checkpoint_id: 'checkpoint-1' })]
  const oldAction = deferred<Readonly<JobSnapshot> | void>()
  const newAction = deferred<Readonly<JobSnapshot> | void>()
  let actionNumber = 0
  gateway.actImpl = async () => {
    actionNumber += 1
    return actionNumber === 1 ? oldAction.promise : newAction.promise
  }
  const controller = new JobDrawerController(gateway)
  await controller.start('ws-1')
  const oldPromise = controller.act('resume', 'job-1')
  await flush()
  assert.equal(controller.pendingAction('job-1'), 'resume')

  controller.stop()
  await controller.start('ws-1')
  await assert.rejects(controller.act('cancel', 'job-1'), /already has an action/)
  assert.equal(gateway.actions.length, 1)
  oldAction.resolve(undefined)
  await oldPromise
  assert.equal(controller.pendingAction('job-1'), undefined)

  const newPromise = controller.act('resume', 'job-1')
  await flush()
  assert.equal(controller.pendingAction('job-1'), 'resume')
  await assert.rejects(controller.act('cancel', 'job-1'), /already has an action/)
  assert.equal(gateway.actions.length, 2)
  newAction.resolve(withState('running', {
    job_revision: 2,
    snapshot_hash: '2'.repeat(64),
  }))
  await newPromise
  assert.equal(controller.pendingAction('job-1'), undefined)
  assert.equal(controller.store.snapshot('job-1')!.job_state, 'running')
})

test('void action fences a pre-action read and forces a causally newer authoritative Snapshot read', async () => {
  const gateway = new FakeGateway()
  const preActionRead = deferred<Readonly<JobSnapshot>>()
  const postActionRead = deferred<Readonly<JobSnapshot>>()
  const timeline: string[] = []
  gateway.discovered = [snapshot({ job_event_high_water: 0 })]
  gateway.readImpl = async () => {
    const number = gateway.readCalls.length
    timeline.push(`read-${number}-start`)
    return number === 1 ? preActionRead.promise : postActionRead.promise
  }
  gateway.actImpl = async () => {
    timeline.push('act-resolve')
    return undefined
  }
  const controller = new JobDrawerController(gateway)
  await controller.start('ws-1')
  await flush()
  gateway.connectCalls[0]!.handlers.signal({
    type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 1 },
  })
  await flush()
  const actionPromise = controller.act('cancel', 'job-1')
  await eventually(() => gateway.readCalls.length === 2, 'void action reused the pre-action Snapshot read')
  assert.deepEqual(timeline, ['read-1-start', 'act-resolve', 'read-2-start'])
  assert.equal(controller.pendingAction('job-1'), 'cancel')

  preActionRead.resolve(snapshot({
    job_revision: 2,
    job_event_high_water: 1,
    snapshot_hash: '3'.repeat(64),
    job_state: 'running',
  }))
  await flush()
  assert.equal(controller.store.projectionCursor('job-1'), 0)
  postActionRead.resolve(withState('cancelling', {
    job_revision: 2,
    job_event_high_water: 1,
    snapshot_hash: '4'.repeat(64),
  }))
  await actionPromise
  assert.equal(controller.store.snapshot('job-1')!.job_state, 'cancelling')
  assert.equal(controller.store.projectionCursor('job-1'), 1)
  assert.equal(controller.pendingAction('job-1'), undefined)
})

test('resume, retry and cancel use injected intents and apply returned Snapshots', async () => {
  const cases: Array<{
    action: JobAction
    initial: Readonly<JobSnapshot>
    replacementState: 'running' | 'cancelling'
  }> = [
    {
      action: 'resume',
      initial: withState('paused', { current_checkpoint_id: 'checkpoint-1' }),
      replacementState: 'running',
    },
    { action: 'retry', initial: withState('failed'), replacementState: 'running' },
    { action: 'cancel', initial: withState('running'), replacementState: 'cancelling' },
  ]
  for (const item of cases) {
    const gateway = new FakeGateway()
    gateway.discovered = [item.initial]
    gateway.nextAction = withState(item.replacementState, {
      job_revision: 2,
      snapshot_hash: '2'.repeat(64),
      attempts: [
        { attempt_id: 'attempt-1', state: 'failed', lease_epoch: 1 },
        { attempt_id: 'attempt-2', state: 'running', lease_epoch: 2 },
      ],
    })
    const controller = new JobDrawerController(gateway)
    await controller.start('ws-1')
    await controller.act(item.action, 'job-1')
    assert.deepEqual(gateway.actions, [{ action: item.action, jobId: 'job-1' }])
    assert.deepEqual(controller.store.items()[0]!.attempts.map(attempt => attempt.attempt_id), [
      'attempt-1',
      'attempt-2',
    ])
  }
})

test('immediate connection flaps use capped exponential backoff until a valid signal proves stability', async () => {
  const gateway = new FakeGateway()
  const timers = new TimerHarness()
  gateway.discovered = [snapshot({ job_event_high_water: 0 })]
  gateway.nextRead = snapshot({ job_revision: 2, job_event_high_water: 1, snapshot_hash: '3'.repeat(64) })
  const controller = new JobDrawerController(gateway, undefined, {
    scheduler: timers,
    reconnectBaseDelayMs: 10,
    reconnectMaxDelayMs: 40,
  })
  await controller.start('ws-1')
  await flush()

  const delays: number[] = []
  for (let index = 0; index < 4; index += 1) {
    gateway.connectCalls[index]!.handlers.closed(new Error(`flap-${index}`))
    delays.push(timers.runNext())
    await flush()
  }
  assert.deepEqual(delays, [10, 20, 40, 40])

  const stable = gateway.connectCalls[4]!
  stable.handlers.signal({
    type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 1 },
  })
  await flush()
  assert.equal(controller.store.connection('job-1'), 'connected')
  stable.handlers.closed(new Error('after stable signal'))
  assert.equal(timers.runNext(), 10)
})

test('private recovery ingress is closed, plain-data-only and job-domain-only', () => {
  const valid = recovery()
  assert.equal(parseJobSseRecovery(valid).aggregate_id, 'job-1')
  assert.throws(() => parseJobSseRecovery({ ...valid, extra: true }), /closed field set/)
  assert.throws(() => parseJobSseRecovery({ ...valid, stream_kind: 'core_event' }), /stream_kind/)
  assert.throws(() => parseJobSseRecovery(Object.assign(Object.create({ inherited: true }), valid)), /plain object/)
  const accessor = { ...valid } as Record<string, unknown>
  Object.defineProperty(accessor, 'snapshot_hash', { get: () => '5'.repeat(64), enumerable: true })
  assert.throws(() => parseJobSseRecovery(accessor), /data properties/)
})
