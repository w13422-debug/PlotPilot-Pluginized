import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  createPluginManagementHttpGateway,
  PLUGIN_RELEASE_LIST_PATH,
  PluginManagementHttpError,
} from '../../../frontend/src/core/plugins/httpGateway.ts'

const hash = (character: string) => character.repeat(64)

function release(marker = 'a') {
  return {
    schema: 'plugin-release-view/v1',
    plugin_id: `com.plotpilot.${marker}`,
    version: '1.2.3',
    kind: 'code',
    package_hash: hash(marker),
    release_id: hash(marker === 'a' ? 'b' : 'a'),
    manifest: { plugin_id: `com.plotpilot.${marker}`, version: '1.2.3', kind: 'code' },
  }
}

test('plugin settings runtime loads the accepted v1 release list through one browser-relative GET', async () => {
  const calls: Array<{ url: string, method: string | undefined, accept: string | null }> = []
  const gateway = createPluginManagementHttpGateway({
    baseUrl: 'https://plotpilot.test/',
    fetch: (async (input, init) => {
      calls.push({
        url: String(input),
        method: init?.method,
        accept: new Headers(init?.headers).get('accept'),
      })
      return new Response(JSON.stringify({ schema: 'plugin-release-list/v1', releases: [release()] }), {
        headers: { 'Content-Type': 'application/json' },
      })
    }) as typeof globalThis.fetch,
  })

  const snapshot = await gateway.loadSnapshot()

  assert.deepEqual(calls, [{
    url: `https://plotpilot.test${PLUGIN_RELEASE_LIST_PATH}`,
    method: 'GET',
    accept: 'application/json',
  }])
  assert.deepEqual(snapshot.plans, [])
  assert.equal(snapshot.selected_plan, null)
  assert.equal(snapshot.operation, null)
  assert.deepEqual(snapshot.releases[0], {
    plugin_id: 'com.plotpilot.a',
    release_id: hash('b'),
    version: '1.2.3',
    package_hash: hash('a'),
    lifecycle_state: 'installed',
    package_present: true,
    active: false,
    lkg: false,
    pin_count: 0,
  })
})

test('plugin settings runtime exposes HTTP and payload failures instead of fabricating a snapshot', async () => {
  const unavailable = createPluginManagementHttpGateway({
    fetch: (async () => new Response(JSON.stringify({
      schema: 'plugin-api-local-error/v1', error_code: 'package_store_error', message: 'PackageStore unavailable', retryable: false,
    }), { status: 503, headers: { 'Content-Type': 'application/json' } })) as typeof globalThis.fetch,
  })
  await assert.rejects(unavailable.loadSnapshot(), (error: unknown) =>
    error instanceof PluginManagementHttpError && error.status === 503 && error.message === 'PackageStore unavailable')

  const malformed = createPluginManagementHttpGateway({
    fetch: (async () => new Response(JSON.stringify({ schema: 'plugin-release-list/v1', releases: [{}] }), {
      headers: { 'Content-Type': 'application/json' },
    })) as typeof globalThis.fetch,
  })
  await assert.rejects(malformed.loadSnapshot(), /unexpected schema shape/)
})

test('fixed settings registry keeps the plugin page visible without mounting a plugin runtime', () => {
  const registry = readFileSync('frontend/src/settings/registry.ts', 'utf8')
  const section = readFileSync('frontend/src/components/settings/plugin-management/PluginManagementSettingsSection.vue', 'utf8')
  const panel = readFileSync('frontend/src/components/settings/plugin-management/PluginManagementPanel.vue', 'utf8')
  assert.equal((registry.match(/id: 'plugin-management'/g) ?? []).length, 1)
  assert.match(registry, /PluginManagementSettingsSection\.vue/)
  assert.match(section, /<PluginManagementPanel \/>/)
  assert.doesNotMatch(section, /createPluginManagementRuntimeComposition|runtime\.gateway|:gateway=/)
  assert.match(section, /当前生产后端没有注册 Plugins v1 路由/)
  assert.match(section, /不会发起插件网络请求/)
  assert.doesNotMatch(section, /已接线的只读 Release/)
  assert.match(panel, /onMounted\(loadSnapshot\)/)
  assert.match(panel, /if \(!props\.gateway\) return/)
  assert.equal(section.includes('/api/v1/plugins'), false)
  assert.equal(readFileSync('frontend/src/core/plugins/httpGateway.ts', 'utf8').includes('/plans'), false)
})
