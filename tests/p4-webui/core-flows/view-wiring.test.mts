import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const home = readFileSync(new URL('../../../frontend/src/views/Home.vue', import.meta.url), 'utf8')
const workbench = readFileSync(new URL('../../../frontend/src/views/Workbench.vue', import.meta.url), 'utf8')
const runtime = readFileSync(new URL('../../../frontend/src/core/flows/runtime.ts', import.meta.url), 'utf8')

test('Home is wired to Core flows without a wizard or legacy project authority', () => {
  assert.match(home, /requireCoreFlowRuntime\(\)\.listProjects\(\)/)
  assert.match(home, /requireCoreFlowRuntime\(\)\.createProject/)
  assert.match(home, /requireCoreFlowRuntime\(\)\.deleteProject/)
  assert.doesNotMatch(home, /novelApi|NovelSetupGuide|isWizardCompleted|setupWizard|StatsSidebar/)
  assert.match(home, /仅创建权威 Core Workspace/)
})

test('Workbench keeps the three-pane split while using document identity for open/edit/save', () => {
  assert.equal((workbench.match(/<n-split/g) ?? []).length, 2)
  assert.match(workbench, /<CoreChapterList/)
  assert.match(workbench, /<CoreChapterEditor/)
  assert.match(workbench, /currentDocumentId/)
  assert.match(workbench, /route\.query\.chapter !== documentId/)
  assert.match(workbench, /requireCoreFlowRuntime\(\)\.saveChapter/)
  assert.doesNotMatch(workbench, /useWorkbench|novelApi|chapterApi|<WorkArea|<ChapterList|<StatsTopBar|<SettingsPanel/)
})

test('runtime composition has no default fake, legacy fallback, or implicit HTTP mount', () => {
  assert.match(runtime, /let runtime: CoreFlows \| null = null/)
  assert.match(runtime, /P0-owned runtime composition/)
  assert.doesNotMatch(runtime, /createCoreHttpFlowGateway|novelApi|chapterApi/)
})
