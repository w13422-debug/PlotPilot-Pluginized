import { createCoreFlows, type CoreFlowIdFactory, type CoreFlows } from './coreFlows.ts'
import type { CoreFlowGateway } from './gateway.ts'

export interface CoreFlowRuntimeBinding {
  gateway: CoreFlowGateway
  createdBy: string
  ids?: CoreFlowIdFactory
}

let runtime: CoreFlows | null = null

/**
 * P0-owned runtime composition calls this once after the authoritative Core
 * HTTP and actor seams exist. This source slice deliberately installs no
 * fallback, fake authority, or legacy API adapter.
 */
export function installCoreFlowRuntime(binding: CoreFlowRuntimeBinding): void {
  if (runtime !== null) throw new Error('Core flow runtime is already installed')
  if (binding.createdBy.trim().length === 0) throw new Error('Core flow runtime requires an authoritative created_by identity')
  runtime = createCoreFlows(binding)
}

export function getCoreFlowRuntime(): CoreFlows | null {
  return runtime
}

export function requireCoreFlowRuntime(): CoreFlows {
  const current = getCoreFlowRuntime()
  if (current === null) throw new Error('Core flow runtime integration is pending')
  return current
}
