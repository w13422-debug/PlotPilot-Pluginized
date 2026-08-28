import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'
import test from 'node:test'

interface HostNode {
  type: string
  text: string
  props: Record<string, unknown>
  children: HostNode[]
  parent: HostNode | null
}

interface MountedDrawer {
  root: HostNode
  events: Array<{ name: string; args: unknown[] }>
  flush(): Promise<void>
  unmount(): void
}

const frontendRoot = resolve(fileURLToPath(new URL('../..', import.meta.url)))
const dependencyRoot = resolve(process.env.PLOTPILOT_FRONTEND_DEP_ROOT ?? frontendRoot)
const sourceRoot = resolve(process.env.PLOTPILOT_FRONTEND_SOURCE_ROOT ?? frontendRoot)
const dependencyRequire = createRequire(join(dependencyRoot, 'package.json'))
let compiledModule: Promise<{ mountTaskDrawer(props: Record<string, unknown>): Promise<MountedDrawer> }> | undefined

const naiveUiStub = String.raw`
import { defineComponent, h } from 'vue'

function slot(slots, name = 'default') {
  return slots[name]?.() ?? []
}

export const NButton = defineComponent({
  name: 'NButton',
  inheritAttrs: false,
  props: { disabled: Boolean, loading: Boolean },
  setup(props, { attrs, slots }) {
    return () => h('button', {
      ...attrs,
      disabled: props.disabled || props.loading,
      'data-loading': String(props.loading),
      onClick: event => {
        if (!props.disabled && !props.loading && typeof attrs.onClick === 'function') attrs.onClick(event)
      },
    }, slot(slots))
  },
})

export const NDrawer = defineComponent({
  name: 'NDrawer',
  props: { show: Boolean },
  emits: ['update:show'],
  setup(props, { slots }) {
    return () => props.show ? h('aside', { 'data-component': 'drawer' }, slot(slots)) : null
  },
})

export const NDrawerContent = defineComponent({
  name: 'NDrawerContent',
  props: { title: String },
  setup(props, { slots }) {
    return () => h('section', { 'data-component': 'drawer-content' }, [
      h('h2', props.title ?? ''),
      ...slot(slots),
    ])
  },
})

export const NEmpty = defineComponent({
  name: 'NEmpty',
  props: { description: String },
  setup(props, { slots }) {
    return () => h('div', { 'data-component': 'empty' }, [
      h('span', props.description ?? ''),
      ...slot(slots, 'extra'),
    ])
  },
})

export const NProgress = defineComponent({
  name: 'NProgress',
  inheritAttrs: false,
  props: { percentage: Number },
  setup(props, { attrs }) {
    return () => h('div', {
      ...attrs,
      role: 'progressbar',
      'data-percentage': String(props.percentage ?? 0),
    }, String(props.percentage ?? 0) + '%')
  },
})

export const NTag = defineComponent({
  name: 'NTag',
  props: { type: String },
  setup(props, { slots }) {
    return () => h('span', { 'data-component': 'tag', 'data-tone': props.type }, slot(slots))
  },
})

export const NCollapse = defineComponent({
  name: 'NCollapse',
  setup(_props, { slots }) {
    return () => h('div', { 'data-component': 'collapse' }, slot(slots))
  },
})

export const NCollapseItem = defineComponent({
  name: 'NCollapseItem',
  props: { title: String, name: String },
  setup(props, { slots }) {
    return () => h('section', { 'data-component': 'collapse-item', 'data-name': props.name }, [
      h('h3', props.title ?? ''),
      ...slot(slots),
    ])
  },
})
`

const entrySource = (componentPath: string): string => String.raw`
import { createRenderer, nextTick } from 'vue'
import TaskDrawer from ${JSON.stringify(componentPath)}

function hostNode(type, text = '') {
  return { type, text, props: {}, children: [], parent: null }
}

const renderer = createRenderer({
  patchProp(element, key, _previous, next) {
    if (next == null) delete element.props[key]
    else element.props[key] = next
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
  createElement(type) {
    return hostNode(type)
  },
  createText(text) {
    return hostNode('#text', text)
  },
  createComment(text) {
    return hostNode('#comment', text)
  },
  setText(node, text) {
    node.text = text
  },
  setElementText(element, text) {
    element.text = text
    element.children = []
  },
  parentNode(node) {
    return node.parent
  },
  nextSibling(node) {
    if (node.parent == null) return null
    const index = node.parent.children.indexOf(node)
    return node.parent.children[index + 1] ?? null
  },
  setScopeId(element, id) {
    element.props[id] = ''
  },
  insertStaticContent(content, parent, anchor) {
    const node = hostNode('#static', content)
    this.insert(node, parent, anchor)
    return [node, node]
  },
})

export async function mountTaskDrawer(props = {}) {
  const root = hostNode('root')
  const events = []
  const app = renderer.createApp(TaskDrawer, {
    ...props,
    onRefresh: (...args) => events.push({ name: 'refresh', args }),
    onResume: (...args) => events.push({ name: 'resume', args }),
    onRetry: (...args) => events.push({ name: 'retry', args }),
    onCancel: (...args) => events.push({ name: 'cancel', args }),
  })
  app.mount(root)
  await nextTick()
  return {
    root,
    events,
    flush: async () => { await nextTick() },
    unmount: () => app.unmount(),
  }
}
`

async function compileTaskDrawer(): Promise<{ mountTaskDrawer(props: Record<string, unknown>): Promise<MountedDrawer> }> {
  if (compiledModule != null) return compiledModule
  compiledModule = (async () => {
    let viteEntry: string
    let vuePluginEntry: string
    let vuePackage: string
    try {
      viteEntry = dependencyRequire.resolve('vite')
      vuePluginEntry = dependencyRequire.resolve('@vitejs/plugin-vue')
      vuePackage = dependencyRequire.resolve('vue/package.json')
    } catch (error) {
      throw new Error(
        `TaskDrawer render test requires the declared frontend dependencies. `
        + `Set PLOTPILOT_FRONTEND_DEP_ROOT and, when needed, PLOTPILOT_FRONTEND_SOURCE_ROOT `
        + `to an exact installed source mirror (dependencies: ${dependencyRoot}; source: ${sourceRoot}).`,
        { cause: error },
      )
    }
    const [{ build }, { default: vue }] = await Promise.all([
      import(pathToFileURL(viteEntry).href),
      import(pathToFileURL(vuePluginEntry).href),
    ])
    const componentPath = resolve(sourceRoot, 'src/components/jobs/TaskDrawer.vue').replaceAll('\\', '/')
    const vueRuntime = resolve(dirname(vuePackage), 'dist/vue.runtime.esm-bundler.js').replaceAll('\\', '/')
    const virtualEntry = 'virtual:task-drawer-entry'
    const virtualNaive = '\0virtual:naive-ui'
    const virtualPlugin = {
      name: 'task-drawer-test-virtuals',
      enforce: 'pre',
      resolveId(source: string) {
        if (source === virtualEntry) return '\0' + virtualEntry
        if (source === 'naive-ui') return virtualNaive
        return null
      },
      load(id: string) {
        if (id === '\0' + virtualEntry) return entrySource(componentPath)
        if (id === virtualNaive) return naiveUiStub
        return null
      },
    }
    const result = await build({
      root: sourceRoot,
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
          output: { format: 'es', entryFileNames: 'task-drawer-test.js' },
        },
      },
    })
    const output = Array.isArray(result) ? result.flatMap(item => item.output) : result.output
    const chunk = output.find(item => item.type === 'chunk' && item.isEntry)
    assert.ok(chunk && chunk.type === 'chunk', 'Vite did not produce a TaskDrawer test chunk')
    const encoded = Buffer.from(chunk.code, 'utf8').toString('base64')
    const loaded = await import(`data:text/javascript;base64,${encoded}`) as {
      mountTaskDrawer(props: Record<string, unknown>): Promise<MountedDrawer>
    }
    assert.equal(
      typeof loaded.mountTaskDrawer,
      'function',
      `compiled TaskDrawer chunk exports: ${Object.keys(loaded).join(', ') || '(none)'}`,
    )
    return loaded
  })()
  return compiledModule
}

function job(state: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    job_id: `job-${state}`,
    workspace_id: 'ws-1',
    job_state: state,
    job_revision: 3,
    steps: [{ step_id: 'step-running', state: 'running', revision: 2 }],
    attempts: [{ attempt_id: 'attempt-running', state: 'running', lease_epoch: 4 }],
    candidate_ids: [],
    current_checkpoint_id: null,
    core_event_high_water: 0,
    job_event_high_water: 2,
    created_at: '2026-08-28T00:00:00Z',
    source_snapshot_hash: 'a'.repeat(64),
    ...overrides,
  }
}

function walk(node: HostNode): HostNode[] {
  return [node, ...node.children.flatMap(walk)]
}

function textContent(node: HostNode): string {
  return node.text + node.children.map(textContent).join('')
}

function find(root: HostNode, predicate: (node: HostNode) => boolean): HostNode | undefined {
  return walk(root).find(predicate)
}

function findAll(root: HostNode, predicate: (node: HostNode) => boolean): HostNode[] {
  return walk(root).filter(predicate)
}

function byText(root: HostNode, type: string, text: string): HostNode | undefined {
  return find(root, node => node.type === type && textContent(node).trim() === text)
}

function card(root: HostNode, jobId: string): HostNode {
  const value = find(root, node => node.props['data-job-id'] === jobId)
  assert.ok(value, `missing rendered card for ${jobId}`)
  return value
}

async function openDrawer(view: MountedDrawer): Promise<void> {
  const trigger = find(view.root, node => node.props['aria-label'] === '打开任务抽屉')
  assert.ok(trigger)
  assert.equal(find(view.root, node => node.props['data-component'] === 'drawer'), undefined)
  ;(trigger.props.onClick as (event: unknown) => void)({})
  await view.flush()
  assert.ok(find(view.root, node => node.props['data-component'] === 'drawer'))
}

function click(node: HostNode): void {
  assert.equal(node.type, 'button')
  ;(node.props.onClick as (event: unknown) => void)({})
}

test('real TaskDrawer render opens, refreshes and presents connection, progress, Step and Attempt states', async () => {
  const { mountTaskDrawer } = await compileTaskDrawer()
  const renderedJob = job('running', {
    job_id: 'job-render',
    steps: [
      { step_id: 'step-partial', state: 'partial', revision: 3 },
      { step_id: 'step-interrupted', state: 'interrupted', revision: 4 },
      { step_id: 'step-pending', state: 'pending', revision: 1 },
    ],
    attempts: [
      { attempt_id: 'attempt-partial', state: 'partial', lease_epoch: 1 },
      { attempt_id: 'attempt-suspended', state: 'suspended', lease_epoch: 2 },
      { attempt_id: 'attempt-fenced', state: 'fenced', lease_epoch: 3 },
    ],
  })
  const view = await mountTaskDrawer({
    jobs: [renderedJob],
    connectionStates: { 'job-render': 'reconnecting' },
  })
  await openDrawer(view)
  const refresh = find(view.root, node => node.props['aria-label'] === '刷新并重新挂接任务')
  assert.ok(refresh)
  click(refresh)
  assert.deepEqual(view.events, [{ name: 'refresh', args: [] }])

  const renderedCard = card(view.root, 'job-render')
  const renderedText = textContent(renderedCard)
  for (const expected of [
    '重连中', 'Running', 'step-partial', 'rev 3', '部分完成',
    'step-interrupted', 'rev 4', '已中断', 'attempt-partial', 'lease 1',
    'attempt-suspended', 'lease 2', '已挂起', 'attempt-fenced', 'lease 3', '已隔离',
  ]) assert.match(renderedText, new RegExp(expected))
  const progress = find(renderedCard, node => node.props.role === 'progressbar')
  assert.ok(progress)
  assert.equal(progress.props['data-percentage'], '67')
  assert.equal(progress.props['aria-label'], '任务进度 2/3')
  assert.match(renderedText, /2 \/ 3 个步骤已结束/)
  view.unmount()
})

test('real TaskDrawer render enforces action truth table and emits only valid explicit intents', async () => {
  const { mountTaskDrawer } = await compileTaskDrawer()
  const scenarios = [
    {
      value: job('paused', { current_checkpoint_id: 'checkpoint-1' }),
      visible: ['继续', '取消'],
      hidden: ['重试'],
      clickLabel: '继续',
      expected: { name: 'resume', args: ['job-paused'] },
    },
    {
      value: job('partial', { current_checkpoint_id: 'checkpoint-1' }),
      visible: ['重试'],
      hidden: ['继续', '取消'],
      clickLabel: '重试',
      expected: { name: 'retry', args: ['job-partial'] },
    },
    {
      value: job('failed', { current_checkpoint_id: 'checkpoint-1' }),
      visible: ['重试'],
      hidden: ['继续', '取消'],
      clickLabel: '重试',
      expected: { name: 'retry', args: ['job-failed'] },
    },
    {
      value: job('needs_attention', { current_checkpoint_id: 'checkpoint-1' }),
      visible: ['继续'],
      hidden: ['重试', '取消'],
      clickLabel: '继续',
      expected: { name: 'resume', args: ['job-needs_attention'] },
    },
    {
      value: job('running'),
      visible: ['取消'],
      hidden: ['继续', '重试'],
      clickLabel: '取消',
      expected: { name: 'cancel', args: ['job-running'] },
    },
  ] as const

  for (const scenario of scenarios) {
    const view = await mountTaskDrawer({ jobs: [scenario.value] })
    await openDrawer(view)
    const renderedCard = card(view.root, scenario.value.job_id as string)
    for (const label of scenario.visible) assert.ok(byText(renderedCard, 'button', label), `missing ${label}`)
    for (const label of scenario.hidden) assert.equal(byText(renderedCard, 'button', label), undefined)
    click(byText(renderedCard, 'button', scenario.clickLabel)!)
    assert.deepEqual(view.events, [scenario.expected])
    view.unmount()
  }
})

test('real TaskDrawer render isolates pending/disabled/loading and action error state per Job', async () => {
  const { mountTaskDrawer } = await compileTaskDrawer()
  const paused = job('paused', { current_checkpoint_id: 'checkpoint-1' })
  const running = job('running')
  const view = await mountTaskDrawer({
    jobs: [paused, running],
    pendingActions: { 'job-paused': 'resume' },
    actionErrors: { 'job-paused': 'checkpoint rejected' },
  })
  await openDrawer(view)
  const pausedCard = card(view.root, 'job-paused')
  const runningCard = card(view.root, 'job-running')
  const resume = byText(pausedCard, 'button', '继续')!
  const pausedCancel = byText(pausedCard, 'button', '取消')!
  const runningCancel = byText(runningCard, 'button', '取消')!
  assert.equal(resume.props.disabled, true)
  assert.equal(resume.props['data-loading'], 'true')
  assert.equal(pausedCancel.props.disabled, true)
  assert.equal(pausedCancel.props['data-loading'], 'false')
  assert.equal(runningCancel.props.disabled, false)

  click(resume)
  click(pausedCancel)
  assert.deepEqual(view.events, [])
  click(runningCancel)
  assert.deepEqual(view.events, [{ name: 'cancel', args: ['job-running'] }])

  const alerts = findAll(view.root, node => node.props.role === 'alert')
  assert.equal(alerts.length, 1)
  assert.equal(textContent(alerts[0]!), 'checkpoint rejected')
  assert.ok(walk(pausedCard).includes(alerts[0]!))
  assert.equal(walk(runningCard).includes(alerts[0]!), false)
  view.unmount()
})
