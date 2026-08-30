<template>
  <section class="plugin-management">
    <header class="panel-header">
      <div>
        <h2>插件管理</h2>
        <p>只读投影与 Plan 草稿边界；所有权威 mutation 在依赖验收前保持停止。</p>
      </div>
      <n-button :loading="refresh.loading_epoch !== null" :disabled="!gateway" @click="loadSnapshot">刷新</n-button>
    </header>

    <n-alert v-if="!gateway" type="warning" :show-icon="true">
      Plugin Management gateway unavailable；未创建 fixture 或第二事实源。
    </n-alert>
    <n-alert v-else-if="refresh.error" type="error" :show-icon="true">{{ refresh.error }}</n-alert>

    <template v-if="refresh.snapshot">
      <div class="release-grid">
        <n-card v-for="release in refresh.snapshot.releases" :key="release.release_id" size="small">
          <strong>{{ release.plugin_id }}</strong>
          <p>{{ release.version }} · {{ release.lifecycle_state }}</p>
          <n-space size="small">
            <n-tag v-if="release.active" type="success" size="small">Active</n-tag>
            <n-tag v-if="release.lkg" type="info" size="small">LKG</n-tag>
            <n-tag v-if="release.pin_count" type="warning" size="small">{{ release.pin_count }} pins</n-tag>
          </n-space>
        </n-card>
      </div>

      <plugin-install-card
        :operation="refresh.snapshot.operation"
        :busy="refresh.loading_epoch !== null"
        :active-operation-id="refresh.snapshot.operation?.operation_id ?? null"
      />
      <plan-manager v-model="planEditor" />
    </template>
  </section>
</template>

<script setup lang="ts">
import { onMounted, shallowRef } from 'vue'
import PlanManager from './PlanManager.vue'
import PluginInstallCard from './PluginInstallCard.vue'
import type { PlanEditorState, PluginManagementReadGateway, SnapshotRefreshState } from '@/core/plugins/types.ts'
import {
  beginSnapshotRefresh,
  commitSnapshotRefresh,
  createPlanEditorState,
  createSnapshotRefreshState,
  failSnapshotRefresh,
  refreshPlanEditor,
} from '@/core/plugins/model.ts'

const props = defineProps<{ gateway?: PluginManagementReadGateway }>()
const refresh = shallowRef<SnapshotRefreshState>(createSnapshotRefreshState())
const planEditor = shallowRef<PlanEditorState>(createPlanEditorState())

async function loadSnapshot() {
  if (!props.gateway) return
  const begun = beginSnapshotRefresh(refresh.value)
  refresh.value = begun.state
  try {
    const value = await props.gateway.loadSnapshot()
    const committed = commitSnapshotRefresh(refresh.value, begun.epoch, value)
    refresh.value = committed
    if (committed.committed_epoch === begun.epoch) {
      planEditor.value = refreshPlanEditor(planEditor.value, committed.snapshot?.selected_plan ?? null)
    }
  } catch (error) {
    refresh.value = failSnapshotRefresh(refresh.value, begun.epoch, error)
  }
}

onMounted(loadSnapshot)
</script>

<style scoped>
.plugin-management { display: grid; gap: 1rem; }
.panel-header { display: flex; justify-content: space-between; gap: 1rem; align-items: start; }
.panel-header h2 { margin: 0; }
.panel-header p { margin: 0.35rem 0 0; color: var(--app-text-secondary); }
.release-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.7rem; }
.release-grid p { margin: 0.35rem 0; color: var(--app-text-secondary); }
</style>
