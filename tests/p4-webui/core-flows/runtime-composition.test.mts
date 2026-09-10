import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  WEBUI_CORE_FLOW_ACTOR_ID,
  installCoreFlowRuntimeComposition,
} from '../../../frontend/src/core/flows/composition.ts'
import { installCoreFlowRuntime, type CoreFlowRuntimeBinding } from '../../../frontend/src/core/flows/runtime.ts'
import type { CoreFlowGateway } from '../../../frontend/src/core/flows/gateway.ts'

const composition = readFileSync(new URL('../../../frontend/src/core/flows/composition.ts', import.meta.url), 'utf8')
const main = readFileSync(new URL('../../../frontend/src/main.ts', import.meta.url), 'utf8')
const gateway = {} as CoreFlowGateway

test('bootstrap composition binds the accepted Core gateway and stable local actor', () => {
  let createGatewayCalls = 0
  let installed: CoreFlowRuntimeBinding | undefined

  installCoreFlowRuntimeComposition({
    createGateway: () => {
      createGatewayCalls += 1
      return gateway
    },
    installRuntime: binding => {
      installed = binding
    },
  })

  assert.equal(createGatewayCalls, 1)
  assert.equal(installed?.gateway, gateway)
  assert.equal(installed?.createdBy, 'plotpilot.webui.local')
  assert.equal(installed?.createdBy, WEBUI_CORE_FLOW_ACTOR_ID)
  assert.ok(installed?.createdBy.trim().length)
})

test('bootstrap initializes Core runtime after API initialization and before Vue mount', () => {
  const apiInitialization = main.indexOf('await initApiClient()')
  const runtimeComposition = main.indexOf('installCoreFlowRuntimeComposition()')
  const vueMount = main.indexOf("app.mount('#app')")

  assert.ok(main.includes("import { installCoreFlowRuntimeComposition } from './core/flows/composition.ts'"))
  assert.ok(apiInitialization >= 0)
  assert.ok(runtimeComposition > apiInitialization)
  assert.ok(vueMount > runtimeComposition)
})

test('composition uses only the accepted Core authority with default same-origin fetch', () => {
  assert.ok(composition.includes('const createGateway = dependencies.createGateway ?? createCoreHttpFlowGateway'))
  assert.ok(composition.includes('const installRuntime = dependencies.installRuntime ?? installCoreFlowRuntime'))
  assert.equal(composition.includes('novelApi') || composition.includes('chapterApi') || composition.includes('legacy') || composition.includes('fake'), false)
  assert.equal(composition.includes('baseUrl') || composition.includes('fetch:'), false)
})

test('runtime registration fails closed for empty and duplicate actor bindings', () => {
  assert.throws(
    () => installCoreFlowRuntime({ gateway, createdBy: '   ' }),
    /requires an authoritative created_by identity/,
  )

  installCoreFlowRuntime({ gateway, createdBy: WEBUI_CORE_FLOW_ACTOR_ID })

  assert.throws(
    () => installCoreFlowRuntime({ gateway, createdBy: WEBUI_CORE_FLOW_ACTOR_ID }),
    /already installed/,
  )
})
