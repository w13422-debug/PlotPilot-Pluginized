import { materializePluginManagementSnapshot } from './model.ts'
import type {
  PluginManagementReadGateway,
  PluginManagementSnapshot,
  PluginReleaseProjection,
} from './types.ts'

/** The sole mounted read route in the accepted Plugins v1 surface. */
export const PLUGIN_RELEASE_LIST_PATH = '/api/v1/plugins/releases' as const

export interface PluginManagementHttpGatewayOptions {
  baseUrl?: string
  fetch?: typeof globalThis.fetch
}

export class PluginManagementHttpError extends Error {
  readonly status: number
  readonly payload: unknown

  constructor(status: number, message: string, payload: unknown) {
    super(message)
    this.name = 'PluginManagementHttpError'
    this.status = status
    this.payload = payload
  }
}

type JsonRecord = Record<string, unknown>

function isRecord(value: unknown): value is JsonRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function requireRecord(value: unknown, label: string): JsonRecord {
  if (!isRecord(value)) throw new Error(`${label} must be a JSON object`)
  return value
}

function requireExactKeys(value: JsonRecord, keys: readonly string[], label: string): void {
  const expected = new Set(keys)
  if (Object.keys(value).some(key => !expected.has(key)) || keys.some(key => !(key in value))) {
    throw new Error(`${label} has an unexpected schema shape`)
  }
}

function requireString(value: unknown, label: string): string {
  if (typeof value !== 'string' || value.length === 0) throw new Error(`${label} must be a non-empty string`)
  return value
}

function joinUrl(baseUrl: string, path: string): string {
  if (baseUrl.length === 0) return path
  return `${baseUrl.replace(/\/$/, '')}/${path.replace(/^\//, '')}`
}

function errorMessage(status: number, payload: unknown): string {
  if (isRecord(payload) && typeof payload.message === 'string' && payload.message.length > 0) {
    return payload.message
  }
  return `Plugin release list request failed with HTTP ${status}`
}

async function readJson(response: Response): Promise<unknown> {
  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.toLowerCase().includes('application/json')) {
    throw new PluginManagementHttpError(
      response.status,
      `Plugin release list returned a non-JSON response (HTTP ${response.status})`,
      null,
    )
  }
  try {
    return await response.json() as unknown
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    throw new PluginManagementHttpError(
      response.status,
      `Plugin release list JSON decode failed: ${message}`,
      null,
    )
  }
}

/**
 * Projects the only authority that the v1 release list proves: a verified
 * package-store entry. Runtime activation, LKG, pins, installs, and Plans have
 * no mounted read authority here, so the UI represents none of them as active.
 */
function projectRelease(value: unknown): PluginReleaseProjection {
  const release = requireRecord(value, 'plugin release')
  requireExactKeys(release, [
    'schema', 'plugin_id', 'version', 'kind', 'package_hash', 'release_id', 'manifest',
  ], 'plugin release')
  if (release.schema !== 'plugin-release-view/v1') throw new Error('plugin release schema is unsupported')
  requireString(release.kind, 'plugin release kind')
  requireRecord(release.manifest, 'plugin release manifest')
  return {
    plugin_id: requireString(release.plugin_id, 'plugin release plugin_id'),
    release_id: requireString(release.release_id, 'plugin release release_id'),
    version: requireString(release.version, 'plugin release version'),
    package_hash: requireString(release.package_hash, 'plugin release package_hash'),
    lifecycle_state: 'installed',
    package_present: true,
    active: false,
    lkg: false,
    pin_count: 0,
  }
}

function projectSnapshot(value: unknown): PluginManagementSnapshot {
  const body = requireRecord(value, 'plugin release list')
  requireExactKeys(body, ['schema', 'releases'], 'plugin release list')
  if (body.schema !== 'plugin-release-list/v1' || !Array.isArray(body.releases)) {
    throw new Error('plugin release list schema is unsupported')
  }
  return materializePluginManagementSnapshot({
    releases: body.releases.map(projectRelease),
    // No accepted public Plan or install-operation read endpoint is mounted.
    plans: [],
    selected_plan: null,
    operation: null,
  })
}

/**
 * Production browser-relative gateway. It intentionally exposes no mutation
 * methods and calls no unmounted v1/v2 plugin route.
 */
export function createPluginManagementHttpGateway(
  options: PluginManagementHttpGatewayOptions = {},
): PluginManagementReadGateway {
  const fetchImpl = options.fetch ?? globalThis.fetch
  if (typeof fetchImpl !== 'function') throw new Error('Plugin Management HTTP runtime requires fetch')
  const baseUrl = options.baseUrl ?? ''

  return {
    async loadSnapshot(): Promise<PluginManagementSnapshot> {
      const response = await fetchImpl(joinUrl(baseUrl, PLUGIN_RELEASE_LIST_PATH), {
        method: 'GET',
        headers: { Accept: 'application/json' },
      })
      const payload = await readJson(response)
      if (!response.ok) throw new PluginManagementHttpError(response.status, errorMessage(response.status, payload), payload)
      return projectSnapshot(payload)
    },
  }
}
