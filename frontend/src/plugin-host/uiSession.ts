import { canonicalJson } from '../contracts/canonical.ts'
import { parsePluginUiTreeV1 } from '../contracts/ingress.ts'
import type { PluginUIAck, PluginUIIntent, PluginUITree } from '../contracts/types.ts'
import { isPluginUiSlot, PLUGIN_UI_COMPONENT_RULES, toValidatedHostTree, type PluginUiComponent, type PluginUiSlot, type ValidatedHostTree } from './slotHost.ts'
import type { ValidatedPluginUiEventBody } from './wireIngress.ts'

export interface UiFreshnessFence { generation_id: string; plugin_release_id: string; workspace_id: string | null; workspace_revision_id: string | null; plan_revision_id: string | null }
export interface UiSessionIdentity extends UiFreshnessFence { uiSessionId: string; workerInstanceId: string; contributionId: string; slot: PluginUiSlot }
export interface UiSessionInitialHighWater { initialRenderSeq: number; initialIntentSeq: number }
export interface PluginUiHostEventInput {
  eventId: string
  eventSeq: number
  actionId: string
  eventType: PluginUIIntent['event_type']
  payloadAssetId: string | null
}
/** Host ledger value, not the plugin-ui-ack/v1 wire DTO. */
export interface RecordedIntentAck { intentId: string; accepted: boolean; errorCode: string | null; coreEventSeq: number | null; jobId: string | null }
export type IntentDecision =
  | { kind: 'dispatch'; intent: Readonly<PluginUIIntent> }
  | { kind: 'pending'; intentId: string; duplicate: true }
  | { kind: 'ack'; ack: Readonly<RecordedIntentAck>; duplicate: boolean }

interface IntentLedgerEntry {
  readonly payload: string
  readonly intent: Readonly<PluginUIIntent>
  ack: Readonly<RecordedIntentAck> | null
}

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

function highWater(value: number | undefined, label: string): number {
  const normalized = value ?? 0
  if (!Number.isSafeInteger(normalized) || normalized < 0) throw new Error(`${label}_invalid`)
  return normalized
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
  private lastRenderSeq: number
  private lastIntentSeq: number
  private readonly intentLedger = new Map<string, IntentLedgerEntry>()

  constructor(identity: UiSessionIdentity, initial: Partial<UiSessionInitialHighWater> = {}) {
    if (!isPluginUiSlot(identity.slot)) throw new Error('unknown_slot')
    this.identity = deepCloneFreeze(identity)
    this.lastRenderSeq = highWater(initial.initialRenderSeq, 'ui_session_initial_render_seq')
    this.lastIntentSeq = highWater(initial.initialIntentSeq, 'ui_session_initial_intent_seq')
  }

  get renderHighWater(): number {
    return this.lastRenderSeq
  }

  get intentHighWater(): number {
    return this.lastIntentSeq
  }

  get currentRenderSeq(): number | null {
    return this.installedTree?.renderSeq ?? null
  }

  installValidatedTree(tree: ValidatedHostTree | Readonly<PluginUITree>): Readonly<ValidatedHostTree> {
    const normalized = Object.hasOwn(tree, 'tree_id')
      ? toValidatedHostTree(parsePluginUiTreeV1(tree as PluginUITree))
      : tree as ValidatedHostTree
    if (normalized.renderSeq <= this.lastRenderSeq) throw new Error('stale_render_seq')
    this.installedTree = deepCloneFreeze(normalized)
    this.lastRenderSeq = normalized.renderSeq
    return this.installedTree
  }

  createHostEvent(input: PluginUiHostEventInput): Readonly<ValidatedPluginUiEventBody> {
    const tree = this.requireInstalledTree()
    return this.validateHostEvent({
      schema: 'plugin-ui-event/v1',
      event_id: input.eventId,
      event_seq: input.eventSeq,
      render_seq: tree.renderSeq,
      action_id: input.actionId,
      event_type: input.eventType,
      payload_asset_id: input.payloadAssetId,
      freshness: {
        generation_id: this.identity.generation_id,
        plugin_release_id: this.identity.plugin_release_id,
        workspace_id: this.identity.workspace_id,
        workspace_revision_id: this.identity.workspace_revision_id,
        plan_revision_id: this.identity.plan_revision_id,
      },
    })
  }

  validateHostEvent(body: Readonly<ValidatedPluginUiEventBody>): Readonly<ValidatedPluginUiEventBody> {
    const tree = this.requireInstalledTree()
    if (body.render_seq !== tree.renderSeq) throw new Error('host_event_render_seq_stale')
    if (!sameFreshness(body.freshness, this.identity)) throw new Error('host_event_freshness_mismatch')
    if (!treeAllowsAction(tree, body.action_id, body.event_type)) throw new Error('host_event_action_not_declared')
    return deepCloneFreeze(body)
  }

  decideValidatedIntent(input: PluginUIIntent): IntentDecision {
    const intent = deepCloneFreeze(input)
    // Freshness/render fencing always precedes duplicate replay.
    if (!this.installedTree || intent.render_seq !== this.installedTree.renderSeq || !sameFreshness(intent.freshness, this.identity)) {
      return { kind: 'ack', ack: rejected(intent.intent_id, '1010'), duplicate: false }
    }
    const payload = canonicalJson(intent)
    const prior = this.intentLedger.get(intent.intent_id)
    if (prior !== undefined) {
      if (prior.payload !== payload) return { kind: 'ack', ack: rejected(intent.intent_id, '1008'), duplicate: true }
      if (prior.ack === null) return { kind: 'pending', intentId: intent.intent_id, duplicate: true }
      return { kind: 'ack', ack: prior.ack, duplicate: true }
    }
    if (intent.intent_seq <= this.lastIntentSeq) return { kind: 'ack', ack: rejected(intent.intent_id, '1010'), duplicate: false }
    if (!treeAllowsAction(this.installedTree, intent.action_id, intent.event_type)) {
      return { kind: 'ack', ack: rejected(intent.intent_id, '1011'), duplicate: false }
    }
    const entry: IntentLedgerEntry = { payload, intent, ack: null }
    this.intentLedger.set(intent.intent_id, entry)
    this.lastIntentSeq = intent.intent_seq
    return { kind: 'dispatch', intent: entry.intent }
  }

  isIntentPending(intentId: string): boolean {
    return this.intentLedger.get(intentId)?.ack === null
  }

  completeValidatedIntent(
    intent: PluginUIIntent,
    ack: RecordedIntentAck | PluginUIAck,
  ): Readonly<RecordedIntentAck> {
    const recorded = Object.hasOwn(ack, 'intent_id')
      ? toRecordedIntentAck(ack as PluginUIAck)
      : deepCloneFreeze(ack as RecordedIntentAck)
    if (recorded.intentId !== intent.intent_id) throw new Error('ack_intent_mismatch')
    const entry = this.intentLedger.get(intent.intent_id)
    if (entry === undefined || entry.payload !== canonicalJson(intent)) throw new Error('intent_not_dispatched')
    if (entry.ack !== null) {
      if (canonicalJson(entry.ack) !== canonicalJson(recorded)) throw new Error('intent_ack_already_completed')
      return entry.ack
    }
    entry.ack = recorded
    return recorded
  }

  recordValidatedAck(intent: PluginUIIntent, ack: RecordedIntentAck | PluginUIAck): Readonly<RecordedIntentAck> {
    return this.completeValidatedIntent(intent, ack)
  }

  private requireInstalledTree(): Readonly<ValidatedHostTree> {
    if (this.installedTree === null) throw new Error('host_event_tree_not_installed')
    return this.installedTree
  }
}
