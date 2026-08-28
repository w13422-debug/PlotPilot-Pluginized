<script setup lang="ts">
import { computed, ref } from 'vue'
import { NButton, NCollapse, NCollapseItem, NDrawer, NDrawerContent, NEmpty, NProgress, NTag } from 'naive-ui'
import {
  jobActionAvailability,
  jobProgress,
  jobProgressPercentage,
  presentAttemptState,
  presentCapabilityStatus,
  presentConnectionState,
  presentJobState,
  presentStepState,
  type JobDrawerItem,
} from '../../core/jobPresentation'
import type { JobAction, JobConnectionState } from '../../core/jobs/types'

const props = withDefaults(defineProps<{
  jobs?: JobDrawerItem[]
  connectionStates?: Readonly<Record<string, JobConnectionState>>
  pendingActions?: Readonly<Record<string, JobAction | undefined>>
  actionErrors?: Readonly<Record<string, string | undefined>>
}>(), {
  jobs: () => [],
  connectionStates: () => ({}),
  pendingActions: () => ({}),
  actionErrors: () => ({}),
})
const emit = defineEmits<{
  cancel: [jobId: string]
  resume: [jobId: string]
  retry: [jobId: string]
  refresh: []
}>()
const open = ref(false)
const unresolvedJobs = computed(() => props.jobs.filter(job => !['succeeded', 'cancelled'].includes(job.job_state)).length)
const ready = presentCapabilityStatus('ready')

function connectionFor(jobId: string): JobConnectionState {
  return props.connectionStates[jobId] ?? 'detached'
}

function isPending(jobId: string, action: JobAction): boolean {
  return props.pendingActions[jobId] === action
}
</script>

<template>
  <n-button class="task-drawer-trigger" secondary strong aria-label="打开任务抽屉" @click="open = true">
    任务<span v-if="unresolvedJobs"> · {{ unresolvedJobs }}</span>
  </n-button>
  <n-drawer v-model:show="open" :width="460" placement="right">
    <n-drawer-content title="任务">
      <div class="drawer-toolbar">
        <n-button text size="small" aria-label="刷新并重新挂接任务" @click="emit('refresh')">刷新</n-button>
      </div>
      <n-empty v-if="jobs.length === 0" description="暂无任务">
        <template #extra>
          <n-tag :type="ready.tone">{{ ready.label }}</n-tag>
          <span class="ready-copy">{{ ready.description }}</span>
        </template>
      </n-empty>

      <article v-for="job in jobs" :key="job.job_id" class="job-card" :data-job-id="job.job_id">
        <header>
          <strong>{{ job.job_id }}</strong>
          <span class="job-tags">
            <n-tag :type="presentConnectionState(connectionFor(job.job_id)).tone" size="small">
              {{ presentConnectionState(connectionFor(job.job_id)).label }}
            </n-tag>
            <n-tag :type="presentJobState(job.job_state).tone" size="small">
              {{ presentJobState(job.job_state).label }}
            </n-tag>
          </span>
        </header>
        <p>{{ presentJobState(job.job_state).description }}</p>
        <n-progress
          type="line"
          :percentage="jobProgressPercentage(job)"
          :indicator-placement="'inside'"
          :aria-label="`任务进度 ${jobProgress(job).completed}/${jobProgress(job).total}`"
        />
        <div class="progress-copy">{{ jobProgress(job).completed }} / {{ jobProgress(job).total }} 个步骤已结束</div>
        <p v-if="actionErrors[job.job_id]" class="action-error" role="alert">{{ actionErrors[job.job_id] }}</p>
        <div class="job-actions">
          <n-button
            v-if="jobActionAvailability(job).resume"
            size="small"
            :loading="isPending(job.job_id, 'resume')"
            :disabled="pendingActions[job.job_id] != null"
            @click="emit('resume', job.job_id)"
          >继续</n-button>
          <n-button
            v-if="jobActionAvailability(job).retry"
            size="small"
            :loading="isPending(job.job_id, 'retry')"
            :disabled="pendingActions[job.job_id] != null"
            @click="emit('retry', job.job_id)"
          >重试</n-button>
          <n-button
            v-if="jobActionAvailability(job).cancel"
            size="small"
            tertiary
            :loading="isPending(job.job_id, 'cancel')"
            :disabled="pendingActions[job.job_id] != null"
            @click="emit('cancel', job.job_id)"
          >取消</n-button>
        </div>
        <n-collapse arrow-placement="right">
          <n-collapse-item :title="`步骤 (${job.steps.length})`" :name="`${job.job_id}:steps`">
            <n-empty v-if="job.steps.length === 0" size="small" description="暂无步骤" />
            <ol v-else class="detail-list step-list">
              <li v-for="step in job.steps" :key="step.step_id">
                <span><strong>{{ step.step_id }}</strong><small> rev {{ step.revision }}</small></span>
                <n-tag :type="presentStepState(step.state).tone" size="small">{{ presentStepState(step.state).label }}</n-tag>
              </li>
            </ol>
          </n-collapse-item>
          <n-collapse-item :title="`Attempts (${job.attempts.length})`" :name="`${job.job_id}:attempts`">
            <n-empty v-if="job.attempts.length === 0" size="small" description="暂无 Attempt" />
            <ol v-else class="detail-list attempt-list">
              <li v-for="attempt in job.attempts" :key="attempt.attempt_id">
                <span><strong>{{ attempt.attempt_id }}</strong><small> lease {{ attempt.lease_epoch }}</small></span>
                <n-tag :type="presentAttemptState(attempt.state).tone" size="small">{{ presentAttemptState(attempt.state).label }}</n-tag>
              </li>
            </ol>
          </n-collapse-item>
          <n-collapse-item title="技术详情" :name="`${job.job_id}:technical`">
            <dl>
              <dt>Workspace</dt><dd>{{ job.workspace_id }}</dd>
              <dt>Revision</dt><dd>{{ job.job_revision }}</dd>
              <dt>Event high-water</dt><dd>{{ job.job_event_high_water }}</dd>
              <dt>Checkpoint</dt><dd>{{ job.current_checkpoint_id ?? '—' }}</dd>
              <dt>Candidates</dt><dd>{{ job.candidate_ids.length ? job.candidate_ids.join(', ') : '—' }}</dd>
              <dt>Snapshot</dt><dd>{{ job.source_snapshot_hash }}</dd>
            </dl>
          </n-collapse-item>
        </n-collapse>
      </article>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.task-drawer-trigger { position: fixed; right: 18px; bottom: 18px; z-index: 1000; }
.ready-copy { margin-left: 8px; color: var(--n-text-color-2); }
.drawer-toolbar { display: flex; justify-content: flex-end; margin-bottom: 8px; }
.job-card { padding: 14px 0; border-bottom: 1px solid var(--n-divider-color); }
.job-card header, .detail-list li { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.job-card p { margin: 6px 0 10px; color: var(--n-text-color-2); }
.job-tags { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.progress-copy { margin-top: 4px; color: var(--n-text-color-3); font-size: 12px; text-align: right; }
.job-actions { display: flex; gap: 8px; margin: 10px 0; }
.action-error { color: var(--n-color-error); font-size: 12px; }
.detail-list { display: grid; gap: 8px; margin: 0; padding-left: 22px; }
.detail-list small { margin-left: 6px; color: var(--n-text-color-3); }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 0; word-break: break-all; }
dt { color: var(--n-text-color-3); }
dd { margin: 0; }
</style>
