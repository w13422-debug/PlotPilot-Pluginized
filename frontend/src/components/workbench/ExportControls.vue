<script setup lang="ts">
import type { FeatureAvailability } from '../../core/workbench/FeatureRuntimeGateway.ts'
defineProps<{ availability: FeatureAvailability }>()
</script>
<template>
  <section class="feature-panel" data-feature-surface="export">
    <h2>导出</h2><p>导出需由 Jobs v2 返回权威 Artifact 定位符。</p>
    <button type="button" :disabled="!availability.available">导出当前作品</button>
    <p v-if="!availability.available" class="feature-reason" role="status">{{ availability.reason }}</p>
  </section>
</template>