import assert from 'node:assert/strict'
import test from 'node:test'
import {
  PLUGIN_UI_SLOTS,
  isPluginUiSlot,
} from '../../frontend/src/plugin-host/slotHost.ts'

test('freezes the eleven v1 slots and rejects dynamic top-level slots', () => {
  assert.equal(PLUGIN_UI_SLOTS.length, 11)
  assert.equal(isPluginUiSlot('job.drawer.detail'), true)
  assert.equal(isPluginUiSlot('plugin.dynamic.navigation'), false)
})
