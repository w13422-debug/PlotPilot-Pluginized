import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { PluginUiSession, type PluginUiAckV1, type PluginUiIntentV1 } from '../../frontend/src/plugin-host/uiSession.ts'
import type { PluginUiTreeV1 } from '../../frontend/src/plugin-host/slotHost.ts'

const fixtures = new URL('../../contracts/examples/fixtures/', import.meta.url)
async function fixture<T>(name: string): Promise<T> {
  return JSON.parse(await readFile(new URL(name, fixtures), 'utf8')) as T
}

function session() {
  return new PluginUiSession({
    uiSessionId: 'ui-session-1', workerInstanceId: 'worker-1', contributionId: 'contribution-1',
    slot: 'workbench.writing-assets.panel', generation_id: 'generation-1',
    plugin_release_id: 'a'.repeat(64), workspace_id: 'ws-1', workspace_revision_id: null, plan_revision_id: null,
  })
}

test('dispatches the exact P0 intent only after installing the exact P0 tree', async () => {
  const host = session()
  const intent = await fixture<PluginUiIntentV1>('plugin-ui-intent.json')
  assert.equal(host.decideIntent(intent).kind, 'ack')
  host.installTree(await fixture<PluginUiTreeV1>('plugin-ui-tree.json'))
  assert.equal(host.decideIntent(intent).kind, 'dispatch')
})

test('returns the same ACK for an identical duplicate and rejects payload drift', async () => {
  const host = session()
  host.installTree(await fixture<PluginUiTreeV1>('plugin-ui-tree.json'))
  const intent = await fixture<PluginUiIntentV1>('plugin-ui-intent.json')
  assert.equal(host.decideIntent(intent).kind, 'dispatch')
  const ack: PluginUiAckV1 = { schema: 'plugin-ui-ack/v1', intent_id: intent.intent_id, accepted: true, error_code: null, core_event_seq: null, job_id: null }
  host.recordAck(intent, ack)
  assert.deepEqual(host.decideIntent(intent), { kind: 'ack', ack, duplicate: true })
  const changed = { ...intent, operation_key: 'op-ui-changed' }
  const decision = host.decideIntent(changed)
  assert.equal(decision.kind, 'ack')
  if (decision.kind === 'ack') assert.equal(decision.ack.error_code, '1008')
})

test('rejects stale freshness, render and unknown actions without dispatching Core mutation', async () => {
  const tree = await fixture<PluginUiTreeV1>('plugin-ui-tree.json')
  const intent = await fixture<PluginUiIntentV1>('plugin-ui-intent.json')
  for (const changed of [
    { ...intent, render_seq: 2 },
    { ...intent, freshness: { ...intent.freshness, workspace_id: 'ws-other' } },
    { ...intent, action_id: 'publication.accept' },
  ]) {
    const host = session(); host.installTree(tree)
    assert.equal(host.decideIntent(changed).kind, 'ack')
  }
})
