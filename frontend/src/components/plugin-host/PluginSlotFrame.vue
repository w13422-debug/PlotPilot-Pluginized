<script setup lang="ts">
import { onErrorCaptured, ref, watch } from 'vue'
import { NAlert, NCard, NSpin, NTag } from 'naive-ui'
import PluginTreeNodeRenderer from './PluginTreeNodeRenderer.vue'
import { presentCapabilityStatus, type CapabilityStatus } from '../../core/jobPresentation.ts'
import type { PluginUiTreeV1, PluginUiSlot } from '../../plugin-host/slotHost.ts'

const props = withDefaults(defineProps<{
  slot: PluginUiSlot
  status: CapabilityStatus
  tree?: PluginUiTreeV1 | null
  interactive?: boolean
}>(), { tree: null, interactive: false })
const emit = defineEmits<{ event: [event: { actionId: string; eventType: string }] }>()
const renderError = ref<string | null>(null)

watch(() => props.tree?.tree_id, () => { renderError.value = null })
onErrorCaptured(error => {
  renderError.value = error instanceof Error ? error.message : 'Slot render failed'
  return false
})
</script>

<template>
  <n-card size="small" class="plugin-slot" :data-slot="slot">
    <template #header>
      <span class="slot-title">{{ slot }}</span>
      <n-tag :type="presentCapabilityStatus(renderError ? 'error' : status).tone" size="small">
        {{ presentCapabilityStatus(renderError ? 'error' : status).label }}
      </n-tag>
    </template>

    <n-alert v-if="renderError" type="error" title="插件界面加载失败">
      当前 Slot 已隔离；固定导航和其他页面仍可使用。
    </n-alert>
    <n-spin v-else-if="status === 'running'" size="small" description="插件界面加载中…" />
    <n-alert v-else-if="status === 'missing'" type="default">需要安装提供此能力的插件。</n-alert>
    <n-alert v-else-if="status === 'disabled'" type="warning">插件已安装但未启用。</n-alert>
    <n-alert v-else-if="status === 'error'" type="error">插件界面不可用；仅当前 Slot 受影响。</n-alert>
    <PluginTreeNodeRenderer
      v-else-if="status === 'ready' && tree"
      :node="tree.root"
      :interactive="interactive"
      @event="emit('event', $event)"
    />
  </n-card>
</template>

<style scoped>
.plugin-slot :deep(.n-card-header__main) { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.slot-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
