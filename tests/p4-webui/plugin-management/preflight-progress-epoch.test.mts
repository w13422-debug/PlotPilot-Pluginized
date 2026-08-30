import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  beginSnapshotRefresh,
  commitSnapshotRefresh,
  createSnapshotRefreshState,
  failSnapshotRefresh,
  interruptInstallOperation,
  isInstallOperationProcessing,
  isInstallOperationTerminal,
  PluginModelError,
  preflightPluginArchive,
} from '../../../frontend/src/core/plugins/model.ts'
import type { InstallLifecycleState, InstallOperationProjection, PluginPlan } from '../../../frontend/src/core/plugins/types.ts'

const plan = (): PluginPlan => JSON.parse(readFileSync('contracts/examples/fixtures/plugin-plan.json', 'utf8'))
const release = (marker: string, overrides = {}) => ({
  plugin_id: `plugin-${marker}`,
  release_id: marker.repeat(64),
  version: '1.0.0',
  package_hash: marker.repeat(64),
  lifecycle_state: 'installed',
  package_present: true,
  active: true,
  lkg: false,
  pin_count: 1,
  ...overrides,
})
const snapshot = (marker: string, overrides = {}) => ({
  releases: [release(marker, overrides)], plans: [plan()], selected_plan: plan(), operation: null,
})
const operation = (state: InstallLifecycleState, overrides: Partial<InstallOperationProjection> = {}): InstallOperationProjection => ({
  operation_id: 'install-1', state, completed: 2, total: null, failure_code: null, stream_status: 'connected', ...overrides,
})

test('preflight accepts zip and ppplugin with a canonical root manifest', () => {
  assert.equal(preflightPluginArchive('plugin.ZIP', ['plugin.json', 'src/main.js']).manifest_path, 'plugin.json')
  const stripped = preflightPluginArchive('插件.ppplugin', ['Package/plugin.json', 'Package/src/main.js'])
  assert.equal(stripped.stripped_root, 'Package')
  assert.deepEqual(stripped.normalized_entries, ['plugin.json', 'src/main.js'])
  const withDirectoryMarkers = preflightPluginArchive('plugin.zip', ['Package/', 'Package/plugin.json', 'Package/src/', 'Package/src/main.js'])
  assert.deepEqual(withDirectoryMarkers.normalized_entries, ['plugin.json', 'src/main.js'])
})

test('preflight uses frozen NFC/full-casefold identities and rejects collisions', () => {
  assert.throws(() => preflightPluginArchive('plugin.zip', ['plugin.json', 'Straße.txt', 'strasse.txt']),
    (error: unknown) => error instanceof PluginModelError && error.code === 'install_archive_path_collision')
  assert.throws(() => preflightPluginArchive('plugin.zip', ['plugin.json', 'é.txt', 'e\u0301.txt']),
    (error: unknown) => error instanceof PluginModelError && error.code === 'install_archive_path_collision')
  const frozenDistinct = preflightPluginArchive('plugin.ppplugin', ['plugin.json', '\uA7CB.txt', '\u0264.txt'])
  assert.equal(frozenDistinct.normalized_entries.length, 3)
})

test('preflight rejects nested-only, ambiguous roots, and non-package extensions', () => {
  for (const [name, entries, code] of [
    ['plugin.zip', ['dependency/plugin.json', 'other/file.txt'], 'install_root_manifest_required'],
    ['plugin.zip', ['one/plugin.json', 'two/file.txt'], 'install_root_manifest_required'],
    ['plugin.tar', ['plugin.json'], 'install_archive_extension_invalid'],
  ] as const) {
    assert.throws(() => preflightPluginArchive(name, entries), (error: unknown) =>
      error instanceof PluginModelError && error.code === code)
  }
})

test('preflight preserves normalizeWindowsPath negative corpus', () => {
  const invalid = [
    'dir\\file.txt', '/absolute.txt', 'C:/drive.txt', 'a//b.txt', './file.txt', '../file.txt',
    'bad?.txt', 'trailing. ', 'CON.txt', `${'a'.repeat(241)}.txt`,
  ]
  for (const entry of invalid) {
    assert.throws(() => preflightPluginArchive('plugin.zip', ['plugin.json', entry]), (error: unknown) =>
      error instanceof PluginModelError && error.code === 'install_archive_path_invalid', entry)
  }
  assert.throws(() => preflightPluginArchive('plugin.zip', ['plugin.json', 'A.txt', 'a.txt']),
    (error: unknown) => error instanceof PluginModelError && error.code === 'install_archive_path_collision')
})

test('five lifecycle terminal states and failure codes stop unknown-total processing', () => {
  for (const state of ['lkg_promoted', 'failed', 'superseded', 'rolled_back', 'safe_mode'] as const) {
    assert.equal(isInstallOperationTerminal(operation(state)), true, state)
    assert.equal(isInstallOperationProcessing(operation(state), true, 'install-1'), false, state)
  }
  assert.equal(isInstallOperationProcessing(operation('qualified'), true, 'install-1'), true)
  assert.equal(isInstallOperationProcessing(operation('qualified'), false, 'install-1'), false)
  assert.equal(isInstallOperationProcessing(operation('qualified'), true, 'other'), false)
  assert.equal(isInstallOperationProcessing(operation('qualified', { failure_code: 'install_failed' }), true, 'install-1'), false)
})

test('stream interruption stops processing without forging lifecycle authority', () => {
  const current = operation('qualified')
  const interrupted = interruptInstallOperation(current)
  assert.equal(interrupted.state, 'qualified')
  assert.equal(interrupted.failure_code, null)
  assert.equal(interrupted.stream_status, 'interrupted')
  assert.equal(isInstallOperationProcessing(interrupted, true, 'install-1'), false)
})

test('snapshot epoch rejects stale success and preserves all new release projections', () => {
  let state = createSnapshotRefreshState()
  const first = beginSnapshotRefresh(state); state = first.state
  const second = beginSnapshotRefresh(state); state = second.state
  state = commitSnapshotRefresh(state, second.epoch, snapshot('b', {
    lifecycle_state: 'retiring', package_present: false, active: false, lkg: true, pin_count: 7,
  }))
  state = commitSnapshotRefresh(state, first.epoch, snapshot('a', {
    lifecycle_state: 'installed', package_present: true, active: true, lkg: false, pin_count: 1,
  }))
  const visible = state.snapshot?.releases[0]
  assert.deepEqual(visible && {
    plugin_id: visible.plugin_id, lifecycle_state: visible.lifecycle_state, package_present: visible.package_present,
    active: visible.active, lkg: visible.lkg, pin_count: visible.pin_count,
  }, { plugin_id: 'plugin-b', lifecycle_state: 'retiring', package_present: false, active: false, lkg: true, pin_count: 7 })
  assert.equal(state.committed_epoch, second.epoch)
  assert.equal(state.loading_epoch, null)
})

test('stale error/finalizer cannot clear or overwrite the current request state', () => {
  let state = createSnapshotRefreshState(snapshot('a'))
  const first = beginSnapshotRefresh(state); state = first.state
  const second = beginSnapshotRefresh(state); state = second.state
  const afterOldError = failSnapshotRefresh(state, first.epoch, new Error('old failure'))
  assert.equal(afterOldError.loading_epoch, second.epoch)
  assert.equal(afterOldError.error, null)
  state = commitSnapshotRefresh(afterOldError, second.epoch, snapshot('b'))
  const afterOldFinalizer = failSnapshotRefresh(state, first.epoch, new Error('old finally'))
  assert.equal(afterOldFinalizer.snapshot?.releases[0]?.plugin_id, 'plugin-b')
  assert.equal(afterOldFinalizer.error, null)
  assert.equal(afterOldFinalizer.loading_epoch, null)
})
