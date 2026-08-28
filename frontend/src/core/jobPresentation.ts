import type { JobState as ContractJobState } from '../contracts/types'
import type { JobAction, JobConnectionState, JobDrawerItem } from './jobs/types'

export type JobState = ContractJobState
export type { JobDrawerItem }

export type CapabilityStatus = 'ready' | 'running' | 'disabled' | 'missing' | 'error'

export interface StatusPresentation {
  label: string
  tone: 'success' | 'info' | 'warning' | 'error' | 'default'
  description: string
}

export interface JobActionAvailability extends Record<JobAction, boolean> {}

const CAPABILITY_PRESENTATION: Record<CapabilityStatus, StatusPresentation> = {
  ready: { label: 'Ready', tone: 'success', description: '能力已就绪' },
  running: { label: 'Running', tone: 'info', description: '任务正在运行' },
  disabled: { label: 'Disabled', tone: 'warning', description: '插件已安装但未启用' },
  missing: { label: 'Missing', tone: 'default', description: '需要安装提供此能力的插件' },
  error: { label: 'Error', tone: 'error', description: '能力发生错误，可查看详情或重试' },
}

const CONNECTION_PRESENTATION: Record<JobConnectionState, StatusPresentation> = {
  detached: { label: '未连接', tone: 'default', description: '当前没有事件连接' },
  connecting: { label: '连接中', tone: 'info', description: '正在连接任务事件流' },
  connected: { label: '已连接', tone: 'success', description: '正在接收任务事件' },
  reconnecting: { label: '重连中', tone: 'warning', description: '连接中断，正在从 durable cursor 重连' },
  needs_snapshot: { label: '同步快照', tone: 'warning', description: '事件存在缺口，正在替换为权威快照' },
  error: { label: '连接异常', tone: 'error', description: '事件恢复失败，等待重连' },
}

export function presentCapabilityStatus(status: CapabilityStatus): StatusPresentation {
  return CAPABILITY_PRESENTATION[status]
}

export function presentConnectionState(state: JobConnectionState): StatusPresentation {
  return CONNECTION_PRESENTATION[state]
}

export function presentJobState(state: JobState): StatusPresentation {
  switch (state) {
    case 'queued': return { label: '等待中', tone: 'default', description: '任务已进入队列' }
    case 'running': return CAPABILITY_PRESENTATION.running
    case 'waiting_user': return { label: '等待确认', tone: 'warning', description: '需要用户处理后继续' }
    case 'paused': return { label: '已暂停', tone: 'warning', description: '可从新 Attempt 继续' }
    case 'cancelling': return { label: '取消中', tone: 'warning', description: '正在安全停止任务' }
    case 'succeeded': return { label: '已完成', tone: 'success', description: '任务成功完成' }
    case 'partial': return { label: '部分完成', tone: 'warning', description: '已有部分结果，可检查后继续' }
    case 'failed': return { label: '失败', tone: 'error', description: '任务失败，可重试或从检查点继续' }
    case 'cancelled': return { label: '已取消', tone: 'default', description: '任务已取消' }
    case 'needs_attention': return { label: '需要处理', tone: 'error', description: '任务需要用户检查' }
  }
}

export function presentStepState(state: string): StatusPresentation {
  switch (state) {
    case 'pending':
    case 'queued': return { label: '等待中', tone: 'default', description: '步骤尚未开始' }
    case 'running': return { label: '执行中', tone: 'info', description: '步骤正在执行' }
    case 'waiting_user': return { label: '等待确认', tone: 'warning', description: '步骤等待用户处理' }
    case 'paused': return { label: '已暂停', tone: 'warning', description: '步骤已暂停' }
    case 'succeeded':
    case 'completed': return { label: '已完成', tone: 'success', description: '步骤已完成' }
    case 'partial': return { label: '部分完成', tone: 'warning', description: '步骤产生了部分结果' }
    case 'failed': return { label: '失败', tone: 'error', description: '步骤执行失败' }
    case 'cancelled': return { label: '已取消', tone: 'default', description: '步骤已取消' }
    case 'skipped': return { label: '已跳过', tone: 'default', description: '步骤未执行' }
    case 'needs_attention': return { label: '需要处理', tone: 'error', description: '步骤需要用户检查' }
    default: return { label: state, tone: 'default', description: '后端报告的步骤状态' }
  }
}

export function presentAttemptState(state: string): StatusPresentation {
  switch (state) {
    case 'created':
    case 'queued': return { label: '已创建', tone: 'default', description: 'Attempt 等待执行' }
    case 'running': return { label: '执行中', tone: 'info', description: 'Attempt 正在执行' }
    case 'succeeded':
    case 'completed': return { label: '已完成', tone: 'success', description: 'Attempt 已完成' }
    case 'failed': return { label: '失败', tone: 'error', description: 'Attempt 执行失败' }
    case 'cancelled': return { label: '已取消', tone: 'default', description: 'Attempt 已取消' }
    default: return { label: state, tone: 'default', description: '后端报告的 Attempt 状态' }
  }
}

export function jobProgress(snapshot: Pick<JobDrawerItem, 'steps'>): { completed: number; total: number } {
  const completedStates = new Set(['succeeded', 'completed', 'partial', 'failed', 'cancelled', 'skipped'])
  return {
    completed: snapshot.steps.filter(step => completedStates.has(step.state)).length,
    total: snapshot.steps.length,
  }
}

export function jobProgressPercentage(snapshot: Pick<JobDrawerItem, 'steps'>): number {
  const progress = jobProgress(snapshot)
  return progress.total === 0 ? 0 : Math.round(progress.completed / progress.total * 100)
}

export function canCancelJob(state: JobState): boolean {
  return state === 'queued' || state === 'running' || state === 'waiting_user' || state === 'paused'
}

export function canResumeJob(state: JobState): boolean {
  return state === 'waiting_user' || state === 'paused' || state === 'needs_attention'
    || state === 'partial' || state === 'failed'
}

export function canRetryJob(state: JobState): boolean {
  return state === 'failed' || state === 'partial' || state === 'needs_attention'
}

export function jobActionAvailability(
  job: Pick<JobDrawerItem, 'job_state' | 'current_checkpoint_id'>,
): JobActionAvailability {
  return {
    cancel: canCancelJob(job.job_state),
    resume: canResumeJob(job.job_state) && job.current_checkpoint_id != null,
    retry: canRetryJob(job.job_state),
  }
}
