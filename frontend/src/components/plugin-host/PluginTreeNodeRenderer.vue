<script setup lang="ts">
import { computed } from 'vue'
import { NAlert, NButton, NInput, NProgress, NSpace, NText } from 'naive-ui'
import { describeRenderer } from '../../plugin-host/rendererModel.ts'
import type { HostRenderNode } from '../../plugin-host/slotHost.ts'

const props = withDefaults(defineProps<{ node: HostRenderNode; interactive?: boolean }>(), { interactive: false })
const emit = defineEmits<{ event: [event: { actionId: string; eventType: string }] }>()
const descriptor = computed(() => describeRenderer(props.node))
const p = computed(() => props.node.props)

function emitFirst(eventType: string) {
  const actionId = props.node.event_ids[0]
  if (actionId) emit('event', { actionId, eventType })
}
</script>

<template>
  <n-space
    v-if="descriptor.component === 'stack'"
    :vertical="p.direction === 'vertical'"
    :size="Number(p.row_gap)"
  >
    <PluginTreeNodeRenderer
      v-for="child in node.children"
      :key="child.key"
      :node="child"
      :interactive="interactive"
      @event="emit('event', $event)"
    />
  </n-space>

  <n-text v-else-if="descriptor.component === 'text'" :type="p.tone === 'error' ? 'error' : undefined">
    {{ p.text }}
  </n-text>

  <n-input
    v-else-if="descriptor.component === 'input'"
    :value="String(p.value)"
    :placeholder="String(p.placeholder)"
    :disabled="Boolean(p.disabled) || !interactive"
    :aria-label="String(p.label)"
    @update:value="emitFirst('change')"
  />

  <n-input
    v-else-if="descriptor.component === 'textarea'"
    type="textarea"
    :value="String(p.value)"
    :rows="Number(p.rows)"
    :disabled="Boolean(p.disabled) || !interactive"
    :aria-label="String(p.label)"
    @update:value="emitFirst('change')"
  />

  <n-button
    v-else-if="descriptor.component === 'button'"
    :type="p.tone === 'primary' ? 'primary' : 'default'"
    :disabled="Boolean(p.disabled) || !interactive"
    @click="emitFirst('click')"
  >
    {{ p.label }}
  </n-button>

  <n-progress
    v-else-if="descriptor.component === 'progress'"
    type="line"
    :percentage="p.total == null ? 0 : Math.round(Number(p.completed) / Math.max(Number(p.total), 1) * 100)"
    :processing="p.state === 'running'"
  >
    {{ p.label }}
  </n-progress>

  <n-alert v-else-if="descriptor.requiresAssetResolver" type="info" :show-icon="false">
    此组件需要版本化 Asset resolver；合同进入集成头后启用。
  </n-alert>

  <n-alert v-else-if="descriptor.requiresCoreNativeControl" type="info" :show-icon="false">
    Candidate 审核仅通过 Core 原生控件提供。
  </n-alert>
</template>
