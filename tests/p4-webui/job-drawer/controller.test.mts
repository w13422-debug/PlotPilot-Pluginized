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
  readonly connectCalls: ConnectCall[] = []
  readonly readContexts: unknown[] = []
  readonly actions: Array<{ action: JobAction; jobId: string }> = []
  async discover(_workspaceId: string): Promise<ReadonlyArray<Readonly<JobSnapshot>>> { return this.discovered }
  async readSnapshot(_jobId: string, context?: unknown): Promise<Readonly<JobSnapshot>> {
    this.readContexts.push(context)
    return this.nextRead
  }
  connect(jobId: string, after: number, handlers: JobStreamHandlers): JobStreamSubscription {
    const handle = new FakeSubscription()
    this.connectCalls.push({ jobId, after, handlers, handle })
    return handle
  }
  async act(action: JobAction, jobId: string): Promise<Readonly<JobSnapshot> | void> {
    this.actions.push({ action, jobId })
    return this.nextAction
  }
}

async function flush(): Promise<void> {
  await Promise.resolve()
  await Promise.resolve()
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

test('refresh hydrates durable snapshots then attaches only backend-active jobs at their own cursors', async () => {
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
  gateway.nextRead = withState('succeeded', { job_revision: 2, job_event_high_water: 1, snapshot_hash: '3'.repeat(64) })
  const controller = new JobDrawerController(gateway)
  await controller.start('ws-1')
  await flush()
  const call = gateway.connectCalls[0]!
  call.handlers.signal({ type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 1 } })
  await flush()
  assert.equal(controller.store.snapshot('job-1')!.job_state, 'succeeded')
  assert.equal(controller.store.cursor('job-1'), 1)
  assert.equal(call.handle.closeCount, 1)
  assert.equal(controller.store.connection('job-1'), 'detached')
})

test('disconnect reconnects once from latest cursor and fences late callbacks from the old subscription', async () => {
  const gateway = new FakeGateway()
  const timers = new TimerHarness()
  gateway.discovered = [snapshot({ job_event_high_water: 4 })]
  const controller = new JobDrawerController(gateway, undefined, { scheduler: timers, reconnectBaseDelayMs: 10, reconnectMaxDelayMs: 40 })
  await controller.start('ws-1')
  await flush()
  const old = gateway.connectCalls[0]!
  old.handlers.closed(new Error('offline'))
  assert.equal(timers.activeCount(), 1)
  assert.equal(controller.store.connection('job-1'), 'reconnecting')
  assert.equal(timers.runNext(), 10)
  await flush()
  const replacement = gateway.connectCalls[1]!
  assert.equal(replacement.after, 4)
  assert.equal(controller.store.connection('job-1'), 'connected')

  old.handlers.signal({ type: 'event', cursor: { stream_kind: 'job_event', job_id: 'job-1', job_event_seq: 5 } })
  old.handlers.closed(new Error('late close'))
  await flush()
  assert.equal(controller.store.cursor('job-1'), 4)
  assert.equal(controller.store.connection('job-1'), 'connected')
  assert.equal(timers.activeCount(), 0)
})

test('gap reconnect requires a matching snapshot and replaces the complete projection', async () => {
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
  gateway.connectCalls[1]!.handlers.signal({ type: 'recovery', recovery: recovery() })
  await flush()
  assert.equal(controller.store.cursor('job-1'), 3)
  assert.deepEqual(controller.store.items()[0]!.steps.map(step => step.step_id), ['replacement-step'])
  assert.equal(controller.store.connection('job-1'), 'connected')
  assert.equal((gateway.readContexts[0] as { recovery: JobSseRecovery }).recovery.snapshot_asset_id, 'asset-job-snapshot-2')
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
  assert.equal(controller.store.cursor('job-1'), 1)
  assert.equal(timers.activeCount(), 1)
  assert.equal(gateway.readContexts.length, 0)
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

test('resume/retry/cancel use injected intents, serialize per Job and apply returned snapshot', async () => {
  const gateway = new FakeGateway()
  gateway.discovered = [withState('failed', { current_checkpoint_id: 'checkpoint-1' })]
  gateway.nextAction = withState('running', {
    job_revision: 2,
    job_event_high_water: 1,
    snapshot_hash: '3'.repeat(64),
    attempts: [
      { attempt_id: 'attempt-1', state: 'failed', lease_epoch: 1 },
      { attempt_id: 'attempt-2', state: 'running', lease_epoch: 2 },
    ],
  })
  const controller = new JobDrawerController(gateway)
  await controller.start('ws-1')
  await controller.act('resume', 'job-1')
  assert.deepEqual(gateway.actions, [{ action: 'resume', jobId: 'job-1' }])
  assert.deepEqual(controller.store.items()[0]!.attempts.map(item => item.attempt_id), ['attempt-1', 'attempt-2'])
})
