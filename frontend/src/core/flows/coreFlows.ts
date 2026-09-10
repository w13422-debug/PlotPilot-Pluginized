import type {
  CoreDocument,
  CoreDocumentCreateCommand,
  CoreDeleteResult,
  CoreDocumentPage,
  CoreDocumentRevisionCreateCommand,
  CoreRevision,
  CoreRevisionContentPage,
  CoreWorkspace,
  CoreWorkspaceCreateCommand,
  CoreWorkspaceDeleteCommand,
  CoreWorkspacePage,
} from '../../contracts/types.ts'
import { CoreFlowHttpError, type CoreFlowGateway } from './gateway.ts'

const PAGE_LIMIT = 100
const CONTENT_PAGE_LENGTH = 65_536
const MAX_PAGE_REQUESTS = 10_000
const MAX_REVISION_CONTENT_LENGTH = 8_388_608
const CHAPTER_DOCUMENT_TYPE = 'core.chapter'

export interface CoreProjectListItem {
  workspaceId: string
  title: string
  status: string
  revision: number
  createdAt: string
  updatedAt: string
}

export interface CoreChapterListItem {
  documentId: string
  workspaceId: string
  title: string
  /** Ephemeral presentation index only; Core authority is documentId. */
  displayIndex: number
  revision: number
  currentRevisionId: string | null
  createdAt: string
  updatedAt: string
}

export interface CoreWorkbenchSnapshot {
  project: CoreProjectListItem
  chapters: CoreChapterListItem[]
}

export interface CoreOpenedChapter {
  chapter: CoreChapterListItem
  savedContent: string
  draftContent: string
  revisionId: string | null
  dirty: boolean
}

export interface CoreFlowIdFactory {
  next(kind: 'workspace' | 'document' | 'operation' | 'revision'): string
}

export interface CreateCoreFlowsOptions {
  gateway: CoreFlowGateway
  ids?: CoreFlowIdFactory
  createdBy?: string
}

function defaultIds(): CoreFlowIdFactory {
  return {
    next(kind) {
      return `${kind}-${globalThis.crypto.randomUUID()}`
    },
  }
}

function assertNonEmpty(value: string, label: string): string {
  const normalized = value.trim()
  if (normalized.length === 0) throw new Error(`${label} must not be empty`)
  return normalized
}

function projectFromWorkspace(workspace: Readonly<CoreWorkspace>): CoreProjectListItem {
  if (workspace.workspace_kind !== 'WritingProject') {
    throw new Error(`Workspace ${workspace.workspace_id} is not a WritingProject`)
  }
  return {
    workspaceId: workspace.workspace_id,
    title: workspace.title,
    status: workspace.status,
    revision: workspace.revision,
    createdAt: workspace.created_at,
    updatedAt: workspace.updated_at,
  }
}

function chaptersFromDocuments(documents: readonly Readonly<CoreDocument>[]): CoreChapterListItem[] {
  return documents
    .filter(document => document.document_type === CHAPTER_DOCUMENT_TYPE)
    .slice()
    .sort((left, right) => left.created_at.localeCompare(right.created_at) || left.document_id.localeCompare(right.document_id))
    .map((document, index) => ({
      documentId: document.document_id,
      workspaceId: document.workspace_id,
      title: document.title,
      displayIndex: index + 1,
      revision: document.revision,
      currentRevisionId: document.current_revision_id,
      createdAt: document.created_at,
      updatedAt: document.updated_at,
    }))
}

export function editCoreChapter(chapter: Readonly<CoreOpenedChapter>, draftContent: string): CoreOpenedChapter {
  return {
    ...chapter,
    draftContent,
    dirty: draftContent !== chapter.savedContent,
  }
}

export interface CoreChapterCreateRecoveryActions {
  createChapter(workspaceId: string, title: string): Promise<Readonly<CoreChapterListItem>>
  refreshWorkspace(workspaceId: string): Promise<void>
  openDocument(documentId: string): Promise<void>
}

export function createCoreChapterRecovery() {
  const activeWorkspaces = new Set<string>()
  const committedByWorkspace = new Map<string, Readonly<{ workspaceId: string; documentId: string }>>()

  function hasCommittedChapter(workspaceId: string): boolean {
    return committedByWorkspace.has(workspaceId)
  }

  function canRunOrdinaryReload(workspaceId: string): boolean {
    return !activeWorkspaces.has(workspaceId) && !hasCommittedChapter(workspaceId)
  }

  async function run(
    workspaceIdInput: string,
    title: string,
    actions: Readonly<CoreChapterCreateRecoveryActions>,
  ): Promise<string> {
    const workspaceId = assertNonEmpty(workspaceIdInput, 'Workspace identity')
    const normalizedTitle = assertNonEmpty(title, 'Chapter title')
    if (activeWorkspaces.has(workspaceId)) throw new Error(`Core chapter create/recovery is already active for ${workspaceId}`)
    activeWorkspaces.add(workspaceId)
    try {
      let committed = committedByWorkspace.get(workspaceId)
      if (committed === undefined) {
        const created = await actions.createChapter(workspaceId, normalizedTitle)
        if (created.workspaceId !== workspaceId) throw new Error('Core chapter create/recovery crossed Workspace')
        committed = { workspaceId, documentId: assertNonEmpty(created.documentId, 'Committed document identity') }
        committedByWorkspace.set(workspaceId, committed)
      }
      await actions.refreshWorkspace(workspaceId)
      await actions.openDocument(committed.documentId)
      if (committedByWorkspace.get(workspaceId) === committed) committedByWorkspace.delete(workspaceId)
      return committed.documentId
    } finally {
      activeWorkspaces.delete(workspaceId)
    }
  }

  return { hasCommittedChapter, canRunOrdinaryReload, run }
}

export function createCoreFlows(options: CreateCoreFlowsOptions) {
  const gateway = options.gateway
  const ids = options.ids ?? defaultIds()
  const pendingCreateCommands = new Map<string, CoreWorkspaceCreateCommand>()
  const pendingChapterCreateCommands = new Map<string, CoreDocumentCreateCommand>()
  const pendingDeleteCommands = new Map<string, CoreWorkspaceDeleteCommand>()
  const pendingSaveCommands = new Map<string, CoreDocumentRevisionCreateCommand>()

  async function listProjects(): Promise<CoreProjectListItem[]> {
    const projects: CoreProjectListItem[] = []
    const seenWorkspaceIds = new Set<string>()
    let offset = 0
    for (let pageCount = 0; pageCount < MAX_PAGE_REQUESTS; pageCount += 1) {
      const page = await gateway.request('workspace.list', {
        schema: 'core-workspace-query/v1', workspace_id: null, offset, limit: PAGE_LIMIT,
      })
      if (page.offset !== offset || page.limit !== PAGE_LIMIT) throw new Error('Core workspace page drifted from the requested offset or limit')
      const expectedNextOffset = offset + page.items.length < page.total ? offset + page.items.length : null
      if (page.next_offset !== expectedNextOffset) throw new Error('Core workspace pagination cursor drifted')
      for (const item of page.items) {
        if (seenWorkspaceIds.has(item.workspace_id)) throw new Error(`Core workspace pagination repeated ${item.workspace_id}`)
        seenWorkspaceIds.add(item.workspace_id)
      }
      projects.push(...page.items.filter(item => item.workspace_kind === 'WritingProject').map(projectFromWorkspace))
      if (page.next_offset === null) return projects
      if (page.next_offset <= offset) throw new Error('Core workspace pagination did not advance')
      offset = page.next_offset
    }
    throw new Error('Core workspace pagination exceeded the safety bound')
  }

  async function createProject(title: string): Promise<CoreProjectListItem> {
    const normalizedTitle = assertNonEmpty(title, 'Project title')
    let command = pendingCreateCommands.get(normalizedTitle)
    if (command === undefined) {
      command = {
        schema: 'core-workspace-create-command/v1',
        operation_key: ids.next('operation'),
        workspace_id: ids.next('workspace'),
        workspace_kind: 'WritingProject',
        title: normalizedTitle,
      }
      pendingCreateCommands.set(normalizedTitle, command)
    }
    let workspace: Readonly<CoreWorkspace>
    try {
      workspace = await gateway.request('workspace.create', command)
    } catch (error: unknown) {
      if (error instanceof CoreFlowHttpError) pendingCreateCommands.delete(normalizedTitle)
      throw error
    }
    if (workspace.workspace_id !== command.workspace_id || workspace.workspace_kind !== command.workspace_kind || workspace.title !== command.title) {
      throw new Error('Core workspace create returned a different identity or title')
    }
    pendingCreateCommands.delete(normalizedTitle)
    return projectFromWorkspace(workspace)
  }

  async function deleteProject(project: Readonly<CoreProjectListItem>): Promise<void> {
    const pendingKey = `${project.workspaceId}\u0000${project.revision}`
    let command = pendingDeleteCommands.get(pendingKey)
    if (command === undefined) {
      command = {
        schema: 'core-workspace-delete-command/v1',
        operation_key: ids.next('operation'),
        workspace_id: project.workspaceId,
        expected_revision: project.revision,
      }
      pendingDeleteCommands.set(pendingKey, command)
    }
    let result: Readonly<CoreDeleteResult>
    try {
      result = await gateway.request('workspace.delete', command)
    } catch (error: unknown) {
      if (error instanceof CoreFlowHttpError) pendingDeleteCommands.delete(pendingKey)
      throw error
    }
    if (result.schema !== 'core-delete-result/v1'
      || result.operation_key !== command.operation_key
      || result.workspace_id !== project.workspaceId
      || result.entity_kind !== 'workspace'
      || result.entity_id !== project.workspaceId
      || result.previous_revision !== project.revision
      || result.deleted !== true) {
      throw new Error('Core workspace delete result is not bound to the requested project')
    }
    pendingDeleteCommands.delete(pendingKey)
  }

  async function createChapter(workspaceIdInput: string, title: string): Promise<CoreChapterListItem> {
    const workspaceId = assertNonEmpty(workspaceIdInput, 'Workspace identity')
    const normalizedTitle = assertNonEmpty(title, 'Chapter title')
    const pendingKey = JSON.stringify([workspaceId, normalizedTitle])
    let command = pendingChapterCreateCommands.get(pendingKey)
    if (command === undefined) {
      command = {
        schema: 'core-document-create-command/v1',
        operation_key: ids.next('operation'),
        document_id: ids.next('document'),
        workspace_id: workspaceId,
        document_type: CHAPTER_DOCUMENT_TYPE,
        title: normalizedTitle,
      }
      pendingChapterCreateCommands.set(pendingKey, command)
    }
    let document: Readonly<CoreDocument>
    try {
      document = await gateway.request('document.create', command)
    } catch (error: unknown) {
      if (error instanceof CoreFlowHttpError) pendingChapterCreateCommands.delete(pendingKey)
      throw error
    }
    if (document.workspace_id !== command.workspace_id
      || document.document_id !== command.document_id
      || document.document_type !== command.document_type
      || document.title !== command.title) {
      throw new Error('Core chapter create returned a different workspace, document, type, or title')
    }
    pendingChapterCreateCommands.delete(pendingKey)
    return chaptersFromDocuments([document])[0]!
  }

  async function listDocuments(workspaceId: string): Promise<Readonly<CoreDocument>[]> {
    const documents: Readonly<CoreDocument>[] = []
    const seenDocumentIds = new Set<string>()
    let offset = 0
    for (let pageCount = 0; pageCount < MAX_PAGE_REQUESTS; pageCount += 1) {
      const page = await gateway.request('document.list', {
        schema: 'core-document-query/v1', workspace_id: workspaceId, document_id: null, offset, limit: PAGE_LIMIT,
      })
      if (page.offset !== offset || page.limit !== PAGE_LIMIT) throw new Error('Core document page drifted from the requested offset or limit')
      const expectedNextOffset = offset + page.items.length < page.total ? offset + page.items.length : null
      if (page.next_offset !== expectedNextOffset) throw new Error('Core document pagination cursor drifted')
      if (page.items.some(item => item.workspace_id !== workspaceId)) throw new Error('Core document page crossed Workspace')
      for (const item of page.items) {
        if (seenDocumentIds.has(item.document_id)) throw new Error(`Core document pagination repeated ${item.document_id}`)
        seenDocumentIds.add(item.document_id)
      }
      documents.push(...page.items)
      if (page.next_offset === null) return documents
      if (page.next_offset <= offset) throw new Error('Core document pagination did not advance')
      offset = page.next_offset
    }
    throw new Error('Core document pagination exceeded the safety bound')
  }

  async function loadWorkbench(workspaceIdInput: string): Promise<CoreWorkbenchSnapshot> {
    const workspaceId = assertNonEmpty(workspaceIdInput, 'Workspace identity')
    const [workspace, documents] = await Promise.all([
      gateway.request('workspace.get', { schema: 'core-workspace-get-query/v1', workspace_id: workspaceId }),
      listDocuments(workspaceId),
    ])
    if (workspace.workspace_id !== workspaceId) throw new Error('Core workspace response crossed Workspace')
    return { project: projectFromWorkspace(workspace), chapters: chaptersFromDocuments(documents) }
  }

  async function readRevisionContent(workspaceId: string, revisionId: string): Promise<string> {
    const chunks: string[] = []
    let offset = 0
    let totalLength: number | null = null
    for (let pageCount = 0; pageCount < MAX_PAGE_REQUESTS; pageCount += 1) {
      const page = await gateway.request('revision.content', {
        schema: 'core-revision-content-query/v1', workspace_id: workspaceId, revision_id: revisionId,
        offset, length: CONTENT_PAGE_LENGTH,
      })
      if (page.revision_id !== revisionId || page.offset !== offset) throw new Error('Core revision content page crossed identity or offset')
      if (totalLength !== null && page.total_length !== totalLength) throw new Error('Core revision content total changed between pages')
      totalLength = page.total_length
      chunks.push(page.text)
      if (page.next_offset === null) return chunks.join('')
      if (page.next_offset <= offset) throw new Error('Core revision content pagination did not advance')
      offset = page.next_offset
    }
    throw new Error('Core revision content pagination exceeded the safety bound')
  }

  async function openChapter(chapter: Readonly<CoreChapterListItem>): Promise<CoreOpenedChapter> {
    const document = await gateway.request('document.get', {
      schema: 'core-document-get-query/v1', workspace_id: chapter.workspaceId, document_id: chapter.documentId,
    })
    if (document.workspace_id !== chapter.workspaceId || document.document_id !== chapter.documentId || document.document_type !== CHAPTER_DOCUMENT_TYPE) {
      throw new Error('Core chapter response crossed its document identity')
    }
    let content = ''
    if (document.current_revision_id !== null) {
      const revision = await gateway.request('revision.get', {
        schema: 'core-revision-get-query/v1', workspace_id: chapter.workspaceId, revision_id: document.current_revision_id,
      })
      if (revision.workspace_id !== chapter.workspaceId
        || revision.document_id !== chapter.documentId
        || revision.node_id !== null
        || revision.revision_id !== document.current_revision_id
        || revision.payload_schema !== 'core.document-text/v1') {
        throw new Error('Core chapter revision crossed its exact current Revision identity or text payload')
      }
      content = await readRevisionContent(chapter.workspaceId, revision.revision_id)
    }
    const currentChapter = { ...chapter, revision: document.revision, currentRevisionId: document.current_revision_id, updatedAt: document.updated_at }
    return { chapter: currentChapter, savedContent: content, draftContent: content, revisionId: document.current_revision_id, dirty: false }
  }

  async function saveChapter(chapter: Readonly<CoreOpenedChapter>): Promise<CoreOpenedChapter> {
    if (!chapter.dirty) return { ...chapter }
    const authoritativeActor = assertNonEmpty(options.createdBy ?? '', 'Core created_by integration identity')
    if ([...chapter.draftContent].length > MAX_REVISION_CONTENT_LENGTH) throw new Error('Core chapter content exceeds the frozen revision limit')
    const pendingKey = JSON.stringify([
      chapter.chapter.workspaceId,
      chapter.chapter.documentId,
      chapter.revisionId,
      chapter.draftContent,
      authoritativeActor,
    ])
    let command = pendingSaveCommands.get(pendingKey)
    if (command === undefined) {
      command = {
        schema: 'core-document-revision-create-command/v1',
        operation_key: ids.next('operation'),
        revision_id: ids.next('revision'),
        workspace_id: chapter.chapter.workspaceId,
        document_id: chapter.chapter.documentId,
        base_revision_id: chapter.revisionId,
        content: chapter.draftContent,
        created_by: authoritativeActor,
        source_candidate_id: null,
        payload_schema: 'core.document-text/v1',
      }
      pendingSaveCommands.set(pendingKey, command)
    }
    let revision: Readonly<CoreRevision>
    try {
      revision = await gateway.request('document.revision.create', command)
    } catch (error: unknown) {
      if (error instanceof CoreFlowHttpError) pendingSaveCommands.delete(pendingKey)
      throw error
    }
    if (revision.workspace_id !== command.workspace_id
      || revision.document_id !== command.document_id
      || revision.node_id !== null
      || revision.revision_id !== command.revision_id
      || revision.parent_revision_id !== command.base_revision_id
      || revision.created_by !== command.created_by
      || revision.source_candidate_id !== command.source_candidate_id
      || revision.payload_schema !== command.payload_schema) {
      throw new Error('Core revision create result crossed its chapter identity')
    }
    pendingSaveCommands.delete(pendingKey)
    return {
      ...chapter,
      chapter: { ...chapter.chapter, currentRevisionId: revision.revision_id },
      savedContent: chapter.draftContent,
      revisionId: revision.revision_id,
      dirty: false,
    }
  }

  return { listProjects, createProject, deleteProject, createChapter, loadWorkbench, openChapter, saveChapter }
}

export type CoreFlows = ReturnType<typeof createCoreFlows>
