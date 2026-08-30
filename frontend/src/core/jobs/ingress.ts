import type { JobEventCursor, JobSseRecovery } from './types.ts'

const ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/
const HASH = /^[0-9a-f]{64}$/

function record(value: unknown, name: string): Record<string, unknown> {
  if (value == null || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${name} must be an object`)
  const prototype = Object.getPrototypeOf(value)
  if (prototype !== Object.prototype && prototype !== null) throw new Error(`${name} must be a plain object`)
  for (const key of Reflect.ownKeys(value)) {
    if (typeof key !== 'string') throw new Error(`${name} cannot contain symbol keys`)
    const descriptor = Object.getOwnPropertyDescriptor(value, key)
    if (descriptor == null || !Object.hasOwn(descriptor, 'value')) throw new Error(`${name} fields must be own data properties`)
  }
  return value as Record<string, unknown>
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[], name: string): void {
  const actual = Reflect.ownKeys(value).map(String).sort()
  const expected = [...keys].sort()
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new Error(`${name} must use the frozen closed field set`)
  }
}

function integer(value: unknown, name: string, minimum: number): number {
  if (!Number.isInteger(value) || (value as number) < minimum) throw new Error(`${name} must be an integer >= ${minimum}`)
  return value as number
}

function nullableId(value: unknown, name: string): string | null {
  if (value === null) return null
  if (typeof value !== 'string' || !ID.test(value)) throw new Error(`${name} must be null or a valid ID`)
  return value
}

function nullableHash(value: unknown, name: string): string | null {
  if (value === null) return null
  if (typeof value !== 'string' || !HASH.test(value)) throw new Error(`${name} must be null or a lowercase SHA-256`)
  return value
}

export function parseJobEventCursor(value: unknown): JobEventCursor {
  const source = record(value, 'JobEventCursor')
  exactKeys(source, ['stream_kind', 'job_id', 'job_event_seq'], 'JobEventCursor')
  if (source.stream_kind !== 'job_event') throw new Error('JobEventCursor stream_kind must be job_event')
  if (typeof source.job_id !== 'string' || !ID.test(source.job_id)) throw new Error('JobEventCursor job_id is invalid')
  return {
    stream_kind: 'job_event',
    job_id: source.job_id,
    job_event_seq: integer(source.job_event_seq, 'JobEventCursor job_event_seq', 1),
  }
}

/** Private closed adapter for the frozen sse-recovery/v1 schema. */
export function parseJobSseRecovery(value: unknown): JobSseRecovery {
  const source = record(value, 'JobSseRecovery')
  exactKeys(source, [
    'schema', 'stream_kind', 'aggregate_id', 'requested_after_seq', 'replay_floor_seq',
    'durable_high_water_seq', 'gap', 'snapshot_required', 'snapshot_schema',
    'snapshot_revision', 'snapshot_asset_id', 'snapshot_hash',
  ], 'JobSseRecovery')
  if (source.schema !== 'sse-recovery/v1') throw new Error('JobSseRecovery schema is invalid')
  if (source.stream_kind !== 'job_event') throw new Error('JobSseRecovery stream_kind must be job_event')
  if (typeof source.gap !== 'boolean' || typeof source.snapshot_required !== 'boolean') {
    throw new Error('JobSseRecovery gap flags must be booleans')
  }
  const snapshotSchema = nullableId(source.snapshot_schema, 'snapshot_schema')
  const snapshotRevision = source.snapshot_revision === null
    ? null
    : integer(source.snapshot_revision, 'snapshot_revision', 1)
  return {
    schema: 'sse-recovery/v1',
    stream_kind: 'job_event',
    aggregate_id: nullableId(source.aggregate_id, 'aggregate_id'),
    requested_after_seq: integer(source.requested_after_seq, 'requested_after_seq', 0),
    replay_floor_seq: integer(source.replay_floor_seq, 'replay_floor_seq', 0),
    durable_high_water_seq: integer(source.durable_high_water_seq, 'durable_high_water_seq', 0),
    gap: source.gap,
    snapshot_required: source.snapshot_required,
    snapshot_schema: snapshotSchema,
    snapshot_revision: snapshotRevision,
    snapshot_asset_id: nullableId(source.snapshot_asset_id, 'snapshot_asset_id'),
    snapshot_hash: nullableHash(source.snapshot_hash, 'snapshot_hash'),
  }
}
