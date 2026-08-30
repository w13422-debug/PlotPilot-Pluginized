import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import test from 'node:test'
import {
  acceptPlanRevisionSave,
  appendPlanBinding,
  buildPlanRevisionSave,
  createPlanEditorState,
  materializePlan,
  movePlanBinding,
  refreshPlanEditor,
  removePlanBinding,
  replacePlanDraft,
  updatePlanEditorDraft,
} from '../../../frontend/src/core/plugins/model.ts'
import type { PluginPlan, PluginPlanBinding } from '../../../frontend/src/core/plugins/types.ts'

async function loadRealVue(): Promise<typeof import('vue')> {
  const require = createRequire(import.meta.url)
  const searchRoots = [
    resolve('frontend'),
    join(process.env.USERPROFILE ?? '', 'Desktop', 'Novel-Agent- (2)', 'novel-agent', 'frontend'),
    resolve('..', '..', 'PlotPilot-Pluginized', 'frontend'),
  ]
  const entry = require.resolve('vue', { paths: searchRoots })
  return import(pathToFileURL(entry).href)
}

const fixture = (): PluginPlan => JSON.parse(readFileSync('contracts/examples/fixtures/plugin-plan.json', 'utf8'))
const addition = (): PluginPlanBinding => ({
  binding_id: 'binding-2', capability_id: 'fixture.second/v1', plugin_id: 'com.plotpilot.fixture.second',
  release_requirement: '2.0.0', order: 20, enabled: true, required: false, propagate_cancel: false,
  parameters_asset_id: null,
})

function sourceFiles(root: string): string[] {
  const result: string[] = []
  for (const name of readdirSync(root)) {
    const path = join(root, name)
    if (statSync(path).isDirectory()) result.push(...sourceFiles(path))
    else if (/\.(?:ts|vue)$/.test(name)) result.push(path)
  }
  return result
}

test('real Vue ref load and save boundary materializes recursive plain values', async () => {
  const { isProxy, reactive, ref, shallowRef } = await loadRealVue()
  const source = reactive(fixture())
  const selected = ref(source)
  assert.equal(isProxy(selected.value), true)
  const plain = materializePlan(selected.value)
  assert.equal(isProxy(plain), false)
  assert.equal(isProxy(plain.bindings), false)
  assert.equal(isProxy(plain.bindings[0]), false)
  let editor = createPlanEditorState(selected.value)
  const draft = replacePlanDraft(editor.draft!, { name: 'Vue draft' })
  editor = updatePlanEditorDraft(editor, draft)
  const editorRef = shallowRef(editor)
  assert.equal(isProxy(editorRef.value.draft), false)
  const intent = buildPlanRevisionSave(editorRef.value)
  assert.equal(intent.expected_base_revision, 1)
  assert.equal(intent.next_revision.revision, 2)
  assert.equal(intent.next_revision.name, 'Vue draft')
  assert.equal(isProxy(intent.next_revision), false)
  assert.equal(isProxy(intent.next_revision.bindings[0]), false)
  const accepted = acceptPlanRevisionSave(editorRef.value, intent, reactive(intent.next_revision))
  assert.equal(accepted.status, 'clean')
  assert.equal(isProxy(accepted.draft), false)
})

test('real Vue append/remove/move uses shallow replacement and never retains Proxy values', async () => {
  const { isProxy, ref, shallowRef } = await loadRealVue()
  const ingress = ref(fixture())
  assert.equal(isProxy(ingress.value), true)
  const draft = shallowRef(materializePlan(ingress.value))
  assert.equal(isProxy(draft.value), false)
  const original = JSON.stringify(ingress.value)
  draft.value = appendPlanBinding(draft.value, ref(addition()).value)
  assert.deepEqual(draft.value.bindings.map(item => item.order), [1, 2])
  assert.equal(JSON.stringify(fixture()), original)
  draft.value = movePlanBinding(draft.value, 'binding-2', 0)
  assert.deepEqual(draft.value.bindings.map(item => item.binding_id), ['binding-2', 'binding-1'])
  draft.value = removePlanBinding(draft.value, 'binding-2')
  assert.deepEqual(draft.value.bindings.map(item => [item.binding_id, item.order]), [['binding-1', 1]])
  const plain = materializePlan(draft.value)
  assert.equal(isProxy(plain), false)
  assert.equal(isProxy(plain.bindings[0]), false)
})

test('real Vue dirty refresh preserves draft and stale authority conflicts without Proxy leakage', async () => {
  const { isProxy, ref, shallowRef } = await loadRealVue()
  let editor = createPlanEditorState(fixture())
  editor = updatePlanEditorDraft(editor, replacePlanDraft(editor.draft!, { name: 'unsaved', description: 'keep me' }))
  const ingress = ref(fixture())
  assert.equal(isProxy(ingress.value), true)
  const state = shallowRef(editor)
  assert.equal(isProxy(state.value.draft), false)
  state.value = refreshPlanEditor(state.value, ingress.value)
  assert.equal(state.value.status, 'dirty')
  assert.equal(state.value.draft?.name, 'unsaved')
  assert.equal(state.value.draft?.description, 'keep me')
  state.value = refreshPlanEditor(state.value, ref({ ...fixture(), revision: 2 }).value)
  assert.equal(state.value.status, 'conflict')
  assert.equal(state.value.draft?.name, 'unsaved')
  const plainConflict = refreshPlanEditor(materializeEditorInput(state.value), { ...fixture(), revision: 3 })
  assert.equal(plainConflict.conflict?.observed_revision, 3)
  assert.equal(isProxy(plainConflict.draft), false)
})

function materializeEditorInput(value: ReturnType<typeof createPlanEditorState>) {
  return {
    status: value.status,
    base: value.base ? materializePlan(value.base) : null,
    draft: value.draft ? materializePlan(value.draft) : null,
    conflict: value.conflict ? { ...value.conflict } : null,
  }
}

test('owned production sources contain no generic graph-clone call', () => {
  const forbidden = ['structured', 'Clone'].join('')
  const roots = [
    'frontend/src/core/plugins',
    'frontend/src/components/settings/plugin-management',
  ]
  for (const root of roots) {
    for (const file of sourceFiles(root)) {
      assert.equal(readFileSync(file, 'utf8').includes(forbidden), false, file)
    }
  }
})
