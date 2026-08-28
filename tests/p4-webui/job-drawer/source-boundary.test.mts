import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const root = new URL('../../../', import.meta.url)

test('TaskDrawer source presents Job, every Step and every Attempt with all three explicit intents', async () => {
  const source = [
    await readFile(new URL('frontend/src/components/jobs/TaskDrawer.vue', root), 'utf8'),
    await readFile(new URL('frontend/src/core/jobPresentation.ts', root), 'utf8'),
  ].join('\n')
  for (const needle of ['job.steps', 'job.attempts', "emit('resume'", "emit('retry'", "emit('cancel'", 'jobProgressPercentage', 'presentConnectionState']) {
    assert.match(source, new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  }
  assert.match(source, /部分完成/)
  assert.match(source, /需要处理/)
})

test('source-only Job core uses injected ports and contains no invented API route or shared shell binding', async () => {
  const files = ['controller.ts', 'types.ts', 'store.ts', 'ingress.ts']
  const source = (await Promise.all(files.map(file => readFile(new URL(`frontend/src/core/jobs/${file}`, root), 'utf8')))).join('\n')
  assert.match(source, /interface JobDrawerGateway/)
  assert.doesNotMatch(source, /\/api\//)
  assert.doesNotMatch(source, /EventSource\s*\(/)
  assert.doesNotMatch(source, /useRouter|use.*Store|Publication/)
})
