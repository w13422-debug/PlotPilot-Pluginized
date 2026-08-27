import { canonicalJson } from '../contracts/canonical.ts'
import { parsePluginUiTreeV1 } from '../contracts/ingress.ts'
import type { PluginUIAck, PluginUIIntent, PluginUITree } from '../contracts/types.ts'
import { isPluginUiSlot, PLUGIN_UI_COMPONENT_RULES, toValidatedHostTree, type PluginUiComponent, type PluginUiSlot, type ValidatedHostTree } from './slotHost.ts'

export interface UiFreshnessFence { generation_id: string; plugin_release_id: string; workspace_id: string | null; workspace_revision_id: string | null; plan_revision_id: string | null }
export interface UiSessionIdentity extends UiFreshnessFence { uiSessionId: string; workerInstanceId: string; contributionId: string; slot: PluginUiSlot }
/** Host ledger value, not the plugin-ui-ack/v1 wire DTO. */
export interface RecordedIntentAck { intentId: string; accepted: boolean; errorCode: string | null; coreEventSeq: number | null; jobId: string | null }
export type IntentDecision = { kind: 'dispatch'; intent: Readonly<PluginUIIntent> } | { kind: 'ack'; ack: Readonly<RecordedIntentAck>; duplicate: boolean }

function deepCloneFreeze<T>(value: T): Readonly<T> {
  const clone = structuredClone(value)
  const freeze = (item: unknown): void => {
    if (item === null || typeof item !== 'object' || Object.isFrozen(item)) return
    for (const key of Object.keys(item)) freeze((item as Record<string, unknown>)[key])
    Object.freeze(item)
  }
  freeze(clone)
  return clone
}
function rejected(intentId: string, errorCode: string): Readonly<RecordedIntentAck> {
  return deepCloneFreeze({ intentId, accepted: false, errorCode, coreEventSeq: null, jobId: null })
}

export function toRecordedIntentAck(ack: Readonly<PluginUIAck>): Readonly<RecordedIntentAck> {
  return deepCloneFreeze({
    intentId: ack.intent_id,
    accepted: ack.accepted,
    errorCode: ack.error_code,
    coreEventSeq: ack.core_event_seq,
    jobId: ack.job_id,
  })
}

export function toPluginUiAck(ack: Readonly<RecordedIntentAck>): Readonly<PluginUIAck> {
  return deepCloneFreeze({
    schema: 'plugin-ui-ack/v1',
    intent_id: ack.intentId,
    accepted: ack.accepted,
    error_code: ack.errorCode,
    core_event_seq: ack.coreEventSeq,
    job_id: ack.jobId,
  })
}
function sameFreshness(left: PluginUIIntent['freshness'], right: UiFreshnessFence): boolean {
  return left.generation_id === right.generation_id && left.plugin_release_id === right.plugin_release_id
    && left.workspace_id === right.workspace_id && left.workspace_revision_id === right.workspace_revision_id
    && left.plan_revision_id === right.plan_revision_id
}
function treeAllowsAction(tree: ValidatedHostTree, actionId: string, eventType: string): boolean {
  const visit = (node: ValidatedHostTree['root']): boolean =>
    (node.event_ids.includes(actionId)
      && (PLUGIN_UI_COMPONENT_RULES[node.component as PluginUiComponent]?.events as readonly string[] | undefined)?.includes(eventType) === true)
      || node.children.some(visit)
  return visit(tree.root)
}

/** Accepts only values already validated by P0 authority; unknown ingress is stopped in wireIngress.ts. */
export class PluginUiSession {
  readonly identity: Readonly<UiSessionIdentity>
  private installedTree: Readonly<ValidatedHostTree> | null = null
  private lastIntentSeq = 0
  private readonly intentPayloads = new Map<string, string>()
  private readonly intentAcks = new Map<string, Readonly<RecordedIntentAck>>()
  constructor(identity: UiSessionIdentity) {
    if (!isPluginUiSlot(identity.slot)) throw new Error('unknown_slot')
    this.identity = deepCloneFreeze(identity)
  }
  installValidatedTree(tree: ValidatedHostTree | Readonly<PluginUITree>): Readonly<ValidatedHostTree> {
    const normalized = Object.hasOwn(tree, 'tree_id')
      ? toValidatedHostTree(parsePluginUiTreeV1(tree as PluginUITree))
      : tree as ValidatedHostTree
    if (this.installedTree && normalized.renderSeq <= this.installedTree.renderSeq) throw new Error('stale_render_seq')
    this.installedTree = deepCloneFreeze(normalized)
    return deepCloneFreeze(this.installedTree)
  }
  decideValidatedIntent(input: PluginUIIntent): IntentDecision {
    const intent = deepCloneFreeze(input)
    // Freshness/render fencing always precedes duplicate replay.
    if (!this.installedTree || intent.render_seq !== this.installedTree.renderSeq || !sameFreshness(intent.freshness, this.identity)) {
      return { kind: 'ack', ack: rejected(intent.intent_id, '1010'), duplicate: false }
    }
    const payload = canonicalJson(intent)
    const priorPayload = this.intentPayloads.get(intent.intent_id)
    if (priorPayload !== undefined) {
      if (priorPayload !== payload) return { kind: 'ack', ack: rejected(intent.intent_id, '1008'), duplicate: true }
      const ack = this.intentAcks.get(intent.intent_id)
      return { kind: 'ack', ack: ack ? deepCloneFreeze(ack) : rejected(intent.intent_id, '1008'), duplicate: true }
    }
    if (intent.intent_seq <= this.lastIntentSeq) return { kind: 'ack', ack: rejected(intent.intent_id, '1010'), duplicate: false }
    if (!treeAllowsAction(this.installedTree as ValidatedHostTree, intent.action_id, intent.event_type)) {
      return { kind: 'ack', ack: rejected(intent.intent_id, '1011'), duplicate: false }
    }
    this.intentPayloads.set(intent.intent_id, payload)
    this.lastIntentSeq = intent.intent_seq
    return { kind: 'dispatch', intent: deepCloneFreeze(intent) }
  }
  recordValidatedAck(intent: PluginUIIntent, ack: RecordedIntentAck | PluginUIAck): void {
    const recorded = Object.hasOwn(ack, 'intent_id')
      ? toRecordedIntentAck(ack as PluginUIAck)
      : deepCloneFreeze(ack as RecordedIntentAck)
    if (recorded.intentId !== intent.intent_id) throw new Error('ack_intent_mismatch')
    if (!this.intentPayloads.has(intent.intent_id)) throw new Error('intent_not_dispatched')
    this.intentAcks.set(intent.intent_id, recorded)
  }
}
