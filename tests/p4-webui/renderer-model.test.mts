import assert from 'node:assert/strict'
import test from 'node:test'
import { describeRenderer } from '../../frontend/src/plugin-host/rendererModel.ts'

test('maps a validated Host button node to a non-asset form renderer', () => {
  const node = { component: 'button', key: 'run', props: { label: 'Run', tone: 'primary', disabled: false }, children: [], event_ids: ['run-action'] }
  assert.deepEqual(describeRenderer(node), {
    component: 'button', kind: 'form', requiresAssetResolver: false, requiresCoreNativeControl: false,
  })
})

test('keeps asset-backed and Candidate controls behind their authority boundaries', () => {
  const base = { key: 'x', children: [], event_ids: [], props: {} }
  assert.equal(describeRenderer({ ...base, component: 'table' }).requiresAssetResolver, true)
  assert.equal(describeRenderer({ ...base, component: 'candidate_preview' }).requiresCoreNativeControl, true)
  assert.throws(() => describeRenderer({ ...base, component: 'iframe' }), /unknown_component/)
})
