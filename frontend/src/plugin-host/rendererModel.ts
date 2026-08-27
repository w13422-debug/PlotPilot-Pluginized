import type { HostRenderNode, PluginUiComponent } from './slotHost.ts'

export type RendererKind = 'layout' | 'display' | 'form' | 'asset' | 'core_native'

const RENDERER_KINDS: Record<PluginUiComponent, RendererKind> = {
  stack: 'layout', text: 'display', progress: 'display',
  input: 'form', textarea: 'form', select: 'asset',
  button: 'form', table: 'asset', tabs: 'asset', diff: 'asset', tree: 'asset', graph: 'asset',
  candidate_preview: 'core_native',
}

export interface RenderDescriptor {
  component: PluginUiComponent
  kind: RendererKind
  requiresAssetResolver: boolean
  requiresCoreNativeControl: boolean
}

export function describeRenderer(node: HostRenderNode): RenderDescriptor {
  const component = node.component as PluginUiComponent
  const kind = RENDERER_KINDS[component]
  if (!kind) throw new Error(`unknown_component:${node.component}`)
  return {
    component,
    kind,
    requiresAssetResolver: kind === 'asset',
    requiresCoreNativeControl: kind === 'core_native',
  }
}
