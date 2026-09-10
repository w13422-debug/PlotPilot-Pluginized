import { createPluginManagementHttpGateway } from './httpGateway.ts'
import type { PluginManagementReadGateway } from './types.ts'

export interface PluginManagementRuntime {
  gateway: PluginManagementReadGateway
}

export interface PluginManagementRuntimeCompositionDependencies {
  createGateway?: () => PluginManagementReadGateway
}

/**
 * Creates the one bounded production runtime consumed by the fixed settings
 * section. Dependency injection keeps Node verification deterministic without
 * introducing a fixture or a second production data source.
 */
export function createPluginManagementRuntimeComposition(
  dependencies: PluginManagementRuntimeCompositionDependencies = {},
): PluginManagementRuntime {
  const createGateway = dependencies.createGateway ?? createPluginManagementHttpGateway
  return { gateway: createGateway() }
}
