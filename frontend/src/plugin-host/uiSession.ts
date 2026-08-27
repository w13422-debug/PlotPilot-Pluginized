import { canonicalJson } from '../contracts/canonical.ts'
import { installPluginUiTree, isPluginUiSlot, type InstalledTree, type PluginUiTreeV1, type PluginUiSlot } from './slotHost.ts'

export interface UiFreshness {
  generation_id: string
  plugin_release_id: string
  workspace_id: string | null
  workspace_revision_id: string | null
  plan_revision_id: string | null
}

export interface PluginUiIntentV1 {
  schema: 'plugin-ui-intent/v1'
  intent_id: string
  intent_seq: number
  render_seq: number
  action_id: string
  event_type: 'change' | 'click' | 'select_row' | 'change_tab' | 'select_node' | 'open_core_operation'
  intent_kind: 'invoke_capability' | 'open_core_operation' | 'request_candidate_preview' | 'request_job_cancel' | 'set_view_state'
  capability_id: string | null
  payload_asset_id: string | null
  operation_key: string
  freshness: UiFreshness
}

export interface PluginUiAckV1 {
  schema: 'plugin-ui-ack/v1'
  intent_id: string
  accepted: boolean
  error_code: string | null
  core_event_seq: number | null
  job_id: string | null
}

export interface UiSessionIdentity extends UiFreshness {
  uiSessionId: string
  workerInstanceId: string
  contributionId: string
  slot: PluginUiSlot
}

export type IntentDecision =
  | { kind: 'dispatch'; intent: PluginUiIntentV1 }
  | { kind: 'ack'; ack: PluginUiAckV1; duplicate: boolean }

function rejected(intentId: string, errorCode: string): PluginUiAckV1 {
  return { schema: 'plugin-ui-ack/v1', intent_id: intentId, accepted: false, error_code: errorCode, core_event_seq: null, job_id: null }
}

function sameFreshness(left: UiFreshness, right: UiFreshness): boolean {
  return left.generation_id === right.generation_id
    && left.plugin_release_id === right.plugin_release_id
    && left.workspace_id === right.workspace_id
    && left.workspace_revision_id === right.workspace_revision_id
    && left.plan_revision_id === right.plan_revision_id
}

function treeAllowsAction(tree: PluginUiTreeV1, actionId: string, eventType: string): boolean {
  const visit = (node: PluginUiTreeV1['root']): boolean =>
    (node.event_ids.includes(actionId) && node.event_ids.includes(eventType))
    || node.children.some(visit)
  return visit(tree.root)
}

export class PluginUiSession {
  readonly identity: UiSessionIdentity
  private installedTree: InstalledTree | null = null
  private lastIntentSeq = 0
  private readonly intentPayloads = new Map<string, string>()
  private readonly intentAcks = new Map<string, PluginUiAckV1>()

  constructor(identity: UiSessionIdentity) {
    if (!isPluginUiSlot(identity.slot)) throw new Error('unknown_slot')
    this.identity = identity
  }

  installTree(tree: PluginUiTreeV1): InstalledTree {
    this.installedTree = installPluginUiTree(this.installedTree, tree)
    return this.installedTree
  }

  decideIntent(intent: PluginUiIntentV1): IntentDecision {
    const payload = canonicalJson(intent)
    const priorPayload = this.intentPayloads.get(intent.intent_id)
    if (priorPayload !== undefined) {
      if (priorPayload !== payload) return { kind: 'ack', ack: rejected(intent.intent_id, '1008'), duplicate: true }
      const ack = this.intentAcks.get(intent.intent_id)
      if (!ack) return { kind: 'ack', ack: rejected(intent.intent_id, '1008'), duplicate: true }
      return { kind: 'ack', ack, duplicate: true }
    }

    if (!this.installedTree) return { kind: 'ack', ack: rejected(intent.intent_id, '1010'), duplicate: false }
    if (intent.render_seq !== this.installedTree.renderSeq) return { kind: 'ack', ack: rejected(intent.intent_id, '1010'), duplicate: false }
    if (!sameFreshness(intent.freshness, this.identity)) return { kind: 'ack', ack: rejected(intent.intent_id, '1010'), duplicate: false }
    if (intent.intent_seq <= this.lastIntentSeq) return { kind: 'ack', ack: rejected(intent.intent_id, '1010'), duplicate: false }
    if (!treeAllowsAction(this.installedTree.tree, intent.action_id, intent.event_type)) {
      return { kind: 'ack', ack: rejected(intent.intent_id, '1011'), duplicate: false }
    }

    this.intentPayloads.set(intent.intent_id, payload)
    this.lastIntentSeq = intent.intent_seq
    return { kind: 'dispatch', intent }
  }

  recordAck(intent: PluginUiIntentV1, ack: PluginUiAckV1): void {
    if (ack.intent_id !== intent.intent_id) throw new Error('ack_intent_mismatch')
    if (!this.intentPayloads.has(intent.intent_id)) throw new Error('intent_not_dispatched')
    this.intentAcks.set(intent.intent_id, ack)
  }
}
