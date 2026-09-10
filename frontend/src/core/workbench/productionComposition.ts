import type { App } from 'vue'
import {
  createCoreV2CandidateHttpGateway,
  createFeatureRuntimeGateway,
  FEATURE_RUNTIME_GATEWAY_KEY,
  type CoreV2CandidateHttpGatewayOptions,
  type CoreV2CandidateGateway,
  type FeatureRuntimeGateway,
} from './FeatureRuntimeGateway.ts'
import { WEBUI_CORE_FLOW_ACTOR_ID } from '../flows/composition.ts'

export interface ProductionFeatureRuntimeCompositionDependencies {
  createCandidateGateway?: (options: CoreV2CandidateHttpGatewayOptions) => CoreV2CandidateGateway
  createRuntime?: typeof createFeatureRuntimeGateway
  fetch?: typeof globalThis.fetch
}

/** Install the fixed production feature surface on one Vue application. */
export function installProductionFeatureRuntimeComposition(
  app: App,
  dependencies: ProductionFeatureRuntimeCompositionDependencies = {},
): FeatureRuntimeGateway {
  const createCandidateGateway = dependencies.createCandidateGateway ?? createCoreV2CandidateHttpGateway
  const candidateGateway = createCandidateGateway({
    fetch: dependencies.fetch ?? globalThis.fetch,
    acceptedBy: WEBUI_CORE_FLOW_ACTOR_ID,
  })
  const createRuntime = dependencies.createRuntime ?? createFeatureRuntimeGateway
  const runtime = createRuntime({
    availability: {
      candidate: { available: true, reason: '' },
      jobDrawer: { available: true, reason: '' },
    },
    coreV2CandidateGateway: candidateGateway,
  })

  app.provide(FEATURE_RUNTIME_GATEWAY_KEY, runtime)
  return runtime
}
