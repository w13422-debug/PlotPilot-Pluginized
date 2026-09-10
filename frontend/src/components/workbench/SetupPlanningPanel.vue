<script setup lang="ts">
import type { FeatureAvailability } from '../../core/workbench/FeatureRuntimeGateway.ts'
defineProps<{ availability: FeatureAvailability }>()
</script>
<template>
  <section class="feature-panel" data-feature-surface="planning">
    <h2>项目规划</h2><p>为当前 Workspace 生成项目规划。</p>
    <button type="button" :disabled="!availability.available">开始规划</button>
    <p v-if="!availability.available" class="feature-reason" role="status">{{ availability.reason }}</p>
  </section>
</template>