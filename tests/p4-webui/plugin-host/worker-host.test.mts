import assert from 'node:assert/strict'
import test from 'node:test'
import type { PluginUIIntent } from '../../../frontend/src/contracts/types.ts'
import {
  PluginWorkerHost,
  type PluginWorkerLike,
} from '../../../frontend/src/plugin-host/workerHost.ts'
import { buildPluginWorkerUrl, parsePluginWorkerUrl } from '../../../frontend/src/plugin-host/workerUrl.ts'
import { PluginSlotWatchdog, type SlotWatchdogOptions } from '../../../frontend/src/plugin-host/watchdog.ts'

const releaseId = 'a'.repeat(64)
const bundleHash = 'b'.repeat(64)

const identity = {
  uiSessionId: 'session-1',
  workerInstanceId: 'worker-1',
  contributionId: 'contribution-1',
  slot: 'workbench.writing-assets.panel' as const,
  generation_id: 'generation-1',
  plugin_release_id: releaseId,
  workspace_id: 'workspace-1',
  workspace_revision_id: null,
  plan_revision_id: null,
}

const tree = {
  schema: 'plugin-ui-tree/v1',
  tree_id: 'tree-1',
  render_seq: 1,
  root: {
    component: 'button',
    key: 'run',
    props: { label: 'Run', tone: 'primary', disabled: false },
    children: [],
    event_ids: ['click'],
  },
}

const intent: PluginUIIntent = {
  schema: 'plugin-ui-intent/v1',
  intent_id: 'intent-1',
  intent_seq: 1,
  render_seq: 1,
  action_id: 'click',
  event_type: 'click',
  intent_kind: 'set_view_state',
  capability_id: null,
  payload_asset_id: null,
  operation_key: 'operation-1',
  freshness: {
    generation_id: identity.generation_id,
    plugin_release_id: identity.plugin_release_id,
    workspace_id: identity.workspace_id,
    workspace_revision_id: identity.workspace_revision_id,
    plan_revision_id: identity.plan_revision_id,
  },
}

function workerMessage(
  messageType: 'render' | 'intent' | 'error',
  body: unknown,
  messageSeq: number,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    schema: 'plugin-ui-message/v1',
    message_id: 'worker-message-' + messageSeq,
    direction: 'worker_to_host',
    message_seq: messageSeq,
    message_type: messageType,
    worker_instance_id: identity.workerInstanceId,
    plugin_release_id: identity.plugin_release_id,
    generation_id: identity.generation_id,
    contribution_id: identity.contributionId,
    slot: identity.slot,
    workspace_id: identity.workspace_id,
    workspace_revision_id: identity.workspace_revision_id,
    plan_revision_id: identity.plan_revision_id,
    body,
    ...overrides,
  }
}

class FakeWorker implements PluginWorkerLike {
  posted: unknown[] = []
  terminated = 0
  onmessage: PluginWorkerLike['onmessage'] = null
  onerror: PluginWorkerLike['onerror'] = null
  onmessageerror: PluginWorkerLike['onmessageerror'] = null

  postMessage(message: unknown): void {
    this.posted.push(message)
  }

  terminate(): void {
    this.terminated += 1
  }

  emit(value: unknown): void {
    this.onmessage?.({ data: value } as MessageEvent<unknown>)
  }
}

class TimerHarness {
  readonly timers: Array<{ handler: () => void; cancelled: boolean }> = []
  readonly options: Omit<SlotWatchdogOptions, 'timeoutMs' | 'onTimeout'> = {
    setTimeout: handler => {
      const timer = { handler, cancelled: false }
      this.timers.push(timer)
      return timer
    },
    clearTimeout: handle => {
      ;(handle as { cancelled: boolean }).cancelled = true
    },
  }

  fireLatest(): void {
    const timer = this.timers[this.timers.length - 1]
    if (timer !== undefined && !timer.cancelled) timer.handler()
  }
}

function newHost(
  worker: FakeWorker,
  timer = new TimerHarness(),
  extra: Partial<ConstructorParameters<typeof PluginWorkerHost>[0]> = {},
): PluginWorkerHost {
  return new PluginWorkerHost({
    identity,
    workerUrl: buildPluginWorkerUrl(releaseId, bundleHash),
    workerFactory: () => worker,
    watchdogTimeoutMs: 10,
    watchdogTimer: timer.options,
    now: () => new Date('2026-08-27T12:00:00.000Z'),
    ...extra,
  })
}

test('starts a Dedicated Worker with init seq 1 and routes validated render/intent messages', () => {
  const worker = new FakeWorker()
  const rendered: unknown[] = []
  const intents: unknown[] = []
  const host = newHost(worker, new TimerHarness(), {
    onRender: treeValue => rendered.push(treeValue),
    onIntent: intentValue => {
      intents.push(intentValue)
      return { intentId: intentValue.intent_id, accepted: true, errorCode: null, coreEventSeq: 3, jobId: null }
    },
  })

  host.start()
  assert.equal(host.state, 'running')
  assert.equal(worker.posted.length, 1)
  assert.deepEqual(worker.posted[0], {
    schema: 'plugin-ui-message/v1',
    message_id: 'host-message-1',
    direction: 'host_to_worker',
    message_seq: 1,
    message_type: 'init',
    worker_instance_id: identity.workerInstanceId,
    plugin_release_id: identity.plugin_release_id,
    generation_id: identity.generation_id,
    contribution_id: identity.contributionId,
    slot: identity.slot,
    workspace_id: identity.workspace_id,
    workspace_revision_id: null,
    plan_revision_id: null,
    body: {
      schema: 'plugin-ui-init/v1',
      ui_session_id: identity.uiSessionId,
      initial_render_seq: 0,
      initial_intent_seq: 0,
      contribution_config_asset_id: null,
    },
  })

  worker.emit(workerMessage('render', tree, 1))
  worker.emit(workerMessage('intent', intent, 2))
  assert.equal(rendered.length, 1)
  assert.equal(intents.length, 1)
  assert.equal(worker.posted.length, 2)
  const ack = worker.posted[1] as Record<string, any>
  assert.equal(ack.direction, 'host_to_worker')
  assert.equal(ack.message_seq, 2)
  assert.deepEqual(ack.body, {
    schema: 'plugin-ui-ack/v1',
    intent_id: 'intent-1',
    accepted: true,
    error_code: null,
    core_event_seq: 3,
    job_id: null,
  })
})

test('fences identity and out-of-order worker messages by terminating only the current Worker', () => {
  const worker = new FakeWorker()
  const protocolErrors: Error[] = []
  const host = newHost(worker, new TimerHarness(), { onProtocolError: error => protocolErrors.push(error) })
  host.start()
  worker.emit(workerMessage('render', tree, 1, { generation_id: 'old-generation' }))
  assert.equal(worker.terminated, 1)
  assert.equal(host.state, 'failed')
  assert.match(protocolErrors[0]?.message ?? '', /identity_mismatch/)

  const secondWorker = new FakeWorker()
  const secondHost = newHost(secondWorker, new TimerHarness())
  secondHost.start()
  secondWorker.emit(workerMessage('render', tree, 1))
  secondWorker.emit(workerMessage('render', tree, 1))
  assert.equal(secondWorker.terminated, 1)
  assert.equal(secondHost.state, 'failed')
})

test('late events from a released Worker cannot affect its replacement', () => {
  const oldWorker = new FakeWorker()
  const host = newHost(oldWorker, new TimerHarness())
  host.start()
  const oldHandler = oldWorker.onmessage
  host.stop()
  assert.equal(oldWorker.terminated, 1)

  const replacement = new FakeWorker()
  ;(host as unknown as { workerFactory: (url: string) => FakeWorker }).workerFactory = () => replacement
  host.start()
  oldHandler?.({ data: workerMessage('render', tree, 1) } as MessageEvent<unknown>)
  assert.equal(replacement.terminated, 0)
  assert.equal(host.state, 'running')
})

test('watchdog timeout terminates the current Worker once and remains Slot-local', () => {
  const first = new FakeWorker()
  const firstTimer = new TimerHarness()
  const firstHost = newHost(first, firstTimer)
  firstHost.start()
  firstTimer.fireLatest()
  assert.equal(first.terminated, 1)
  assert.equal(firstHost.state, 'failed')

  const second = new FakeWorker()
  const secondTimer = new TimerHarness()
  const secondHost = newHost(second, secondTimer)
  secondHost.start()
  firstTimer.fireLatest()
  assert.equal(second.terminated, 0)
  secondHost.stop()
})

test('the standalone watchdog identity-fences replacement workers', () => {
  const timer = new TimerHarness()
  const first = new FakeWorker()
  const second = new FakeWorker()
  const watchdog = new PluginSlotWatchdog({
    timeoutMs: 10,
    ...timer.options,
  })
  watchdog.arm(first)
  watchdog.arm(second)
  timer.timers[0]!.handler()
  assert.equal(first.terminated, 0)
  assert.equal(second.terminated, 0)
  timer.timers[1]!.handler()
  assert.equal(second.terminated, 1)
})

test('accepts only the immutable same-origin release/hash Worker route', () => {
  const path = buildPluginWorkerUrl(releaseId, bundleHash)
  const absolute = buildPluginWorkerUrl(releaseId, bundleHash, 'https://plotpilot.test')
  assert.equal(parsePluginWorkerUrl(path, { expectedOrigin: 'https://plotpilot.test' }).releaseId, releaseId)
  assert.equal(parsePluginWorkerUrl(absolute, { expectedOrigin: 'https://plotpilot.test' }).bundleHash, bundleHash)
  for (const candidate of [
    'blob:https://plotpilot.test/worker',
    'data:text/javascript,worker',
    'https://evil.test' + path,
    path + '?cache=1',
    path + '/',
    path.replace(bundleHash, bundleHash.toUpperCase()),
    path.replace('/worker.js', '/worker.js/../worker.js'),
  ]) {
    assert.throws(() => parsePluginWorkerUrl(candidate, { expectedOrigin: 'https://plotpilot.test' }))
  }
})
