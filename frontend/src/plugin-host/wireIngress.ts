import pluginUiDisposeSchema from '../../../contracts/json-schema/plugin-ui-dispose-v1.schema.json' with { type: 'json' }
import pluginUiErrorSchema from '../../../contracts/json-schema/plugin-ui-error-v1.schema.json' with { type: 'json' }
import pluginUiEventSchema from '../../../contracts/json-schema/plugin-ui-event-v1.schema.json' with { type: 'json' }
import pluginUiInitSchema from '../../../contracts/json-schema/plugin-ui-init-v1.schema.json' with { type: 'json' }
import pluginUiMessageSchema from '../../../contracts/json-schema/plugin-ui-message-v1.schema.json' with { type: 'json' }
import { parsePluginUiAckV1, parsePluginUiIntentV1, parsePluginUiTreeV1 } from '../contracts/ingress.ts'
import { deepCloneFreeze, parseJsonSchema, type JsonSchema } from '../contracts/schema-ingress.ts'
import type { PluginUIAck, PluginUIIntent, PluginUITree } from '../contracts/types.ts'

export type PluginUiMessageDirection = 'host_to_worker' | 'worker_to_host'
export type PluginUiMessageType = 'init' | 'render' | 'intent' | 'ack' | 'error' | 'dispose'

/** Body view for the published plugin-ui-init/v1 schema. */
export interface ValidatedPluginUiInitBody {
  schema: 'plugin-ui-init/v1'
  ui_session_id: string
  initial_render_seq: number
  initial_intent_seq: number
  contribution_config_asset_id: string | null
}

/** Body view for the published plugin-ui-event/v1 schema. */
export interface ValidatedPluginUiEventBody {
  schema: 'plugin-ui-event/v1'
  event_id: string
  event_seq: number
  render_seq: number
  action_id: string
  event_type: PluginUIIntent['event_type']
  payload_asset_id: string | null
  freshness: PluginUIIntent['freshness']
}

/** Body view for the published plugin-ui-error/v1 schema. */
export interface ValidatedPluginUiErrorBody {
  schema: 'plugin-ui-error/v1'
  code: string
  message: string
  details_asset_id: string | null
  retryable: boolean
}

/** Body view for the published plugin-ui-dispose/v1 schema. */
export interface ValidatedPluginUiDisposeBody {
  schema: 'plugin-ui-dispose/v1'
  ui_session_id: string
  reason: 'normal_shutdown' | 'slot_unmounted' | 'generation_changed' | 'workspace_changed' | 'watchdog'
  deadline_at: string
}

/**
 * Host-normalized message view.
 *
 * The P0 tree, intent and ACK DTOs remain the only wire DTOs for those
 * bodies. This type only normalizes the envelope names for Slot Host use;
 * it does not publish a second contract or re-declare a P0 DTO.
 */
export type ValidatedPluginUiMessage =
  | Readonly<{
    schema: 'plugin-ui-message/v1'
    messageId: string
    direction: 'host_to_worker'
    messageSeq: number
    messageType: 'init'
    workerInstanceId: string
    pluginReleaseId: string
    generationId: string
    contributionId: string
    slot: string
    workspaceId: string | null
    workspaceRevisionId: string | null
    planRevisionId: string | null
    body: Readonly<ValidatedPluginUiInitBody>
  }>
  | Readonly<{
    schema: 'plugin-ui-message/v1'
    messageId: string
    direction: 'host_to_worker'
    messageSeq: number
    messageType: 'intent'
    workerInstanceId: string
    pluginReleaseId: string
    generationId: string
    contributionId: string
    slot: string
    workspaceId: string | null
    workspaceRevisionId: string | null
    planRevisionId: string | null
    body: Readonly<ValidatedPluginUiEventBody>
  }>
  | Readonly<{
    schema: 'plugin-ui-message/v1'
    messageId: string
    direction: 'host_to_worker'
    messageSeq: number
    messageType: 'ack'
    workerInstanceId: string
    pluginReleaseId: string
    generationId: string
    contributionId: string
    slot: string
    workspaceId: string | null
    workspaceRevisionId: string | null
    planRevisionId: string | null
    body: Readonly<PluginUIAck>
  }>
  | Readonly<{
    schema: 'plugin-ui-message/v1'
    messageId: string
    direction: 'host_to_worker'
    messageSeq: number
    messageType: 'error'
    workerInstanceId: string
    pluginReleaseId: string
    generationId: string
    contributionId: string
    slot: string
    workspaceId: string | null
    workspaceRevisionId: string | null
    planRevisionId: string | null
    body: Readonly<ValidatedPluginUiErrorBody>
  }>
  | Readonly<{
    schema: 'plugin-ui-message/v1'
    messageId: string
    direction: 'host_to_worker'
    messageSeq: number
    messageType: 'dispose'
    workerInstanceId: string
    pluginReleaseId: string
    generationId: string
    contributionId: string
    slot: string
    workspaceId: string | null
    workspaceRevisionId: string | null
    planRevisionId: string | null
    body: Readonly<ValidatedPluginUiDisposeBody>
  }>
  | Readonly<{
    schema: 'plugin-ui-message/v1'
    messageId: string
    direction: 'worker_to_host'
    messageSeq: number
    messageType: 'render'
    workerInstanceId: string
    pluginReleaseId: string
    generationId: string
    contributionId: string
    slot: string
    workspaceId: string | null
    workspaceRevisionId: string | null
    planRevisionId: string | null
    body: Readonly<PluginUITree>
  }>
  | Readonly<{
    schema: 'plugin-ui-message/v1'
    messageId: string
    direction: 'worker_to_host'
    messageSeq: number
    messageType: 'intent'
    workerInstanceId: string
    pluginReleaseId: string
    generationId: string
    contributionId: string
    slot: string
    workspaceId: string | null
    workspaceRevisionId: string | null
    planRevisionId: string | null
    body: Readonly<PluginUIIntent>
  }>
  | Readonly<{
    schema: 'plugin-ui-message/v1'
    messageId: string
    direction: 'worker_to_host'
    messageSeq: number
    messageType: 'error'
    workerInstanceId: string
    pluginReleaseId: string
    generationId: string
    contributionId: string
    slot: string
    workspaceId: string | null
    workspaceRevisionId: string | null
    planRevisionId: string | null
    body: Readonly<ValidatedPluginUiErrorBody>
  }>

interface ParsedPluginUiMessage {
  schema: 'plugin-ui-message/v1'
  message_id: string
  direction: PluginUiMessageDirection
  message_seq: number
  message_type: PluginUiMessageType
  worker_instance_id: string
  plugin_release_id: string
  generation_id: string
  contribution_id: string
  slot: string
  workspace_id: string | null
  workspace_revision_id: string | null
  plan_revision_id: string | null
  body: unknown
}

export class PluginUiValidatorUnavailableError extends Error {
  constructor(family: 'tree' | 'intent' | 'ack') {
    super(`p0_plugin_ui_${family}_validator_unavailable`)
    this.name = 'PluginUiValidatorUnavailableError'
  }
}

function rejectUnsafePrototype(value: unknown): asserts value is Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('wire_value_must_be_plain_object')
  const prototype = Object.getPrototypeOf(value)
  if (prototype !== Object.prototype && prototype !== null) throw new Error('wire_value_prototype_rejected')
  if (!Object.hasOwn(value, 'schema')) throw new Error('wire_value_missing_own_schema')
}

function isLegacyPlaceholder(value: Record<string, unknown>): boolean {
  const descriptor = Object.getOwnPropertyDescriptor(value, 'schema')
  return descriptor !== undefined && 'value' in descriptor && descriptor.value === 'placeholder'
}

/** Parse the P0-owned tree DTO; no host-local tree contract is introduced. */
export function ingestPluginUiTree(value: unknown): Readonly<PluginUITree> {
  rejectUnsafePrototype(value)
  if (isLegacyPlaceholder(value)) throw new PluginUiValidatorUnavailableError('tree')
  return parsePluginUiTreeV1(value)
}

/** Parse the P0-owned intent DTO; no host-local intent contract is introduced. */
export function ingestPluginUiIntent(value: unknown): Readonly<PluginUIIntent> {
  rejectUnsafePrototype(value)
  if (isLegacyPlaceholder(value)) throw new PluginUiValidatorUnavailableError('intent')
  return parsePluginUiIntentV1(value)
}

/** Parse the P0-owned ACK DTO; no host-local ACK contract is introduced. */
export function ingestPluginUiAck(value: unknown): Readonly<PluginUIAck> {
  rejectUnsafePrototype(value)
  if (isLegacyPlaceholder(value)) throw new PluginUiValidatorUnavailableError('ack')
  return parsePluginUiAckV1(value)
}

function normalizedEnvelopeFields(value: ParsedPluginUiMessage): Readonly<{
  schema: 'plugin-ui-message/v1'
  messageId: string
  workerInstanceId: string
  pluginReleaseId: string
  generationId: string
  contributionId: string
  slot: string
  workspaceId: string | null
  workspaceRevisionId: string | null
  planRevisionId: string | null
  messageSeq: number
}> {
  return {
    schema: value.schema,
    messageId: value.message_id,
    workerInstanceId: value.worker_instance_id,
    pluginReleaseId: value.plugin_release_id,
    generationId: value.generation_id,
    contributionId: value.contribution_id,
    slot: value.slot,
    workspaceId: value.workspace_id,
    workspaceRevisionId: value.workspace_revision_id,
    planRevisionId: value.plan_revision_id,
    messageSeq: value.message_seq,
  }
}

function normalizeMessage(
  value: ParsedPluginUiMessage,
  direction: PluginUiMessageDirection,
  messageType: PluginUiMessageType,
  body: unknown,
): ValidatedPluginUiMessage {
  return deepCloneFreeze({
    ...normalizedEnvelopeFields(value),
    direction,
    messageType,
    body,
  }) as ValidatedPluginUiMessage
}

/**
 * Validate the complete published plugin-ui-message/v1 envelope and return a
 * host-local normalized view. The tree, intent and ACK branches are parsed a
 * second time through the P0 ingress functions so their semantic checks and
 * DTO ownership remain authoritative.
 */
export function ingestPluginUiMessage(value: unknown): ValidatedPluginUiMessage {
  rejectUnsafePrototype(value)
  const parsed = parseJsonSchema<ParsedPluginUiMessage>(
    value,
    pluginUiMessageSchema as JsonSchema,
    'plugin-ui-message/v1',
  )

  switch (`${parsed.direction}:${parsed.message_type}`) {
    case 'host_to_worker:init':
      return normalizeMessage(
        parsed,
        'host_to_worker',
        'init',
        parseJsonSchema<ValidatedPluginUiInitBody>(parsed.body, pluginUiInitSchema as JsonSchema, 'plugin-ui-init/v1'),
      )
    case 'host_to_worker:intent':
      return normalizeMessage(
        parsed,
        'host_to_worker',
        'intent',
        parseJsonSchema<ValidatedPluginUiEventBody>(parsed.body, pluginUiEventSchema as JsonSchema, 'plugin-ui-event/v1'),
      )
    case 'host_to_worker:ack':
      return normalizeMessage(parsed, 'host_to_worker', 'ack', ingestPluginUiAck(parsed.body))
    case 'host_to_worker:error':
      return normalizeMessage(
        parsed,
        'host_to_worker',
        'error',
        parseJsonSchema<ValidatedPluginUiErrorBody>(parsed.body, pluginUiErrorSchema as JsonSchema, 'plugin-ui-error/v1'),
      )
    case 'host_to_worker:dispose':
      return normalizeMessage(
        parsed,
        'host_to_worker',
        'dispose',
        parseJsonSchema<ValidatedPluginUiDisposeBody>(parsed.body, pluginUiDisposeSchema as JsonSchema, 'plugin-ui-dispose/v1'),
      )
    case 'worker_to_host:render':
      return normalizeMessage(parsed, 'worker_to_host', 'render', ingestPluginUiTree(parsed.body))
    case 'worker_to_host:intent':
      return normalizeMessage(parsed, 'worker_to_host', 'intent', ingestPluginUiIntent(parsed.body))
    case 'worker_to_host:error':
      return normalizeMessage(
        parsed,
        'worker_to_host',
        'error',
        parseJsonSchema<ValidatedPluginUiErrorBody>(parsed.body, pluginUiErrorSchema as JsonSchema, 'plugin-ui-error/v1'),
      )
    default:
      throw new Error('plugin_ui_message_direction_type_rejected')
  }
}
