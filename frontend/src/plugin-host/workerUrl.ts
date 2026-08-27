const RELEASE_OR_HASH = /^[0-9a-f]{64}$/
const WORKER_PATH = /^\/__plotpilot\/plugin-worker\/([0-9a-f]{64})\/([0-9a-f]{64})\/worker\.js$/

export const PLUGIN_WORKER_ROUTE_PREFIX = '/__plotpilot/plugin-worker/'

export interface PluginWorkerUrlValidationOptions {
  /** Origin of the Shell page. Absolute URLs are rejected if it is absent. */
  expectedOrigin?: string
  expectedReleaseId?: string
  expectedBundleHash?: string
}

export interface ParsedPluginWorkerUrl {
  /** The accepted root-relative or same-origin absolute primitive string. */
  url: string
  /** The URL origin when available; relative paths have a null origin. */
  origin: string | null
  path: string
  releaseId: string
  bundleHash: string
}

export class PluginWorkerUrlError extends TypeError {
  readonly code: string

  constructor(code: string) {
    super(code)
    this.name = 'PluginWorkerUrlError'
    this.code = code
  }
}

function reject(code: string): never {
  throw new PluginWorkerUrlError(code)
}

function ambientOrigin(): string | undefined {
  const location = (globalThis as typeof globalThis & { location?: { origin?: unknown } }).location
  return typeof location?.origin === 'string' ? location.origin : undefined
}

function normalizeOrigin(value: string): string {
  if (typeof value !== 'string' || value.length === 0) reject('plugin_worker_url_origin_invalid')
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    reject('plugin_worker_url_origin_invalid')
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') reject('plugin_worker_url_origin_invalid')
  if (parsed.username !== '' || parsed.password !== '') reject('plugin_worker_url_origin_invalid')
  return parsed.origin
}

function expectedIdentity(value: string | undefined, label: string): string | undefined {
  if (value === undefined) return undefined
  if (!RELEASE_OR_HASH.test(value)) reject(`${label}_invalid`)
  return value
}

function rawAbsolutePath(value: string): string {
  const schemeEnd = value.indexOf('://')
  if (schemeEnd < 0) return ''
  const authorityAndPath = value.slice(schemeEnd + 3)
  const firstPathSeparator = authorityAndPath.indexOf('/')
  return firstPathSeparator < 0 ? '' : authorityAndPath.slice(firstPathSeparator)
}

function parsePath(path: string): { releaseId: string; bundleHash: string } {
  const match = WORKER_PATH.exec(path)
  if (match === null) reject('plugin_worker_url_path_invalid')
  return { releaseId: match[1], bundleHash: match[2] }
}

/**
 * Validate the only Worker URL accepted by the Plugin Host.
 *
 * The raw path is checked before URL parsing can normalize dot segments or
 * encoded characters. This deliberately accepts a same-origin absolute URL or
 * the exact root-relative Core route, and nothing else.
 */
export function parsePluginWorkerUrl(
  value: unknown,
  options: PluginWorkerUrlValidationOptions = {},
): ParsedPluginWorkerUrl {
  if (typeof value !== 'string') reject('plugin_worker_url_type_invalid')
  const raw = value
  if (raw.length === 0) reject('plugin_worker_url_empty')
  if (/[\u0000-\u0020\u007f]/u.test(raw)) reject('plugin_worker_url_whitespace')
  if (raw.includes('%')) reject('plugin_worker_url_encoding_forbidden')
  if (raw.includes('\\')) reject('plugin_worker_url_backslash_forbidden')
  if (raw.includes('?') || raw.includes('#')) reject('plugin_worker_url_query_fragment_forbidden')

  const expectedReleaseId = expectedIdentity(options.expectedReleaseId, 'plugin_worker_release')
  const expectedBundleHash = expectedIdentity(options.expectedBundleHash, 'plugin_worker_bundle_hash')
  const configuredOrigin = options.expectedOrigin === undefined
    ? ambientOrigin()
    : normalizeOrigin(options.expectedOrigin)

  if (raw.startsWith('/') && !raw.startsWith('//')) {
    const { releaseId, bundleHash } = parsePath(raw)
    if (expectedReleaseId !== undefined && releaseId !== expectedReleaseId) reject('plugin_worker_url_release_mismatch')
    if (expectedBundleHash !== undefined && bundleHash !== expectedBundleHash) reject('plugin_worker_url_bundle_hash_mismatch')
    return {
      url: raw,
      origin: configuredOrigin ?? null,
      path: raw,
      releaseId,
      bundleHash,
    }
  }

  if (!/^[A-Za-z][A-Za-z0-9+.-]*:\/\//u.test(raw)) reject('plugin_worker_url_absolute_or_root_relative_required')
  const rawPath = rawAbsolutePath(raw)
  const { releaseId, bundleHash } = parsePath(rawPath)
  let parsed: URL
  try {
    parsed = new URL(raw)
  } catch {
    reject('plugin_worker_url_invalid')
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') reject('plugin_worker_url_scheme_forbidden')
  if (parsed.username !== '' || parsed.password !== '') reject('plugin_worker_url_credentials_forbidden')
  if (configuredOrigin === undefined) reject('plugin_worker_url_origin_unavailable')
  if (parsed.origin !== configuredOrigin) reject('plugin_worker_url_cross_origin')
  if (parsed.pathname !== rawPath) reject('plugin_worker_url_path_normalized')
  if (expectedReleaseId !== undefined && releaseId !== expectedReleaseId) reject('plugin_worker_url_release_mismatch')
  if (expectedBundleHash !== undefined && bundleHash !== expectedBundleHash) reject('plugin_worker_url_bundle_hash_mismatch')
  return {
    url: parsed.href,
    origin: parsed.origin,
    path: parsed.pathname,
    releaseId,
    bundleHash,
  }
}

/** Return the accepted URL string or throw a typed gate error. */
export function validatePluginWorkerUrl(
  value: unknown,
  options: PluginWorkerUrlValidationOptions = {},
): string {
  return parsePluginWorkerUrl(value, options).url
}

export const assertPluginWorkerUrl = validatePluginWorkerUrl
export const assertImmutablePluginWorkerUrl = validatePluginWorkerUrl

export function isPluginWorkerUrl(
  value: unknown,
  options: PluginWorkerUrlValidationOptions = {},
): boolean {
  try {
    parsePluginWorkerUrl(value, options)
    return true
  } catch {
    return false
  }
}

export function buildPluginWorkerUrl(
  releaseId: string,
  bundleHash: string,
  expectedOrigin?: string,
): string {
  if (!RELEASE_OR_HASH.test(releaseId)) reject('plugin_worker_release_invalid')
  if (!RELEASE_OR_HASH.test(bundleHash)) reject('plugin_worker_bundle_hash_invalid')
  const path = `${PLUGIN_WORKER_ROUTE_PREFIX}${releaseId}/${bundleHash}/worker.js`
  if (expectedOrigin === undefined) return path
  const origin = normalizeOrigin(expectedOrigin)
  return new URL(path, origin).href
}

export const createPluginWorkerUrl = buildPluginWorkerUrl
