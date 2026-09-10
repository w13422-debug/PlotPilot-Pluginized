import {
  parseHttpRequestV2,
  routeForV2,
  validateHttpExchangeV2,
} from '../../contracts/m4-m5-http-v2.ts'
import { inject, provide, type InjectionKey } from 'vue'

type JsonRecord = Record<string, unknown>
type CoreV2CandidateRouteId = 'candidate.get' | 'candidate.preview' | 'candidate.review' | 'publication.accept'

export type FeatureSurface =
  | 'planning'
  | 'generation'
  | 'candidate'
  | 'checkpoint'
  | 'foreshadow'
  | 'storyBible'
  | 'export'
  | 'jobDrawer'

export interface FeatureAvailability {
  readonly available: boolean
  readonly reason: string
}

export type FeatureAvailabilityProjection = Readonly<Record<FeatureSurface, FeatureAvailability>>

export interface CandidateIdentity {
  readonly workspaceId: string
  readonly candidateId: string
}

export type CandidateReviewDecision = 'approve' | 'reject'
export type CandidateExpectedStatus = 'complete' | 'partial'

/** Panel-owned fields; Workspace ownership is supplied by Workbench. */
export interface CandidateReviewDraft {
  readonly candidateId: string
  readonly decision: CandidateReviewDecision
  readonly expectedStatus: CandidateExpectedStatus
}

export interface CandidateReviewInput extends CandidateIdentity {
  readonly decision: CandidateReviewDecision
  readonly expectedStatus: CandidateExpectedStatus
}

export function bindWorkspaceCandidateReview(
  identity: CandidateIdentity,
  draft: CandidateReviewDraft,
): CandidateReviewInput {
  return {
    workspaceId: identity.workspaceId,
    candidateId: identity.candidateId,
    decision: draft.decision,
    expectedStatus: draft.expectedStatus,
  }
}

/** Accepted Core v2 candidate/publication seam; it has no legacy fallback. */
export interface CoreV2CandidateGateway {
  previewCandidate(identity: CandidateIdentity): Promise<Readonly<JsonRecord>>
  reviewCandidate(input: CandidateReviewInput): Promise<Readonly<JsonRecord>>
  acceptCandidate(identity: CandidateIdentity): Promise<Readonly<JsonRecord>>
}

export interface CoreV2CandidateHttpGatewayOptions {
  baseUrl?: string
  fetch: typeof globalThis.fetch
  acceptedBy: string
  createOperationKey?: (kind: 'review' | 'publication') => string
}

export interface FeatureRuntimeGatewayOptions {
  /** Missing availability is disabled and checking it makes no request. */
  availability?: Partial<Record<FeatureSurface, FeatureAvailability>>
  coreV2CandidateGateway?: CoreV2CandidateGateway
}

export class FeatureUnavailableError extends Error {
  readonly surface: FeatureSurface

  constructor(surface: FeatureSurface, reason: string) {
    super(reason)
    this.name = 'FeatureUnavailableError'
    this.surface = surface
  }
}

export function candidateInputDisabled(availability: FeatureAvailability, busy: boolean): boolean {
  return !availability.available || busy
}

export function candidateOperationEnabled(
  availability: FeatureAvailability,
  busy: boolean,
  candidateId: string,
): boolean {
  return !candidateInputDisabled(availability, busy) && candidateId.trim().length > 0
}

export interface WorkspaceOwnedCandidateOperationHandlers<Result> {
  onStart(): void
  onSuccess(result: Result): void
  onError(error: unknown): void
  onSettled(): void
}

/**
 * The caller supplies its current Workspace-generation predicate. Once it
 * becomes false, no completion path may update UI result, message, or busy
 * state. This deliberately has no mutable process-wide ownership.
 */
export async function runWorkspaceOwnedCandidateOperation<Result>(
  isCurrent: () => boolean,
  operation: () => Promise<Result>,
  handlers: WorkspaceOwnedCandidateOperationHandlers<Result>,
): Promise<void> {
  if (!isCurrent()) return
  handlers.onStart()
  try {
    const result = await operation()
    if (isCurrent()) handlers.onSuccess(result)
  } catch (error: unknown) {
    if (isCurrent()) handlers.onError(error)
  } finally {
    if (isCurrent()) handlers.onSettled()
  }
}

const DEFAULT_REASONS: Readonly<Record<FeatureSurface, string>> = {
  planning: 'Missing: Planner plugin RunSnapshot issuance is not mounted.',
  generation: 'Missing: Jobs v2 start requires an authoritative RunSnapshot and writer epoch.',
  candidate: 'Missing: Core v2 Candidate review gateway is not mounted.',
  checkpoint: 'Disabled: Jobs v2 exposes resume in Task Drawer, but no rich checkpoint history projection is mounted.',
  foreshadow: 'Missing: Core story read projection for the foreshadow ledger is not mounted.',
  storyBible: 'Missing: Core story read projection for story evolution and Bible is not mounted.',
  export: 'Missing: Export plugin artifact lookup and download authority are not mounted.',
  jobDrawer: 'Missing: Jobs v2 runtime availability projection is not mounted.',
}

function unavailable(surface: FeatureSurface, reason?: string): FeatureAvailability {
  return { available: false, reason: reason?.trim() || DEFAULT_REASONS[surface] }
}

function available(): FeatureAvailability {
  return { available: true, reason: '' }
}

function requestedAvailability(surface: FeatureSurface, requested: FeatureAvailability | undefined): FeatureAvailability {
  return requested?.available === true ? available() : unavailable(surface, requested?.reason)
}

function requireIdentity(value: string, label: string): string {
  if (value.length === 0 || value.trim() !== value) throw new Error(`${label} must be a non-empty stable identity`)
  return value
}

function defaultOperationKey(kind: 'review' | 'publication'): string {
  const uuid = globalThis.crypto?.randomUUID?.()
  if (uuid !== undefined) return `webui-${kind}-${uuid}`
  return `webui-${kind}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

function joinUrl(baseUrl: string, path: string): string {
  return baseUrl.length === 0 ? path : `${baseUrl.replace(/\/$/, '')}/${path.replace(/^\//, '')}`
}

function buildRequest(routeId: CoreV2CandidateRouteId, request: Readonly<JsonRecord>, baseUrl: string): [string, RequestInit] {
  const route = routeForV2(routeId)
  let path = route.path_template
  const pathIdentity = new Set(route.path_identity)
  for (const identity of pathIdentity) {
    const value = request[identity]
    if (typeof value !== 'string' || value.length === 0) throw new Error(`Core v2 path identity ${identity} is missing`)
    path = path.replace(`{${identity}}`, encodeURIComponent(value))
  }
  const headers = { Accept: 'application/json' }
  if (route.method === 'GET') {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(request)) {
      if (key === 'schema' || pathIdentity.has(key) || value === null) continue
      query.set(key, String(value))
    }
    const suffix = query.size === 0 ? '' : `?${query.toString()}`
    return [joinUrl(baseUrl, `${path}${suffix}`), { method: route.method, headers }]
  }
  return [joinUrl(baseUrl, path), {
    method: route.method,
    headers: { ...headers, 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  }]
}

function isJsonRecord(value: unknown): value is JsonRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function responseCandidateId(result: Readonly<JsonRecord>): unknown {
  if (typeof result.candidate_id === 'string') return result.candidate_id
  const candidate = result.candidate
  return isJsonRecord(candidate) ? candidate.candidate_id : undefined
}

function assertSameIdentity(result: Readonly<JsonRecord>, identity: CandidateIdentity, routeId: CoreV2CandidateRouteId): void {
  if (result.workspace_id !== identity.workspaceId || responseCandidateId(result) !== identity.candidateId) {
    throw new Error(`Core v2 ${routeId} response crossed the requested Workspace or Candidate identity`)
  }
}

function authoritativeCandidate(result: Readonly<JsonRecord>, identity: CandidateIdentity): Readonly<JsonRecord> {
  const candidate = result.candidate
  if (!isJsonRecord(candidate)
    || candidate.workspace_id !== identity.workspaceId
    || candidate.candidate_id !== identity.candidateId) {
    throw new Error('Core v2 candidate.get did not return the requested authoritative Candidate')
  }
  return candidate
}

function requireReviewInput(input: CandidateReviewInput): CandidateReviewInput {
  const decision = input.decision
  const expectedStatus = input.expectedStatus
  if (decision !== 'approve' && decision !== 'reject') throw new Error('Candidate decision is invalid')
  if (expectedStatus !== 'complete' && expectedStatus !== 'partial') throw new Error('Candidate expected status is invalid')
  return {
    workspaceId: requireIdentity(input.workspaceId, 'Workspace identity'),
    candidateId: requireIdentity(input.candidateId, 'Candidate identity'),
    decision,
    expectedStatus,
  }
}

/**
 * Thin adapter for only accepted Core v2 candidate/publication routes. This
 * slice never auto-installs it, so unavailable features perform zero fetches.
 */
export function createCoreV2CandidateHttpGateway(options: CoreV2CandidateHttpGatewayOptions): CoreV2CandidateGateway {
  if (typeof options.fetch !== 'function') throw new Error('Core v2 Candidate gateway requires fetch')
  const acceptedBy = requireIdentity(options.acceptedBy, 'Core v2 accepted_by')
  const baseUrl = options.baseUrl ?? ''
  const createOperationKey = options.createOperationKey ?? defaultOperationKey

  async function request(
    routeId: CoreV2CandidateRouteId,
    input: JsonRecord,
    candidateValue?: Readonly<JsonRecord>,
  ): Promise<Readonly<JsonRecord>> {
    const parsedRequest = parseHttpRequestV2(routeId, input) as JsonRecord
    const [url, init] = buildRequest(routeId, parsedRequest, baseUrl)
    const response = await options.fetch(url, init)
    const contentType = response.headers.get('content-type') ?? ''
    if (!contentType.toLowerCase().includes('application/json')) throw new Error(`Core v2 ${routeId} returned a non-JSON response`)
    const payload: unknown = await response.json()
    const { response: parsedResponse } = await validateHttpExchangeV2(
      routeId,
      parsedRequest,
      response.status,
      payload,
      candidateValue,
    )
    if (!response.ok) throw new Error(String(parsedResponse.message ?? `Core v2 ${routeId} failed with HTTP ${response.status}`))
    return parsedResponse
  }

  return {
    async previewCandidate(identity) {
      const normalized = { workspaceId: requireIdentity(identity.workspaceId, 'Workspace identity'), candidateId: requireIdentity(identity.candidateId, 'Candidate identity') }
      const result = await request('candidate.preview', {
        schema: 'candidate-preview-query/v2', workspace_id: normalized.workspaceId, candidate_id: normalized.candidateId, offset: 0, length: 1024,
      })
      assertSameIdentity(result, normalized, 'candidate.preview')
      return result
    },
    async reviewCandidate(input) {
      const normalized = requireReviewInput(input)
      const result = await request('candidate.review', {
        schema: 'candidate-review-command/v2',
        operation_key: createOperationKey('review'),
        workspace_id: normalized.workspaceId,
        candidate_id: normalized.candidateId,
        decision: normalized.decision,
        decided_by: acceptedBy,
        expected_status: normalized.expectedStatus,
      })
      assertSameIdentity(result, normalized, 'candidate.review')
      return result
    },
    async acceptCandidate(identity) {
      const normalized = { workspaceId: requireIdentity(identity.workspaceId, 'Workspace identity'), candidateId: requireIdentity(identity.candidateId, 'Candidate identity') }
      const candidateResult = await request('candidate.get', {
        schema: 'candidate-get-query/v2', workspace_id: normalized.workspaceId, candidate_id: normalized.candidateId,
      })
      assertSameIdentity(candidateResult, normalized, 'candidate.get')
      const candidate = authoritativeCandidate(candidateResult, normalized)
      const result = await request('publication.accept', {
        schema: 'publication-command/v2', publication_operation_key: createOperationKey('publication'), workspace_id: normalized.workspaceId, candidate_id: normalized.candidateId, accepted_by: acceptedBy,
      }, candidate)
      assertSameIdentity(result, normalized, 'publication.accept')
      return result
    },
  }
}

export interface FeatureRuntimeGateway {
  readonly availability: FeatureAvailabilityProjection
  previewCandidate(identity: CandidateIdentity): Promise<Readonly<JsonRecord>>
  reviewCandidate(input: CandidateReviewInput): Promise<Readonly<JsonRecord>>
  acceptCandidate(identity: CandidateIdentity): Promise<Readonly<JsonRecord>>
}

/** Fixed capability assembly. Rendering the default is side-effect free. */
export function createFeatureRuntimeGateway(options: FeatureRuntimeGatewayOptions = {}): FeatureRuntimeGateway {
  const requested = options.availability ?? {}
  const candidateGateway = options.coreV2CandidateGateway
  const candidateRequested = requestedAvailability('candidate', requested.candidate)
  const availability: FeatureAvailabilityProjection = {
    planning: unavailable('planning', requested.planning?.reason),
    generation: unavailable('generation', requested.generation?.reason),
    candidate: candidateRequested.available && candidateGateway !== undefined
      ? available()
      : unavailable('candidate', candidateRequested.available
        ? 'Missing: Core v2 Candidate review gateway binding is not mounted.'
        : candidateRequested.reason),
    checkpoint: unavailable('checkpoint', requested.checkpoint?.reason),
    foreshadow: unavailable('foreshadow', requested.foreshadow?.reason),
    storyBible: unavailable('storyBible', requested.storyBible?.reason),
    export: unavailable('export', requested.export?.reason),
    jobDrawer: requestedAvailability('jobDrawer', requested.jobDrawer),
  }

  function requireCandidate(): CoreV2CandidateGateway {
    if (!availability.candidate.available || candidateGateway === undefined) {
      throw new FeatureUnavailableError('candidate', availability.candidate.reason)
    }
    return candidateGateway
  }

  return {
    availability,
    previewCandidate: async identity => requireCandidate().previewCandidate(identity),
    reviewCandidate: async input => requireCandidate().reviewCandidate(input),
    acceptCandidate: async identity => requireCandidate().acceptCandidate(identity),
  }
}

export const FEATURE_RUNTIME_GATEWAY_KEY: InjectionKey<FeatureRuntimeGateway> = Symbol('FeatureRuntimeGateway')

/** Provide a runtime to one Vue app/component tree only. */
export function provideFeatureRuntimeGateway(runtime: FeatureRuntimeGateway): void {
  provide(FEATURE_RUNTIME_GATEWAY_KEY, runtime)
}

/** A missing provider has the deterministic, side-effect-free unavailable surface. */
export function useFeatureRuntimeGateway(): FeatureRuntimeGateway {
  return inject(FEATURE_RUNTIME_GATEWAY_KEY, () => createFeatureRuntimeGateway(), true)
}
