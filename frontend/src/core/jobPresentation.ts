export type JobState =
  | 'queued'
  | 'running'
  | 'waiting_user'
  | 'paused'
  | 'cancelling'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'cancelled'
  | 'needs_attention'

/** Host-only drawer view model. It is not a Core wire DTO. */
export interface JobDrawerItem {
  job_id: string
  workspace_id: string
  job_state: JobState
  job_revision: number
  steps: Array<{ step_id: string; state: string; revision: number }>
  attempts: Array<{ attempt_id: string; state: string; lease_epoch: number }>
  job_event_high_water: number
}

export type CapabilityStatus = 'ready' | 'running' | 'disabled' | 'missing' | 'error'

export interface StatusPresentation {
  label: string
  tone: 'success' | 'info' | 'warning' | 'error' | 'default'
  description: string
}

const CAPABILITY_PRESENTATION: Record<CapabilityStatus, StatusPresentation> = {
  ready: { label: 'Ready', tone: 'success', description: '能力已就绪' },
  running: { label: 'Running', tone: 'info', description: '任务正在运行' },
  disabled: { label: 'Disabled', tone: 'warning', description: '插件已安装但未启用' },
  missing: { label: 'Missing', tone: 'default', description: '需要安装提供此能力的插件' },
  error: { label: 'Error', tone: 'error', description: '能力发生错误，可查看详情或重试' },
}

export function presentCapabilityStatus(status: CapabilityStatus): StatusPresentation {
  return CAPABILITY_PRESENTATION[status]
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
    case 'failed': return CAPABILITY_PRESENTATION.error
    case 'cancelled': return { label: '已取消', tone: 'default', description: '任务已取消' }
    case 'needs_attention': return { label: '需要处理', tone: 'error', description: '任务需要用户检查' }
  }
}

export function jobProgress(snapshot: JobDrawerItem): { completed: number; total: number } {
  const completedStates = new Set(['succeeded', 'partial', 'failed', 'cancelled'])
  return {
    completed: snapshot.steps.filter(step => completedStates.has(step.state)).length,
    total: snapshot.steps.length,
  }
}

export function canCancelJob(state: JobState): boolean {
  return state === 'queued' || state === 'running' || state === 'waiting_user' || state === 'paused'
}

export function canResumeJob(state: JobState): boolean {
  return state === 'waiting_user' || state === 'paused' || state === 'needs_attention' || state === 'partial' || state === 'failed'
}
