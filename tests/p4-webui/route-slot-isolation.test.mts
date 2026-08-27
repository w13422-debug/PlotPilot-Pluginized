import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const root = new URL('../../', import.meta.url)

test('keeps plugin Slot rendering out of the app and router ownership boundary', async () => {
  const [app, router] = await Promise.all([
    readFile(new URL('frontend/src/App.vue', root), 'utf8'),
    readFile(new URL('frontend/src/router/index.ts', root), 'utf8'),
  ])
  assert.doesNotMatch(app, /PluginSlotFrame|PluginTreeNodeRenderer/)
  assert.doesNotMatch(router, /plugin-host|PluginSlot/)
})

test('contains renderer exceptions inside the Slot frame', async () => {
  const frame = await readFile(new URL('frontend/src/components/plugin-host/PluginSlotFrame.vue', root), 'utf8')
  assert.match(frame, /onErrorCaptured/)
  assert.match(frame, /return false/)
  assert.match(frame, /当前 Slot 已隔离；固定导航和其他页面仍可使用。/)
})
