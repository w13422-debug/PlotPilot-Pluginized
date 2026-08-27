import type { PluginUIAck, PluginUIIntent } from '../contracts/types.ts'
import { deepCloneFreeze } from '../contracts/schema-ingress.ts'
import {
  PluginUiSession,
  toPluginUiAck,
  type PluginUiHostEventInput,
  type RecordedIntentAck,
  type UiSessionIdentity,
} from './uiSession.ts'
import {
  ingestPluginUiAck,
  ingestPluginUiMessage,
  type ValidatedPluginUiDisposeBody,
  type ValidatedPluginUiErrorBody,
  type ValidatedPluginUiEventBody,
  type ValidatedPluginUiInitBody,
  type ValidatedPluginUiMessage,
} from './wireIngress.ts'
import { parsePluginWorkerUrl } from './workerUrl.ts'
import { PluginSlotWatchdog, type SlotWatchdogOptions, type WatchdogWorker } from './watchdog.ts'
import type { ValidatedHostTree } from './slotHost.ts'

export interface PluginWorkerLike extends WatchdogWorker {
  postMessage(message: unknown): void
  onmessage: ((event: MessageEvent<unknown>) => void) | null
  onerror: ((event: unknown) => void) | null
  onmessageerror: ((event: MessageEvent<unknown>) => void) | null
}

export type PluginWorkerFactory = (url: string) => PluginWorkerLike

export type PluginWorkerHostState = 'idle' | 'starting' | 'running' | 'stopped' | 'failed'
export type ValidatedPluginUiEventMessage = Extract<
  ValidatedPluginUiMessage,
  { direction: 'host_to_worker'; messageType: 'intent' }
>

export type IntentHandlerResult =
  | Readonly<PluginUIAck>
  | Readonly<RecordedIntentAck>
  | Promise<Readonly<PluginUIAck> | Readonly<RecordedIntentAck>>

export interface PluginWorkerHostOptions {
  identity: UiSessionIdentity
  workerUrl: string
  expectedOrigin?: string
  expectedBundleHash?: string
  session?: PluginUiSession
  workerFactory?: PluginWorkerFactory
  watchdogTimeoutMs?: number
  watchdog?: PluginSlotWatchdog
  watchdogTimer?: Omit<SlotWatchdogOptions, 'timeoutMs' | 'onTimeout'>
  initialRenderSeq?: number
  initialIntentSeq?: number
  contributionConfigAssetId?: string | null
  now?: () => Date
  onRender?: (tree: Readonly<ValidatedHostTree>, message: ValidatedPluginUiMessage) => void
  onIntent?: (intent: Readonly<PluginUIIntent>, message: ValidatedPluginUiMessage) => IntentHandlerResult
  onAck?: (ack: Readonly<PluginUIAck>, message: ValidatedPluginUiMessage) => void
  onError?: (error: Error) => void
  onProtocolError?: (error: Error) => void
  onWatchdogTimeout?: (worker: PluginWorkerLike) => void
  onStateChange?: (state: PluginWorkerHostState) => void
}

interface WorkerLifecycle {
  readonly token: object
  readonly worker: PluginWorkerLike
}

const defaultWorkerFactory: PluginWorkerFactory = url =>
  new Worker(url, { type: 'module' }) as unknown as PluginWorkerLike

function sameIdentity(left: UiSessionIdentity, right: UiSessionIdentity): boolean {
  return left.uiSessionId === right.uiSessionId
    && left.workerInstanceId === right.workerInstanceId
    && left.contributionId === right.contributionId
    && left.slot === right.slot
    && left.generation_id === right.generation_id
    && left.plugin_release_id === right.plugin_release_id
    && left.workspace_id === right.workspace_id
    && left.workspace_revision_id === right.workspace_revision_id
    && left.plan_revision_id === right.plan_revision_id
}

function asError(value: unknown, fallback: string): Error {
  return value instanceof Error ? value : new Error(fallback)
}

function protocolDeadline(now: () => Date): string {
  const date = now()
  const timestamp = date instanceof Date ? date.getTime() : Number.NaN
  if (!Number.isFinite(timestamp)) throw new Error('dispose_deadline_clock_invalid')
  const iso = new Date(timestamp + 1000).toISOString()
  return iso.endsWith('.000Z') ? `${iso.slice(0, -5)}Z` : iso
}

function isRecordedAck(value: unknown): value is Readonly<RecordedIntentAck> {
  return value !== null
    && typeof value === 'object'
    && Object.hasOwn(value, 'intentId')
    && Object.hasOwn(value, 'accepted')
}

function isPromiseLike(value: unknown): value is Promise<unknown> {
  return value !== null
    && typeof value === 'object'
    && typeof (value as { then?: unknown }).then === 'function'
}

function assertWorkerLike(value: unknown): asserts value is PluginWorkerLike {
  if (value === null || typeof value !== 'object'
    || typeof (value as { postMessage?: unknown }).postMessage !== 'function'
    || typeof (value as { terminate?: unknown }).terminate !== 'function') {
    throw new Error('worker_factory_return_invalid')
  }
}

function configuredHighWater(value: number | undefined, label: string): number | undefined {
  if (value !== undefined && (!Number.isSafeInteger(value) || value < 0)) {
    throw new Error(`${label}_invalid`)
  }
  return value
}

/**
 * Per-Slot Dedicated Worker host. It owns only message transport, session
 * fencing and lifecycle; Core dispatch and UI mounting remain injected ports.
 */
export class PluginWorkerHost {
  readonly identity: Readonly<UiSessionIdentity>
  readonly workerUrl: string
  readonly workerReleaseId: string
  readonly workerBundleHash: string
  readonly watchdog: PluginSlotWatchdog
  private readonly workerFactory: PluginWorkerFactory
  private readonly contributionConfigAssetId: string | null
  private readonly now: () => Date
  private readonly options: PluginWorkerHostOptions
  private sessionValue: PluginUiSession
  private lifecycle: WorkerLifecycle | null = null
  private stateValue: PluginWorkerHostState = 'idle'
  private outboundMessageSeq = 0
  private inboundMessageSeq = 0
  private readonly inboundMessageIds = new Set<string>()
  private messageIdCounter = 0
  private hasStarted = false

  constructor(options: PluginWorkerHostOptions) {
    const configuredRenderSeq = configuredHighWater(options.initialRenderSeq, 'worker_host_initial_render_seq')
    const configuredIntentSeq = configuredHighWater(options.initialIntentSeq, 'worker_host_initial_intent_seq')
    const initialSession = options.session ?? new PluginUiSession(options.identity, {
      initialRenderSeq: configuredRenderSeq ?? 0,
      initialIntentSeq: configuredIntentSeq ?? 0,
    })
    if (!sameIdentity(initialSession.identity, options.identity)) throw new Error('worker_host_session_identity_mismatch')
    if (configuredRenderSeq !== undefined && configuredRenderSeq !== initialSession.renderHighWater) {
      throw new Error('worker_host_session_render_high_water_mismatch')
    }
    if (configuredIntentSeq !== undefined && configuredIntentSeq !== initialSession.intentHighWater) {
      throw new Error('worker_host_session_intent_high_water_mismatch')
    }
    this.identity = initialSession.identity
    this.sessionValue = initialSession
    const parsedWorkerUrl = parsePluginWorkerUrl(options.workerUrl, {
      expectedOrigin: options.expectedOrigin,
      expectedReleaseId: this.identity.plugin_release_id,
      expectedBundleHash: options.expectedBundleHash,
    })
    this.workerUrl = parsedWorkerUrl.url
    this.workerReleaseId = parsedWorkerUrl.releaseId
    this.workerBundleHash = parsedWorkerUrl.bundleHash
    this.workerFactory = options.workerFactory ?? defaultWorkerFactory
    this.contributionConfigAssetId = options.contributionConfigAssetId ?? null
    this.now = options.now ?? (() => new Date())
    this.options = options
    if (options.watchdog !== undefined) {
      this.watchdog = options.watchdog
    } else {
      const timer = options.watchdogTimer
      this.watchdog = new PluginSlotWatchdog({
        timeoutMs: options.watchdogTimeoutMs ?? 5000,
        setTimeout: timer?.setTimeout,
        clearTimeout: timer?.clearTimeout,
        onTimeout: worker => this.handleWatchdogTimeout(worker),
      })
    }
  }

  get state(): PluginWorkerHostState {
    return this.stateValue
  }

  get session(): PluginUiSession {
    return this.sessionValue
  }

  get worker(): PluginWorkerLike | null {
    return this.lifecycle?.worker ?? null
  }

  start(): PluginWorkerLike {
    if (this.lifecycle !== null) throw new Error('worker_host_already_running')
    if (this.hasStarted) {
      this.sessionValue = new PluginUiSession(this.identity, {
        initialRenderSeq: this.sessionValue.renderHighWater,
        initialIntentSeq: this.sessionValue.intentHighWater,
      })
    }
    this.hasStarted = true
    this.outboundMessageSeq = 0
    this.inboundMessageSeq = 0
    this.inboundMessageIds.clear()
    this.transition('starting')

    let worker: PluginWorkerLike
    try {
      worker = this.workerFactory(this.workerUrl)
      assertWorkerLike(worker)
    } catch (error) {
      const failure = asError(error, 'worker_factory_failed')
      this.notify(failure, true)
      this.transition('failed')
      throw failure
    }

    const lifecycle: WorkerLifecycle = { token: {}, worker }
    this.lifecycle = lifecycle
    worker.onmessage = event => {
      // MessageEvent.data is deliberately treated as an unknown trust-boundary value.
      const value: unknown = event.data
      this.receiveFromWorker(lifecycle, value)
    }
    worker.onerror = event => this.handleWorkerError(lifecycle, asError(event, 'worker_error'))
    worker.onmessageerror = () => this.handleWorkerError(lifecycle, new Error('worker_message_clone_failed'))

    try {
      this.watchdog.arm(worker)
      this.sendInit(lifecycle)
      if (this.lifecycle === lifecycle) this.transition('running')
      return worker
    } catch (error) {
      const failure = asError(error, 'worker_start_failed')
      this.notify(failure, true)
      this.terminateLifecycle(lifecycle, true)
      this.transition('failed')
      throw failure
    }
  }

  stop(reason: ValidatedPluginUiDisposeBody['reason'] = 'normal_shutdown'): boolean {
    const lifecycle = this.lifecycle
    if (lifecycle === null) {
      this.transition('stopped')
      return false
    }
    try {
      this.sendDispose(reason, protocolDeadline(this.now), lifecycle)
    } catch (error) {
      this.notify(asError(error, 'worker_dispose_failed'), true)
    } finally {
      this.terminateLifecycle(lifecycle, true)
      this.transition('stopped')
    }
    return true
  }

  dispose(reason: ValidatedPluginUiDisposeBody['reason'] = 'normal_shutdown'): boolean {
    return this.stop(reason)
  }

  sendEvent(input: PluginUiHostEventInput): ValidatedPluginUiEventMessage {
    const body = this.sessionValue.createHostEvent(input)
    return this.sendHostMessage('intent', body) as ValidatedPluginUiEventMessage
  }

  sendValidatedEvent(body: Readonly<ValidatedPluginUiEventBody>): ValidatedPluginUiEventMessage {
    const validated = this.sessionValue.validateHostEvent(body)
    return this.sendHostMessage('intent', validated) as ValidatedPluginUiEventMessage
  }

  sendAck(
    ack: Readonly<PluginUIAck> | Readonly<RecordedIntentAck>,
    lifecycle = this.requireLifecycle(),
  ): ValidatedPluginUiMessage {
    const body = isRecordedAck(ack)
      ? ingestPluginUiAck(toPluginUiAck(ack))
      : ingestPluginUiAck(ack)
    return this.sendHostMessage('ack', body, lifecycle)
  }

  sendError(body: Readonly<ValidatedPluginUiErrorBody>): ValidatedPluginUiMessage {
    return this.sendHostMessage('error', body)
  }

  sendDispose(
    reason: ValidatedPluginUiDisposeBody['reason'],
    deadlineAt = protocolDeadline(this.now),
    lifecycle = this.requireLifecycle(),
  ): ValidatedPluginUiMessage {
    const body: ValidatedPluginUiDisposeBody = {
      schema: 'plugin-ui-dispose/v1',
      ui_session_id: this.identity.uiSessionId,
      reason,
      deadline_at: deadlineAt,
    }
    return this.sendHostMessage('dispose', body, lifecycle)
  }

  /** Direct test/adapter ingress using the same path as MessageEvent.data. */
  receiveMessage(value: unknown): void {
    const lifecycle = this.lifecycle
    if (lifecycle !== null) this.receiveFromWorker(lifecycle, value)
  }

  handleMessage(value: unknown): void {
    this.receiveMessage(value)
  }

  private sendInit(lifecycle: WorkerLifecycle): void {
    const body: ValidatedPluginUiInitBody = {
      schema: 'plugin-ui-init/v1',
      ui_session_id: this.identity.uiSessionId,
      initial_render_seq: this.sessionValue.renderHighWater,
      initial_intent_seq: this.sessionValue.intentHighWater,
      contribution_config_asset_id: this.contributionConfigAssetId,
    }
    this.sendHostMessage('init', body, lifecycle)
  }

  private sendHostMessage(
    messageType: 'init' | 'intent' | 'ack' | 'error' | 'dispose',
    body: ValidatedPluginUiInitBody | ValidatedPluginUiEventBody | PluginUIAck | ValidatedPluginUiErrorBody | ValidatedPluginUiDisposeBody,
    lifecycle = this.requireLifecycle(),
  ): ValidatedPluginUiMessage {
    if (this.lifecycle !== lifecycle) throw new Error('worker_host_lifecycle_fenced')
    this.outboundMessageSeq += 1
    this.messageIdCounter += 1
    const wire = deepCloneFreeze({
      schema: 'plugin-ui-message/v1',
      message_id: `host-message-${this.messageIdCounter}`,
      direction: 'host_to_worker',
      message_seq: this.outboundMessageSeq,
      message_type: messageType,
      worker_instance_id: this.identity.workerInstanceId,
      plugin_release_id: this.identity.plugin_release_id,
      generation_id: this.identity.generation_id,
      contribution_id: this.identity.contributionId,
      slot: this.identity.slot,
      workspace_id: this.identity.workspace_id,
      workspace_revision_id: this.identity.workspace_revision_id,
      plan_revision_id: this.identity.plan_revision_id,
      body,
    })
    const validated = ingestPluginUiMessage(wire)
    lifecycle.worker.postMessage(wire)
    return validated
  }

  private receiveFromWorker(lifecycle: WorkerLifecycle, value: unknown): void {
    if (this.lifecycle !== lifecycle) return
    let message: ValidatedPluginUiMessage
    try {
      message = ingestPluginUiMessage(value)
      this.assertWorkerMessage(message)
    } catch (error) {
      this.protocolFailure(lifecycle, asError(error, 'worker_message_rejected'))
      return
    }
    this.inboundMessageSeq = message.messageSeq
    this.inboundMessageIds.add(message.messageId)
    this.watchdog.refresh(lifecycle.worker)
    try {
      this.dispatchMessage(lifecycle, message)
    } catch (error) {
      this.protocolFailure(lifecycle, asError(error, 'worker_message_processing_failed'))
    }
  }

  private assertWorkerMessage(
    message: ValidatedPluginUiMessage,
  ): asserts message is Extract<ValidatedPluginUiMessage, { direction: 'worker_to_host' }> {
    if (message.direction !== 'worker_to_host') throw new Error('worker_message_direction_rejected')
    if (message.workerInstanceId !== this.identity.workerInstanceId
      || message.pluginReleaseId !== this.identity.plugin_release_id
      || message.generationId !== this.identity.generation_id
      || message.contributionId !== this.identity.contributionId
      || message.slot !== this.identity.slot
      || message.workspaceId !== this.identity.workspace_id
      || message.workspaceRevisionId !== this.identity.workspace_revision_id
      || message.planRevisionId !== this.identity.plan_revision_id) {
      throw new Error('worker_message_identity_mismatch')
    }
    if (message.messageSeq !== this.inboundMessageSeq + 1) {
      throw new Error('worker_message_sequence_rejected')
    }
    if (this.inboundMessageIds.has(message.messageId)) {
      throw new Error('worker_message_id_replayed')
    }
  }

  private dispatchMessage(
    lifecycle: WorkerLifecycle,
    message: Extract<ValidatedPluginUiMessage, { direction: 'worker_to_host' }>,
  ): void {
    switch (message.messageType) {
      case 'render': {
        const tree = this.sessionValue.installValidatedTree(message.body)
        try {
          this.options.onRender?.(tree, message)
        } catch (error) {
          this.notify(asError(error, 'plugin_render_handler_failed'), false)
        }
        return
      }
      case 'intent':
        this.dispatchIntent(lifecycle, message)
        return
      case 'error':
        try {
          this.options.onError?.(new Error(`${message.body.code}:${message.body.message}`))
        } catch (error) {
          this.notify(asError(error, 'plugin_error_handler_failed'), false)
        }
        return
      default:
        throw new Error('worker_message_type_rejected')
    }
  }

  private dispatchIntent(
    lifecycle: WorkerLifecycle,
    message: Extract<ValidatedPluginUiMessage, { direction: 'worker_to_host'; messageType: 'intent' }>,
  ): void {
    const decision = this.sessionValue.decideValidatedIntent(message.body)
    if (decision.kind === 'ack') {
      const ackBody = toPluginUiAck(decision.ack)
      const ack = this.sendAck(ackBody, lifecycle)
      this.notifyAck(ackBody, ack)
      return
    }
    if (decision.kind === 'pending') return
    if (this.options.onIntent === undefined) {
      this.completeIntentFailure(lifecycle, decision.intent, new Error('plugin_intent_handler_unavailable'))
      return
    }
    let result: IntentHandlerResult
    try {
      result = this.options.onIntent(decision.intent, message)
    } catch (error) {
      this.completeIntentFailure(lifecycle, decision.intent, asError(error, 'plugin_intent_handler_failed'))
      return
    }
    if (isPromiseLike(result)) {
      void result.then(
        value => this.completeIntentResult(lifecycle, decision.intent, value),
        error => this.completeIntentFailure(lifecycle, decision.intent, asError(error, 'plugin_intent_handler_failed')),
      )
    } else {
      this.completeIntentResult(lifecycle, decision.intent, result)
    }
  }

  private completeIntentResult(
    lifecycle: WorkerLifecycle,
    intent: Readonly<PluginUIIntent>,
    result: unknown,
  ): void {
    if (this.lifecycle !== lifecycle) return
    if (result === undefined) {
      this.completeIntentFailure(lifecycle, intent, new Error('plugin_intent_handler_result_missing'))
      return
    }
    try {
      const ack = isRecordedAck(result) ? toPluginUiAck(result) : ingestPluginUiAck(result)
      const completed = this.sessionValue.completeValidatedIntent(intent, ack)
      const ackBody = toPluginUiAck(completed)
      const sent = this.sendAck(ackBody, lifecycle)
      this.notifyAck(ackBody, sent)
    } catch (error) {
      this.completeIntentFailure(lifecycle, intent, asError(error, 'plugin_intent_ack_invalid'))
    }
  }

  private completeIntentFailure(
    lifecycle: WorkerLifecycle,
    intent: Readonly<PluginUIIntent>,
    error: Error,
  ): void {
    if (this.lifecycle !== lifecycle) return
    const ack: Readonly<RecordedIntentAck> = deepCloneFreeze({
      intentId: intent.intent_id,
      accepted: false,
      errorCode: '1010',
      coreEventSeq: null,
      jobId: null,
    })
    try {
      const completed = this.sessionValue.completeValidatedIntent(intent, ack)
      const ackBody = toPluginUiAck(completed)
      const sent = this.sendAck(ackBody, lifecycle)
      this.notifyAck(ackBody, sent)
    } catch (ackError) {
      this.protocolFailure(lifecycle, asError(ackError, 'plugin_intent_failure_ack_rejected'))
    }
    try {
      this.options.onError?.(error)
    } catch (handlerError) {
      this.notify(asError(handlerError, 'plugin_intent_error_handler_failed'), false)
    }
  }

  private notifyAck(ack: Readonly<PluginUIAck>, message: ValidatedPluginUiMessage): void {
    try {
      this.options.onAck?.(ack, message)
    } catch (error) {
      this.notify(asError(error, 'plugin_ack_handler_failed'), false)
    }
  }

  private handleWorkerError(lifecycle: WorkerLifecycle, error: Error): void {
    if (this.lifecycle !== lifecycle) return
    this.notify(error, true)
    this.terminateLifecycle(lifecycle, true)
    this.transition('failed')
  }

  private protocolFailure(lifecycle: WorkerLifecycle, error: Error): void {
    if (this.lifecycle !== lifecycle) return
    this.notify(error, true)
    this.terminateLifecycle(lifecycle, true)
    this.transition('failed')
  }

  private handleWatchdogTimeout(worker: WatchdogWorker): void {
    const lifecycle = this.lifecycle
    if (lifecycle === null || lifecycle.worker !== worker) return
    lifecycle.worker.onmessage = null
    lifecycle.worker.onerror = null
    lifecycle.worker.onmessageerror = null
    this.lifecycle = null
    const error = new Error('plugin_worker_watchdog_timeout')
    try {
      this.options.onWatchdogTimeout?.(lifecycle.worker)
    } catch (handlerError) {
      this.notify(asError(handlerError, 'watchdog_handler_failed'), false)
    }
    this.notify(error, false)
    this.transition('failed')
  }

  private terminateLifecycle(lifecycle: WorkerLifecycle, terminate: boolean): void {
    if (this.lifecycle !== lifecycle) return
    this.lifecycle = null
    this.watchdog.disarm(lifecycle.worker)
    lifecycle.worker.onmessage = null
    lifecycle.worker.onerror = null
    lifecycle.worker.onmessageerror = null
    if (terminate) {
      try {
        lifecycle.worker.terminate()
      } catch (error) {
        this.notify(asError(error, 'worker_terminate_failed'), false)
      }
    }
  }

  private requireLifecycle(): WorkerLifecycle {
    if (this.lifecycle === null) throw new Error('worker_host_not_running')
    return this.lifecycle
  }

  private notify(error: Error, protocol: boolean): void {
    if (protocol) {
      try {
        this.options.onProtocolError?.(error)
      } catch {
        // Error reporting is deliberately isolated from the Slot lifecycle.
      }
    }
    try {
      this.options.onError?.(error)
    } catch {
      // Error reporting is deliberately isolated from the Slot lifecycle.
    }
  }

  private transition(state: PluginWorkerHostState): void {
    this.stateValue = state
    try {
      this.options.onStateChange?.(state)
    } catch {
      // State observers cannot affect the Worker lifecycle.
    }
  }
}

export { PluginWorkerHost as WorkerHost }
