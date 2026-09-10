import coreApiMethodMatrix from '../../../../contracts/json-schema/core-api-method-matrix.v1.json' with { type: 'json' }
import {
  parseCoreAuthorityCommandQueryV1,
  parseCoreHttpRequestErrorV1,
} from '../../contracts/core-api.ts'
import type {
  CoreAuthorityCommandQuery,
  CoreDeleteResult,
  CoreDocument,
  CoreDocumentCreateCommand,
  CoreDocumentGetQuery,
  CoreDocumentPage,
  CoreDocumentQuery,
  CoreDocumentRevisionCreateCommand,
  CoreHttpError,
  CoreHttpRequestError,
  CoreRevision,
  CoreRevisionContentPage,
  CoreRevisionGetQuery,
  CoreContentQuery,
  CoreWorkspace,
  CoreWorkspaceCreateCommand,
  CoreWorkspaceDeleteCommand,
  CoreWorkspaceGetQuery,
  CoreWorkspacePage,
  CoreWorkspaceQuery,
} from '../../contracts/types.ts'

export interface CoreFlowRouteMap {
  'workspace.list': { request: CoreWorkspaceQuery; response: CoreWorkspacePage }
  'workspace.get': { request: CoreWorkspaceGetQuery; response: CoreWorkspace }
  'workspace.create': { request: CoreWorkspaceCreateCommand; response: CoreWorkspace }
  'workspace.delete': { request: CoreWorkspaceDeleteCommand; response: CoreDeleteResult }
  'document.list': { request: CoreDocumentQuery; response: CoreDocumentPage }
  'document.create': { request: CoreDocumentCreateCommand; response: CoreDocument }
  'document.get': { request: CoreDocumentGetQuery; response: CoreDocument }
  'revision.get': { request: CoreRevisionGetQuery; response: CoreRevision }
  'revision.content': { request: CoreContentQuery; response: CoreRevisionContentPage }
  'document.revision.create': { request: CoreDocumentRevisionCreateCommand; response: CoreRevision }
}

export type CoreFlowRouteId = keyof CoreFlowRouteMap

export interface CoreFlowGateway {
  request<K extends CoreFlowRouteId>(
    routeId: K,
    request: CoreFlowRouteMap[K]['request'],
  ): Promise<Readonly<CoreFlowRouteMap[K]['response']>>
}

export interface CoreHttpFlowGatewayOptions {
  baseUrl?: string
  fetch?: typeof globalThis.fetch
}

type CoreRoute = (typeof coreApiMethodMatrix.routes)[number]

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function parseCoreError(value: unknown): Readonly<CoreHttpError | CoreHttpRequestError> {
  if (!isRecord(value)) throw new Error('Core HTTP error response must be an object')
  if (value.schema === 'core-http-request-error/v1') return parseCoreHttpRequestErrorV1(value)
  if (value.schema !== 'core-http-error/v1') throw new Error('Core HTTP error response has an unknown schema')
  const parsed = parseCoreAuthorityCommandQueryV1(value) as unknown as Readonly<CoreHttpError>
  return parsed
}

export class CoreFlowHttpError extends Error {
  readonly status: number
  readonly errorCode: string
  readonly retryable: boolean

  constructor(status: number, payload: Readonly<CoreHttpError | CoreHttpRequestError>) {
    super(payload.message)
    this.name = 'CoreFlowHttpError'
    this.status = status
    this.errorCode = payload.error_code
    this.retryable = payload.retryable
  }
}

function routeFor(routeId: CoreFlowRouteId): CoreRoute {
  const route = coreApiMethodMatrix.routes.find(candidate => candidate.route_id === routeId)
  if (route === undefined) throw new Error(`Unknown frozen Core route: ${routeId}`)
  return route
}

function joinUrl(baseUrl: string, path: string): string {
  if (baseUrl.length === 0) return path
  return `${baseUrl.replace(/\/$/, '')}/${path.replace(/^\//, '')}`
}

function buildRequest(route: CoreRoute, request: Readonly<Record<string, unknown>>, baseUrl: string): [string, RequestInit] {
  let path = route.path_template
  const pathIdentities = new Set<string>(route.path_identity)
  for (const identity of pathIdentities) {
    const value = request[identity]
    if (typeof value !== 'string' || value.length === 0) throw new Error(`Core path identity ${identity} is missing`)
    path = path.replace(`{${identity}}`, encodeURIComponent(value))
  }

  const headers = { Accept: 'application/json' }
  if (route.method === 'GET') {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(request)) {
      if (key === 'schema' || pathIdentities.has(key) || value === null) continue
      query.set(key, String(value))
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : ''
    return [joinUrl(baseUrl, `${path}${suffix}`), { method: route.method, headers }]
  }

  return [joinUrl(baseUrl, path), {
    method: route.method,
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  }]
}

export function createCoreHttpFlowGateway(options: CoreHttpFlowGatewayOptions = {}): CoreFlowGateway {
  const fetchImpl = options.fetch ?? globalThis.fetch
  if (typeof fetchImpl !== 'function') throw new Error('Core HTTP flow requires a fetch implementation')
  const baseUrl = options.baseUrl ?? ''

  return {
    async request<K extends CoreFlowRouteId>(routeId: K, input: CoreFlowRouteMap[K]['request']) {
      const route = routeFor(routeId)
      const request = parseCoreAuthorityCommandQueryV1(input)
      if (request.schema !== route.request_schema) {
        throw new Error(`Core route ${routeId} requires ${route.request_schema}, received ${request.schema}`)
      }
      const [url, init] = buildRequest(route, request as unknown as Readonly<Record<string, unknown>>, baseUrl)
      const response = await fetchImpl(url, init)
      const contentType = response.headers.get('content-type') ?? ''
      if (!contentType.toLowerCase().includes('application/json')) {
        throw new Error(`Core route ${routeId} returned a non-JSON response`)
      }
      const raw: unknown = await response.json()
      if (!response.ok) {
        const error = parseCoreError(raw)
        const failure = route.failure_statuses.find(candidate => candidate.status === response.status)
        const requestFailureAllowed = response.status === 400
          && error.schema === 'core-http-request-error/v1'
          && error.error_code !== 'range_out_of_bounds'
        if (!requestFailureAllowed && (failure === undefined || !failure.error_codes.includes(error.error_code as never))) {
          throw new Error(`Core route ${routeId} returned an undeclared failure status/error pair`)
        }
        throw new CoreFlowHttpError(response.status, error)
      }
      if (!route.success_statuses.includes(response.status as never)) {
        throw new Error(`Core route ${routeId} returned undeclared success status ${response.status}`)
      }
      const parsed = parseCoreAuthorityCommandQueryV1(raw)
      if (parsed.schema !== route.result_schema) {
        throw new Error(`Core route ${routeId} returned ${parsed.schema}; expected ${route.result_schema}`)
      }
      return parsed as unknown as Readonly<CoreFlowRouteMap[K]['response']>
    },
  }
}
