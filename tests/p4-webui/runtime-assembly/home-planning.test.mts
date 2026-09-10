import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import {
  FeatureUnavailableError,
  candidateInputDisabled,
  candidateOperationEnabled,
  createFeatureRuntimeGateway,
  runWorkspaceOwnedCandidateOperation,
  type CoreV2CandidateGateway,
} from '../../../frontend/src/core/workbench/FeatureRuntimeGateway.ts'

const root = new URL('../../../', import.meta.url)

async function source(relative: string): Promise<string> {
  return readFile(new URL(relative, root), 'utf8')
}

test('F1 new Workspace uses the existing direct Home-to-Workbench route without a wizard', async () => {
  const home = await source('frontend/src/views/Home.vue')
  assert.match(home, /requireCoreFlowRuntime\(\)\.createProject\(newBook\.value\.title\)/)
  assert.match(home, /await router\.push\(`\/book\/\$\{result\.workspaceId\}\/workbench`\)/)
  assert.doesNotMatch(home, /wizard|onboarding|import.*token/i)
  assert.doesNotMatch(home, /novelApi|chapterApi|bibleApi|foreshadowApi/i)
})

test('F2 unavailable plugin-dependent surfaces stay visible, disabled, reasoned, and make zero requests', async () => {
  const componentPaths = [
    'frontend/src/components/workbench/SetupPlanningPanel.vue',
    'frontend/src/components/workbench/GenerationControls.vue',
    'frontend/src/components/workbench/CandidateReviewPanel.vue',
    'frontend/src/components/workbench/StoryStatePanel.vue',
    'frontend/src/components/workbench/CheckpointRecoveryPanel.vue',
    'frontend/src/components/workbench/ExportControls.vue',
  ]
  const sources = await Promise.all(componentPaths.map(source))
  for (const component of sources) {
    assert.match(component, /<section/)
    assert.match(component, /feature-reason/)
  }
  assert.match(sources[0]!, /:disabled="!availability\.available"/)
  assert.match(sources[1]!, /:disabled="!availability\.available"/)
  assert.match(sources[2]!, /:disabled="!canRun"/)
  assert.match(sources[2]!, /:disabled="inputDisabled"/)
  assert.match(sources[4]!, /:disabled="!availability\.available"/)
  assert.match(sources[5]!, /:disabled="!availability\.available"/)

  const unavailable = { available: false, reason: 'not mounted' }
  const available = { available: true, reason: '' }
  assert.equal(candidateInputDisabled(unavailable, false), true)
  assert.equal(candidateInputDisabled(available, true), true)
  assert.equal(candidateInputDisabled(available, false), false)
  assert.equal(candidateOperationEnabled(unavailable, false, 'candidate-1'), false)
  assert.equal(candidateOperationEnabled(available, true, 'candidate-1'), false)
  assert.equal(candidateOperationEnabled(available, false, '   '), false)
  assert.equal(candidateOperationEnabled(available, false, 'candidate-1'), true)

  let requests = 0
  const core: CoreV2CandidateGateway = {
    previewCandidate: async () => { requests += 1; return {} },
    reviewCandidate: async () => { requests += 1; return {} },
    acceptCandidate: async () => { requests += 1; return {} },
  }
  const runtime = createFeatureRuntimeGateway({ coreV2CandidateGateway: core })
  for (const capability of Object.values(runtime.availability)) assert.equal(capability.available, false)
  await assert.rejects(
    runtime.previewCandidate({ workspaceId: 'ws-1', candidateId: 'candidate-1' }),
    error => error instanceof FeatureUnavailableError && error.surface === 'candidate',
  )
  assert.equal(requests, 0)
})

test('F3 Jobs availability mounts only the accepted TaskDrawerHost seam', async () => {
  const workbench = await source('frontend/src/views/Workbench.vue')
  const generation = await source('frontend/src/components/workbench/GenerationControls.vue')
  const runtime = createFeatureRuntimeGateway({
    availability: { jobDrawer: { available: true, reason: '' } },
  })
  assert.equal(runtime.availability.jobDrawer.available, true)
  assert.match(workbench, /import TaskDrawerHost from '\.\.\/components\/jobs\/TaskDrawerHost\.vue'/)
  assert.match(workbench, /v-if="featureAvailability\.jobDrawer\.available"/)
  assert.match(workbench, /<TaskDrawerHost :workspace-id="slug" \/>/)
  assert.doesNotMatch(workbench, /import TaskDrawer from/)
  assert.match(generation, /defineEmits<\{ showTaskStatus: \[\] \}>/)
  assert.match(generation, /@click="showTaskStatus"/)
  assert.match(workbench, /@show-task-status="showTaskDrawer"/)
  assert.match(workbench, /taskDrawerAnchor\.value/)
  assert.match(workbench, /scrollIntoView\(\{ block: 'nearest' \}\)/)
  assert.match(workbench, /focus\(\{ preventScroll: true \}\)/)
})

test('Candidate Workspace ownership suppresses stale result, error, and busy mutations', async () => {
  const workbench = await source('frontend/src/views/Workbench.vue')
  assert.match(workbench, /runWorkspaceOwnedCandidateOperation\(/)
  assert.match(workbench, /isWorkspaceCurrent\(token, workspaceId\)/)

  let current = true
  let release: ((value: string) => void) | undefined
  const pendingResult = new Promise<string>(resolve => { release = resolve })
  const staleEffects: string[] = []
  const pending = runWorkspaceOwnedCandidateOperation(
    () => current,
    () => pendingResult,
    {
      onStart: () => staleEffects.push('start'),
      onSuccess: result => staleEffects.push(`success:${result}`),
      onError: () => staleEffects.push('error'),
      onSettled: () => staleEffects.push('settled'),
    },
  )
  assert.deepEqual(staleEffects, ['start'])
  current = false
  release!('late')
  await pending
  assert.deepEqual(staleEffects, ['start'])

  current = true
  const currentEffects: string[] = []
  await runWorkspaceOwnedCandidateOperation(
    () => current,
    async () => 'current',
    {
      onStart: () => currentEffects.push('start'),
      onSuccess: result => currentEffects.push(`success:${result}`),
      onError: () => currentEffects.push('error'),
      onSettled: () => currentEffects.push('settled'),
    },
  )
  assert.deepEqual(currentEffects, ['start', 'success:current', 'settled'])

  let reject: ((reason?: unknown) => void) | undefined
  const pendingError = new Promise<string>((_resolve, rejectPromise) => { reject = rejectPromise })
  const staleErrorEffects: string[] = []
  const failed = runWorkspaceOwnedCandidateOperation(
    () => current,
    () => pendingError,
    {
      onStart: () => staleErrorEffects.push('start'),
      onSuccess: () => staleErrorEffects.push('success'),
      onError: () => staleErrorEffects.push('error'),
      onSettled: () => staleErrorEffects.push('settled'),
    },
  )
  current = false
  reject!(new Error('late failure'))
  await failed
  assert.deepEqual(staleErrorEffects, ['start'])
})
