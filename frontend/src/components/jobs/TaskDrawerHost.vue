<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NButton } from 'naive-ui'
import { createJobDrawerRuntime } from '../../core/jobs/composition.ts'
import type { JobAction, JobConnectionState, JobDrawerItem } from '../../core/jobs/types.ts'
import TaskDrawer from './TaskDrawer.vue'

const props = defineProps<{
  workspaceId: string
}>()

const controller = createJobDrawerRuntime().controller
const jobs = ref<JobDrawerItem[]>([])
const connectionStates = ref<Readonly<Record<string, JobConnectionState>>>({})
const pendingActions = ref<Record<string, JobAction | undefined>>({})
const actionErrors = ref<Record<string, string | undefined>>({})
const runtimeError = ref<string | null>(null)

let disposed = false
let workspaceGeneration = 0

function messageOf(error: unknown): string {
  return error instanceof Error && error.message.length > 0 ? error.message : '任务服务暂时不可用，请稍后重试。'
}

function syncFromController(): void {
  jobs.value = controller.store.items()
  connectionStates.value = controller.store.connectionStates()
}

const unsubscribe = controller.store.subscribe(syncFromController)

async function loadWorkspace(workspaceId: string): Promise<void> {
  const generation = ++workspaceGeneration
  runtimeError.value = null
  actionErrors.value = {}
  pendingActions.value = {}
  if (workspaceId.trim().length === 0) {
    controller.stop()
    syncFromController()
    return
  }
  try {
    await controller.start(workspaceId)
    if (disposed || generation !== workspaceGeneration) return
    syncFromController()
  } catch (error) {
    if (disposed || generation !== workspaceGeneration) return
    runtimeError.value = messageOf(error)
    syncFromController()
  }
}

async function refresh(): Promise<void> {
  runtimeError.value = null
  try {
    await controller.refresh()
    syncFromController()
  } catch (error) {
    runtimeError.value = messageOf(error)
  }
}

async function act(action: JobAction, jobId: string): Promise<void> {
  runtimeError.value = null
  actionErrors.value = { ...actionErrors.value, [jobId]: undefined }
  pendingActions.value = { ...pendingActions.value, [jobId]: action }
  try {
    await controller.act(action, jobId)
    syncFromController()
  } catch (error) {
    actionErrors.value = { ...actionErrors.value, [jobId]: messageOf(error) }
  } finally {
    const next = { ...pendingActions.value }
    delete next[jobId]
    pendingActions.value = next
  }
}

onMounted(() => {
  void loadWorkspace(props.workspaceId)
})

watch(
  () => props.workspaceId,
  (workspaceId, previousWorkspaceId) => {
    if (workspaceId === previousWorkspaceId) return
    void loadWorkspace(workspaceId)
  },
)

onBeforeUnmount(() => {
  disposed = true
  workspaceGeneration += 1
  unsubscribe()
  controller.stop()
})
</script>

<template>
  <section class="task-drawer-host" aria-label="任务运行状态">
    <div v-if="runtimeError" class="runtime-error" role="alert">
      <span>任务加载失败：{{ runtimeError }}</span>
      <n-button size="small" secondary @click="refresh">重试</n-button>
    </div>
    <TaskDrawer
      :jobs="jobs"
      :connection-states="connectionStates"
      :pending-actions="pendingActions"
      :action-errors="actionErrors"
      @refresh="refresh"
      @resume="act('resume', $event)"
      @retry="act('retry', $event)"
      @cancel="act('cancel', $event)"
    />
  </section>
</template>

<style scoped>
.runtime-error {
  position: fixed;
  right: 18px;
  bottom: 66px;
  z-index: 1001;
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: min(460px, calc(100vw - 36px));
  padding: 8px 10px;
  border: 1px solid var(--n-color-error);
  border-radius: 6px;
  background: var(--n-color-error-suppl);
  color: var(--n-color-error);
  font-size: 12px;
}
</style>
