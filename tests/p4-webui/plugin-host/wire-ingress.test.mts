import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import {
  ingestPluginUiAck,
  ingestPluginUiIntent,
  ingestPluginUiMessage,
  ingestPluginUiTree,
} from '../../../frontend/src/plugin-host/wireIngress.ts'

const releaseId = 'a'.repeat(64)

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

const intent = {
  schema: 'plugin-ui-intent/v1',
  intent_id: 'intent-1',
  intent_seq: 1,
  render_seq: 1,
  action_id: 'click',
  event_type: 'click',
  intent_kind: 'set_view_state',
  capability_id: null,
  payload_asset_id: null,
  operation_key: 'op-1',
  freshness: {
    generation_id: 'generation-1',
    plugin_release_id: releaseId,
    workspace_id: 'workspace-1',
    workspace_revision_id: null,
    plan_revision_id: null,
  },
}

const ack = {
  schema: 'plugin-ui-ack/v1',
  intent_id: 'intent-1',
  accepted: true,
  error_code: null,
  core_event_seq: 2,
  job_id: null,
}

function message(
  direction: 'host_to_worker' | 'worker_to_host',
  messageType: 'init' | 'render' | 'intent' | 'ack' | 'error' | 'dispose',
  body: unknown,
  messageSeq = 1,
): Record<string, unknown> {
  return {
    schema: 'plugin-ui-message/v1',
    message_id: 'message-' + messageSeq,
    direction,
    message_seq: messageSeq,
    message_type: messageType,
    worker_instance_id: 'worker-1',
    plugin_release_id: releaseId,
    generation_id: 'generation-1',
    contribution_id: 'contribution-1',
    slot: 'workbench.writing-assets.panel',
    workspace_id: 'workspace-1',
    workspace_revision_id: null,
    plan_revision_id: null,
    body,
  }
}

test('uses the published P0 parsers for tree, intent and ACK ingress', () => {
  const parsedTree = ingestPluginUiTree(tree)
  const parsedIntent = ingestPluginUiIntent(intent)
  const parsedAck = ingestPluginUiAck(ack)
  assert.equal(parsedTree.tree_id, 'tree-1')
  assert.equal(parsedIntent.intent_id, 'intent-1')
  assert.equal(parsedAck.intent_id, 'intent-1')
  assert.equal(Object.isFrozen(parsedTree), true)
  assert.equal(Object.isFrozen(parsedIntent), true)
  assert.equal(Object.isFrozen(parsedAck), true)
})

test('normalizes a published message envelope without re-declaring its P0 body DTO', async () => {
  const fixture = JSON.parse(await readFile('contracts/examples/fixtures/plugin-ui-message.json', 'utf8'))
  const parsed = ingestPluginUiMessage(fixture)
  assert.deepEqual(
    {
      messageId: parsed.messageId,
      messageSeq: parsed.messageSeq,
      direction: parsed.direction,
      messageType: parsed.messageType,
      workerInstanceId: parsed.workerInstanceId,
      pluginReleaseId: parsed.pluginReleaseId,
    },
    {
      messageId: 'message-1',
      messageSeq: 1,
      direction: 'worker_to_host',
      messageType: 'render',
      workerInstanceId: 'worker-1',
      pluginReleaseId: releaseId,
    },
  )
  assert.equal(parsed.body.schema, 'plugin-ui-tree/v1')
  assert.equal(Object.isFrozen(parsed), true)
  assert.equal(Object.isFrozen(parsed.body), true)
})

test('accepts every closed direction/type/body branch and rejects matrix drift', () => {
  const init = {
    schema: 'plugin-ui-init/v1',
    ui_session_id: 'session-1',
    initial_render_seq: 0,
    initial_intent_seq: 0,
    contribution_config_asset_id: null,
  }
  const event = {
    schema: 'plugin-ui-event/v1',
    event_id: 'event-1',
    event_seq: 1,
    render_seq: 1,
    action_id: 'click',
    event_type: 'click',
    payload_asset_id: null,
    freshness: intent.freshness,
  }
  const error = {
    schema: 'plugin-ui-error/v1',
    code: 'failed',
    message: 'failed',
    details_asset_id: null,
    retryable: false,
  }
  const dispose = {
    schema: 'plugin-ui-dispose/v1',
    ui_session_id: 'session-1',
    reason: 'normal_shutdown',
    deadline_at: '2026-08-27T12:00:01Z',
  }
  for (const [direction, messageType, body] of [
    ['host_to_worker', 'init', init],
    ['host_to_worker', 'intent', event],
    ['host_to_worker', 'ack', ack],
    ['host_to_worker', 'error', error],
    ['host_to_worker', 'dispose', dispose],
    ['worker_to_host', 'render', tree],
    ['worker_to_host', 'intent', intent],
    ['worker_to_host', 'error', error],
  ] as const) {
    assert.equal(ingestPluginUiMessage(message(direction, messageType, body)).messageType, messageType)
  }
  assert.throws(() => ingestPluginUiMessage(message('worker_to_host', 'render', ack)), /oneOf|schema|const/)
  assert.throws(() => ingestPluginUiMessage(message('host_to_worker', 'ack', tree)), /oneOf|schema|const/)
})

test('rejects unknown tree components, props and events through the P0 closed parser', () => {
  const unknownComponent = structuredClone(tree)
  unknownComponent.root.component = 'iframe'
  assert.throws(() => ingestPluginUiTree(unknownComponent), /oneOf|component|schema/)

  const unknownProp = structuredClone(tree)
  unknownProp.root.props.extra = true
  assert.throws(() => ingestPluginUiTree(unknownProp), /unknown property|oneOf|schema/)

  const unknownEvent = structuredClone(tree)
  unknownEvent.root.event_ids = ['submit']
  assert.throws(() => ingestPluginUiTree(unknownEvent), /not allowed|oneOf|schema/)
})
