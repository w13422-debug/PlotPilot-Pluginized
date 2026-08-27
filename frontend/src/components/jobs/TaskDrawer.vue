<script setup lang="ts">
import { computed, ref } from 'vue'
import { NButton, NCollapse, NCollapseItem, NDrawer, NDrawerContent, NEmpty, NProgress, NTag } from 'naive-ui'
import {
  canCancelJob,
  canResumeJob,
  jobProgress,
  presentCapabilityStatus,
  presentJobState,
  type JobSnapshotV1,
} from '../../core/jobPresentation'

const props = withDefaults(defineProps<{ jobs?: JobSnapshotV1[] }>(), { jobs: () => [] })
const emit = defineEmits<{
  cancel: [jobId: string]
  resume: [jobId: string]
  retry: [jobId: string]
}>()
const open = ref(false)
const activeJobs = computed(() => props.jobs.filter(job => !['succeeded', 'cancelled'].includes(job.job_state)).length)
const ready = presentCapabilityStatus('ready')
</script>

<template>
  <n-button class="task-drawer-trigger" secondary strong aria-label="打开任务抽屉" @click="open = true">
    任务<span v-if="activeJobs"> · {{ activeJobs }}</span>
  </n-button>
  <n-drawer v-model:show="open" :width="420" placement="right">
    <n-drawer-content title="任务">
      <n-empty v-if="jobs.length === 0" description="暂无任务">
        <template #extra>
          <n-tag :type="ready.tone">{{ ready.label }}</n-tag>
          <span class="ready-copy">{{ ready.description }}</span>
        </template>
      </n-empty>

      <article v-for="job in jobs" :key="job.job_id" class="job-card">
        <header>
          <strong>{{ job.job_id }}</strong>
          <n-tag :type="presentJobState(job.job_state).tone" size="small">
            {{ presentJobState(job.job_state).label }}
          </n-tag>
        </header>
        <p>{{ presentJobState(job.job_state).description }}</p>
        <n-progress
          type="line"
          :percentage="jobProgress(job).total ? Math.round(jobProgress(job).completed / jobProgress(job).total * 100) : 0"
          :indicator-placement="'inside'"
        />
        <div class="job-actions">
          <n-button v-if="canResumeJob(job.job_state)" size="small" @click="emit('resume', job.job_id)">继续</n-button>
          <n-button v-if="job.job_state === 'failed'" size="small" @click="emit('retry', job.job_id)">重试</n-button>
          <n-button v-if="canCancelJob(job.job_state)" size="small" tertiary @click="emit('cancel', job.job_id)">取消</n-button>
        </div>
        <n-collapse arrow-placement="right">
          <n-collapse-item title="技术详情" :name="job.job_id">
            <dl>
              <dt>Workspace</dt><dd>{{ job.workspace_id }}</dd>
              <dt>Revision</dt><dd>{{ job.job_revision }}</dd>
              <dt>Event high-water</dt><dd>{{ job.job_event_high_water }}</dd>
              <dt>Attempt</dt><dd>{{ job.attempts.at(-1)?.attempt_id ?? '—' }}</dd>
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
.job-card { padding: 14px 0; border-bottom: 1px solid var(--n-divider-color); }
.job-card header { display: flex; justify-content: space-between; gap: 12px; }
.job-card p { margin: 6px 0 10px; color: var(--n-text-color-2); }
.job-actions { display: flex; gap: 8px; margin: 10px 0; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 0; word-break: break-all; }
dt { color: var(--n-text-color-3); }
dd { margin: 0; }
</style>
