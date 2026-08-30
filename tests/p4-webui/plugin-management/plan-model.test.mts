import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  acceptPlanRevisionSave,
  appendPlanBinding,
  buildPlanRevisionSave,
  createPlanEditorState,
  materializePlan,
  movePlanBinding,
  PluginModelError,
  refreshPlanEditor,
  removePlanBinding,
  replacePlanDraft,
  updatePlanEditorDraft,
} from '../../../frontend/src/core/plugins/model.ts'
import type { PluginPlan, PluginPlanBinding } from '../../../frontend/src/core/plugins/types.ts'

const fixture = (): PluginPlan => JSON.parse(readFileSync('contracts/examples/fixtures/plugin-plan.json', 'utf8'))
const secondBinding = (overrides: Partial<PluginPlanBinding> = {}): PluginPlanBinding => ({
  binding_id: 'binding-2',
  capability_id: 'fixture.second/v1',
  plugin_id: 'com.plotpilot.fixture.second',
  release_requirement: '2.0.0',
  order: 20,
  enabled: true,
  required: false,
  propagate_cancel: false,
  parameters_asset_id: 'parameters-2',
  ...overrides,
})

test('field materialization densifies legal sparse order without mutating authority input', () => {
  const source = fixture()
  source.bindings.push(secondBinding())
  const before = JSON.stringify(source)
  const plain = materializePlan(source)
  assert.equal(JSON.stringify(source), before)
  assert.deepEqual(plain.bindings.map(item => item.order), [1, 2])
  assert.deepEqual(
    { schema: plain.schema, plan_id: plain.plan_id, revision: plain.revision },
    { schema: source.schema, plan_id: source.plan_id, revision: source.revision },
  )
  assert.notEqual(plain, source)
  assert.notEqual(plain.bindings, source.bindings)
  assert.notEqual(plain.bindings[0], source.bindings[0])
})

test('append adds to normalized tail and leaves all non-order authority fields unchanged', () => {
  const source = fixture()
  const original = JSON.parse(JSON.stringify(source)) as PluginPlan
  const next = appendPlanBinding(source, secondBinding({ order: 99 }))
  assert.deepEqual(next.bindings.map(item => [item.binding_id, item.order]), [['binding-1', 1], ['binding-2', 2]])
  assert.deepEqual(source, original)
  assert.deepEqual(
    { ...next, bindings: undefined },
    { ...materializePlan(source), bindings: undefined },
  )
  assert.equal(next.bindings[1]?.parameters_asset_id, 'parameters-2')
})

test('move and remove return dense new values and reject unsafe boundaries', () => {
  const source = appendPlanBinding(fixture(), secondBinding())
  const moved = movePlanBinding(source, 'binding-2', 0)
  assert.deepEqual(moved.bindings.map(item => [item.binding_id, item.order]), [['binding-2', 1], ['binding-1', 2]])
  const removed = removePlanBinding(moved, 'binding-2')
  assert.deepEqual(removed.bindings.map(item => [item.binding_id, item.order]), [['binding-1', 1]])
  assert.deepEqual(source.bindings.map(item => item.binding_id), ['binding-1', 'binding-2'])
  assert.throws(() => movePlanBinding(source, 'binding-1', -1), (error: unknown) =>
    error instanceof PluginModelError && error.code === 'plan_binding_move_out_of_range')
  assert.throws(() => movePlanBinding(source, 'binding-2', source.bindings.length), (error: unknown) =>
    error instanceof PluginModelError && error.code === 'plan_binding_move_out_of_range')
})

test('removing an interpreter in use fails closed and leaves the Plan unchanged', () => {
  const source = fixture()
  source.data_bindings = [{
    data_binding_id: 'data-1', data_plugin_id: 'com.plotpilot.data', release_requirement: '1.0.0',
    format_id: 'style/v1', interpreter_binding_id: 'binding-1', order: 10, enabled: true,
    parameters_asset_id: null,
  }]
  const before = JSON.stringify(source)
  assert.throws(() => removePlanBinding(source, 'binding-1'), (error: unknown) =>
    error instanceof PluginModelError && error.code === 'plan_binding_in_use')
  assert.equal(JSON.stringify(source), before)
})

test('synthesizer is closed, exact, enabled, and cleared outside synthesize mode', () => {
  const source = fixture()
  source.result_mode = 'synthesize'
  source.synthesizer = {
    binding_id: 'binding-1', capability_id: 'fixture.echo/v1', plugin_id: 'com.plotpilot.fixture.code',
    release_requirement: '1.0.0',
  }
  assert.equal(materializePlan(source).synthesizer?.binding_id, 'binding-1')
  const separate = replacePlanDraft(source, { result_mode: 'separate' })
  assert.equal(separate.synthesizer, null)
  assert.throws(() => materializePlan({ ...source, synthesizer: { ...source.synthesizer, plugin_id: 'drift' } }),
    (error: unknown) => error instanceof PluginModelError && error.code === 'plan_synthesizer_unresolved')
})

test('dirty editor builds an immutable create-revision CAS intent and accepts exact response only', () => {
  const base = fixture()
  const clean = createPlanEditorState(base)
  const draft = replacePlanDraft(clean.draft!, { name: 'Edited Plan', description: 'local draft' })
  const dirty = updatePlanEditorDraft(clean, draft)
  const intent = buildPlanRevisionSave(dirty)
  assert.equal(intent.operation, 'create_revision')
  assert.equal(intent.plan_id, 'plan-1')
  assert.equal(intent.expected_base_revision, 1)
  assert.equal(intent.next_revision.revision, 2)
  assert.equal(intent.next_revision.name, 'Edited Plan')
  assert.equal(base.revision, 1)
  const saved = { ...intent.next_revision }
  const accepted = acceptPlanRevisionSave(dirty, intent, saved)
  assert.equal(accepted.status, 'clean')
  assert.equal(accepted.base?.revision, 2)
  assert.throws(() => acceptPlanRevisionSave(dirty, { ...intent, expected_base_revision: 2 }, saved), (error: unknown) =>
    error instanceof PluginModelError && error.code === 'plan_save_stale_base')
})

test('dirty refresh preserves same-base draft and new authority revision creates explicit conflict', () => {
  const clean = createPlanEditorState(fixture())
  const dirty = updatePlanEditorDraft(clean, replacePlanDraft(clean.draft!, { name: 'unsaved' }))
  const preserved = refreshPlanEditor(dirty, fixture())
  assert.equal(preserved.status, 'dirty')
  assert.equal(preserved.draft?.name, 'unsaved')
  assert.equal(preserved.base?.description, 'A deterministic plan')
  const conflict = refreshPlanEditor(dirty, { ...fixture(), revision: 2, name: 'authority r2' })
  assert.equal(conflict.status, 'conflict')
  assert.equal(conflict.draft?.name, 'unsaved')
  assert.deepEqual(conflict.conflict, {
    plan_id: 'plan-1', expected_base_revision: 1, observed_revision: 2, reason: 'revision_advanced',
  })
  assert.throws(() => buildPlanRevisionSave(conflict), (error: unknown) =>
    error instanceof PluginModelError && error.code === 'plan_save_not_dirty')
  assert.throws(() => updatePlanEditorDraft(conflict, replacePlanDraft(conflict.draft!, { name: 'edit after conflict' })),
    (error: unknown) => error instanceof PluginModelError && error.code === 'plan_editor_conflict_unresolved')
})

test('same immutable revision content drift conflicts for clean and dirty editors', () => {
  const clean = createPlanEditorState(fixture())
  const drift = { ...fixture(), name: 'forged same revision' }
  const cleanConflict = refreshPlanEditor(clean, drift)
  assert.equal(cleanConflict.status, 'conflict')
  assert.equal(cleanConflict.draft?.name, 'Fixture Plan')
  assert.equal(cleanConflict.conflict?.reason, 'immutable_revision_drift')
  const dirty = updatePlanEditorDraft(clean, replacePlanDraft(clean.draft!, { description: 'local' }))
  const dirtyConflict = refreshPlanEditor(dirty, drift)
  assert.equal(dirtyConflict.status, 'conflict')
  assert.equal(dirtyConflict.draft?.description, 'local')
  assert.equal(dirtyConflict.conflict?.reason, 'immutable_revision_drift')
})

test('save acceptance is bound to the exact request and rejects same-base local or result drift', () => {
  const clean = createPlanEditorState(fixture())
  const first = updatePlanEditorDraft(clean, replacePlanDraft(clean.draft!, { name: 'edit-A' }))
  const intent = buildPlanRevisionSave(first)
  const second = updatePlanEditorDraft(first, replacePlanDraft(first.draft!, { name: 'edit-B' }))
  assert.throws(() => acceptPlanRevisionSave(second, intent, intent.next_revision), (error: unknown) =>
    error instanceof PluginModelError && error.code === 'plan_save_request_stale')
  assert.throws(() => acceptPlanRevisionSave(first, intent, { ...intent.next_revision, name: 'server-drift' }),
    (error: unknown) => error instanceof PluginModelError && error.code === 'plan_save_result_drift')
})

test('prototype-backed Plan and nested binding ingress are rejected', () => {
  const inheritedPlan = Object.create(fixture())
  assert.throws(() => materializePlan(inheritedPlan), (error: unknown) =>
    error instanceof PluginModelError && error.code === 'plan_plain_object_required')
  const source = fixture()
  source.bindings = [Object.assign(Object.create({ hidden: true }), source.bindings[0])]
  assert.throws(() => materializePlan(source), (error: unknown) =>
    error instanceof PluginModelError && error.code === 'plan_binding_plain_object_required')
})

test('same plan id can coexist at multiple revisions but exact duplicate identity is rejected in snapshots', async () => {
  const { materializePluginManagementSnapshot } = await import('../../../frontend/src/core/plugins/model.ts')
  const plan1 = fixture()
  const plan2 = { ...fixture(), revision: 2 }
  const snapshot = { releases: [], plans: [plan1, plan2], selected_plan: plan2, operation: null }
  assert.deepEqual(materializePluginManagementSnapshot(snapshot).plans.map(plan => plan.revision), [1, 2])
  assert.throws(() => materializePluginManagementSnapshot({ ...snapshot, plans: [plan1, plan1] }),
    (error: unknown) => error instanceof PluginModelError && error.code === 'snapshot_plan_identity_duplicate')
  assert.throws(() => materializePluginManagementSnapshot({ ...snapshot, plans: [plan1], selected_plan: plan2 }),
    (error: unknown) => error instanceof PluginModelError && error.code === 'snapshot_selected_plan_unlisted')
})
