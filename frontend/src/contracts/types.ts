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

/* ------------------------------------------------------------------------- *
 * M1 public Core/UI contracts
 * ------------------------------------------------------------------------- */

export type JobState =
  | 'queued'
  | 'running'
  | 'waiting_user'
  | 'paused'
  | 'cancelling'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'cancelled'
  | 'needs_attention'

export interface JobSnapshotStep {
  step_id: Id
  state: string
  revision: number
}

export interface JobSnapshotAttempt {
  attempt_id: Id
  state: string
  lease_epoch: number
}

export interface JobSnapshotStreamHighWater {
  stream_id: Id
  step_id: Id
  output_role: string
  target: Target
  acked_prefix_seq: number
  acked_bytes: number
  acked_prefix_hash: Hash
}

export interface JobSnapshot {
  schema: 'job-snapshot/v1'
  job_id: Id
  workspace_id: Id
  job_state: JobState
  job_revision: number
  steps: JobSnapshotStep[]
  attempts: JobSnapshotAttempt[]
  candidate_ids: Id[]
  current_checkpoint_id: Id | null
  stream_high_waters: JobSnapshotStreamHighWater[]
  core_event_high_water: number
  job_event_high_water: number
  created_at: string
  snapshot_hash: Hash
}

export type PluginUIComponent =
  | 'stack'
  | 'text'
  | 'input'
  | 'textarea'
  | 'select'
  | 'button'
  | 'table'
  | 'tabs'
  | 'diff'
  | 'tree'
  | 'graph'
  | 'progress'
  | 'candidate_preview'

export interface PluginUINodeBase<C extends PluginUIComponent, P> {
  component: C
  key: Id
  props: P
  children: PluginUINode[]
  event_ids: Id[]
}

export interface PluginUIStackProps { direction: 'horizontal' | 'vertical'; row_gap: number }
export interface PluginUITextProps { text: string; tone: string }
export interface PluginUIInputProps { label: string; value: string; placeholder: string; disabled: boolean }
export interface PluginUITextareaProps { label: string; value: string; rows: number; disabled: boolean }
export interface PluginUISelectProps { label: string; value: string; options_asset_id: Id; disabled: boolean }
export interface PluginUIButtonProps { label: string; tone: string; disabled: boolean }
export interface PluginUITableProps { columns_asset_id: Id; rows_asset_id: Id; empty_text: string }
export interface PluginUITabsProps { active_tab: string; tabs_asset_id: Id }
export interface PluginUIDiffProps { before_asset_id: Id; after_asset_id: Id; language: string }
export interface PluginUITreeProps { nodes_asset_id: Id; selected_id: Id | null }
export interface PluginUIGraphProps { graph_asset_id: Id; layout: string }
export interface PluginUIProgressProps { label: string; completed: number; total: number | null; state: string }
export interface PluginUICandidatePreviewProps { candidate_id: Id; view_mode: string }

export type PluginUIStack = PluginUINodeBase<'stack', PluginUIStackProps>
export type PluginUIText = PluginUINodeBase<'text', PluginUITextProps>
export type PluginUIInput = PluginUINodeBase<'input', PluginUIInputProps>
export type PluginUITextarea = PluginUINodeBase<'textarea', PluginUITextareaProps>
export type PluginUISelect = PluginUINodeBase<'select', PluginUISelectProps>
export type PluginUIButton = PluginUINodeBase<'button', PluginUIButtonProps>
export type PluginUITable = PluginUINodeBase<'table', PluginUITableProps>
export type PluginUITabs = PluginUINodeBase<'tabs', PluginUITabsProps>
export type PluginUIDiff = PluginUINodeBase<'diff', PluginUIDiffProps>
export type PluginUITreeNode = PluginUINodeBase<'tree', PluginUITreeProps>
export type PluginUIGraph = PluginUINodeBase<'graph', PluginUIGraphProps>
export type PluginUIProgress = PluginUINodeBase<'progress', PluginUIProgressProps>
export type PluginUICandidatePreview = PluginUINodeBase<'candidate_preview', PluginUICandidatePreviewProps>

/** The closed 13-component discriminator union from formal design §84.10. */
export type PluginUINode =
  | PluginUIStack
  | PluginUIText
  | PluginUIInput
  | PluginUITextarea
  | PluginUISelect
  | PluginUIButton
  | PluginUITable
  | PluginUITabs
  | PluginUIDiff
  | PluginUITreeNode
  | PluginUIGraph
  | PluginUIProgress
  | PluginUICandidatePreview

export interface PluginUITree {
  schema: 'plugin-ui-tree/v1'
  tree_id: Id
  render_seq: number
  root: PluginUINode
}

export interface PluginUIAck {
  schema: 'plugin-ui-ack/v1'
  intent_id: Id
  accepted: boolean
  error_code: string | null
  core_event_seq: number | null
  job_id: Id | null
}

export type CoreEntityKind = 'document' | 'node' | 'relation' | 'workspace'

export interface CoreWorkspace {
  schema: 'core-workspace/v1'
  workspace_id: Id
  workspace_kind: string
  title: string
  status: string
  current_plan_revision_id: Id | null
  created_at: string
  updated_at: string
  revision: number
}

export interface CoreDocument {
  schema: 'core-document/v1'
  document_id: Id
  workspace_id: Id
  document_type: string
  title: string
  current_revision_id: Id | null
  created_at: string
  updated_at: string
  revision: number
}

export interface CoreNode {
  schema: 'core-node/v1'
  node_id: Id
  workspace_id: Id
  document_id: Id | null
  node_type: string
  title: string
  parent_node_id: Id | null
  position: number
  current_revision_id: Id | null
  created_at: string
  updated_at: string
  revision: number
}

export interface CoreRelation {
  schema: 'core-relation/v1'
  relation_id: Id
  workspace_id: Id
  relation_type: string
  source_id: Id
  target_id: Id
  revision_id: Id | null
  created_at: string
}

export interface CoreRevision {
  schema: 'core-revision/v1'
  revision_id: Id
  workspace_id: Id
  document_id: Id | null
  node_id: Id | null
  parent_revision_id: Id | null
  content_hash: Hash
  created_by: Id
  source_candidate_id: Id | null
  created_at: string
  revision_number: number
  payload_schema: Id | null
}

/** Closed payload carried by one Core Document Revision for a PlotPilot Project Brief. */
export type ProjectBriefLengthTier = 'short' | 'standard' | 'epic'

export interface ProjectBriefGenres {
  genre: string
}

export interface ProjectBriefStructure {
  story_structure: string
  pacing_control: string
  writing_style: string
  special_requirements: string
}

export interface ProjectBriefMarket {
  world_preset: string
}

export interface ProjectBriefLength {
  tier: ProjectBriefLengthTier
  use_custom: boolean
  custom_chapters: number
  custom_words_per_chapter: number
}

export interface ProjectBriefContent {
  workspace_id: Id
  premise: string
  genres: ProjectBriefGenres
  target_words: number
  structure: ProjectBriefStructure
  market: ProjectBriefMarket
  length: ProjectBriefLength
}

/** UI-facing names preserve the existing Home model while the persisted shape stays snake_case. */
export interface ProjectBriefHomeInput {
  premise: string
  genre: string
  worldPreset: string
  storyStructure: string
  pacingControl: string
  writingStyle: string
  specialRequirements: string
  lengthTier: ProjectBriefLengthTier
  useCustomLength: boolean
  customChapters: number
  customWordsPerChapter: number
}

export type CoreAuthorityEntity = CoreWorkspace | CoreDocument | CoreNode | CoreRelation | CoreRevision

export interface CorePage<T, S extends string = string> {
  schema: S
  items: T[]
  offset: number
  limit: number
  total: number
  next_offset: number | null
}

export interface CoreRevisionContentPage {
  schema: 'core-revision-content-page/v1'
  revision_id: Id
  offset: number
  length: number
  total_length: number
  text: string
  next_offset: number | null
}

export interface CoreWorkspacePage extends CorePage<CoreWorkspace, 'core-workspace-page/v1'> {}
export interface CoreDocumentPage extends CorePage<CoreDocument, 'core-document-page/v1'> {}
export interface CoreNodePage extends CorePage<CoreNode, 'core-node-page/v1'> {}
export interface CoreRelationPage extends CorePage<CoreRelation, 'core-relation-page/v1'> {}
export interface CoreRevisionPage extends CorePage<CoreRevision, 'core-revision-page/v1'> {}

export interface CoreWorkspaceQuery {
  schema: 'core-workspace-query/v1'
  workspace_id: Id | null
  offset: number
  limit: number
}

export interface CoreWorkspaceGetQuery {
  schema: 'core-workspace-get-query/v1'
  workspace_id: Id
}

export interface CoreDocumentQuery {
  schema: 'core-document-query/v1'
  workspace_id: Id
  document_id: Id | null
  offset: number
  limit: number
}

export interface CoreDocumentGetQuery {
  schema: 'core-document-get-query/v1'
  workspace_id: Id
  document_id: Id
}

export interface CoreNodeQuery {
  schema: 'core-node-query/v1'
  workspace_id: Id
  node_id: Id | null
  document_id: Id | null
  parent_node_id: Id | null
  offset: number
  limit: number
}

export interface CoreNodeGetQuery {
  schema: 'core-node-get-query/v1'
  workspace_id: Id
  node_id: Id
}

export interface CoreRelationQuery {
  schema: 'core-relation-query/v1'
  workspace_id: Id
  relation_id: Id | null
  source_id: Id | null
  target_id: Id | null
  relation_type: Id | null
  offset: number
  limit: number
}

export interface CoreDocumentRevisionQuery {
  schema: 'core-document-revision-query/v1'
  workspace_id: Id
  document_id: Id
  revision_id: Id | null
  offset: number
  limit: number
}

export interface CoreNodeRevisionQuery {
  schema: 'core-node-revision-query/v1'
  workspace_id: Id
  node_id: Id
  revision_id: Id | null
  offset: number
  limit: number
}

export interface CoreRevisionGetQuery {
  schema: 'core-revision-get-query/v1'
  workspace_id: Id
  revision_id: Id
}

export interface CoreContentQuery {
  schema: 'core-revision-content-query/v1'
  workspace_id: Id
  revision_id: Id
  offset: number
  length: number
}

export interface CoreWorkspaceCreateCommand {
  schema: 'core-workspace-create-command/v1'
  operation_key: Id
  workspace_id: Id
  workspace_kind: string
  title: string
}

export interface CoreWorkspaceUpdateCommand {
  schema: 'core-workspace-update-command/v1'
  operation_key: Id
  workspace_id: Id
  expected_revision: number
  title: string | null
  status: string | null
}

export interface CoreWorkspaceDeleteCommand {
  schema: 'core-workspace-delete-command/v1'
  operation_key: Id
  workspace_id: Id
  expected_revision: number
}

export interface CoreDocumentCreateCommand {
  schema: 'core-document-create-command/v1'
  operation_key: Id
  document_id: Id
  workspace_id: Id
  document_type: string
  title: string
}

export interface CoreDocumentUpdateCommand {
  schema: 'core-document-update-command/v1'
  operation_key: Id
  document_id: Id
  workspace_id: Id
  expected_revision: number
  title: string
}

export interface CoreNodeCreateCommand {
  schema: 'core-node-create-command/v1'
  operation_key: Id
  node_id: Id
  workspace_id: Id
  document_id: Id | null
  node_type: string
  title: string
  parent_node_id: Id | null
  position: number
}

export interface CoreNodeUpdateCommand {
  schema: 'core-node-update-command/v1'
  operation_key: Id
  node_id: Id
  workspace_id: Id
  expected_revision: number
  title: string
  parent_node_id: Id | null
  position: number
}

export interface CoreNodeDeleteCommand {
  schema: 'core-node-delete-command/v1'
  operation_key: Id
  node_id: Id
  workspace_id: Id
  expected_revision: number
}

export interface CoreRelationCreateCommand {
  schema: 'core-relation-create-command/v1'
  operation_key: Id
  relation_id: Id
  workspace_id: Id
  relation_type: string
  source_id: Id
  target_id: Id
  revision_id: Id | null
}

export interface CoreRelationDeleteCommand {
  schema: 'core-relation-delete-command/v1'
  operation_key: Id
  relation_id: Id
  workspace_id: Id
  expected_revision_id: Id | null
}

export interface CoreDocumentRevisionCreateCommand {
  schema: 'core-document-revision-create-command/v1'
  operation_key: Id
  revision_id: Id
  workspace_id: Id
  document_id: Id
  base_revision_id: Id | null
  content: string
  created_by: Id
  source_candidate_id: Id | null
  payload_schema: Id | null
}

export interface CoreNodeRevisionCreateCommand {
  schema: 'core-node-revision-create-command/v1'
  operation_key: Id
  revision_id: Id
  workspace_id: Id
  node_id: Id
  base_revision_id: Id | null
  content: string
  created_by: Id
  source_candidate_id: Id | null
  payload_schema: Id | null
}

export interface CoreDeleteResult {
  schema: 'core-delete-result/v1'
  operation_key: Id
  workspace_id: Id
  entity_kind: 'workspace' | 'node' | 'relation'
  entity_id: Id
  previous_revision: number | null
  deleted: true
  idempotent: boolean
}

export type CoreAuthorityCommandQuery =
  | CoreAuthorityEntity
  | CoreWorkspacePage
  | CoreDocumentPage
  | CoreNodePage
  | CoreRelationPage
  | CoreRevisionPage
  | CoreWorkspaceQuery
  | CoreWorkspaceGetQuery
  | CoreDocumentQuery
  | CoreDocumentGetQuery
  | CoreNodeQuery
  | CoreNodeGetQuery
  | CoreRelationQuery
  | CoreDocumentRevisionQuery
  | CoreNodeRevisionQuery
  | CoreRevisionGetQuery
  | CoreContentQuery
  | CoreRevisionContentPage
  | CoreWorkspaceCreateCommand
  | CoreWorkspaceUpdateCommand
  | CoreWorkspaceDeleteCommand
  | CoreDocumentCreateCommand
  | CoreDocumentUpdateCommand
  | CoreNodeCreateCommand
  | CoreNodeUpdateCommand
  | CoreNodeDeleteCommand
  | CoreRelationCreateCommand
  | CoreRelationDeleteCommand
  | CoreDocumentRevisionCreateCommand
  | CoreNodeRevisionCreateCommand
  | CoreDeleteResult

export interface CoreAuthorityValidationOptions {
  expectedWorkspaceId?: Id
}

export interface PublicationCommand {
  schema: 'publication-command/v1'
  publication_operation_key: Id
  workspace_id: Id
  candidate_id: Id
  accepted_by: Id
}

export interface CoreRevisionRef {
  revision_id: Id
  workspace_id: Id
  entity_kind: EntityKind
  entity_id: Id
  content_hash: Hash
  revision_number: number
}

export interface PublicationResult {
  schema: 'publication-result/v1'
  publication_id: Id
  candidate_id: Id
  workspace_id: Id
  entity_kind: EntityKind
  entity_id: Id
  resulting_revision: CoreRevisionRef
  idempotent: boolean
}

export type PublicationCommandResult = PublicationCommand | PublicationResult

export type CoreHttpErrorCode =
  | 'unknown_reference'
  | 'cross_workspace'
  | 'stale_cas'
  | 'operation_key_reuse'
  | 'incomplete_publication'

export interface CoreHttpError {
  schema: 'core-http-error/v1'
  error_code: CoreHttpErrorCode
  message: string
  retryable: boolean
}

export type CoreHttpRequestErrorCode =
  | 'malformed_json'
  | 'invalid_request'
  | 'invalid_query'
  | 'range_out_of_bounds'

export interface CoreHttpRequestError {
  schema: 'core-http-request-error/v1'
  error_code: CoreHttpRequestErrorCode
  message: string
  retryable: false
}

export type CoreHttpRequestFailureSource =
  | 'json_decode'
  | 'closed_request_schema'
  | 'query_decode'
  | 'asset_range_bounds'

export interface CoreHttpRequestFailureBinding {
  source: CoreHttpRequestFailureSource
  error_code: CoreHttpRequestErrorCode
  scope: 'all_core_routes' | 'asset.range'
}

export interface CoreHttpRequestFailurePolicy {
  schema: 'core-http-request-failure-policy/v1'
  status: 400
  error_schema: 'core-http-request-error/v1'
  retryable: false
  bindings: CoreHttpRequestFailureBinding[]
}

export interface PublicationValidationOptions {
  expectedWorkspaceId?: Id
  command?: PublicationCommand
}

export interface AssetMetadata {
  schema: 'asset-metadata/v1'
  asset_id: Id
  sha256: Hash
  mime: string
  size: number
  logical_role: string
  provenance: string
  rebuildable: boolean
}

export interface AssetQuery {
  schema: 'asset-query/v1'
  asset_id: Id
}

export interface AssetReadRangeQuery {
  schema: 'asset-read-range-query/v1'
  asset_id: Id
  offset: number
  length: number
}

export interface AssetReadRange {
  schema: 'asset-read-range/v1'
  asset_id: Id
  offset: number
  length: number
  total_size: number
  base64_chunk: string
  next_offset: number | null
  content_hash: Hash
}

export type AssetMetadataResponse = AssetQuery | AssetReadRangeQuery | AssetMetadata | AssetReadRange

export interface ExportCurrentRevision {
  ordinal: number
  document_id: Id
  document_type: string
  title: string
  revision_id: Id
  content_asset_id: Id
  content_hash: Hash
  mime: string
  encoding: string
}

export interface ExportCurrentRevisions {
  schema: 'export-current-revisions/v1'
  workspace_id: Id
  core_snapshot_revision: number
  ordered_revisions: ExportCurrentRevision[]
  generated_at: string
}

export interface ExportValidationOptions {
  expectedWorkspaceId?: Id
  runSnapshot?: Pick<RunSnapshot, 'workspace_id' | 'input_revisions' | 'parameters_asset_id' | 'asset_hashes' | 'snapshot_hash'>
}

export interface ExportAssetValidationOptions {
  /** Optional assertion for the Asset ID already frozen in RunSnapshot. */
  assetId?: Id
}

export type CoreHttpMethod = 'GET' | 'POST' | 'PATCH' | 'DELETE'

export interface CoreHttpRouteFixture<TRequest = unknown, TResponse = unknown> {
  method: CoreHttpMethod
  path: string
  request?: TRequest
  status: number
  response: TResponse
  headers?: Record<string, string>
}

export interface CoreHttpExchange<TRequest = unknown, TResponse = unknown> {
  request: TRequest
  status: number
  response: TResponse
}

/** Data-only typed fixture; it intentionally exposes no plugin or Publication method. */
export class CoreHttpContractFixture<TRequest = unknown, TResponse = unknown> {
  readonly method: CoreHttpMethod
  readonly path: string
  readonly request: TRequest | undefined
  readonly status: number
  readonly response: TResponse
  readonly headers: Record<string, string> | undefined

  constructor(route: CoreHttpRouteFixture<TRequest, TResponse>) {
    this.method = route.method
    this.path = route.path
    this.request = route.request
    this.status = route.status
    this.response = route.response
    this.headers = route.headers
  }
}

export type OperationContext = 'control' | 'install' | 'attempt'

export interface OperationContextIdentityBase {
  schema: 'operation-context-identity/v1'
  protocol_version: '1'
  generation_id: Id
  plugin_release_id: Hash
}

export interface ControlOperationContextIdentity extends OperationContextIdentityBase { context: 'control' }
export interface InstallOperationContextIdentity extends OperationContextIdentityBase { context: 'install'; install_operation_id: Id }
export interface AttemptOperationContextIdentity extends OperationContextIdentityBase { context: 'attempt'; job_id: Id; step_id: Id; attempt_id: Id }
export type OperationContextIdentity = ControlOperationContextIdentity | InstallOperationContextIdentity | AttemptOperationContextIdentity
