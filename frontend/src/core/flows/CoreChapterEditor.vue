<template>
  <section class="core-chapter-editor">
    <header class="core-chapter-editor__header">
      <div class="core-chapter-editor__identity">
        <span class="core-chapter-editor__book">{{ bookTitle }}</span>
        <strong>{{ hasChapter ? chapterTitle : '请选择章节' }}</strong>
      </div>
      <n-button
        size="small"
        type="primary"
        :disabled="!hasChapter || !dirty || loading"
        :loading="saving"
        @click="$emit('save')"
      >
        保存
      </n-button>
    </header>

    <n-spin :show="loading" class="core-chapter-editor__spin" description="加载章节…">
      <n-input
        v-if="hasChapter"
        class="core-chapter-editor__input"
        type="textarea"
        :value="modelValue"
        :disabled="saving"
        :autosize="false"
        placeholder="在这里撰写章节正文…"
        @update:value="$emit('update:modelValue', $event)"
      />
      <div v-else class="core-chapter-editor__empty">
        从左侧章节列表选择一章开始写作
      </div>
    </n-spin>
  </section>
</template>

<script setup lang="ts">
defineProps<{
  bookTitle: string
  chapterTitle: string
  modelValue: string
  hasChapter: boolean
  dirty: boolean
  loading: boolean
  saving: boolean
}>()

defineEmits<{
  'update:modelValue': [value: string]
  save: []
}>()
</script>

<style scoped>
.core-chapter-editor {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--app-surface);
}

.core-chapter-editor__header {
  min-height: 52px;
  padding: 8px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  border-bottom: 1px solid var(--plotpilot-split-border);
}

.core-chapter-editor__identity {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.core-chapter-editor__identity strong,
.core-chapter-editor__book {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.core-chapter-editor__book {
  color: var(--app-text-muted);
  font-size: 12px;
}

.core-chapter-editor__spin {
  flex: 1;
  min-height: 0;
}

.core-chapter-editor__spin :deep(.n-spin-content),
.core-chapter-editor__input {
  height: 100%;
  min-height: 0;
}

.core-chapter-editor__input :deep(textarea) {
  height: 100% !important;
  min-height: 100% !important;
  padding: 24px clamp(20px, 5vw, 72px);
  border: 0;
  border-radius: 0;
  font-size: 16px;
  line-height: 1.9;
  resize: none;
}

.core-chapter-editor__empty {
  height: 100%;
  display: grid;
  place-items: center;
  color: var(--app-text-muted);
}
</style>
