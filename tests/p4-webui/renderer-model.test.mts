import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { describeRenderer } from '../../frontend/src/plugin-host/rendererModel.ts'
import type { PluginUiTreeV1 } from '../../frontend/src/plugin-host/slotHost.ts'

const fixturePath = new URL('../../contracts/examples/fixtures/plugin-ui-tree.json', import.meta.url)

test('maps the P0 button fixture to a non-asset form renderer', async () => {
  const tree = JSON.parse(await readFile(fixturePath, 'utf8')) as PluginUiTreeV1
  assert.deepEqual(describeRenderer(tree.root), {
    component: 'button', kind: 'form', requiresAssetResolver: false, requiresCoreNativeControl: false,
  })
})

test('keeps asset-backed and Candidate controls behind their authority boundaries', () => {
  const base = { key: 'x', children: [], event_ids: [], props: {} }
  assert.equal(describeRenderer({ ...base, component: 'table' }).requiresAssetResolver, true)
  assert.equal(describeRenderer({ ...base, component: 'candidate_preview' }).requiresCoreNativeControl, true)
  assert.throws(() => describeRenderer({ ...base, component: 'iframe' }), /unknown_component/)
})
