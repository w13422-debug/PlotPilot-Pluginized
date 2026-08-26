import type { RpcMeta, RpcMethod, RpcNotification, RpcRequest } from './types'
import { canonicalJson, utf8 } from './canonical'

export const WORKER_METHODS = [
  'runtime.handshake', 'runtime.health', 'runtime.heartbeat', 'capability.describe', 'settings.validate',
  'migration.plan', 'migration.apply', 'migration.verify', 'job.start', 'job.resume', 'job.pause', 'job.cancel', 'runtime.shutdown',
] as const

export const HOST_METHODS = [
  'host.asset.read/v1', 'host.asset.create/v1', 'host.asset.upload.status/v1', 'host.model.invoke/v1',
  'host.capability.invoke/v1', 'host.capability.poll/v1', 'host.capability.cancel/v1', 'host.candidate.stage/v1',
  'host.checkpoint.commit/v1', 'host.stream.commit/v1', 'host.job.event/v1', 'host.job.await_user/v1', 'host.job.complete/v1',
  'host.log/v1', 'host.migration.lease.renew/v1', 'host.migration.lease.release/v1',
] as const

const REQUEST_METHODS = [...WORKER_METHODS, ...HOST_METHODS].filter(method => method !== 'runtime.heartbeat') as readonly Exclude<RpcMethod, 'runtime.heartbeat'>[]

export const ERROR_CODES = {
  incompatible_generation: 1001,
  stale_lease: 1002,
  cancelled: 1003,
  deadline_exceeded: 1004,
  asset_error: 1005,
  settings_invalid: 1006,
  migration_failed: 1007,
  duplicate_request: 1008,
  uncertain_external_effect: 1009,
  invalid_transition: 1010,
  result_contract_mismatch: 1011,
  data_interpreter_unavailable: 1012,
  release_retiring: 1013,
  checkpoint_invalid: 1014,
} as const

export function buildRequest(method: string, params: Record<string, unknown>, meta: RpcMeta, id: string): RpcRequest {
  if (!(REQUEST_METHODS as readonly string[]).includes(method)) {
    throw new Error('unknown or notification-only RPC method')
  }
  return { jsonrpc: '2.0', id, method: method as Exclude<RpcMethod, 'runtime.heartbeat'>, meta, params }
}

export function buildHeartbeat(params: Record<string, unknown>, meta: RpcMeta): RpcNotification {
  if (meta.context !== 'attempt') throw new Error('heartbeat requires attempt meta')
  return { jsonrpc: '2.0', method: 'runtime.heartbeat', meta, params }
}

export function encodeFrame(message: Record<string, unknown>): Uint8Array {
  const payload = utf8(canonicalJson(message))
  if (payload.byteLength > 8 * 1024 * 1024) throw new Error('RPC body exceeds 8 MiB')
  const header = `Content-Length: ${payload.byteLength}\r\nContent-Type: application/json; charset=utf-8\r\n\r\n`
  return new Uint8Array([...utf8(header), ...payload])
}
