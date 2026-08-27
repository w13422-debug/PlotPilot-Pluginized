import jobSnapshotSchema from '../../../contracts/json-schema/job-snapshot-v1.schema.json' with { type: 'json' }
import pluginUiTreeSchema from '../../../contracts/json-schema/plugin-ui-tree-v1.schema.json' with { type: 'json' }
import pluginUiIntentSchema from '../../../contracts/json-schema/plugin-ui-intent-v1.schema.json' with { type: 'json' }
import pluginUiAckSchema from '../../../contracts/json-schema/plugin-ui-ack-v1.schema.json' with { type: 'json' }
import { parseJsonSchema, type JsonSchema } from './schema-ingress.ts'
import { verifyJobSnapshot } from './verifier.ts'
import type { JobSnapshot, PluginUIAck, PluginUIComponent, PluginUIIntent, PluginUITree } from './types.ts'

const EVENT_ALLOWLIST: Readonly<Record<PluginUIComponent, readonly string[]>> = {
  stack: [],
  text: [],
  input: ['change'],
  textarea: ['change'],
  select: ['change'],
  button: ['click'],
  table: ['select_row'],
  tabs: ['change_tab'],
  diff: [],
  tree: ['select_node'],
  graph: ['select_node'],
  progress: [],
  candidate_preview: ['open_core_operation'],
}

function assertTreeEventAllowlist(node: PluginUITree['root'], path = '$.root'): void {
  const allowed = EVENT_ALLOWLIST[node.component]
  const events = node.event_ids
  if (allowed.length === 0 && events.length !== 0) {
    throw new Error(`plugin-ui-tree component ${node.component} cannot declare events at ${path}`)
  }
  if (events.some(event => !allowed.includes(event))) {
    throw new Error(`plugin-ui-tree event is not allowed for ${node.component} at ${path}.event_ids`)
  }
  node.children.forEach((child, index) => assertTreeEventAllowlist(child, `${path}.children[${index}]`))
}

/** Parse and semantically verify the job snapshot; the verifier is intentionally not bypassed. */
export async function parseJobSnapshotV1(value: unknown): Promise<Readonly<JobSnapshot>> {
  const parsed = parseJsonSchema<JobSnapshot>(value, jobSnapshotSchema as JsonSchema, 'job-snapshot/v1')
  await verifyJobSnapshot(parsed as unknown as Record<string, unknown>)
  return parsed
}

export function parsePluginUiTreeV1(value: unknown): Readonly<PluginUITree> {
  const parsed = parseJsonSchema<PluginUITree>(value, pluginUiTreeSchema as JsonSchema, 'plugin-ui-tree/v1')
  assertTreeEventAllowlist(parsed.root)
  return parsed
}

export function parsePluginUiIntentV1(value: unknown): Readonly<PluginUIIntent> {
  return parseJsonSchema<PluginUIIntent>(value, pluginUiIntentSchema as JsonSchema, 'plugin-ui-intent/v1')
}

export function parsePluginUiAckV1(value: unknown): Readonly<PluginUIAck> {
  return parseJsonSchema<PluginUIAck>(value, pluginUiAckSchema as JsonSchema, 'plugin-ui-ack/v1')
}

export const verifyJobSnapshotV1 = parseJobSnapshotV1
export const verifyPluginUiTreeV1 = parsePluginUiTreeV1
export const verifyPluginUiIntentV1 = parsePluginUiIntentV1
export const verifyPluginUiAckV1 = parsePluginUiAckV1
