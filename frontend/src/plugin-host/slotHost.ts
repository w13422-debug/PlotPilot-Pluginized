export const PLUGIN_UI_SLOTS = [
  'library.import.action',
  'library.project-create.extension',
  'workbench.reference.panel',
  'workbench.writing-assets.panel',
  'workbench.generate.context',
  'workbench.story-state.panel',
  'workbench.quality.panel',
  'outline.visualization.panel',
  'job.drawer.detail',
  'settings.plugins.section',
  'settings.models.section',
] as const

export type PluginUiSlot = typeof PLUGIN_UI_SLOTS[number]

export const PLUGIN_UI_COMPONENT_RULES = {
  stack: { props: ['direction', 'row_gap'], events: [] },
  text: { props: ['text', 'tone'], events: [] },
  input: { props: ['label', 'value', 'placeholder', 'disabled'], events: ['change'] },
  textarea: { props: ['label', 'value', 'rows', 'disabled'], events: ['change'] },
  select: { props: ['label', 'value', 'options_asset_id', 'disabled'], events: ['change'] },
  button: { props: ['label', 'tone', 'disabled'], events: ['click'] },
  table: { props: ['columns_asset_id', 'rows_asset_id', 'empty_text'], events: ['select_row'] },
  tabs: { props: ['active_tab', 'tabs_asset_id'], events: ['change_tab'] },
  diff: { props: ['before_asset_id', 'after_asset_id', 'language'], events: [] },
  tree: { props: ['nodes_asset_id', 'selected_id'], events: ['select_node'] },
  graph: { props: ['graph_asset_id', 'layout'], events: ['select_node'] },
  progress: { props: ['label', 'completed', 'total', 'state'], events: [] },
  candidate_preview: { props: ['candidate_id', 'view_mode'], events: ['open_core_operation'] },
} as const

export type PluginUiComponent = keyof typeof PLUGIN_UI_COMPONENT_RULES

/** Internal renderer model produced only after a future P0 validator succeeds. */
export interface HostRenderNode {
  component: string
  key: string
  props: Record<string, unknown>
  children: HostRenderNode[]
  event_ids: string[]
}

export interface ValidatedHostTree {
  treeId: string
  renderSeq: number
  root: HostRenderNode
}

export function isPluginUiSlot(value: string): value is PluginUiSlot {
  return (PLUGIN_UI_SLOTS as readonly string[]).includes(value)
}
