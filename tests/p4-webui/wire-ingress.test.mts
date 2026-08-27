import assert from 'node:assert/strict'
import test from 'node:test'
import { ingestPluginUiAck, ingestPluginUiIntent, ingestPluginUiTree } from '../../frontend/src/plugin-host/wireIngress.ts'

test('stops tree, intent and ACK unknown ingress until P0 validators exist', () => {
  for (const ingest of [ingestPluginUiTree, ingestPluginUiIntent, ingestPluginUiAck]) assert.throws(() => ingest({ schema: 'placeholder' }), /validator_unavailable/)
})
test('rejects prototype-backed values and inherited schema before validator dispatch', () => {
  const inherited = Object.create({ schema: 'plugin-ui-intent/v1' })
  assert.throws(() => ingestPluginUiIntent(inherited), /prototype_rejected/)
  const nullProto = Object.create(null); nullProto.value = 1
  assert.throws(() => ingestPluginUiIntent(nullProto), /missing_own_schema/)
})
