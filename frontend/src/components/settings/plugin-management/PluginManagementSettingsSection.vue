<template>
  <section class="plugin-management-settings" aria-label="插件管理设置">
    <n-alert type="warning" :show-icon="true" class="runtime-limits">
      <template #header>插件功能尚未注册</template>
      当前生产后端没有注册 Plugins v1 路由，因此本页不挂载 gateway，也不会发起插件网络请求。
      安装、启用、停用、退休和 Plan 发布入口保持可见但禁用；注册相应 plugin/runtime 后再启用。
      <n-space size="small" wrap class="unsupported-actions">
        <n-button disabled>启用当前 Release</n-button>
        <n-button disabled>停用当前 Release</n-button>
        <n-button disabled>退休当前 Release</n-button>
      </n-space>
    </n-alert>
    <PluginManagementPanel />
  </section>
</template>

<script setup lang="ts">
import PluginManagementPanel from './PluginManagementPanel.vue'
</script>

<style scoped>
.plugin-management-settings { display: grid; gap: 1rem; }
.runtime-limits { line-height: 1.55; }
.unsupported-actions { margin-top: 0.75rem; }
</style>
