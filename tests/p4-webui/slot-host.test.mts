import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import {
  PLUGIN_UI_SLOTS,
  installPluginUiTree,
  isPluginUiSlot,
  validatePluginUiTree,
  type PluginUiTreeV1,
} from '../../frontend/src/plugin-host/slotHost.ts'

const fixturePath = new URL('../../contracts/examples/fixtures/plugin-ui-tree.json', import.meta.url)

test('accepts the exact P0 plugin UI tree fixture', async () => {
  const tree = JSON.parse(await readFile(fixturePath, 'utf8')) as PluginUiTreeV1
  assert.deepEqual(validatePluginUiTree(tree), { ok: true })
  assert.equal(installPluginUiTree(null, tree).renderSeq, 1)
})

test('freezes the eleven v1 slots and rejects dynamic top-level slots', () => {
  assert.equal(PLUGIN_UI_SLOTS.length, 11)
  assert.equal(isPluginUiSlot('job.drawer.detail'), true)
  assert.equal(isPluginUiSlot('plugin.dynamic.navigation'), false)
})

test('rejects unknown components, props, events and stale renders', async () => {
  const tree = JSON.parse(await readFile(fixturePath, 'utf8')) as PluginUiTreeV1
  assert.equal(validatePluginUiTree({ ...tree, root: { ...tree.root, component: 'iframe' } }).ok, false)
  assert.equal(validatePluginUiTree({ ...tree, root: { ...tree.root, props: { ...tree.root.props, href: '/api' } } }).ok, false)
  assert.equal(validatePluginUiTree({ ...tree, root: { ...tree.root, event_ids: ['submit'] } }).ok, false)
  const installed = installPluginUiTree(null, tree)
  assert.throws(() => installPluginUiTree(installed, tree), /stale_render_seq/)
})
