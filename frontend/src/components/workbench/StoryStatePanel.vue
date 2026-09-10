<script setup lang="ts">
import type { FeatureAvailability } from '../../core/workbench/FeatureRuntimeGateway.ts'
const props = defineProps<{ foreshadowAvailability: FeatureAvailability; storyBibleAvailability: FeatureAvailability }>()
const emit = defineEmits<{ refreshForeshadow: []; refreshStoryBible: [] }>()
function refreshForeshadow(): void { if (props.foreshadowAvailability.available) emit('refreshForeshadow') }
function refreshStoryBible(): void { if (props.storyBibleAvailability.available) emit('refreshStoryBible') }
</script>
<template>
  <section class="feature-panel" data-feature-surface="story-state">
    <section class="story-state-section" data-flow="FLOW-07" role="region" aria-label="FLOW-07：伏笔账本">
      <h2>伏笔账本</h2><p>FLOW-07：读取并核对伏笔状态。</p>
      <button type="button" :disabled="!foreshadowAvailability.available" @click="refreshForeshadow">刷新伏笔账本</button>
      <p v-if="!foreshadowAvailability.available" class="feature-reason" role="status">{{ foreshadowAvailability.reason }}</p>
    </section>
    <section class="story-state-section" data-flow="FLOW-08" role="region" aria-label="FLOW-08：故事演进与 Bible">
      <h2>故事演进 / Bible</h2><p>FLOW-08：读取故事演进与 Bible。</p>
      <button type="button" :disabled="!storyBibleAvailability.available" @click="refreshStoryBible">刷新故事演进 / Bible</button>
      <p v-if="!storyBibleAvailability.available" class="feature-reason" role="status">{{ storyBibleAvailability.reason }}</p>
    </section>
  </section>
</template>
