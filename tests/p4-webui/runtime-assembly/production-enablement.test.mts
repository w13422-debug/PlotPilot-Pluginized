import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { createSSRApp } from '../../../frontend/node_modules/vue/index.mjs'
import {
  installProductionFeatureRuntimeComposition,
  type ProductionFeatureRuntimeCompositionDependencies,
} from '../../../frontend/src/core/workbench/productionComposition.ts'
import { useFeatureRuntimeGateway } from '../../../frontend/src/core/workbench/FeatureRuntimeGateway.ts'

const root = new URL('../../../', import.meta.url)

function source(relative: string): string {
  return readFileSync(new URL(relative, root), 'utf8')
}

test('production composition installs one per-app gateway with only Candidate and Job Drawer available', () => {
  const gatewayOptions: Array<{ acceptedBy: string, fetch: typeof globalThis.fetch, hasBaseUrl: boolean }> = []
  const candidateGateway = {
    previewCandidate: async () => ({}),
    reviewCandidate: async () => ({}),
    acceptCandidate: async () => ({}),
  }
  const fetch = (() => Promise.reject(new Error('not called'))) as typeof globalThis.fetch
  const createCandidateGateway: NonNullable<ProductionFeatureRuntimeCompositionDependencies['createCandidateGateway']> = options => {
    gatewayOptions.push({ acceptedBy: options.acceptedBy, fetch: options.fetch, hasBaseUrl: 'baseUrl' in options })
    return candidateGateway
  }

  const firstApp = createSSRApp({})
  const secondApp = createSSRApp({})
  const first = installProductionFeatureRuntimeComposition(firstApp, { createCandidateGateway, fetch })
  const second = installProductionFeatureRuntimeComposition(secondApp, { createCandidateGateway, fetch })

  assert.notStrictEqual(first, second)
  assert.strictEqual(firstApp.runWithContext(() => useFeatureRuntimeGateway()), first)
  assert.strictEqual(secondApp.runWithContext(() => useFeatureRuntimeGateway()), second)
  assert.deepEqual(gatewayOptions, [
    { acceptedBy: 'plotpilot.webui.local', fetch, hasBaseUrl: false },
    { acceptedBy: 'plotpilot.webui.local', fetch, hasBaseUrl: false },
  ])
  assert.equal(first.availability.candidate.available, true)
  assert.equal(first.availability.jobDrawer.available, true)
  for (const surface of ['planning', 'generation', 'checkpoint', 'foreshadow', 'storyBible', 'export'] as const) {
    assert.equal(first.availability[surface].available, false, `${surface} must remain disabled`)
  }
})

test('production composition provides directly on the Vue app and does not create another Job runtime', () => {
  const composition = source('frontend/src/core/workbench/productionComposition.ts')
  const main = source('frontend/src/main.ts')

  assert.match(composition, /app\.provide\(FEATURE_RUNTIME_GATEWAY_KEY, runtime\)/)
  assert.doesNotMatch(composition, /runWithContext|createJobDrawerRuntime|JobDrawerController/)
  assert.match(composition, /createCoreV2CandidateHttpGateway/)
  assert.match(composition, /WEBUI_CORE_FLOW_ACTOR_ID/)
  assert.match(composition, /candidate: \{ available: true, reason: '' \}/)
  assert.match(composition, /jobDrawer: \{ available: true, reason: '' \}/)

  const apiInitialization = main.indexOf('await initApiClient()')
  const flowComposition = main.indexOf('installCoreFlowRuntimeComposition()')
  const productionComposition = main.indexOf('installProductionFeatureRuntimeComposition(app)')
  const vueMount = main.indexOf("app.mount('#app')")
  assert.match(main, /import \{ installProductionFeatureRuntimeComposition \} from '\.\/core\/workbench\/productionComposition\.ts'/)
  assert.ok(productionComposition > flowComposition)
  assert.ok(flowComposition > apiInitialization)
  assert.ok(vueMount > productionComposition)
})
