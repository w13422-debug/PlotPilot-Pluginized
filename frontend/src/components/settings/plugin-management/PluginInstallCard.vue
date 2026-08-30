<template>
  <n-card size="small" title="安装与生命周期" :bordered="true">
    <n-alert type="info" :show-icon="true">
      Plugin API G2 与 Runtime Composition 尚未接线；本代仅保留权威预检和只读进度投影，不模拟安装、启停或退休。
    </n-alert>
    <div v-if="operation" class="operation">
      <div class="operation-head">
        <strong>{{ operation.operation_id }}</strong>
        <n-tag size="small">{{ operation.state }}</n-tag>
      </div>
      <n-progress
        type="line"
        :percentage="percentage"
        :processing="processing"
        :status="operation.failure_code ? 'error' : 'default'"
      />
      <p v-if="operation.stream_status === 'interrupted'" class="attention">连接已中断，等待权威快照对账。</p>
    </div>
    <n-button class="install-button" disabled>选择 .zip / .ppplugin</n-button>
  </n-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { InstallOperationProjection } from '@/core/plugins/types.ts'
import { isInstallOperationProcessing } from '@/core/plugins/model.ts'

const props = defineProps<{
  operation: InstallOperationProjection | null
  busy: boolean
  activeOperationId: string | null
}>()

const percentage = computed(() => {
  if (!props.operation || props.operation.total === null || props.operation.total === 0) return 0
  return Math.min(100, Math.round((props.operation.completed / props.operation.total) * 100))
})
const processing = computed(() => props.operation
  ? isInstallOperationProcessing(props.operation, props.busy, props.activeOperationId)
  : false)
</script>

<style scoped>
.operation { margin-top: 0.8rem; }
.operation-head { display: flex; justify-content: space-between; margin-bottom: 0.5rem; }
.attention { color: var(--warning-color); margin: 0.4rem 0 0; }
.install-button { margin-top: 0.8rem; }
</style>
