<template>
  <aside class="core-chapter-list">
    <header class="core-chapter-list__header">
      <n-button quaternary size="small" @click="$emit('back')">← 返回</n-button>
      <strong>章节</strong>
      <div class="core-chapter-list__actions">
        <n-button size="small" type="primary" :disabled="busy" @click="$emit('create')">新建章节</n-button>
        <n-button quaternary size="small" :disabled="busy" @click="$emit('refresh')">刷新</n-button>
      </div>
    </header>
    <n-scrollbar class="core-chapter-list__scroll">
      <button
        v-for="chapter in chapters"
        :key="chapter.documentId"
        class="core-chapter-list__item"
        :class="{ active: chapter.documentId === currentDocumentId }"
        :disabled="busy"
        type="button"
        @click="$emit('select', chapter.documentId)"
      >
        <span>{{ chapter.displayIndex }}</span>
        <strong>{{ chapter.title }}</strong>
      </button>
      <div v-if="chapters.length === 0" class="core-chapter-list__empty">暂无 Core 章节</div>
    </n-scrollbar>
  </aside>
</template>

<script setup lang="ts">
import type { CoreChapterListItem } from './coreFlows.ts'

defineProps<{
  chapters: readonly CoreChapterListItem[]
  currentDocumentId: string | null
  busy: boolean
}>()
defineEmits<{
  select: [documentId: string]
  create: []
  back: []
  refresh: []
}>()
</script>

<style scoped>
.core-chapter-list { height: 100%; min-height: 0; display: flex; flex-direction: column; background: var(--app-surface); border-right: 1px solid var(--plotpilot-split-border); }
.core-chapter-list__header { min-height: 52px; padding: 8px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--plotpilot-split-border); }
.core-chapter-list__actions { display: flex; align-items: center; gap: 2px; }
.core-chapter-list__scroll { flex: 1; min-height: 0; padding: 8px; }
.core-chapter-list__item { width: 100%; padding: 10px; display: grid; grid-template-columns: 28px 1fr; gap: 8px; text-align: left; color: inherit; border: 0; border-radius: 8px; background: transparent; cursor: pointer; }
.core-chapter-list__item:hover, .core-chapter-list__item.active { background: var(--app-surface-hover, rgba(99, 102, 241, 0.09)); }
.core-chapter-list__item span { color: var(--app-text-muted); }
.core-chapter-list__item strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.core-chapter-list__empty { padding: 24px 8px; text-align: center; color: var(--app-text-muted); }
</style>
