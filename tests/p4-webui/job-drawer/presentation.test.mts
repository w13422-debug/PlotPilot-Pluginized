import assert from 'node:assert/strict'
import test from 'node:test'
import {
  canCancelJob,
  canResumeJob,
  canRetryJob,
  jobActionAvailability,
  jobProgress,
  jobProgressPercentage,
  presentAttemptState,
  presentJobState,
  presentStepState,
} from '../../../frontend/src/core/jobPresentation.ts'
import { toJobDrawerItem } from '../../../frontend/src/core/jobs/types.ts'
import type { JobState } from '../../../frontend/src/contracts/types.ts'
import { snapshot, withState } from './fixtures.ts'

test('presents all ten frozen Job states without folding partial or needs_attention into failed', () => {
  const states = ['queued', 'running', 'waiting_user', 'paused', 'cancelling', 'succeeded', 'partial', 'failed', 'cancelled', 'needs_attention'] as const
  const labels = states.map(state => presentJobState(state).label)
  assert.equal(new Set(labels).size, states.length)
  assert.equal(presentJobState('partial').label, '部分完成')
  assert.equal(presentJobState('needs_attention').label, '需要处理')
  assert.equal(presentJobState('failed').tone, 'error')
})

test('presents every authoritative Step and Attempt state while preserving future-state fallback', () => {
  const steps = {
    pending: ['等待中', 'default', '步骤尚未开始'],
    running: ['执行中', 'info', '步骤正在执行'],
    waiting_user: ['等待确认', 'warning', '步骤等待用户处理'],
    paused: ['已暂停', 'warning', '步骤已暂停'],
    succeeded: ['已完成', 'success', '步骤已完成'],
    partial: ['部分完成', 'warning', '步骤产生了部分结果'],
    failed: ['失败', 'error', '步骤执行失败'],
    cancelled: ['已取消', 'default', '步骤已取消'],
    interrupted: ['已中断', 'warning', '步骤被中断，可查看任务详情'],
    needs_attention: ['需要处理', 'error', '步骤需要用户检查'],
  } as const
  for (const [state, expected] of Object.entries(steps)) {
    const actual = presentStepState(state)
    assert.deepEqual([actual.label, actual.tone, actual.description], expected, `Step ${state}`)
  }

  const attempts = {
    created: ['已创建', 'default', 'Attempt 等待执行'],
    running: ['执行中', 'info', 'Attempt 正在执行'],
    cancelling: ['取消中', 'warning', 'Attempt 正在安全停止'],
    succeeded: ['已完成', 'success', 'Attempt 已完成'],
    partial: ['部分完成', 'warning', 'Attempt 产生了部分结果'],
    failed: ['失败', 'error', 'Attempt 执行失败'],
    cancelled: ['已取消', 'default', 'Attempt 已取消'],
    interrupted: ['已中断', 'warning', 'Attempt 被中断'],
    suspended: ['已挂起', 'warning', 'Attempt 已挂起，等待恢复条件'],
    fenced: ['已隔离', 'error', 'Attempt 已被 fencing 隔离'],
  } as const
  for (const [state, expected] of Object.entries(attempts)) {
    const actual = presentAttemptState(state)
    assert.deepEqual([actual.label, actual.tone, actual.description], expected, `Attempt ${state}`)
  }
  assert.deepEqual(presentStepState('future_step'), {
    label: 'future_step',
    tone: 'default',
    description: '后端报告的步骤状态',
  })
  assert.deepEqual(presentAttemptState('future_attempt'), {
    label: 'future_attempt',
    tone: 'default',
    description: '后端报告的 Attempt 状态',
  })
})

test('calculates progress for empty, mixed, partial and unknown Step states', () => {
  assert.deepEqual(jobProgress({ steps: [] }), { completed: 0, total: 0 })
  assert.equal(jobProgressPercentage({ steps: [] }), 0)
  const mixed = { steps: [
    { step_id: '1', state: 'succeeded', revision: 1 },
    { step_id: '2', state: 'partial', revision: 1 },
    { step_id: '3', state: 'needs_attention', revision: 1 },
    { step_id: '4', state: 'future_state', revision: 1 },
  ] }
  assert.deepEqual(jobProgress(mixed), { completed: 2, total: 4 })
  assert.equal(jobProgressPercentage(mixed), 50)
  assert.equal(presentStepState('future_state').label, 'future_state')
  assert.equal(presentAttemptState('future_attempt').label, 'future_attempt')
})

test('enforces the ten-state action truth table and checkpoint-gated resume', () => {
  const truthTable: Record<JobState, { cancel: boolean; resume: boolean; retry: boolean }> = {
    queued: { cancel: true, resume: false, retry: false },
    running: { cancel: true, resume: false, retry: false },
    waiting_user: { cancel: true, resume: true, retry: false },
    paused: { cancel: true, resume: true, retry: false },
    cancelling: { cancel: false, resume: false, retry: false },
    succeeded: { cancel: false, resume: false, retry: false },
    partial: { cancel: false, resume: false, retry: true },
    failed: { cancel: false, resume: false, retry: true },
    cancelled: { cancel: false, resume: false, retry: false },
    needs_attention: { cancel: false, resume: true, retry: false },
  }
  for (const [state, expected] of Object.entries(truthTable) as Array<[JobState, typeof truthTable[JobState]]>) {
    assert.deepEqual(jobActionAvailability({
      job_state: state,
      current_checkpoint_id: 'checkpoint-1',
    }), expected, state)
    assert.equal(canCancelJob(state), expected.cancel)
    assert.equal(canResumeJob(state), expected.resume)
    assert.equal(canRetryJob(state), expected.retry)
    assert.equal(jobActionAvailability({ job_state: state, current_checkpoint_id: null }).resume, false)
  }

  const failed = toJobDrawerItem(withState('failed', { current_checkpoint_id: 'checkpoint-1' }))
  assert.deepEqual(jobActionAvailability(failed), { cancel: false, resume: false, retry: true })
  assert.doesNotMatch(presentJobState('failed').description, /继续|检查点/)
  assert.doesNotMatch(presentJobState('partial').description, /继续|检查点/)
  const pausedWithoutCheckpoint = toJobDrawerItem(withState('paused'))
  assert.deepEqual(jobActionAvailability(pausedWithoutCheckpoint), { cancel: true, resume: false, retry: false })
  assert.equal(snapshot().job_state, 'running')
})
