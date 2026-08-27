export class PluginUiValidatorUnavailableError extends Error {
  constructor(family: 'tree' | 'intent' | 'ack') { super(`p0_plugin_ui_${family}_validator_unavailable`); this.name = 'PluginUiValidatorUnavailableError' }
}
function rejectUnsafePrototype(value: unknown): void {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('wire_value_must_be_plain_object')
  const prototype = Object.getPrototypeOf(value)
  if (prototype !== Object.prototype && prototype !== null) throw new Error('wire_value_prototype_rejected')
  if (!Object.hasOwn(value, 'schema')) throw new Error('wire_value_missing_own_schema')
}
/** All wire ingress remains stopped until P0 publishes executable closed validators. */
export function ingestPluginUiTree(value: unknown): never { rejectUnsafePrototype(value); throw new PluginUiValidatorUnavailableError('tree') }
export function ingestPluginUiIntent(value: unknown): never { rejectUnsafePrototype(value); throw new PluginUiValidatorUnavailableError('intent') }
export function ingestPluginUiAck(value: unknown): never { rejectUnsafePrototype(value); throw new PluginUiValidatorUnavailableError('ack') }
