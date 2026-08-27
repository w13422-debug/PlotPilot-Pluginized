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

export interface PluginUiTreeNode {
  component: string
  key: string
  props: Record<string, unknown>
  children: PluginUiTreeNode[]
  event_ids: string[]
}

export interface PluginUiTreeV1 {
  schema: 'plugin-ui-tree/v1'
  tree_id: string
  render_seq: number
  root: PluginUiTreeNode
}

export interface InstalledTree {
  treeId: string
  renderSeq: number
  tree: PluginUiTreeV1
}

export type TreeValidation = { ok: true } | { ok: false; code: string; path: string }

export function isPluginUiSlot(value: string): value is PluginUiSlot {
  return (PLUGIN_UI_SLOTS as readonly string[]).includes(value)
}

function sameMembers(actual: string[], expected: readonly string[]): boolean {
  return actual.length === expected.length && actual.every(value => expected.includes(value))
}

function validateNode(node: PluginUiTreeNode, path: string): TreeValidation {
  if (!(node.component in PLUGIN_UI_COMPONENT_RULES)) {
    return { ok: false, code: 'unknown_component', path: `${path}.component` }
  }
  const rule = PLUGIN_UI_COMPONENT_RULES[node.component as PluginUiComponent]
  const propKeys = Object.keys(node.props)
  if (!sameMembers(propKeys, rule.props)) return { ok: false, code: 'invalid_props', path: `${path}.props` }
  if (!node.event_ids.every(event => (rule.events as readonly string[]).includes(event))) {
    return { ok: false, code: 'unknown_event', path: `${path}.event_ids` }
  }
  for (let index = 0; index < node.children.length; index += 1) {
    const result = validateNode(node.children[index], `${path}.children[${index}]`)
    if (!result.ok) return result
  }
  return { ok: true }
}

export function validatePluginUiTree(tree: PluginUiTreeV1): TreeValidation {
  if (tree.schema !== 'plugin-ui-tree/v1') return { ok: false, code: 'invalid_schema', path: 'schema' }
  if (!Number.isInteger(tree.render_seq) || tree.render_seq < 1) return { ok: false, code: 'invalid_render_seq', path: 'render_seq' }
  return validateNode(tree.root, 'root')
}

export function installPluginUiTree(current: InstalledTree | null, tree: PluginUiTreeV1): InstalledTree {
  const validation = validatePluginUiTree(tree)
  if (!validation.ok) throw new Error(`${validation.code}:${validation.path}`)
  if (current && tree.render_seq <= current.renderSeq) throw new Error('stale_render_seq')
  return { treeId: tree.tree_id, renderSeq: tree.render_seq, tree }
}
