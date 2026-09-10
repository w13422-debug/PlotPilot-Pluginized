<script setup lang="ts">
import type { FeatureAvailability } from '../../core/workbench/FeatureRuntimeGateway.ts'
const props = defineProps<{ availability: FeatureAvailability; jobDrawerAvailability: FeatureAvailability }>()
const emit = defineEmits<{ showTaskStatus: [] }>()
function showTaskStatus(): void {
  if (props.jobDrawerAvailability.available) emit('showTaskStatus')
}
</script>
<template>
  <section class="feature-panel" data-feature-surface="generation">
    <h2>内容生成</h2><p>生成必须绑定 Jobs v2 的权威 RunSnapshot。</p>
    <button type="button" :disabled="!availability.available">开始生成</button>
    <p v-if="!availability.available" class="feature-reason" role="status">{{ availability.reason }}</p>
    <button type="button" :disabled="!jobDrawerAvailability.available" @click="showTaskStatus">任务状态</button>
    <p v-if="!jobDrawerAvailability.available" class="feature-reason" role="status">{{ jobDrawerAvailability.reason }}</p>
  </section>
</template>
