import { JobDrawerController } from './controller.ts'
import { createJobHttpGateway, type JobHttpGatewayOptions } from './httpGateway.ts'
import type { JobDrawerGateway } from './types.ts'

export interface JobDrawerRuntime {
  controller: JobDrawerController
}

export interface JobDrawerRuntimeDependencies {
  createGateway?: () => JobDrawerGateway
  createController?: (gateway: JobDrawerGateway) => JobDrawerController
}

/** Compose the isolated Job drawer runtime without binding it to a page shell. */
export function createJobDrawerRuntime(
  options: JobHttpGatewayOptions = {},
  dependencies: JobDrawerRuntimeDependencies = {},
): JobDrawerRuntime {
  const createGateway = dependencies.createGateway ?? (() => createJobHttpGateway(options))
  const createController = dependencies.createController ?? (gateway => new JobDrawerController(gateway))
  return { controller: createController(createGateway()) }
}
