<script setup lang="ts">
import type { FeatureAvailability } from '../../core/workbench/FeatureRuntimeGateway.ts'
defineProps<{ availability: FeatureAvailability }>()
</script>
<template>
  <section class="feature-panel" data-feature-surface="checkpoint-recovery">
    <h2>检查点恢复</h2><p>已挂接的 Jobs v2 任务可在任务抽屉中恢复。</p>
    <button type="button" :disabled="!availability.available">恢复检查点</button>
    <p v-if="!availability.available" class="feature-reason" role="status">{{ availability.reason }}</p>
  </section>
</template>