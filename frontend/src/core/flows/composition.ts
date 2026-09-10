import { createCoreHttpFlowGateway, type CoreFlowGateway } from './gateway.ts'
import { installCoreFlowRuntime, type CoreFlowRuntimeBinding } from './runtime.ts'

/** Stable attribution for the approved local-only, single-user WebUI. */
export const WEBUI_CORE_FLOW_ACTOR_ID = 'plotpilot.webui.local'

export interface CoreFlowRuntimeCompositionDependencies {
  createGateway?: () => CoreFlowGateway
  installRuntime?: (binding: CoreFlowRuntimeBinding) => void
}

function requireWebUiCoreFlowActor(): string {
  const actorId = WEBUI_CORE_FLOW_ACTOR_ID.trim()
  if (actorId.length === 0) throw new Error('Core WebUI actor binding must not be empty')
  return actorId
}

/**
 * Installs the accepted Core HTTP gateway before Vue mounts. Omitting gateway
 * options deliberately keeps frozen absolute /api/v1 routes on same-origin fetch.
 */
export function installCoreFlowRuntimeComposition(
  dependencies: CoreFlowRuntimeCompositionDependencies = {},
): void {
  const createdBy = requireWebUiCoreFlowActor()
  const createGateway = dependencies.createGateway ?? createCoreHttpFlowGateway
  const installRuntime = dependencies.installRuntime ?? installCoreFlowRuntime
  installRuntime({ gateway: createGateway(), createdBy })
}
