export type PluginPlanResultMode = 'separate' | 'compare' | 'synthesize'

export interface PluginPlanBinding {
  binding_id: string
  capability_id: string
  plugin_id: string
  release_requirement: string
  order: number
  enabled: boolean
  required: boolean
  propagate_cancel: boolean
  parameters_asset_id: string | null
}

export interface PluginPlanDataBinding {
  data_binding_id: string
  data_plugin_id: string
  release_requirement: string
  format_id: string
  interpreter_binding_id: string
  order: number
  enabled: boolean
  parameters_asset_id: string | null
}

export interface PluginPlanSynthesizer {
  binding_id: string
  capability_id: string
  plugin_id: string
  release_requirement: string
}

export interface PluginPlanUiDefault {
  slot: string
  expanded: boolean
}

/** Closed, plain-data projection of the public plugin-plan/v1 schema. */
export interface PluginPlan {
  schema: 'plugin-plan/v1'
  plan_id: string
  revision: number
  name: string
  description: string
  bindings: PluginPlanBinding[]
  data_bindings: PluginPlanDataBinding[]
  skill_preset_revision_id: string | null
  result_mode: PluginPlanResultMode
  synthesizer: PluginPlanSynthesizer | null
  model_profile_revision_id: string | null
  ui_defaults: PluginPlanUiDefault[]
}

export interface PlanRevisionSaveIntent {
  operation: 'create_revision'
  plan_id: string
  expected_base_revision: number
  next_revision: PluginPlan
}

/** Reserved discriminants keep future create/copy adapters distinct from revision CAS. */
export type PlanSaveIntent =
  | PlanRevisionSaveIntent
  | { operation: 'create'; expected_base_revision: null; next_revision: PluginPlan }
  | { operation: 'copy'; source_plan_id: string; source_revision: number; expected_base_revision: null; next_revision: PluginPlan }

export interface PlanRefreshConflict {
  plan_id: string
  expected_base_revision: number
  observed_revision: number
  reason: 'revision_advanced' | 'immutable_revision_drift'
}

export type PlanEditorStatus = 'empty' | 'clean' | 'dirty' | 'conflict'

export interface PlanEditorState {
  status: PlanEditorStatus
  base: PluginPlan | null
  draft: PluginPlan | null
  conflict: PlanRefreshConflict | null
}

export type InstallLifecycleState =
  | 'selected'
  | 'staged'
  | 'verified'
  | 'package_published'
  | 'settings_validated'
  | 'qualified'
  | 'pending_apply'
  | 'current_committed'
  | 'lkg_pending'
  | 'lkg_promoted'
  | 'failed'
  | 'superseded'
  | 'rollback_armed'
  | 'rolled_back'
  | 'safe_mode'

export interface InstallOperationProjection {
  operation_id: string
  state: InstallLifecycleState
  completed: number
  total: number | null
  failure_code: string | null
  stream_status: 'connected' | 'interrupted' | 'settled'
}

export interface PluginReleaseProjection {
  plugin_id: string
  release_id: string
  version: string
  package_hash: string
  lifecycle_state: 'installed' | 'retiring' | 'retired'
  package_present: boolean
  active: boolean
  lkg: boolean
  pin_count: number
}

/** Internal adapter read model. It is not a public wire contract. */
export interface PluginManagementSnapshot {
  releases: PluginReleaseProjection[]
  plans: PluginPlan[]
  selected_plan: PluginPlan | null
  operation: InstallOperationProjection | null
}

export interface SnapshotRefreshState {
  issued_epoch: number
  committed_epoch: number
  loading_epoch: number | null
  snapshot: PluginManagementSnapshot | null
  error: string | null
}

export interface ArchivePreflightResult {
  archive_name: string
  stripped_root: string | null
  normalized_entries: string[]
  manifest_path: 'plugin.json'
}

export interface PluginManagementReadGateway {
  loadSnapshot(): Promise<unknown>
}
