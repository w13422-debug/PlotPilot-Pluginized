<template>
  <section class="plan-manager">
    <header class="plan-header">
      <div>
        <h3>Plan 编辑器</h3>
        <p>编辑会生成新的 immutable revision；当前仅准备 CAS 请求，不发布真实保存。</p>
      </div>
      <n-tag v-if="modelValue.draft" size="small" :bordered="false">
        {{ modelValue.draft.plan_id }} · r{{ modelValue.draft.revision }}
      </n-tag>
    </header>

    <n-alert v-if="modelValue.status === 'conflict'" type="warning" :show-icon="true">
      服务端已出现 revision {{ modelValue.conflict?.observed_revision }}；本地草稿已保留，请显式重载后再继续。
    </n-alert>

    <div v-if="modelValue.draft" class="plan-fields">
      <label>
        <span>名称</span>
        <n-input :value="modelValue.draft.name" @update:value="updateText('name', $event)" />
      </label>
      <label>
        <span>说明</span>
        <n-input
          type="textarea"
          :value="modelValue.draft.description"
          @update:value="updateText('description', $event)"
        />
      </label>

      <div class="bindings">
        <article v-for="(binding, index) in modelValue.draft.bindings" :key="binding.binding_id" class="binding-row">
          <div>
            <strong>{{ index + 1 }}. {{ binding.binding_id }}</strong>
            <small>{{ binding.plugin_id }} · {{ binding.capability_id }} · {{ binding.release_requirement }}</small>
          </div>
          <n-button-group size="tiny">
            <n-button :disabled="index === 0" @click="move(binding.binding_id, index - 1)">上移</n-button>
            <n-button :disabled="index === modelValue.draft.bindings.length - 1" @click="move(binding.binding_id, index + 1)">下移</n-button>
            <n-button :disabled="binding.required" @click="remove(binding.binding_id)">移除</n-button>
          </n-button-group>
        </article>
      </div>

      <footer class="plan-actions">
        <span v-if="modelValue.status === 'dirty'">本地草稿未保存</span>
        <n-tooltip>
          <template #trigger>
            <n-button type="primary" disabled>保存为新 revision</n-button>
          </template>
          等待 Plan repository 与 Plan-switch CAS 验收后接线
        </n-tooltip>
      </footer>
    </div>
    <n-empty v-else description="暂无可编辑 Plan" />
  </section>
</template>

<script setup lang="ts">
import type { PlanEditorState, PluginPlan } from '@/core/plugins/types.ts'
import {
  movePlanBinding,
  removePlanBinding,
  replacePlanDraft,
  updatePlanEditorDraft,
} from '@/core/plugins/model.ts'

const props = defineProps<{ modelValue: PlanEditorState }>()
const emit = defineEmits<{ 'update:modelValue': [PlanEditorState] }>()

function replaceDraft(next: PluginPlan) {
  emit('update:modelValue', updatePlanEditorDraft(props.modelValue, next))
}

function updateText(field: 'name' | 'description', value: string) {
  if (!props.modelValue.draft) return
  replaceDraft(replacePlanDraft(props.modelValue.draft, { [field]: value }))
}

function move(bindingId: string, targetIndex: number) {
  if (!props.modelValue.draft) return
  replaceDraft(movePlanBinding(props.modelValue.draft, bindingId, targetIndex))
}

function remove(bindingId: string) {
  if (!props.modelValue.draft) return
  replaceDraft(removePlanBinding(props.modelValue.draft, bindingId))
}
</script>

<style scoped>
.plan-manager { display: grid; gap: 0.9rem; }
.plan-header { display: flex; justify-content: space-between; gap: 1rem; align-items: start; }
.plan-header h3 { margin: 0; }
.plan-header p { margin: 0.3rem 0 0; color: var(--app-text-secondary); }
.plan-fields { display: grid; gap: 0.8rem; }
.plan-fields > label { display: grid; gap: 0.35rem; }
.plan-fields > label > span { font-size: var(--font-size-sm); color: var(--app-text-secondary); }
.bindings { display: grid; gap: 0.45rem; }
.binding-row { display: flex; justify-content: space-between; gap: 1rem; padding: 0.65rem; border: 1px solid var(--app-border); border-radius: 8px; }
.binding-row small { display: block; margin-top: 0.2rem; color: var(--app-text-muted); }
.plan-actions { display: flex; align-items: center; justify-content: space-between; color: var(--app-text-secondary); }
</style>
