/**
 * A deliberately small, fail-closed Draft 2020-12 JSON-Schema reader.
 *
 * This module is used at trust boundaries.  It does not coerce values, strip
 * fields, or retain references to the caller's object graph.  The supported
 * keywords are the ones used by the PlotPilot contracts; annotations are
 * ignored and unsupported assertion keywords are not silently interpreted.
 */

export type JsonSchema = boolean | Readonly<Record<string, unknown>>

type JsonRecord = Record<string, unknown>
type JsonContainer = JsonRecord | unknown[]

export class SchemaIngressError extends TypeError {
  readonly path: string

  constructor(message: string, path = '$') {
    super(`${message} at ${path}`)
    this.name = 'SchemaIngressError'
    this.path = path
  }
}

interface ValidationContext {
  readonly root: JsonSchema
  readonly active: WeakSet<object>
}

const hasOwn = (value: object, key: string): boolean =>
  Object.prototype.hasOwnProperty.call(value, key)

function schemaObject(value: JsonSchema, path: string): Readonly<Record<string, unknown>> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new SchemaIngressError('schema node must be an object or boolean', path)
  }
  return value
}

function fail(message: string, path: string): never {
  throw new SchemaIngressError(message, path)
}

function isPlainObject(value: unknown): value is JsonRecord {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

function assertDataContainer(value: unknown, path: string): asserts value is JsonContainer {
  if (Array.isArray(value)) {
    const prototype = Object.getPrototypeOf(value)
    if (prototype !== Array.prototype && prototype !== null) fail('array prototype must be the native or null prototype', path)
    if (Object.getOwnPropertySymbols(value).length !== 0) fail('JSON arrays cannot contain symbol properties', path)

    for (const key of Object.getOwnPropertyNames(value)) {
      if (key === 'length') {
        const descriptor = Object.getOwnPropertyDescriptor(value, key)
        if (descriptor == null || !('value' in descriptor) || descriptor.get !== undefined || descriptor.set !== undefined) {
          fail('array length must be a data property', path)
        }
        continue
      }
      const index = Number(key)
      if (!Number.isSafeInteger(index) || index < 0 || String(index) !== key || index >= value.length) {
        fail(`array has a non-index own property ${JSON.stringify(key)}`, path)
      }
      const descriptor = Object.getOwnPropertyDescriptor(value, key)
      if (descriptor == null || !('value' in descriptor) || descriptor.get !== undefined || descriptor.set !== undefined || !descriptor.enumerable) {
        fail(`array item ${key} must be an enumerable data property`, `${path}[${key}]`)
      }
    }
    for (let index = 0; index < value.length; index += 1) {
      const descriptor = Object.getOwnPropertyDescriptor(value, String(index))
      if (descriptor == null || !('value' in descriptor)) fail('sparse arrays are not JSON values', `${path}[${index}]`)
    }
    return
  }

  if (!isPlainObject(value)) fail('object must have Object.prototype or null prototype', path)
  if (Object.getOwnPropertySymbols(value).length !== 0) fail('JSON objects cannot contain symbol properties', path)
  for (const key of Object.getOwnPropertyNames(value)) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key)
    if (descriptor == null || !('value' in descriptor) || descriptor.get !== undefined || descriptor.set !== undefined || !descriptor.enumerable) {
      fail(`property ${JSON.stringify(key)} must be an enumerable data property`, `${path}.${key}`)
    }
  }
}

function typeMatches(value: unknown, type: string): boolean {
  switch (type) {
    case 'null': return value === null
    case 'boolean': return typeof value === 'boolean'
    case 'object': return isPlainObject(value)
    case 'array': return Array.isArray(value)
    case 'string': return typeof value === 'string'
    case 'number': return typeof value === 'number' && Number.isFinite(value)
    case 'integer': return typeof value === 'number' && Number.isSafeInteger(value)
    default: return false
  }
}

function assertType(value: unknown, schema: Readonly<Record<string, unknown>>, path: string): void {
  const declared = schema.type
  if (declared === undefined) return
  const types = typeof declared === 'string' ? [declared] : Array.isArray(declared) ? declared : fail('schema type must be a string or string array', path)
  if (types.length === 0 || types.some(type => typeof type !== 'string')) fail('schema type array must contain strings', path)
  if (!(types as string[]).some(type => typeMatches(value, type))) fail(`expected type ${types.join('|')}`, path)
}

function resolveReference(root: JsonSchema, reference: string, path: string): JsonSchema {
  if (!reference.startsWith('#/')) fail(`only local JSON-Schema references are supported: ${reference}`, path)
  let current: unknown = root
  for (const rawPart of reference.slice(2).split('/')) {
    const part = rawPart.replace(/~1/g, '/').replace(/~0/g, '~')
    if (current === null || typeof current !== 'object' || !hasOwn(current, part)) fail(`unresolved JSON-Schema reference ${reference}`, path)
    current = (current as JsonRecord)[part]
  }
  if (typeof current !== 'boolean' && (current === null || typeof current !== 'object' || Array.isArray(current))) {
    fail(`referenced schema ${reference} is not a schema node`, path)
  }
  return current as JsonSchema
}

function jsonEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true
  if (typeof left !== typeof right || left === null || right === null) return false
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false
    return left.every((item, index) => jsonEqual(item, right[index]))
  }
  if (typeof left !== 'object' || typeof right !== 'object') return false
  const leftObject = left as JsonRecord
  const rightObject = right as JsonRecord
  const leftKeys = Object.keys(leftObject).sort()
  const rightKeys = Object.keys(rightObject).sort()
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key, index) => key === rightKeys[index] && jsonEqual(leftObject[key], rightObject[key]))
}

function structuralKey(value: unknown): string {
  if (value === null) return 'null'
  if (typeof value === 'string') return `s:${JSON.stringify(value)}`
  if (typeof value === 'boolean') return value ? 'b:1' : 'b:0'
  if (typeof value === 'number') return `n:${Object.is(value, -0) ? '0' : String(value)}`
  if (Array.isArray(value)) return `[${value.map(structuralKey).join(',')}]`
  const record = value as JsonRecord
  return `{${Object.keys(record).sort().map(key => `${JSON.stringify(key)}:${structuralKey(record[key])}`).join(',')}}`
}

function cloneWithoutSchema(value: unknown, path: string, active = new WeakSet<object>()): unknown {
  if (value === null || typeof value !== 'object') return value
  assertDataContainer(value, path)
  if (active.has(value)) fail('cyclic input is not a JSON value', path)
  active.add(value)
  try {
    if (Array.isArray(value)) {
      const output: unknown[] = []
      for (let index = 0; index < value.length; index += 1) output.push(cloneWithoutSchema(value[index], `${path}[${index}]`, active))
      return output
    }
    const output: JsonRecord = Object.create(Object.getPrototypeOf(value) === null ? null : Object.prototype) as JsonRecord
    for (const key of Object.keys(value)) {
      Object.defineProperty(output, key, {
        configurable: true,
        enumerable: true,
        writable: true,
        value: cloneWithoutSchema(value[key], `${path}.${key}`, active),
      })
    }
    return output
  } finally {
    active.delete(value)
  }
}

function validateNode(value: unknown, schema: JsonSchema, path: string, context: ValidationContext): unknown {
  if (schema === false) fail('schema rejects this value', path)
  if (schema === true) return cloneWithoutSchema(value, path, context.active)
  const node = schemaObject(schema, path)

  if (typeof node.$ref === 'string') {
    return validateNode(value, resolveReference(context.root, node.$ref, path), path, context)
  }
  if (node.$ref !== undefined) fail('$ref must be a string', path)

  if (node.oneOf !== undefined) {
    if (!Array.isArray(node.oneOf) || node.oneOf.length === 0) fail('oneOf must be a non-empty schema array', path)
    const matches: unknown[] = []
    for (const [index, branch] of node.oneOf.entries()) {
      try {
        matches.push(validateNode(value, branch as JsonSchema, path, context))
      } catch (error) {
        if (!(error instanceof SchemaIngressError)) throw error
        void index
      }
    }
    if (matches.length !== 1) fail(`oneOf matched ${matches.length} schemas`, path)
    return matches[0]
  }
  if (node.anyOf !== undefined) {
    if (!Array.isArray(node.anyOf) || node.anyOf.length === 0) fail('anyOf must be a non-empty schema array', path)
    for (const branch of node.anyOf) {
      try {
        return validateNode(value, branch as JsonSchema, path, context)
      } catch (error) {
        if (!(error instanceof SchemaIngressError)) throw error
      }
    }
    fail('anyOf matched no schema', path)
  }

  if (value !== null && typeof value === 'object') assertDataContainer(value, path)
  if (node.const !== undefined && !jsonEqual(value, node.const)) fail('value does not equal const', path)
  if (node.enum !== undefined) {
    if (!Array.isArray(node.enum) || !node.enum.some(candidate => jsonEqual(value, candidate))) fail('value is not in enum', path)
  }
  assertType(value, node, path)

  if (typeof value === 'string') {
    if (node.pattern !== undefined) {
      if (typeof node.pattern !== 'string') fail('schema pattern must be a string', path)
      let expression: RegExp
      try {
        expression = new RegExp(node.pattern, 'u')
      } catch {
        fail('schema pattern is not a valid Unicode regular expression', path)
      }
      if (!expression.test(value)) fail('string does not match pattern', path)
    }
    const length = [...value].length
    if (node.minLength !== undefined && (typeof node.minLength !== 'number' || length < node.minLength)) fail('string is shorter than minLength', path)
    if (node.maxLength !== undefined && (typeof node.maxLength !== 'number' || length > node.maxLength)) fail('string is longer than maxLength', path)
    return value
  }

  if (typeof value === 'number') {
    if (node.minimum !== undefined && (typeof node.minimum !== 'number' || value < node.minimum)) fail('number is below minimum', path)
    if (node.maximum !== undefined && (typeof node.maximum !== 'number' || value > node.maximum)) fail('number is above maximum', path)
    return value
  }

  if (Array.isArray(value)) {
    if (node.minItems !== undefined && (typeof node.minItems !== 'number' || value.length < node.minItems)) fail('array is shorter than minItems', path)
    if (node.maxItems !== undefined && (typeof node.maxItems !== 'number' || value.length > node.maxItems)) fail('array is longer than maxItems', path)
    const itemSchema = node.items
    const output: unknown[] = []
    const active = context.active
    if (active.has(value)) fail('cyclic input is not a JSON value', path)
    active.add(value)
    try {
      if (node.items !== undefined && !Array.isArray(itemSchema)) {
        for (let index = 0; index < value.length; index += 1) output.push(validateNode(value[index], itemSchema as JsonSchema, `${path}[${index}]`, context))
      } else if (Array.isArray(itemSchema)) {
        for (let index = 0; index < value.length; index += 1) {
          const schemaAtIndex = itemSchema[index] as JsonSchema | undefined
          output.push(schemaAtIndex === undefined ? cloneWithoutSchema(value[index], `${path}[${index}]`, context.active) : validateNode(value[index], schemaAtIndex, `${path}[${index}]`, context))
        }
      } else {
        for (let index = 0; index < value.length; index += 1) output.push(cloneWithoutSchema(value[index], `${path}[${index}]`, context.active))
      }
    } finally {
      active.delete(value)
    }
    if (node.uniqueItems === true) {
      const keys = output.map(structuralKey)
      if (new Set(keys).size !== keys.length) fail('array items must be unique', path)
    } else if (node.uniqueItems !== undefined && node.uniqueItems !== false) {
      fail('uniqueItems must be boolean', path)
    }
    return output
  }

  if (isPlainObject(value)) {
    const properties = node.properties
    if (properties !== undefined && (properties === null || typeof properties !== 'object' || Array.isArray(properties))) fail('schema properties must be an object', path)
    const propertySchemas = (properties ?? {}) as Readonly<Record<string, unknown>>
    const required = node.required
    if (required !== undefined) {
      if (!Array.isArray(required) || required.some(key => typeof key !== 'string')) fail('schema required must be a string array', path)
      for (const key of required as string[]) if (!hasOwn(value, key)) fail(`required property ${JSON.stringify(key)} is missing`, path)
    }

    const additional = node.additionalProperties
    if (additional !== undefined && typeof additional !== 'boolean' && (additional === null || typeof additional !== 'object' || Array.isArray(additional))) fail('additionalProperties must be boolean or schema', path)
    const output: JsonRecord = Object.create(Object.getPrototypeOf(value) === null ? null : Object.prototype) as JsonRecord
    const active = context.active
    if (active.has(value)) fail('cyclic input is not a JSON value', path)
    active.add(value)
    try {
      for (const key of Object.keys(value)) {
        const childPath = `${path}.${key}`
        if (hasOwn(propertySchemas, key)) {
          Object.defineProperty(output, key, { configurable: true, enumerable: true, writable: true, value: validateNode(value[key], propertySchemas[key] as JsonSchema, childPath, context) })
        } else if (additional === false) {
          fail(`unknown property ${JSON.stringify(key)}`, path)
        } else if (additional !== undefined && typeof additional === 'object') {
          Object.defineProperty(output, key, { configurable: true, enumerable: true, writable: true, value: validateNode(value[key], additional as JsonSchema, childPath, context) })
        } else {
          Object.defineProperty(output, key, { configurable: true, enumerable: true, writable: true, value: cloneWithoutSchema(value[key], childPath, active) })
        }
      }
    } finally {
      active.delete(value)
    }
    return output
  }

  return value
}

function freezeDeep<T>(value: T, seen = new WeakSet<object>()): T {
  if (value === null || typeof value !== 'object') return value
  if (seen.has(value)) return value
  seen.add(value)
  for (const key of Reflect.ownKeys(value)) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key)
    if (descriptor !== undefined && 'value' in descriptor) freezeDeep(descriptor.value, seen)
  }
  Object.freeze(value)
  return value
}

/** Validate an unknown wire value and return an independent, deeply frozen copy. */
export function parseJsonSchema<T>(value: unknown, schema: JsonSchema, label = 'value'): T {
  const context: ValidationContext = { root: schema, active: new WeakSet<object>() }
  return freezeDeep(validateNode(value, schema, label || '$', context) as T)
}

/** Alias used by contract modules that prefer an assertion-shaped name. */
export function validateJsonSchema<T>(value: unknown, schema: JsonSchema, label = 'value'): T {
  return parseJsonSchema<T>(value, schema, label)
}

/** Clone and freeze a value which has already passed a closed contract check. */
export function deepCloneFreeze<T>(value: T): T {
  return freezeDeep(cloneWithoutSchema(value, '$') as T)
}
