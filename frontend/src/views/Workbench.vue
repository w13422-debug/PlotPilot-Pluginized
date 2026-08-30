<template>
  <div class="workbench">
    <header class="core-workbench-header">
      <strong>{{ bookTitle || slug }}</strong>
      <span>Core Workspace · {{ slug }}</span>
    </header>

    <n-spin :show="pageLoading" class="workbench-spin" description="加载工作台…">
      <div class="workbench-inner">
        <n-split
          direction="horizontal"
          :min="WORKBENCH_SPLIT.sidebarMin"
          :max="WORKBENCH_SPLIT.sidebarMax"
          :default-size="WORKBENCH_SPLIT.sidebarDefault"
        >
          <template #1>
            <CoreChapterList
              :chapters="chapters"
              :current-document-id="currentDocumentId"
              :busy="chapterLoading || chapterSaving"
              @select="handleChapterSelect"
              @back="goHome"
              @refresh="handleChapterUpdated"
            />
          </template>

          <template #2>
            <div class="wb-main-split" :class="{ 'wb-right-collapsed': rightCollapsed }">
              <n-split
                direction="horizontal"
                :min="WORKBENCH_SPLIT.mainMin"
                :max="WORKBENCH_SPLIT.mainMax"
                :default-size="WORKBENCH_SPLIT.mainDefault"
              >
                <template #1>
                  <CoreChapterEditor
                    :book-title="bookTitle"
                    :chapter-title="currentChapter?.title ?? ''"
                    :model-value="chapterContent"
                    :has-chapter="currentChapter !== null"
                    :dirty="openedChapter?.dirty ?? false"
                    :loading="chapterLoading"
                    :saving="chapterSaving"
                    @update:model-value="handleChapterEdit"
                    @save="handleChapterSave"
                  />
                </template>

                <template #2>
                  <div v-if="rightCollapsed" class="wb-right-strip" @click="toggleRight">
                    <span class="wb-strip-icon">◀</span>
                  </div>
                  <aside v-else class="core-right-panel">
                    <header>
                      <strong>Core 信息</strong>
                      <n-button quaternary size="tiny" @click="toggleRight">收起</n-button>
                    </header>
                    <dl v-if="currentChapter">
                      <dt>Document ID</dt><dd>{{ currentChapter.documentId }}</dd>
                      <dt>Revision ID</dt><dd>{{ openedChapter?.revisionId ?? '尚未保存' }}</dd>
                      <dt>状态</dt><dd>{{ openedChapter?.dirty ? '有未保存编辑' : '已同步' }}</dd>
                    </dl>
                    <p v-else>选择章节后显示 Core 文档与 Revision 身份。</p>
                  </aside>
                </template>
              </n-split>
            </div>
          </template>
        </n-split>
      </div>
    </n-spin>

    <TaskDrawer />
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { useDebouncedTask } from '../composables/useDebouncedTask'
import TaskDrawer from '../components/jobs/TaskDrawer.vue'
import CoreChapterEditor from '../core/flows/CoreChapterEditor.vue'
import CoreChapterList from '../core/flows/CoreChapterList.vue'
import {
  editCoreChapter,
  type CoreChapterListItem,
  type CoreOpenedChapter,
  type CoreWorkbenchSnapshot,
} from '../core/flows/coreFlows.ts'
import {
  ScopedRequestGeneration,
  type ScopedGenerationToken,
} from '../core/flows/asyncGeneration.ts'
import { requireCoreFlowRuntime } from '../core/flows/runtime.ts'
import { WORKBENCH_SPLIT } from '../design/layoutDensity'
import { storageKeys } from '@/config/storageKeys'
import { runtimePerformance } from '@/config/performance'
import { readStorageBoolean, writeStorageBoolean } from '@/utils/storage'

const route = useRoute()
const router = useRouter()
const message = useMessage()

const slug = computed(() => String(route.params.slug ?? ''))

const bookTitle = ref('')
const chapters = ref<CoreChapterListItem[]>([])
const pageLoading = ref(true)
const currentDocumentId = ref<string | null>(null)
const chapterContent = ref('')
const chapterLoading = ref(false)
const chapterSaving = ref(false)
const openedChapter = ref<CoreOpenedChapter | null>(null)
const draftByDocument = new Map<string, string>()
const workspaceGeneration = new ScopedRequestGeneration()
let openSequence = 0
let saveSequence = 0
let deskSequence = 0

function isWorkspaceCurrent(token: Readonly<ScopedGenerationToken>, workspaceId: string): boolean {
  return workspaceGeneration.isCurrent(token) && token.scope === workspaceId && slug.value === workspaceId
}

function workspaceDraftKey(workspaceId: string, documentId: string): string {
  return `${workspaceId}\u0000${documentId}`
}

function beginWorkspaceTransition(workspaceId: string): ScopedGenerationToken {
  const token = workspaceGeneration.begin(workspaceId)
  openSequence += 1
  saveSequence += 1
  deskSequence += 1
  chapterDeskReload.cancel()
  bookTitle.value = workspaceId
  chapters.value = []
  currentDocumentId.value = null
  openedChapter.value = null
  chapterContent.value = ''
  chapterLoading.value = false
  chapterSaving.value = false
  pageLoading.value = true
  return token
}

async function loadDesk(workspaceId: string, token: Readonly<ScopedGenerationToken>): Promise<boolean> {
  const requestSequence = ++deskSequence
  let snapshot: CoreWorkbenchSnapshot
  try {
    snapshot = await requireCoreFlowRuntime().loadWorkbench(workspaceId)
  } catch (error: unknown) {
    if (!isWorkspaceCurrent(token, workspaceId) || requestSequence !== deskSequence) return false
    throw error
  }
  if (!isWorkspaceCurrent(token, workspaceId) || requestSequence !== deskSequence) return false
  bookTitle.value = snapshot.project.title
  chapters.value = snapshot.chapters
  if (currentDocumentId.value !== null && !chapters.value.some(chapter => chapter.documentId === currentDocumentId.value)) {
    currentDocumentId.value = null
    openedChapter.value = null
    chapterContent.value = ''
  }
  return true
}

function goHome() {
  void router.push('/')
}

async function goToChapter(documentId: string, token = workspaceGeneration.capture()) {
  const requestSequence = ++openSequence
  if (token === null || !isWorkspaceCurrent(token, token.scope)) {
    chapterLoading.value = false
    return
  }
  const workspaceId = token.scope
  const target = chapters.value.find(chapter => chapter.documentId === documentId)
  if (target === undefined || target.workspaceId !== workspaceId) {
    chapterLoading.value = false
    message.error('章节不在当前 Core 文档列表中')
    return
  }
  chapterLoading.value = true
  try {
    let opened = await requireCoreFlowRuntime().openChapter(target)
    if (!isWorkspaceCurrent(token, workspaceId) || requestSequence !== openSequence) return
    const pendingDraft = draftByDocument.get(workspaceDraftKey(workspaceId, documentId))
    if (pendingDraft !== undefined) opened = editCoreChapter(opened, pendingDraft)
    currentDocumentId.value = documentId
    openedChapter.value = opened
    chapterContent.value = opened.draftContent
    if (route.query.chapter !== documentId) {
      await router.replace({ query: { ...route.query, chapter: documentId } })
    }
  } catch (error: unknown) {
    if (!isWorkspaceCurrent(token, workspaceId) || requestSequence !== openSequence) return
    message.error(error instanceof Error ? error.message : '加载章节失败')
  } finally {
    if (isWorkspaceCurrent(token, workspaceId) && requestSequence === openSequence) chapterLoading.value = false
  }
}

async function handleChapterSelect(documentId: string) {
  await goToChapter(documentId)
}

function handleChapterEdit(content: string) {
  const opened = openedChapter.value
  if (opened !== null && opened.chapter.workspaceId === slug.value) {
    chapterContent.value = content
    openedChapter.value = editCoreChapter(opened, content)
    const draftKey = workspaceDraftKey(
      openedChapter.value.chapter.workspaceId,
      openedChapter.value.chapter.documentId,
    )
    if (openedChapter.value.dirty) draftByDocument.set(draftKey, content)
    else draftByDocument.delete(draftKey)
  }
}

async function handleChapterSave() {
  if (openedChapter.value === null || !openedChapter.value.dirty) return
  const token = workspaceGeneration.capture()
  if (token === null || !isWorkspaceCurrent(token, token.scope)) return
  const savingChapter = openedChapter.value
  const savingDocumentId = savingChapter.chapter.documentId
  const workspaceId = token.scope
  if (savingChapter.chapter.workspaceId !== workspaceId || currentDocumentId.value !== savingDocumentId) return
  const requestSequence = ++saveSequence
  chapterSaving.value = true
  try {
    const saved = await requireCoreFlowRuntime().saveChapter(savingChapter)
    if (!isWorkspaceCurrent(token, workspaceId)
      || requestSequence !== saveSequence
      || currentDocumentId.value !== savingDocumentId
      || openedChapter.value?.chapter.documentId !== savingDocumentId
      || openedChapter.value.draftContent !== savingChapter.draftContent) return
    draftByDocument.delete(workspaceDraftKey(workspaceId, savingDocumentId))
    openedChapter.value = saved
    chapterContent.value = saved.draftContent
    message.success('保存成功')
    handleChapterUpdated()
  } catch (error: unknown) {
    if (!isWorkspaceCurrent(token, workspaceId) || requestSequence !== saveSequence) return
    message.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    if (isWorkspaceCurrent(token, workspaceId) && requestSequence === saveSequence) chapterSaving.value = false
  }
}

async function runChapterDeskReload() {
  const token = workspaceGeneration.capture()
  if (token !== null) await loadDesk(token.scope, token)
}

/** 合并短时间内的多次「整桌刷新」：全托管状态抖动 / 多源 emit 时只拉一次 API，减轻闪烁与日志刷屏 */
const chapterDeskReload = useDebouncedTask(
  runChapterDeskReload,
  () => runtimePerformance.workbench.deskReloadDebounceMs,
  {
    onError: () => {
      message.error('刷新工作台失败，请检查网络与后端是否已启动')
    },
  },
)

const handleChapterUpdated = () => {
  chapterDeskReload.schedule()
}

const rightCollapsed = ref(readStorageBoolean(storageKeys.workbenchRightPanelCollapsed))

function toggleRight() {
  rightCollapsed.value = !rightCollapsed.value
  writeStorageBoolean(storageKeys.workbenchRightPanelCollapsed, rightCollapsed.value)
}

const currentChapter = computed(() => {
  if (currentDocumentId.value === null) return null
  return chapters.value.find(chapter => chapter.documentId === currentDocumentId.value) ?? null
})

function parseChapterQuery(q: unknown): string | null {
  if (q == null || q === '') return null
  const raw = Array.isArray(q) ? q[0] : q
  return typeof raw === 'string' && raw.length > 0 ? raw : null
}

async function syncChapterFromRoute(token = workspaceGeneration.capture()) {
  if (token === null || !isWorkspaceCurrent(token, token.scope)) return
  const documentId = parseChapterQuery(route.query.chapter)
  if (documentId !== null && documentId !== currentDocumentId.value) {
    await goToChapter(documentId, token)
  }
}

onMounted(async () => {
  const workspaceId = slug.value
  const token = beginWorkspaceTransition(workspaceId)
  try {
    if (await loadDesk(workspaceId, token)) await syncChapterFromRoute(token)
  } catch (error: unknown) {
    if (!isWorkspaceCurrent(token, workspaceId)) return
    message.error(error instanceof Error ? error.message : '加载 Core 工作台失败')
    bookTitle.value = workspaceId
  } finally {
    if (isWorkspaceCurrent(token, workspaceId)) pageLoading.value = false
  }
})

onUnmounted(() => {
  workspaceGeneration.invalidate()
  openSequence += 1
  saveSequence += 1
  deskSequence += 1
  chapterDeskReload.cancel()
})

watch(
  () => route.query.chapter,
  () => {
    const token = workspaceGeneration.capture()
    void syncChapterFromRoute(token)
  }
)

watch(
  slug,
  async (next, prev) => {
    if (prev === next) return
    const token = beginWorkspaceTransition(next)
    if (!next) {
      pageLoading.value = false
      return
    }
    try {
      if (await loadDesk(next, token)) await syncChapterFromRoute(token)
    } catch (error: unknown) {
      if (!isWorkspaceCurrent(token, next)) return
      message.error(error instanceof Error ? error.message : '切换 Core 项目失败')
      bookTitle.value = next
    } finally {
      if (isWorkspaceCurrent(token, next)) pageLoading.value = false
    }
  }
)
</script>

<style scoped>
.workbench {
  height: 100vh;
  min-height: 0;
  max-height: 100vh;
  overflow: hidden;
  background: var(--app-page-bg, #f0f2f8);
  display: flex;
  flex-direction: column;
}

.core-workbench-header {
  min-height: 56px;
  padding: 8px 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  color: var(--app-text);
  background: var(--app-surface);
  border-bottom: 1px solid var(--app-divider, rgba(15, 23, 42, 0.08));
}

.core-workbench-header span {
  color: var(--app-text-muted);
  font-size: 12px;
}

.core-right-panel {
  height: 100%;
  padding: 14px;
  overflow: auto;
  color: var(--app-text);
  background: var(--app-surface);
  border-left: 1px solid var(--plotpilot-split-border);
}

.core-right-panel header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 18px;
}

.core-right-panel dl { margin: 0; display: grid; gap: 6px; }
.core-right-panel dt { color: var(--app-text-muted); font-size: 12px; }
.core-right-panel dd { margin: 0 0 10px; overflow-wrap: anywhere; }

.workbench-spin {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.workbench-spin :deep(.n-spin-content) {
  flex: 1;
  min-height: 0;
  height: auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.workbench-inner {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.workbench-inner :deep(.n-split) {
  flex: 1;
  min-height: 0;
  height: 100%;
}

.workbench-inner :deep(.n-split-pane-1),
.workbench-inner :deep(.n-split-pane-2) {
  min-height: 0;
  overflow: hidden;
}

/* ── Right sidebar collapse ─────────────────────────── */

.wb-main-split {
  height: 100%;
  width: 100%;
  overflow: hidden;
}

.wb-right-collapsed :deep(.n-split-pane-1) {
  flex: 1 1 0 !important;
  width: 0 !important;
  max-width: none !important;
}

.wb-right-collapsed :deep(.n-split-pane-2) {
  flex: 0 0 32px !important;
  width: 32px !important;
  min-width: 0 !important;
  max-width: 32px !important;
  overflow: hidden;
}

.wb-right-collapsed :deep(.n-split__gutter) {
  display: none !important;
}

.wb-right-strip {
  height: 100%;
  width: 32px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  background: var(--app-surface);
  border-left: 1px solid var(--plotpilot-split-border);
  color: var(--app-text-muted);
  font-size: 12px;
  transition: background 0.15s, color 0.15s;
  user-select: none;
}

.wb-right-strip:hover {
  background: var(--plotpilot-panel-muted);
  color: var(--app-text-primary);
}
</style>
