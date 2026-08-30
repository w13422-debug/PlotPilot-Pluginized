import { normalizeWindowsPath, unicodeNfcCasefold } from '../../contracts/verifier.ts'
import type {
  ArchivePreflightResult,
  InstallLifecycleState,
  InstallOperationProjection,
  PlanEditorState,
  PlanRevisionSaveIntent,
  PluginManagementSnapshot,
  PluginPlan,
  PluginPlanBinding,
  PluginPlanDataBinding,
  PluginPlanResultMode,
  PluginPlanSynthesizer,
  PluginPlanUiDefault,
  PluginReleaseProjection,
  SnapshotRefreshState,
} from './types.ts'

const ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/
const SEMVER = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/
const HASH = /^[0-9a-f]{64}$/
const PLAN_KEYS = [
  'schema', 'plan_id', 'revision', 'name', 'description', 'bindings', 'data_bindings',
  'skill_preset_revision_id', 'result_mode', 'synthesizer', 'model_profile_revision_id', 'ui_defaults',
] as const

export class PluginModelError extends Error {
  readonly code: string

  constructor(code: string, message = code) {
    super(message)
    this.name = 'PluginModelError'
    this.code = code
  }
}

function fail(code: string, message = code): never {
  throw new PluginModelError(code, message)
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) fail(`${label}_object_required`)
  const prototype = Object.getPrototypeOf(value)
  if (prototype !== Object.prototype && prototype !== null) fail(`${label}_plain_object_required`)
  return value as Record<string, unknown>
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[], label: string): void {
  const expected = new Set(keys)
  if (Object.keys(value).some(key => !expected.has(key)) || keys.some(key => !(key in value))) {
    fail(`${label}_shape_invalid`)
  }
}

function string(value: unknown, label: string, pattern?: RegExp): string {
  if (typeof value !== 'string' || (pattern && !pattern.test(value))) fail(`${label}_invalid`)
  return value
}

function nullableId(value: unknown, label: string): string | null {
  return value === null ? null : string(value, label, ID)
}

function boolean(value: unknown, label: string): boolean {
  if (typeof value !== 'boolean') fail(`${label}_invalid`)
  return value
}

function integer(value: unknown, label: string, minimum = 0): number {
  if (!Number.isInteger(value) || (value as number) < minimum) fail(`${label}_invalid`)
  return value as number
}

function array(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) fail(`${label}_array_required`)
  return value
}

function materializeBinding(value: unknown): PluginPlanBinding {
  const item = record(value, 'plan_binding')
  exactKeys(item, [
    'binding_id', 'capability_id', 'plugin_id', 'release_requirement', 'order', 'enabled',
    'required', 'propagate_cancel', 'parameters_asset_id',
  ], 'plan_binding')
  return {
    binding_id: string(item.binding_id, 'binding_id', ID),
    capability_id: string(item.capability_id, 'capability_id', ID),
    plugin_id: string(item.plugin_id, 'plugin_id', ID),
    release_requirement: string(item.release_requirement, 'release_requirement', SEMVER),
    order: integer(item.order, 'binding_order', 1),
    enabled: boolean(item.enabled, 'binding_enabled'),
    required: boolean(item.required, 'binding_required'),
    propagate_cancel: boolean(item.propagate_cancel, 'binding_propagate_cancel'),
    parameters_asset_id: nullableId(item.parameters_asset_id, 'parameters_asset_id'),
  }
}

function materializeDataBinding(value: unknown): PluginPlanDataBinding {
  const item = record(value, 'plan_data_binding')
  exactKeys(item, [
    'data_binding_id', 'data_plugin_id', 'release_requirement', 'format_id',
    'interpreter_binding_id', 'order', 'enabled', 'parameters_asset_id',
  ], 'plan_data_binding')
  return {
    data_binding_id: string(item.data_binding_id, 'data_binding_id', ID),
    data_plugin_id: string(item.data_plugin_id, 'data_plugin_id', ID),
    release_requirement: string(item.release_requirement, 'data_release_requirement', SEMVER),
    format_id: string(item.format_id, 'format_id', ID),
    interpreter_binding_id: string(item.interpreter_binding_id, 'interpreter_binding_id', ID),
    order: integer(item.order, 'data_binding_order', 1),
    enabled: boolean(item.enabled, 'data_binding_enabled'),
    parameters_asset_id: nullableId(item.parameters_asset_id, 'data_parameters_asset_id'),
  }
}

function materializeSynthesizer(value: unknown): PluginPlanSynthesizer | null {
  if (value === null) return null
  const item = record(value, 'plan_synthesizer')
  exactKeys(item, ['binding_id', 'capability_id', 'plugin_id', 'release_requirement'], 'plan_synthesizer')
  return {
    binding_id: string(item.binding_id, 'synthesizer_binding_id', ID),
    capability_id: string(item.capability_id, 'synthesizer_capability_id', ID),
    plugin_id: string(item.plugin_id, 'synthesizer_plugin_id', ID),
    release_requirement: string(item.release_requirement, 'synthesizer_release_requirement', SEMVER),
  }
}

function materializeUiDefault(value: unknown): PluginPlanUiDefault {
  const item = record(value, 'plan_ui_default')
  exactKeys(item, ['slot', 'expanded'], 'plan_ui_default')
  return { slot: string(item.slot, 'ui_default_slot', ID), expanded: boolean(item.expanded, 'ui_default_expanded') }
}

function dense<T extends { order: number }>(items: T[]): T[] {
  return items
    .map((item, index) => ({ item, index }))
    .sort((left, right) => left.item.order - right.item.order || left.index - right.index)
    .map(({ item }, index) => ({ ...item, order: index + 1 }))
}

function assertStrictlyAscendingOrder<T extends { order: number }>(items: readonly T[], code: string): void {
  for (let index = 1; index < items.length; index += 1) {
    if (items[index]!.order <= items[index - 1]!.order) fail(code)
  }
}

function reindex<T extends { order: number }>(items: T[]): T[] {
  return items.map((item, index) => ({ ...item, order: index + 1 }))
}

/** Reads every schema field and constructs a new plain Plan value; Proxy identity is never retained. */
export function materializePlan(value: unknown): PluginPlan {
  const item = record(value, 'plan')
  exactKeys(item, PLAN_KEYS, 'plan')
  if (item.schema !== 'plugin-plan/v1') fail('plan_schema_invalid')
  const mode = string(item.result_mode, 'plan_result_mode') as PluginPlanResultMode
  if (!['separate', 'compare', 'synthesize'].includes(mode)) fail('plan_result_mode_invalid')
  const bindings = array(item.bindings, 'plan_bindings').map(materializeBinding)
  assertStrictlyAscendingOrder(bindings, 'plan_binding_order_invalid')
  const dataBindings = array(item.data_bindings, 'plan_data_bindings').map(materializeDataBinding)
  assertStrictlyAscendingOrder(dataBindings, 'plan_data_binding_order_invalid')
  const plan: PluginPlan = {
    schema: 'plugin-plan/v1',
    plan_id: string(item.plan_id, 'plan_id', ID),
    revision: integer(item.revision, 'plan_revision', 1),
    name: string(item.name, 'plan_name'),
    description: string(item.description, 'plan_description'),
    bindings: dense(bindings),
    data_bindings: dense(dataBindings),
    skill_preset_revision_id: nullableId(item.skill_preset_revision_id, 'skill_preset_revision_id'),
    result_mode: mode,
    synthesizer: materializeSynthesizer(item.synthesizer),
    model_profile_revision_id: nullableId(item.model_profile_revision_id, 'model_profile_revision_id'),
    ui_defaults: array(item.ui_defaults, 'plan_ui_defaults').map(materializeUiDefault),
  }
  validatePlan(plan)
  return plan
}

export function validatePlan(plan: PluginPlan): void {
  if (plan.name.length === 0) fail('plan_name_empty')
  const ids = new Set<string>()
  for (const [index, binding] of plan.bindings.entries()) {
    if (ids.has(binding.binding_id)) fail('plan_binding_id_duplicate')
    ids.add(binding.binding_id)
    if (binding.order !== index + 1) fail('plan_binding_order_invalid')
  }
  const dataIds = new Set<string>()
  for (const [index, binding] of plan.data_bindings.entries()) {
    if (dataIds.has(binding.data_binding_id)) fail('plan_data_binding_id_duplicate')
    dataIds.add(binding.data_binding_id)
    if (binding.order !== index + 1) fail('plan_data_binding_order_invalid')
    const interpreter = plan.bindings.find(item => item.binding_id === binding.interpreter_binding_id)
    if (!interpreter || !interpreter.enabled) fail('plan_data_interpreter_unavailable')
  }
  const slots = new Set<string>()
  for (const item of plan.ui_defaults) {
    if (slots.has(item.slot)) fail('plan_ui_default_slot_duplicate')
    slots.add(item.slot)
  }
  if (plan.result_mode !== 'synthesize') {
    if (plan.synthesizer !== null) fail('plan_synthesizer_forbidden')
    return
  }
  if (plan.synthesizer === null) fail('plan_synthesizer_required')
  const match = plan.bindings.find(binding =>
    binding.enabled && binding.binding_id === plan.synthesizer?.binding_id &&
    binding.capability_id === plan.synthesizer?.capability_id &&
    binding.plugin_id === plan.synthesizer?.plugin_id &&
    binding.release_requirement === plan.synthesizer?.release_requirement)
  if (!match) fail('plan_synthesizer_unresolved')
}

export function appendPlanBinding(plan: unknown, binding: unknown): PluginPlan {
  const next = materializePlan(plan)
  const addition = materializeBinding(binding)
  if (next.bindings.some(item => item.binding_id === addition.binding_id)) fail('plan_binding_id_duplicate')
  next.bindings = [...next.bindings, { ...addition, order: next.bindings.length + 1 }]
  return materializePlan(next)
}

export function removePlanBinding(plan: unknown, bindingId: string): PluginPlan {
  const next = materializePlan(plan)
  if (!next.bindings.some(item => item.binding_id === bindingId)) fail('plan_binding_missing')
  if (next.data_bindings.some(item => item.interpreter_binding_id === bindingId)) fail('plan_binding_in_use')
  next.bindings = reindex(next.bindings.filter(item => item.binding_id !== bindingId))
  if (next.synthesizer?.binding_id === bindingId) {
    next.synthesizer = null
    next.result_mode = 'separate'
  }
  return materializePlan(next)
}

export function movePlanBinding(plan: unknown, bindingId: string, targetIndex: number): PluginPlan {
  const next = materializePlan(plan)
  const from = next.bindings.findIndex(item => item.binding_id === bindingId)
  if (from < 0) fail('plan_binding_missing')
  if (!Number.isInteger(targetIndex) || targetIndex < 0 || targetIndex >= next.bindings.length) fail('plan_binding_move_out_of_range')
  const reordered = [...next.bindings]
  const [moved] = reordered.splice(from, 1)
  if (!moved) fail('plan_binding_missing')
  reordered.splice(targetIndex, 0, moved)
  next.bindings = reindex(reordered)
  return materializePlan(next)
}

export function replacePlanDraft(plan: unknown, patch: Partial<Pick<PluginPlan,
  'name' | 'description' | 'result_mode' | 'synthesizer' | 'skill_preset_revision_id' | 'model_profile_revision_id'>>): PluginPlan {
  const current = materializePlan(plan)
  const next: PluginPlan = {
    ...current,
    name: patch.name ?? current.name,
    description: patch.description ?? current.description,
    result_mode: patch.result_mode ?? current.result_mode,
    synthesizer: patch.synthesizer === undefined ? current.synthesizer : patch.synthesizer,
    skill_preset_revision_id: patch.skill_preset_revision_id === undefined ? current.skill_preset_revision_id : patch.skill_preset_revision_id,
    model_profile_revision_id: patch.model_profile_revision_id === undefined ? current.model_profile_revision_id : patch.model_profile_revision_id,
  }
  if (next.result_mode !== 'synthesize') next.synthesizer = null
  return materializePlan(next)
}

export function createPlanEditorState(plan: unknown | null = null): PlanEditorState {
  if (plan === null) return { status: 'empty', base: null, draft: null, conflict: null }
  const plain = materializePlan(plan)
  return { status: 'clean', base: materializePlan(plain), draft: materializePlan(plain), conflict: null }
}

export function updatePlanEditorDraft(state: PlanEditorState, draft: unknown): PlanEditorState {
  if (state.status === 'conflict') fail('plan_editor_conflict_unresolved')
  if (!state.base) fail('plan_editor_base_missing')
  const plain = materializePlan(draft)
  if (plain.plan_id !== state.base.plan_id || plain.revision !== state.base.revision) fail('plan_editor_authority_drift')
  return { status: 'dirty', base: materializePlan(state.base), draft: plain, conflict: null }
}

export function refreshPlanEditor(state: PlanEditorState, incoming: unknown | null): PlanEditorState {
  if (incoming === null) {
    if (state.status === 'dirty' || state.status === 'conflict') return materializeEditor(state)
    return createPlanEditorState()
  }
  const observed = materializePlan(incoming)
  if (!state.base || !state.draft || state.status === 'empty') return createPlanEditorState(observed)
  if (observed.plan_id === state.base.plan_id && observed.revision === state.base.revision && !plansEqual(observed, state.base)) {
    return {
      status: 'conflict',
      base: materializePlan(state.base),
      draft: materializePlan(state.draft),
      conflict: {
        plan_id: state.base.plan_id,
        expected_base_revision: state.base.revision,
        observed_revision: observed.revision,
        reason: 'immutable_revision_drift',
      },
    }
  }
  if (state.status === 'dirty' || state.status === 'conflict') {
    if (observed.plan_id !== state.base.plan_id || observed.revision === state.base.revision) return materializeEditor(state)
    return {
      status: 'conflict',
      base: materializePlan(state.base),
      draft: materializePlan(state.draft),
      conflict: {
        plan_id: state.base.plan_id,
        expected_base_revision: state.base.revision,
        observed_revision: observed.revision,
        reason: 'revision_advanced',
      },
    }
  }
  return createPlanEditorState(observed)
}

function materializeEditor(state: PlanEditorState): PlanEditorState {
  return {
    status: state.status,
    base: state.base ? materializePlan(state.base) : null,
    draft: state.draft ? materializePlan(state.draft) : null,
    conflict: state.conflict ? {
      plan_id: string(state.conflict.plan_id, 'conflict_plan_id', ID),
      expected_base_revision: integer(state.conflict.expected_base_revision, 'conflict_expected_revision', 1),
      observed_revision: integer(state.conflict.observed_revision, 'conflict_observed_revision', 1),
      reason: state.conflict.reason === 'immutable_revision_drift' ? 'immutable_revision_drift' : 'revision_advanced',
    } : null,
  }
}

function plansEqual(leftValue: unknown, rightValue: unknown): boolean {
  const left = materializePlan(leftValue)
  const right = materializePlan(rightValue)
  const bindingEqual = (a: PluginPlanBinding, b: PluginPlanBinding) =>
    a.binding_id === b.binding_id && a.capability_id === b.capability_id && a.plugin_id === b.plugin_id &&
    a.release_requirement === b.release_requirement && a.order === b.order && a.enabled === b.enabled &&
    a.required === b.required && a.propagate_cancel === b.propagate_cancel && a.parameters_asset_id === b.parameters_asset_id
  const dataBindingEqual = (a: PluginPlanDataBinding, b: PluginPlanDataBinding) =>
    a.data_binding_id === b.data_binding_id && a.data_plugin_id === b.data_plugin_id &&
    a.release_requirement === b.release_requirement && a.format_id === b.format_id &&
    a.interpreter_binding_id === b.interpreter_binding_id && a.order === b.order && a.enabled === b.enabled &&
    a.parameters_asset_id === b.parameters_asset_id
  const synthesizerEqual = left.synthesizer === null
    ? right.synthesizer === null
    : right.synthesizer !== null && left.synthesizer.binding_id === right.synthesizer.binding_id &&
      left.synthesizer.capability_id === right.synthesizer.capability_id &&
      left.synthesizer.plugin_id === right.synthesizer.plugin_id &&
      left.synthesizer.release_requirement === right.synthesizer.release_requirement
  return left.schema === right.schema && left.plan_id === right.plan_id && left.revision === right.revision &&
    left.name === right.name && left.description === right.description &&
    left.skill_preset_revision_id === right.skill_preset_revision_id && left.result_mode === right.result_mode &&
    synthesizerEqual && left.model_profile_revision_id === right.model_profile_revision_id &&
    left.bindings.length === right.bindings.length && left.bindings.every((item, index) => bindingEqual(item, right.bindings[index]!)) &&
    left.data_bindings.length === right.data_bindings.length && left.data_bindings.every((item, index) => dataBindingEqual(item, right.data_bindings[index]!)) &&
    left.ui_defaults.length === right.ui_defaults.length && left.ui_defaults.every((item, index) =>
      item.slot === right.ui_defaults[index]?.slot && item.expanded === right.ui_defaults[index]?.expanded)
}

export function buildPlanRevisionSave(state: PlanEditorState): PlanRevisionSaveIntent {
  if (state.status !== 'dirty' || !state.base || !state.draft) fail('plan_save_not_dirty')
  const base = materializePlan(state.base)
  const draft = materializePlan(state.draft)
  if (base.plan_id !== draft.plan_id || base.revision !== draft.revision) fail('plan_save_stale_base')
  return {
    operation: 'create_revision',
    plan_id: base.plan_id,
    expected_base_revision: base.revision,
    next_revision: materializePlan({ ...draft, revision: base.revision + 1 }),
  }
}

export function acceptPlanRevisionSave(state: PlanEditorState, intent: PlanRevisionSaveIntent, saved: unknown): PlanEditorState {
  if (state.status !== 'dirty' || !state.base) fail('plan_save_state_stale')
  if (intent.operation !== 'create_revision' || state.base.plan_id !== intent.plan_id ||
      state.base.revision !== intent.expected_base_revision || !state.draft) fail('plan_save_stale_base')
  const currentNext = materializePlan({ ...materializePlan(state.draft), revision: state.base.revision + 1 })
  if (!plansEqual(currentNext, intent.next_revision)) fail('plan_save_request_stale')
  const plain = materializePlan(saved)
  if (!plansEqual(plain, intent.next_revision)) fail('plan_save_result_drift')
  return createPlanEditorState(plain)
}

function materializeRelease(value: unknown): PluginReleaseProjection {
  const item = record(value, 'plugin_release_projection')
  exactKeys(item, ['plugin_id', 'release_id', 'version', 'package_hash', 'lifecycle_state', 'package_present', 'active', 'lkg', 'pin_count'], 'plugin_release_projection')
  const lifecycle = string(item.lifecycle_state, 'release_lifecycle')
  if (!['installed', 'retiring', 'retired'].includes(lifecycle)) fail('release_lifecycle_invalid')
  return {
    plugin_id: string(item.plugin_id, 'release_plugin_id', ID),
    release_id: string(item.release_id, 'release_id', HASH),
    version: string(item.version, 'release_version', SEMVER),
    package_hash: string(item.package_hash, 'package_hash', HASH),
    lifecycle_state: lifecycle as PluginReleaseProjection['lifecycle_state'],
    package_present: boolean(item.package_present, 'package_present'),
    active: boolean(item.active, 'release_active'),
    lkg: boolean(item.lkg, 'release_lkg'),
    pin_count: integer(item.pin_count, 'release_pin_count'),
  }
}

const INSTALL_STATES: InstallLifecycleState[] = [
  'selected', 'staged', 'verified', 'package_published', 'settings_validated', 'qualified',
  'pending_apply', 'current_committed', 'lkg_pending', 'lkg_promoted', 'failed', 'superseded',
  'rollback_armed', 'rolled_back', 'safe_mode',
]
const TERMINAL_STATES = new Set<InstallLifecycleState>(['lkg_promoted', 'failed', 'superseded', 'rolled_back', 'safe_mode'])

export function materializeInstallOperation(value: unknown): InstallOperationProjection {
  const item = record(value, 'install_operation')
  exactKeys(item, ['operation_id', 'state', 'completed', 'total', 'failure_code', 'stream_status'], 'install_operation')
  const state = string(item.state, 'install_state') as InstallLifecycleState
  if (!INSTALL_STATES.includes(state)) fail('install_state_invalid')
  const total = item.total === null ? null : integer(item.total, 'install_total')
  const completed = integer(item.completed, 'install_completed')
  if (total !== null && completed > total) fail('install_progress_invalid')
  const streamStatus = string(item.stream_status, 'install_stream_status')
  if (!['connected', 'interrupted', 'settled'].includes(streamStatus)) fail('install_stream_status_invalid')
  return {
    operation_id: string(item.operation_id, 'install_operation_id', ID),
    state,
    completed,
    total,
    failure_code: item.failure_code === null ? null : string(item.failure_code, 'install_failure_code', ID),
    stream_status: streamStatus as InstallOperationProjection['stream_status'],
  }
}

export function isInstallOperationTerminal(operation: unknown): boolean {
  const plain = materializeInstallOperation(operation)
  return TERMINAL_STATES.has(plain.state) || plain.failure_code !== null
}

export function isInstallOperationProcessing(operation: unknown, busy: boolean, activeOperationId: string | null): boolean {
  const plain = materializeInstallOperation(operation)
  return busy && activeOperationId === plain.operation_id && plain.stream_status === 'connected' && !isInstallOperationTerminal(plain)
}

export function interruptInstallOperation(operation: unknown): InstallOperationProjection {
  const plain = materializeInstallOperation(operation)
  return materializeInstallOperation({ ...plain, stream_status: 'interrupted' })
}

export function materializePluginManagementSnapshot(value: unknown): PluginManagementSnapshot {
  const item = record(value, 'plugin_management_snapshot')
  exactKeys(item, ['releases', 'plans', 'selected_plan', 'operation'], 'plugin_management_snapshot')
  const plans = array(item.plans, 'snapshot_plans').map(materializePlan)
  const identity = new Set<string>()
  for (const plan of plans) {
    const key = `${plan.plan_id}:${plan.revision}`
    if (identity.has(key)) fail('snapshot_plan_identity_duplicate')
    identity.add(key)
  }
  const releases = array(item.releases, 'snapshot_releases').map(materializeRelease)
  const releaseIdentity = new Set<string>()
  for (const release of releases) {
    const key = `${release.plugin_id}:${release.release_id}`
    if (releaseIdentity.has(key)) fail('snapshot_release_identity_duplicate')
    releaseIdentity.add(key)
  }
  const selectedPlan = item.selected_plan === null ? null : materializePlan(item.selected_plan)
  if (selectedPlan) {
    const listed = plans.find(plan => plan.plan_id === selectedPlan.plan_id && plan.revision === selectedPlan.revision)
    if (!listed || !plansEqual(listed, selectedPlan)) fail('snapshot_selected_plan_unlisted')
  }
  return {
    releases,
    plans,
    selected_plan: selectedPlan,
    operation: item.operation === null ? null : materializeInstallOperation(item.operation),
  }
}

export function createSnapshotRefreshState(snapshot: unknown | null = null): SnapshotRefreshState {
  return {
    issued_epoch: 0,
    committed_epoch: 0,
    loading_epoch: null,
    snapshot: snapshot === null ? null : materializePluginManagementSnapshot(snapshot),
    error: null,
  }
}

export function beginSnapshotRefresh(state: SnapshotRefreshState): { state: SnapshotRefreshState; epoch: number } {
  const epoch = state.issued_epoch + 1
  return { state: { ...state, issued_epoch: epoch, loading_epoch: epoch, error: null }, epoch }
}

export function commitSnapshotRefresh(state: SnapshotRefreshState, epoch: number, snapshot: unknown): SnapshotRefreshState {
  if (epoch !== state.issued_epoch) return { ...state }
  return {
    issued_epoch: state.issued_epoch,
    committed_epoch: epoch,
    loading_epoch: null,
    snapshot: materializePluginManagementSnapshot(snapshot),
    error: null,
  }
}

export function failSnapshotRefresh(state: SnapshotRefreshState, epoch: number, error: unknown): SnapshotRefreshState {
  if (epoch !== state.issued_epoch) return { ...state }
  return { ...state, loading_epoch: null, error: error instanceof Error ? error.message : String(error) }
}

export function preflightPluginArchive(archiveName: string, rawEntries: readonly string[]): ArchivePreflightResult {
  const extensionIdentity = unicodeNfcCasefold(archiveName)
  if (!extensionIdentity.endsWith('.zip') && !extensionIdentity.endsWith('.ppplugin')) fail('install_archive_extension_invalid')
  if (rawEntries.length === 0) fail('install_archive_empty')
  let entries: string[]
  try {
    const normalized = rawEntries.map(rawEntry => {
      if (typeof rawEntry !== 'string' || rawEntry.length === 0) fail('install_archive_path_invalid')
      const directory = rawEntry.endsWith('/')
      const candidate = directory ? rawEntry.slice(0, -1) : rawEntry
      return { path: normalizeWindowsPath(candidate), directory }
    })
    const allIdentities = new Set<string>()
    for (const entry of normalized) {
      const identity = unicodeNfcCasefold(entry.path)
      if (allIdentities.has(identity)) fail('install_archive_path_collision')
      allIdentities.add(identity)
    }
    entries = normalized.filter(entry => !entry.directory).map(entry => entry.path)
  } catch (error) {
    if (error instanceof PluginModelError) throw error
    fail('install_archive_path_invalid', error instanceof Error ? error.message : 'install_archive_path_invalid')
  }
  if (entries.length === 0) fail('install_archive_empty')
  const unique = new Set<string>()
  for (const entry of entries) {
    const identity = unicodeNfcCasefold(entry)
    if (unique.has(identity)) fail('install_archive_path_collision')
    unique.add(identity)
  }
  let strippedRoot: string | null = null
  if (!entries.some(entry => unicodeNfcCasefold(entry) === 'plugin.json')) {
    const roots = new Set(entries.map(entry => entry.split('/')[0]).filter(Boolean))
    if (roots.size !== 1 || entries.some(entry => !entry.includes('/'))) fail('install_root_manifest_required')
    strippedRoot = [...roots][0] ?? null
    if (!strippedRoot) fail('install_root_manifest_required')
    entries = entries.map(entry => entry.slice(strippedRoot!.length + 1))
    if (!entries.some(entry => unicodeNfcCasefold(entry) === 'plugin.json')) fail('install_root_manifest_required')
    const strippedIdentities = new Set<string>()
    for (const entry of entries) {
      const identity = unicodeNfcCasefold(entry)
      if (strippedIdentities.has(identity)) fail('install_archive_path_collision')
      strippedIdentities.add(identity)
    }
  }
  return {
    archive_name: archiveName.normalize('NFC'),
    stripped_root: strippedRoot,
    normalized_entries: [...entries],
    manifest_path: 'plugin.json',
  }
}
