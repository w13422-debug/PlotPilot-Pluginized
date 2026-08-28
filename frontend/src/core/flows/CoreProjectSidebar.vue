<template>
  <aside class="core-project-sidebar" :class="{ 'is-collapsed': collapsed }">
    <div class="core-project-sidebar__brand">{{ collapsed ? '墨' : '墨枢 · PlotPilot' }}</div>
    <n-button type="primary" block @click="$emit('createProject')">
      {{ collapsed ? '+' : '新建书目' }}
    </n-button>
    <div class="core-project-sidebar__count">
      <strong>{{ projectCount }}</strong>
      <span v-if="!collapsed">Core 项目</span>
    </div>
    <n-button quaternary block @click="$emit('refresh')">
      {{ collapsed ? '↻' : '刷新项目' }}
    </n-button>
    <n-button quaternary block @click="$emit('update:collapsed', !collapsed)">
      {{ collapsed ? '›' : '收起侧栏' }}
    </n-button>
  </aside>
</template>

<script setup lang="ts">
defineProps<{ projectCount: number; collapsed: boolean }>()
defineEmits<{
  createProject: []
  refresh: []
  'update:collapsed': [value: boolean]
}>()
</script>

<style scoped>
.core-project-sidebar {
  position: fixed;
  inset: 0 auto 0 0;
  z-index: 20;
  width: 300px;
  padding: 18px 14px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  border-right: 1px solid var(--app-divider, rgba(15, 23, 42, 0.08));
  background: var(--app-surface);
  transition: width 0.2s ease;
}
.core-project-sidebar.is-collapsed { width: 52px; padding-inline: 6px; }
.core-project-sidebar__brand { min-height: 38px; display: grid; place-items: center; font-weight: 700; }
.core-project-sidebar__count { display: flex; align-items: baseline; justify-content: center; gap: 6px; padding: 18px 4px; }
.core-project-sidebar__count strong { font-size: 24px; }
</style>
