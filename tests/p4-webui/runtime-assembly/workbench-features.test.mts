import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'
import {
  FEATURE_RUNTIME_GATEWAY_KEY,
  bindWorkspaceCandidateReview,
  createCoreV2CandidateHttpGateway,
  createFeatureRuntimeGateway,
  useFeatureRuntimeGateway,
  type CandidateIdentity,
  type CandidateReviewDraft,
  type CoreV2CandidateGateway,
} from '../../../frontend/src/core/workbench/FeatureRuntimeGateway.ts'

const root = new URL('../../../', import.meta.url)
const frontendRoot = resolve(fileURLToPath(new URL('../../../frontend/', import.meta.url)))
const dependencyRequire = createRequire(join(frontendRoot, 'package.json'))

async function source(relative: string): Promise<string> {
  return readFile(new URL(relative, root), 'utf8')
}

type JsonRecord = Record<string, unknown>

type FeatureComponentName = 'candidate' | 'generation' | 'storyState'

interface HostNode {
  readonly type: string
  text: string
  readonly props: Record<string, unknown>
  readonly children: HostNode[]
  parent: HostNode | null
  value: unknown
  selected: boolean
}

interface MountedFeatureComponent {
  readonly root: HostNode
  readonly events: Array<{ name: string; args: unknown[] }>
  click(node: HostNode): Promise<void>
  setValue(node: HostNode, value: string): Promise<void>
  unmount(): void
}

interface FeatureComponentModule {
  mountFeature(name: FeatureComponentName, props: Record<string, unknown>): Promise<MountedFeatureComponent>
}

let compiledFeatureModule: Promise<FeatureComponentModule> | undefined

const featureEntrySource = (candidatePath: string, generationPath: string, storyStatePath: string): string => String.raw`
import { createRenderer, nextTick } from 'vue'
import CandidateReviewPanel from ${JSON.stringify(candidatePath)}
import GenerationControls from ${JSON.stringify(generationPath)}
import StoryStatePanel from ${JSON.stringify(storyStatePath)}

if (globalThis.Document === undefined) globalThis.Document = class Document {}
if (globalThis.ShadowRoot === undefined) globalThis.ShadowRoot = class ShadowRoot {}

const components = Object.freeze({
  candidate: CandidateReviewPanel,
  generation: GenerationControls,
  storyState: StoryStatePanel,
})

function collectOptions(node) {
  return node.children.flatMap(child => child.type === 'option' ? [child] : collectOptions(child))
}

function hostNode(type, text = '') {
  const listeners = Object.create(null)
  return {
    type,
    text,
    props: {},
    children: [],
    parent: null,
    value: '',
    defaultValue: '',
    selected: false,
    multiple: false,
    get parentNode() { return this.parent },
    get options() { return collectOptions(this) },
    addEventListener(name, handler) {
      ;(listeners[name] ??= []).push(handler)
    },
    removeEventListener(name, handler) {
      const handlers = listeners[name]
      if (handlers === undefined) return
      const index = handlers.indexOf(handler)
      if (index >= 0) handlers.splice(index, 1)
    },
    dispatchEvent(event) {
      const payload = { ...event, target: this, currentTarget: this }
      for (const handler of listeners[event.type] ?? []) handler(payload)
      return true
    },
    getRootNode() { return { activeElement: null } },
  }
}

const renderer = createRenderer({
  patchProp(element, key, _previous, next) {
    if (next == null) delete element.props[key]
    else element.props[key] = next
    if (key === 'value') element.value = next ?? ''
    if (key === 'selected') element.selected = Boolean(next)
    if (key === 'multiple') element.multiple = Boolean(next)
  },
  insert(child, parent, anchor = null) {
    if (child.parent != null) {
      const oldIndex = child.parent.children.indexOf(child)
      if (oldIndex >= 0) child.parent.children.splice(oldIndex, 1)
    }
    child.parent = parent
    const index = anchor == null ? -1 : parent.children.indexOf(anchor)
    if (index < 0) parent.children.push(child)
    else parent.children.splice(index, 0, child)
  },
  remove(child) {
    if (child.parent == null) return
    const index = child.parent.children.indexOf(child)
    if (index >= 0) child.parent.children.splice(index, 1)
    child.parent = null
  },
  createElement(type) { return hostNode(type) },
  createText(text) { return hostNode('#text', text) },
  createComment(text) { return hostNode('#comment', text) },
  setText(node, text) { node.text = text },
  setElementText(element, text) {
    element.text = text
    element.children = []
  },
  parentNode(node) { return node.parent },
  nextSibling(node) {
    if (node.parent == null) return null
    const index = node.parent.children.indexOf(node)
    return node.parent.children[index + 1] ?? null
  },
  setScopeId(element, id) { element.props[id] = '' },
  insertStaticContent(content, parent, anchor) {
    const node = hostNode('#static', content)
    this.insert(node, parent, anchor)
    return [node, node]
  },
})

function emitInto(events, name) {
  return (...args) => events.push({ name, args })
}

function invoke(listener, event) {
  if (Array.isArray(listener)) {
    for (const item of listener) item(event)
  } else if (typeof listener === 'function') {
    listener(event)
  }
}

export async function mountFeature(name, props = {}) {
  const component = components[name]
  if (component === undefined) throw new Error('unknown feature component: ' + name)
  const root = hostNode('root')
  const events = []
  const app = renderer.createApp(component, {
    ...props,
    onPreview: emitInto(events, 'preview'),
    onReview: emitInto(events, 'review'),
    onAccept: emitInto(events, 'accept'),
    onShowTaskStatus: emitInto(events, 'showTaskStatus'),
    onRefreshForeshadow: emitInto(events, 'refreshForeshadow'),
    onRefreshStoryBible: emitInto(events, 'refreshStoryBible'),
  })
  app.mount(root)
  await nextTick()
  return {
    root,
    events,
    async click(node) {
      if (node.props.disabled !== true) {
        invoke(node.props.onClick, { type: 'click', target: node, currentTarget: node })
      }
      await nextTick()
    },
    async setValue(node, value) {
      if (node.props.disabled === true) {
        await nextTick()
        return
      }
      node.value = value
      if (node.type === 'select') {
        for (const option of node.options) option.selected = String(option.value) === value
      }
      node.dispatchEvent({ type: node.type === 'select' ? 'change' : 'input' })
      await nextTick()
    },
    unmount() { app.unmount() },
  }
}
`

async function compileFeatureComponents(): Promise<FeatureComponentModule> {
  if (compiledFeatureModule !== undefined) return compiledFeatureModule
  compiledFeatureModule = (async () => {
    const viteEntry = dependencyRequire.resolve('vite')
    const vuePluginEntry = dependencyRequire.resolve('@vitejs/plugin-vue')
    const vuePackage = dependencyRequire.resolve('vue/package.json')
    const [{ build }, { default: vue }] = await Promise.all([
      import(pathToFileURL(viteEntry).href),
      import(pathToFileURL(vuePluginEntry).href),
    ])
    const componentPath = (relative: string): string => resolve(frontendRoot, relative).replaceAll('\\', '/')
    const vueRuntime = resolve(dirname(vuePackage), 'dist/vue.runtime.esm-bundler.js').replaceAll('\\', '/')
    const virtualEntry = 'virtual:workbench-feature-components'
    const virtualPlugin = {
      name: 'workbench-feature-component-tests',
      enforce: 'pre' as const,
      resolveId(id: string) {
        return id === virtualEntry ? `\0${virtualEntry}` : null
      },
      load(id: string) {
        return id === `\0${virtualEntry}`
          ? featureEntrySource(
            componentPath('src/components/workbench/CandidateReviewPanel.vue'),
            componentPath('src/components/workbench/GenerationControls.vue'),
            componentPath('src/components/workbench/StoryStatePanel.vue'),
          )
          : null
      },
    }
    const result = await build({
      root: frontendRoot,
      configFile: false,
      logLevel: 'silent',
      plugins: [virtualPlugin, vue()],
      resolve: {
        alias: [{ find: 'vue', replacement: vueRuntime }],
        dedupe: ['vue'],
      },
      define: {
        __VUE_OPTIONS_API__: 'true',
        __VUE_PROD_DEVTOOLS__: 'false',
        __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false',
      },
      build: {
        write: false,
        target: 'node20',
        minify: false,
        cssCodeSplit: false,
        modulePreload: false,
        rollupOptions: {
          input: virtualEntry,
          preserveEntrySignatures: 'strict',
          output: { format: 'es', entryFileNames: 'workbench-feature-components.js' },
        },
      },
    })
    const output = Array.isArray(result) ? result.flatMap(item => item.output) : result.output
    const chunk = output.find(item => item.type === 'chunk' && item.isEntry)
    assert.ok(chunk && chunk.type === 'chunk', 'Vite did not produce the feature-component test chunk')
    const encoded = Buffer.from(chunk.code, 'utf8').toString('base64')
    const loaded = await import(`data:text/javascript;base64,${encoded}`) as FeatureComponentModule
    assert.equal(typeof loaded.mountFeature, 'function')
    return loaded
  })()
  return compiledFeatureModule
}

function walk(node: HostNode): HostNode[] {
  return [node, ...node.children.flatMap(walk)]
}

function textContent(node: HostNode): string {
  return node.text + node.children.map(textContent).join('')
}

function findAll(rootNode: HostNode, predicate: (node: HostNode) => boolean): HostNode[] {
  return walk(rootNode).filter(predicate)
}

function byText(rootNode: HostNode, type: string, text: string): HostNode | undefined {
  return findAll(rootNode, node => node.type === type && textContent(node).trim() === text)[0]
}

interface HttpExchange {
  readonly route_id: string
  readonly request: JsonRecord
  readonly response: JsonRecord
  readonly status: number
}

async function httpExchanges(): Promise<ReadonlyArray<HttpExchange>> {
  const fixture = JSON.parse(await source('contracts/golden/m4-m5-public-surface-v2/http.json')) as {
    exchanges: HttpExchange[]
  }
  return fixture.exchanges
}

function exchangeFor(exchanges: ReadonlyArray<HttpExchange>, routeId: string): HttpExchange {
  const exchange = exchanges.find(item => item.route_id === routeId)
  assert.ok(exchange, `missing HTTP golden for ${routeId}`)
  return exchange
}

function jsonResponse(exchange: HttpExchange, response = exchange.response): Response {
  return new Response(JSON.stringify(response), {
    status: exchange.status,
    headers: { 'content-type': 'application/json' },
  })
}

test('F4 Candidate preview, review, and accept delegate only to an available Core v2 gateway', async () => {
  const calls: Array<{ operation: string, input: CandidateIdentity | (CandidateIdentity & { decision: string, expectedStatus: string }) }> = []
  const core: CoreV2CandidateGateway = {
    previewCandidate: async identity => { calls.push({ operation: 'preview', input: identity }); return { schema: 'candidate-preview-result/v2' } },
    reviewCandidate: async input => { calls.push({ operation: 'review', input }); return { schema: 'candidate-review-result/v2' } },
    acceptCandidate: async identity => { calls.push({ operation: 'accept', input: identity }); return { schema: 'publication-result/v2' } },
  }
  const runtime = createFeatureRuntimeGateway({
    availability: { candidate: { available: true, reason: '' } },
    coreV2CandidateGateway: core,
  })
  const identity = { workspaceId: 'workspace-1', candidateId: 'candidate-1' }
  await runtime.previewCandidate(identity)
  await runtime.reviewCandidate({ ...identity, decision: 'approve', expectedStatus: 'complete' })
  await runtime.acceptCandidate(identity)
  assert.deepEqual(calls, [
    { operation: 'preview', input: identity },
    { operation: 'review', input: { ...identity, decision: 'approve', expectedStatus: 'complete' } },
    { operation: 'accept', input: identity },
  ])
})

test('F4 Workspace-owned review binding ignores panel authority and reaches the runtime with the full input', async () => {
  const calls: unknown[] = []
  const runtime = createFeatureRuntimeGateway({
    availability: { candidate: { available: true, reason: '' } },
    coreV2CandidateGateway: {
      previewCandidate: async () => ({}),
      reviewCandidate: async input => { calls.push(input); return { reviewed: true } },
      acceptCandidate: async () => ({}),
    },
  })
  const panelDraft: CandidateReviewDraft & { readonly workspaceId: string } = {
    workspaceId: 'workspace-from-untrusted-panel',
    candidateId: '  candidate-owned  ',
    decision: 'reject',
    expectedStatus: 'partial',
  }
  const currentIdentity: CandidateIdentity = {
    workspaceId: 'workspace-current',
    candidateId: panelDraft.candidateId.trim(),
  }

  const input = bindWorkspaceCandidateReview(currentIdentity, panelDraft)
  assert.deepEqual(input, {
    workspaceId: 'workspace-current',
    candidateId: 'candidate-owned',
    decision: 'reject',
    expectedStatus: 'partial',
  })
  await runtime.reviewCandidate(input)
  assert.deepEqual(calls, [input])
})

test('F4 real HTTP adapter sends the full review command and validates authoritative Candidate publication CAS', async () => {
  const exchanges = await httpExchanges()
  const review = exchangeFor(exchanges, 'candidate.review')
  const candidateGet = exchangeFor(exchanges, 'candidate.get')
  const publication = exchangeFor(exchanges, 'publication.accept')
  const requests: Array<{ path: string, method: string | undefined, body: unknown }> = []
  const operationKinds: string[] = []
  const gateway = createCoreV2CandidateHttpGateway({
    baseUrl: 'https://core.test',
    acceptedBy: 'user-v2',
    createOperationKey: kind => {
      operationKinds.push(kind)
      return kind === 'review' ? 'review-op-v2' : 'publication-op-v2'
    },
    fetch: async (url, init) => {
      const path = new URL(String(url)).pathname
      const body = init?.body === undefined ? undefined : JSON.parse(String(init.body))
      requests.push({ path, method: init?.method, body })
      if (path.endsWith('/review')) {
        assert.equal(init?.method, 'POST')
        assert.deepEqual(body, review.request)
        return jsonResponse(review)
      }
      if (path.endsWith('/candidates/candidate-v2')) {
        assert.equal(init?.method, 'GET')
        assert.equal(body, undefined)
        return jsonResponse(candidateGet)
      }
      if (path.endsWith('/publications:accept')) {
        assert.equal(init?.method, 'POST')
        assert.deepEqual(body, publication.request)
        return jsonResponse(publication)
      }
      throw new Error('unexpected adapter path')
    },
  })

  assert.deepEqual(
    await gateway.reviewCandidate({
      workspaceId: 'ws-1',
      candidateId: 'candidate-v2',
      decision: 'approve',
      expectedStatus: 'complete',
    }),
    review.response,
  )
  assert.deepEqual(
    await gateway.acceptCandidate({ workspaceId: 'ws-1', candidateId: 'candidate-v2' }),
    publication.response,
  )
  assert.deepEqual(operationKinds, ['review', 'publication'])
  assert.deepEqual(requests, [
    {
      path: '/api/v2/core/workspaces/ws-1/candidates/candidate-v2/review',
      method: 'POST',
      body: review.request,
    },
    {
      path: '/api/v2/core/workspaces/ws-1/candidates/candidate-v2',
      method: 'GET',
      body: undefined,
    },
    {
      path: '/api/v2/core/workspaces/ws-1/publications:accept',
      method: 'POST',
      body: publication.request,
    },
  ])
})

test('F4 real HTTP adapter fails closed on operation-key and Candidate/CAS exchange mismatches', async () => {
  const exchanges = await httpExchanges()
  const review = exchangeFor(exchanges, 'candidate.review')
  const candidateGet = exchangeFor(exchanges, 'candidate.get')
  const publication = exchangeFor(exchanges, 'publication.accept')

  const wrongOperation = createCoreV2CandidateHttpGateway({
    acceptedBy: 'user-v2',
    createOperationKey: () => 'review-op-v2',
    fetch: async () => jsonResponse(review, { ...review.response, operation_key: 'review-op-wrong' }),
  })
  await assert.rejects(
    wrongOperation.reviewCandidate({
      workspaceId: 'ws-1',
      candidateId: 'candidate-v2',
      decision: 'approve',
      expectedStatus: 'complete',
    }),
    /operation key does not match request/,
  )

  const casMismatch = structuredClone(publication.response)
  ;(casMismatch.cas as JsonRecord).base_content_hash = '0'.repeat(64)
  const wrongCas = createCoreV2CandidateHttpGateway({
    baseUrl: 'https://core.test',
    acceptedBy: 'user-v2',
    createOperationKey: () => 'publication-op-v2',
    fetch: async url => {
      const path = new URL(String(url)).pathname
      if (path.endsWith('/candidates/candidate-v2')) return jsonResponse(candidateGet)
      if (path.endsWith('/publications:accept')) return jsonResponse(publication, casMismatch)
      throw new Error('unexpected adapter path')
    },
  })
  await assert.rejects(
    wrongCas.acceptCandidate({ workspaceId: 'ws-1', candidateId: 'candidate-v2' }),
    /Publication result CAS base is not bound to Candidate write_set/,
  )
})

test('F5 Feature runtime is injected per Vue app without process-global contamination', async () => {
  const { createSSRApp } = await import('../../../frontend/node_modules/vue/index.mjs')
  const first = createFeatureRuntimeGateway({
    availability: { candidate: { available: true, reason: '' } },
    coreV2CandidateGateway: {
      previewCandidate: async () => ({ app: 'first' }),
      reviewCandidate: async () => ({ app: 'first' }),
      acceptCandidate: async () => ({ app: 'first' }),
    },
  })
  const second = createFeatureRuntimeGateway({
    availability: { candidate: { available: true, reason: '' } },
    coreV2CandidateGateway: {
      previewCandidate: async () => ({ app: 'second' }),
      reviewCandidate: async () => ({ app: 'second' }),
      acceptCandidate: async () => ({ app: 'second' }),
    },
  })
  const firstApp = createSSRApp({})
  const secondApp = createSSRApp({})
  const defaultApp = createSSRApp({})
  firstApp.provide(FEATURE_RUNTIME_GATEWAY_KEY, first)
  secondApp.provide(FEATURE_RUNTIME_GATEWAY_KEY, second)

  assert.strictEqual(firstApp.runWithContext(() => useFeatureRuntimeGateway()), first)
  assert.strictEqual(secondApp.runWithContext(() => useFeatureRuntimeGateway()), second)
  const fallback = defaultApp.runWithContext(() => useFeatureRuntimeGateway())
  assert.notStrictEqual(fallback, first)
  assert.notStrictEqual(fallback, second)
  assert.equal(fallback.availability.candidate.available, false)

  const runtimeSource = await source('frontend/src/core/workbench/FeatureRuntimeGateway.ts')
  assert.match(runtimeSource, /FEATURE_RUNTIME_GATEWAY_KEY/)
  assert.match(runtimeSource, /provideFeatureRuntimeGateway/)
  assert.doesNotMatch(runtimeSource, /installedRuntime|installFeatureRuntimeGateway|getFeatureRuntimeGateway/)
})

test('F5 executable StoryStatePanel projection keeps FLOW-07/FLOW-08 as distinct named regions and actions', async () => {
  const { mountFeature } = await compileFeatureComponents()
  const available = { available: true, reason: '' }
  const view = await mountFeature('storyState', {
    foreshadowAvailability: available,
    storyBibleAvailability: available,
  })
  const regions = findAll(view.root, node => node.props.role === 'region')
  assert.equal(regions.length, 2)
  assert.deepEqual(
    regions.map(region => [region.props['data-flow'], region.props['aria-label']]),
    [
      ['FLOW-07', 'FLOW-07：伏笔账本'],
      ['FLOW-08', 'FLOW-08：故事演进与 Bible'],
    ],
  )
  const foreshadowAction = byText(regions[0]!, 'button', '刷新伏笔账本')
  const storyBibleAction = byText(regions[1]!, 'button', '刷新故事演进 / Bible')
  assert.ok(foreshadowAction)
  assert.ok(storyBibleAction)
  await view.click(foreshadowAction)
  await view.click(storyBibleAction)
  assert.deepEqual(view.events, [
    { name: 'refreshForeshadow', args: [] },
    { name: 'refreshStoryBible', args: [] },
  ])
  view.unmount()
})

test('CandidateReviewPanel executable controls disable without authority or while busy and emit nothing', async () => {
  const { mountFeature } = await compileFeatureComponents()
  for (const props of [
    { availability: { available: false, reason: 'not mounted' }, result: '', busy: false },
    { availability: { available: true, reason: '' }, result: '', busy: true },
  ]) {
    const view = await mountFeature('candidate', props)
    const inputs = findAll(view.root, node => node.type === 'input' || node.type === 'select')
    const actions = findAll(view.root, node => node.type === 'button')
    assert.equal(inputs.length, 3)
    assert.equal(actions.length, 3)
    assert.ok(inputs.every(input => input.props.disabled === true))
    assert.ok(actions.every(action => action.props.disabled === true))
    for (const action of actions) await view.click(action)
    assert.deepEqual(view.events, [])
    view.unmount()
  }
})

test('CandidateReviewPanel executable controls emit trimmed identity and selected review fields', async () => {
  const { mountFeature } = await compileFeatureComponents()
  const view = await mountFeature('candidate', {
    availability: { available: true, reason: '' },
    result: '',
    busy: false,
  })
  const candidateId = findAll(view.root, node => node.type === 'input')[0]
  const selects = findAll(view.root, node => node.type === 'select')
  const actions = findAll(view.root, node => node.type === 'button')
  assert.ok(candidateId)
  assert.equal(selects.length, 2)
  assert.equal(actions.length, 3)
  assert.ok(actions.every(action => action.props.disabled === true))

  await view.setValue(candidateId, '  candidate-behavior  ')
  await view.setValue(selects[0]!, 'reject')
  await view.setValue(selects[1]!, 'partial')
  assert.ok(actions.every(action => action.props.disabled === false))
  for (const action of actions) await view.click(action)
  assert.deepEqual(view.events, [
    { name: 'preview', args: ['candidate-behavior'] },
    {
      name: 'review',
      args: [{ candidateId: 'candidate-behavior', decision: 'reject', expectedStatus: 'partial' }],
    },
    { name: 'accept', args: ['candidate-behavior'] },
  ])
  view.unmount()
})

test('GenerationControls executable Task Drawer action emits once when available and never when unavailable', async () => {
  const { mountFeature } = await compileFeatureComponents()
  const unavailable = await mountFeature('generation', {
    availability: { available: false, reason: 'generation unavailable' },
    jobDrawerAvailability: { available: false, reason: 'drawer unavailable' },
  })
  const unavailableAction = byText(unavailable.root, 'button', '任务状态')
  assert.ok(unavailableAction)
  assert.equal(unavailableAction.props.disabled, true)
  await unavailable.click(unavailableAction)
  assert.deepEqual(unavailable.events, [])
  unavailable.unmount()

  const available = await mountFeature('generation', {
    availability: { available: false, reason: 'generation unavailable' },
    jobDrawerAvailability: { available: true, reason: '' },
  })
  const availableAction = byText(available.root, 'button', '任务状态')
  assert.ok(availableAction)
  assert.equal(availableAction.props.disabled, false)
  await available.click(availableAction)
  assert.deepEqual(available.events, [{ name: 'showTaskStatus', args: [] }])
  available.unmount()
})

test('F6 preserves the reviewed Core chapter create/open/edit/save/reload recovery wiring', async () => {
  const workbench = await source('frontend/src/views/Workbench.vue')
  for (const required of [
    'createCoreChapterRecovery()',
    'chapterCreateRecovery.run(workspaceId, title',
    'requireCoreFlowRuntime().createChapter(targetWorkspaceId, targetTitle)',
    'chapterCreateRecovery.hasCommittedChapter(workspaceId)',
    'chapterCreateRecovery.canRunOrdinaryReload(token.scope)',
    'requireCoreFlowRuntime().openChapter(target)',
    'requireCoreFlowRuntime().saveChapter(savingChapter)',
  ]) assert.ok(workbench.includes(required), `missing reviewed chapter flow: ${required}`)
})

test('F7 feature assembly keeps the Core/Jobs identity boundary and never imports legacy novel authority', async () => {
  const paths = [
    'frontend/src/core/workbench/FeatureRuntimeGateway.ts',
    'frontend/src/views/Workbench.vue',
    'frontend/src/components/workbench/SetupPlanningPanel.vue',
    'frontend/src/components/workbench/GenerationControls.vue',
    'frontend/src/components/workbench/CandidateReviewPanel.vue',
    'frontend/src/components/workbench/StoryStatePanel.vue',
    'frontend/src/components/workbench/CheckpointRecoveryPanel.vue',
    'frontend/src/components/workbench/ExportControls.vue',
  ]
  const assembled = (await Promise.all(paths.map(source))).join('\n')
  assert.match(assembled, /workspaceId/)
  assert.match(assembled, /candidateId/)
  assert.doesNotMatch(assembled, /novel_id|novelApi|chapterApi|bibleApi|foreshadowApi|evolutionApi|snapshotApi|workflowApi/)
  assert.doesNotMatch(assembled, /frontend\/src\/api\/(novel|bible|foreshadow|evolution|snapshot|workflow)/)
})
