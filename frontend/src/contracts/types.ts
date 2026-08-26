export type Hash = string
export type Id = string
export type ContractId = 'candidate-batch/v1' | 'artifact-bundle/v1' | 'diagnostic-bundle/v1'
export type EntityKind = 'document' | 'node_structure' | 'relation_set'

export interface Scope {
  document_id: Id | null
  node_id: Id | null
  operation: Id
}

export interface InputRevision {
  document_id: Id
  revision_id: Id
  content_hash: Hash
}

export interface AssetHash {
  asset_id: Id
  sha256: Hash
}

export interface PluginReleaseBinding {
  plugin_id: Id
  release_id: Id
  package_hash: Hash
  data_generation_id: Id | null
}

export interface SettingsBinding {
  plugin_id: Id
  settings_revision_id: Id
  scope: 'global' | 'workspace'
  scope_id: Id | null
  schema_hash: Hash
  validated_by_release_id: Id
}

export interface DataBinding {
  data_plugin_id: Id
  data_release_id: Id
  format_id: Id
  bundle_asset_id: Id
  bundle_hash: Hash
  interpreter_binding_id: Id
  order: number
}

export interface SkillBinding {
  skill_id: Id
  release_id: Id
  package_hash: Hash
  parameters_asset_id: Id | null
  order: number
}

export interface RunSnapshot {
  schema: 'run-snapshot/v1'
  snapshot_id: Id
  core_contract_version: string
  workspace_id: Id
  scope: Scope
  input_revisions: InputRevision[]
  plan_revision_id: Id
  plugin_releases: PluginReleaseBinding[]
  plugin_settings_revisions: SettingsBinding[]
  data_bindings: DataBinding[]
  skill_releases: SkillBinding[]
  model_profile_revision_id: Id | null
  parameters_asset_id: Id | null
  asset_hashes: AssetHash[]
  request_key: Hash
  run_intent_id: Id
  created_at: string
  snapshot_hash: Hash
}

export interface Target {
  workspace_id: Id
  entity_kind: EntityKind
  entity_id: Id
}

export interface WriteSetEntry extends Target {
  revision_id: Id
  content_hash: Hash
}

export interface SourceRef {
  workspace_id: Id | null
  source_type: Id
  source_id: Id
  revision_or_hash: string
}

export interface CandidateItem {
  schema: 'candidate-item/v1'
  item_id: Id
  item_kind: EntityKind | 'incomplete_stream'
  target: Target
  mutation: {
    mode: 'replace' | 'text_patch' | 'structure_patch' | 'relation_patch' | 'append_text'
    payload_schema: Id
    payload_hash: Hash
  }
  payload_asset_id: Id
  base: { revision_id: Id; content_hash: Hash }
  write_set: WriteSetEntry[]
  parent_candidate_ids: Id[]
  source_refs: SourceRef[]
  status: 'complete' | 'partial' | 'failed' | 'skipped'
}

export interface ArtifactItem {
  schema: 'artifact-item/v1'
  item_id: Id
  artifact_kind: Id
  payload_asset_id: Id
  payload_hash: Hash
  mime: string
  source_refs: SourceRef[]
  status: 'complete' | 'partial' | 'failed' | 'skipped'
}

export interface DiagnosticItem {
  schema: 'diagnostic-item/v1'
  item_id: Id
  severity: 'info' | 'warning' | 'error'
  code: string
  message: string
  details_asset_id: Id | null
  details_hash: Hash | null
  source_refs: SourceRef[]
  status: 'complete' | 'failed' | 'skipped'
}

export interface SkillChainRef {
  schema: 'skill-chain-ref/v1'
  chain_result_id: Id
  asset_id: Id | null
  asset_hash: Hash | null
  result_bundle_id: Id | null
  result_item_id: Id | null
  stream_id: Id | null
  acked_prefix_hash: Hash | null
}

export interface Producer {
  plugin_id: Id
  release_id: Hash
  capability_id: Id
  job_id: Id
  step_id: Id
  attempt_id: Id
  lease_epoch: number
}

export interface ResultBundle {
  schema: 'result-bundle/v1'
  contract_id: ContractId
  bundle_id: Id
  bundle_type: 'candidate_batch' | 'artifact' | 'diagnostic'
  producer: Producer
  input_snapshot_hash: Hash
  items: Array<CandidateItem | ArtifactItem | DiagnosticItem>
  warnings: Array<{ code: string; message: string; details_asset_id: Id | null }>
  partial: boolean
  provenance_receipt_id: Id
  skill_chain_result_refs: SkillChainRef[]
}

export interface RpcMetaBase {
  protocol_version: '1'
  generation_id: Id
  plugin_release_id: Hash
  deadline_at: string
  operation_id: Id
}

export interface RpcControlMeta extends RpcMetaBase { context: 'control' }
export interface RpcInstallMeta extends RpcMetaBase { context: 'install'; install_operation_id: Id; install_lease_epoch: number }
export interface RpcAttemptMeta extends RpcMetaBase { context: 'attempt'; job_id: Id; step_id: Id; attempt_id: Id; lease_epoch: number }
export type RpcMeta = RpcControlMeta | RpcInstallMeta | RpcAttemptMeta
export type RpcMethod =
  | 'runtime.handshake' | 'runtime.health' | 'runtime.heartbeat' | 'capability.describe' | 'settings.validate'
  | 'migration.plan' | 'migration.apply' | 'migration.verify' | 'job.start' | 'job.resume' | 'job.pause'
  | 'job.cancel' | 'runtime.shutdown'
  | 'host.asset.read/v1' | 'host.asset.create/v1' | 'host.asset.upload.status/v1' | 'host.model.invoke/v1'
  | 'host.capability.invoke/v1' | 'host.capability.poll/v1' | 'host.capability.cancel/v1' | 'host.candidate.stage/v1'
  | 'host.checkpoint.commit/v1' | 'host.stream.commit/v1' | 'host.job.event/v1' | 'host.job.await_user/v1'
  | 'host.job.complete/v1' | 'host.log/v1' | 'host.migration.lease.renew/v1' | 'host.migration.lease.release/v1'

export interface RpcRequest {
  jsonrpc: '2.0'
  id: string
  method: RpcMethod
  meta: RpcMeta
  params: Record<string, unknown>
}

export interface RpcNotification {
  jsonrpc: '2.0'
  method: 'runtime.heartbeat'
  meta: RpcAttemptMeta
  params: Record<string, unknown>
}

export interface RpcSuccess {
  jsonrpc: '2.0'
  id: string
  result: Record<string, unknown>
}

export interface RpcError {
  jsonrpc: '2.0'
  id: string | null
  error: { code: number; message: string; data: { error_id: Id; retryable: boolean; details_asset_id: Id | null } | null }
}

export interface PluginUIIntent {
  schema: 'plugin-ui-intent/v1'
  intent_id: Id
  intent_seq: number
  render_seq: number
  action_id: Id
  event_type: 'change' | 'click' | 'select_row' | 'change_tab' | 'select_node' | 'open_core_operation'
  intent_kind: 'invoke_capability' | 'open_core_operation' | 'request_candidate_preview' | 'request_job_cancel' | 'set_view_state'
  capability_id: Id | null
  payload_asset_id: Id | null
  operation_key: Id
  freshness: { generation_id: Id; plugin_release_id: Hash; workspace_id: Id | null; workspace_revision_id: Id | null; plan_revision_id: Id | null }
}

export interface BackupBundle {
  schema: 'backup-bundle/v1'
  backup_id: Id
  library_root_id: Id
  backup_epoch: number
  mode: 'full' | 'data' | 'workspace'
  workspace_ids: Id[]
  core_contract_version: string
  core_snapshot_hash: Hash
  workspace_snapshot_hash: Hash | null
  current_generation_id: Id | null
  lkg_generation_id: Id | null
  asset_closure_root: Hash
  plugin_releases: Array<{ plugin_id: Id; release_id: Hash; package_hash: Hash; package_present: boolean }>
  projection_rebuild_required: Array<{ plugin_id: Id; release_id: Hash; reason: 'plugin_projection_rebuild_required' }>
  files: Array<{ path: string; size: number; sha256: Hash; role: 'core_db' | 'plugin_db' | 'asset' | 'package' | 'metadata' }>
  created_at: string
  verification: { databases_valid: boolean; assets_valid: boolean; files_valid: boolean; compatible: boolean; verified_at: string }
  bundle_hash: Hash
}
