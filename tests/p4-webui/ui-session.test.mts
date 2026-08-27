import assert from 'node:assert/strict'
import test from 'node:test'
import type { PluginUIIntent } from '../../frontend/src/contracts/types.ts'
import { PluginUiSession, type RecordedIntentAck } from '../../frontend/src/plugin-host/uiSession.ts'
import type { ValidatedHostTree } from '../../frontend/src/plugin-host/slotHost.ts'

const tree = (renderSeq = 1): ValidatedHostTree => ({ treeId: `tree-${renderSeq}`, renderSeq,
  root: { component: 'button', key: 'run', props: { label: 'Run', tone: 'primary', disabled: false }, children: [], event_ids: ['run-action', 'click'] } })
const intent = (overrides: Partial<PluginUIIntent> = {}): PluginUIIntent => ({ schema: 'plugin-ui-intent/v1', intent_id: 'intent-1', intent_seq: 1,
  render_seq: 1, action_id: 'run-action', event_type: 'click', intent_kind: 'set_view_state', capability_id: null,
  payload_asset_id: null, operation_key: 'op-1', freshness: { generation_id: 'generation-1', plugin_release_id: 'a'.repeat(64),
    workspace_id: 'ws-1', workspace_revision_id: null, plan_revision_id: null }, ...overrides })
const session = () => new PluginUiSession({ uiSessionId: 'session-1', workerInstanceId: 'worker-1', contributionId: 'c-1',
  slot: 'workbench.writing-assets.panel', generation_id: 'generation-1', plugin_release_id: 'a'.repeat(64),
  workspace_id: 'ws-1', workspace_revision_id: null, plan_revision_id: null })

test('fences old-render duplicate before replaying a previously accepted ACK', () => {
  const host = session(); host.installValidatedTree(tree(1)); const first = intent()
  assert.equal(host.decideValidatedIntent(first).kind, 'dispatch')
  host.recordValidatedAck(first, { intentId: 'intent-1', accepted: true, errorCode: null, coreEventSeq: 2, jobId: null })
  host.installValidatedTree(tree(2))
  const replay = host.decideValidatedIntent(first)
  assert.equal(replay.kind, 'ack'); if (replay.kind === 'ack') { assert.equal(replay.ack.accepted, false); assert.equal(replay.duplicate, false) }
})

test('replays identical current-render ACK and rejects payload drift', () => {
  const host = session(); host.installValidatedTree(tree()); const first = intent(); host.decideValidatedIntent(first)
  const ack: RecordedIntentAck = { intentId: 'intent-1', accepted: true, errorCode: null, coreEventSeq: 2, jobId: null }
  host.recordValidatedAck(first, ack)
  const replay = host.decideValidatedIntent(first); assert.equal(replay.kind, 'ack'); if (replay.kind === 'ack') assert.equal(replay.ack.accepted, true)
  const drift = host.decideValidatedIntent(intent({ operation_key: 'changed' })); assert.equal(drift.kind, 'ack'); if (drift.kind === 'ack') assert.equal(drift.ack.errorCode, '1008')
})

test('deep-clones and freezes identity, installed tree, dispatch and ACK values', () => {
  const identity = { uiSessionId: 'session-1', workerInstanceId: 'worker-1', contributionId: 'c-1', slot: 'workbench.writing-assets.panel' as const,
    generation_id: 'generation-1', plugin_release_id: 'a'.repeat(64), workspace_id: 'ws-1', workspace_revision_id: null, plan_revision_id: null }
  const host = new PluginUiSession(identity); identity.workspace_id = 'mutated'; assert.equal(host.identity.workspace_id, 'ws-1'); assert.equal(Object.isFrozen(host.identity), true)
  const sourceTree = tree(); const installed = host.installValidatedTree(sourceTree); sourceTree.root.key = 'mutated'; assert.equal(installed.root.key, 'run'); assert.equal(Object.isFrozen(installed.root), true)
  const sourceIntent = intent(); const decision = host.decideValidatedIntent(sourceIntent); sourceIntent.operation_key = 'mutated'
  assert.equal(decision.kind, 'dispatch'); if (decision.kind === 'dispatch') { assert.equal(decision.intent.operation_key, 'op-1'); assert.equal(Object.isFrozen(decision.intent.freshness), true) }
  const sourceAck: RecordedIntentAck = { intentId: 'intent-1', accepted: true, errorCode: null, coreEventSeq: 2, jobId: null }; host.recordValidatedAck(intent(), sourceAck); sourceAck.accepted = false
  const replay = host.decideValidatedIntent(intent()); assert.equal(replay.kind, 'ack'); if (replay.kind === 'ack') { assert.equal(replay.ack.accepted, true); assert.equal(Object.isFrozen(replay.ack), true) }
})
