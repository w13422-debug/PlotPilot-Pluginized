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
  ProjectBriefContent,
  ProjectBriefHomeInput,
} from '../../contracts/types.ts'
import { CoreFlowHttpError, type CoreFlowGateway } from './gateway.ts'
import {
  PROJECT_BRIEF_DOCUMENT_TYPE,
  PROJECT_BRIEF_PAYLOAD_SCHEMA,
  PROJECT_BRIEF_TITLE,
  preflightProjectBriefInput,
  projectBriefContentsEqual,
  projectBriefDocumentId,
  parseSerializedProjectBriefContent,
  serializeProjectBriefContent,
} from './projectBrief.ts'

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

export interface CoreProjectBriefSnapshot {
  workspaceId: string
  documentId: string
  revisionId: string
  content: Readonly<ProjectBriefContent>
}

export interface CoreProjectBriefCreateResult {
  workspaceId: string
  project: CoreProjectListItem
  brief: CoreProjectBriefSnapshot
}

/** Workspace creation succeeded, but its mandatory Project Brief was not yet verified. */
export class CoreProjectBriefPartialCreateError extends Error {
  readonly workspaceId: string

  constructor(workspaceId: string, cause: unknown) {
    const reason = cause instanceof Error ? cause.message : 'unknown Core failure'
    super(`Core Workspace ${workspaceId} 已创建，但 Project Brief 尚未验证保存；请使用相同输入重试，未进入工作台。${reason}`)
    this.name = 'CoreProjectBriefPartialCreateError'
    this.workspaceId = workspaceId
  }
}

export interface CoreFlowIdFactory {
  next(kind: 'workspace' | 'document' | 'operation' | 'revision'): string
}

export interface CreateCoreFlowsOptions {
  gateway: CoreFlowGateway
  ids?: CoreFlowIdFactory
  createdBy?: string
}

interface PendingProjectBriefCreate {
  workspaceCommand: CoreWorkspaceCreateCommand
  fingerprint?: string
  content?: Readonly<ProjectBriefContent>
  workspace?: CoreProjectListItem
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
  const pendingProjectBriefCreates = new Map<string, PendingProjectBriefCreate>()
  const pendingProjectBriefDocumentCommands = new Map<string, CoreDocumentCreateCommand>()
  const pendingProjectBriefRevisionCommands = new Map<string, CoreDocumentRevisionCreateCommand>()

  function authoritativeActor(): string {
    return assertNonEmpty(options.createdBy ?? '', 'Core created_by integration identity')
  }

  function isDefinitePreEffectRejection(error: unknown): boolean {
    return error instanceof CoreFlowHttpError && error.status === 400 && error.retryable === false
  }

  function isStaleCas(error: unknown): boolean {
    return error instanceof CoreFlowHttpError && error.errorCode === 'stale_cas'
  }

  function assertProjectBriefDocument(
    document: Readonly<CoreDocument>,
    workspaceId: string,
    documentId: string,
    expectedTitle?: string,
  ): void {
    if (document.schema !== 'core-document/v1'
      || document.workspace_id !== workspaceId
      || document.document_id !== documentId
      || document.document_type !== PROJECT_BRIEF_DOCUMENT_TYPE
      || (expectedTitle !== undefined && document.title !== expectedTitle)) {
      throw new Error('Core Project Brief Document crossed its exact identity, type, or title')
    }
  }

  function assertProjectBriefRevision(
    revision: Readonly<CoreRevision>,
    workspaceId: string,
    documentId: string,
    revisionId: string,
    actor: string,
    parentRevisionId?: string | null,
  ): void {
    if (revision.schema !== 'core-revision/v1'
      || revision.workspace_id !== workspaceId
      || revision.document_id !== documentId
      || revision.node_id !== null
      || revision.revision_id !== revisionId
      || revision.created_by !== actor
      || revision.source_candidate_id !== null
      || revision.payload_schema !== PROJECT_BRIEF_PAYLOAD_SCHEMA
      || (parentRevisionId !== undefined && revision.parent_revision_id !== parentRevisionId)) {
      throw new Error('Core Project Brief Revision crossed its exact identity, parent, actor, or payload schema')
    }
  }

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

  async function findProjectBriefDocument(workspaceId: string, documentId: string): Promise<Readonly<CoreDocument> | null> {
    const documents = await listDocuments(workspaceId)
    const atExpectedIdentity = documents.filter(document => document.document_id === documentId)
    if (atExpectedIdentity.length > 1) throw new Error('Core Project Brief Document identity is duplicated')
    const existing = atExpectedIdentity[0]
    if (existing !== undefined && existing.document_type !== PROJECT_BRIEF_DOCUMENT_TYPE) {
      throw new Error('Core Project Brief deterministic Document identity has the wrong type')
    }
    const briefs = documents.filter(document => document.document_type === PROJECT_BRIEF_DOCUMENT_TYPE)
    if (briefs.some(document => document.document_id !== documentId) || briefs.length > 1) {
      throw new Error('Core Workspace has more than one Project Brief Document')
    }
    if (existing === undefined) return null
    assertProjectBriefDocument(existing, workspaceId, documentId)
    return existing
  }

  async function getProjectBriefDocument(workspaceId: string, documentId: string): Promise<Readonly<CoreDocument>> {
    const document = await gateway.request('document.get', {
      schema: 'core-document-get-query/v1', workspace_id: workspaceId, document_id: documentId,
    })
    assertProjectBriefDocument(document, workspaceId, documentId)
    return document
  }

  async function readProjectBriefFromDocument(
    workspaceId: string,
    documentId: string,
    actor: string,
    expectedParentRevisionId?: string | null,
  ): Promise<CoreProjectBriefSnapshot> {
    const document = await getProjectBriefDocument(workspaceId, documentId)
    if (document.current_revision_id === null) throw new Error('Core Project Brief Document has no current Revision')
    const revision = await gateway.request('revision.get', {
      schema: 'core-revision-get-query/v1', workspace_id: workspaceId, revision_id: document.current_revision_id,
    })
    assertProjectBriefRevision(
      revision,
      workspaceId,
      documentId,
      document.current_revision_id,
      actor,
      expectedParentRevisionId,
    )
    const serialized = await readRevisionContent(workspaceId, revision.revision_id)
    const content = parseSerializedProjectBriefContent(serialized, workspaceId)
    return { workspaceId, documentId, revisionId: revision.revision_id, content }
  }

  async function loadProjectBrief(workspaceIdInput: string): Promise<CoreProjectBriefSnapshot | null> {
    const workspaceId = assertNonEmpty(workspaceIdInput, 'Workspace identity')
    const documentId = await projectBriefDocumentId(workspaceId)
    const document = await findProjectBriefDocument(workspaceId, documentId)
    if (document === null) return null
    return readProjectBriefFromDocument(workspaceId, documentId, authoritativeActor())
  }

  async function ensureProjectBriefDocument(workspaceId: string, documentId: string): Promise<Readonly<CoreDocument>> {
    let command = pendingProjectBriefDocumentCommands.get(documentId)
    if (command === undefined) {
      const existing = await findProjectBriefDocument(workspaceId, documentId)
      if (existing !== null) return existing
      command = {
        schema: 'core-document-create-command/v1',
        operation_key: ids.next('operation'),
        document_id: documentId,
        workspace_id: workspaceId,
        document_type: PROJECT_BRIEF_DOCUMENT_TYPE,
        title: PROJECT_BRIEF_TITLE,
      }
      pendingProjectBriefDocumentCommands.set(documentId, command)
    }
    if (command.schema !== 'core-document-create-command/v1'
      || command.workspace_id !== workspaceId
      || command.document_id !== documentId
      || command.document_type !== PROJECT_BRIEF_DOCUMENT_TYPE
      || command.title !== PROJECT_BRIEF_TITLE) {
      throw new Error('Core Project Brief pending Document command crossed Workspace identity')
    }

    let document: Readonly<CoreDocument>
    try {
      document = await gateway.request('document.create', command)
    } catch (error: unknown) {
      if (isDefinitePreEffectRejection(error)) pendingProjectBriefDocumentCommands.delete(documentId)
      throw error
    }
    assertProjectBriefDocument(document, workspaceId, documentId, PROJECT_BRIEF_TITLE)
    return document
  }

  function clearPendingProjectBriefRevision(command: Readonly<CoreDocumentRevisionCreateCommand>): void {
    if (pendingProjectBriefRevisionCommands.get(command.document_id) === command) {
      pendingProjectBriefRevisionCommands.delete(command.document_id)
    }
  }

  async function submitProjectBriefRevision(
    workspaceId: string,
    documentId: string,
    baseRevisionId: string | null,
    content: Readonly<ProjectBriefContent>,
    actor: string,
    serialized: string,
  ): Promise<CoreProjectBriefSnapshot> {
    const pending = pendingProjectBriefRevisionCommands.get(documentId)
    let command: CoreDocumentRevisionCreateCommand
    if (pending !== undefined) {
      if (pending.schema !== 'core-document-revision-create-command/v1'
        || pending.workspace_id !== workspaceId
        || pending.document_id !== documentId
        || pending.base_revision_id !== baseRevisionId
        || pending.content !== serialized
        || pending.created_by !== actor
        || pending.payload_schema !== PROJECT_BRIEF_PAYLOAD_SCHEMA
        || pending.source_candidate_id !== null) {
        throw new Error('Core Project Brief has an unresolved pending Revision; retry the exact unchanged input')
      }
      command = pending
    } else {
      command = {
        schema: 'core-document-revision-create-command/v1',
        operation_key: ids.next('operation'),
        revision_id: ids.next('revision'),
        workspace_id: workspaceId,
        document_id: documentId,
        base_revision_id: baseRevisionId,
        content: serialized,
        created_by: actor,
        source_candidate_id: null,
        payload_schema: PROJECT_BRIEF_PAYLOAD_SCHEMA,
      }
      pendingProjectBriefRevisionCommands.set(documentId, command)
    }

    let revision: Readonly<CoreRevision>
    try {
      revision = await gateway.request('document.revision.create', command)
    } catch (error: unknown) {
      if (isStaleCas(error)) {
        clearPendingProjectBriefRevision(command)
        try {
          await readProjectBriefFromDocument(workspaceId, documentId, actor, command.base_revision_id)
        } catch {
          // The authoritative reload is best-effort; stale CAS remains visible either way.
        }
        throw new Error('Core Project Brief stale CAS; authoritative state was reloaded and no overwrite was attempted')
      }
      if (isDefinitePreEffectRejection(error)) clearPendingProjectBriefRevision(command)
      throw error
    }
    assertProjectBriefRevision(revision, workspaceId, documentId, command.revision_id, actor, command.base_revision_id)
    const verified = await readProjectBriefFromDocument(
      workspaceId,
      documentId,
      actor,
      command.base_revision_id,
    )
    if (verified.revisionId !== command.revision_id || !projectBriefContentsEqual(verified.content, content)) {
      throw new Error('Core Project Brief Revision was not durably verified with the requested content')
    }
    clearPendingProjectBriefRevision(command)
    pendingProjectBriefDocumentCommands.delete(documentId)
    return verified
  }

  async function persistProjectBrief(
    workspaceId: string,
    content: Readonly<ProjectBriefContent>,
    actor: string,
  ): Promise<CoreProjectBriefSnapshot> {
    const serialized = serializeProjectBriefContent(content, workspaceId)
    const documentId = await projectBriefDocumentId(workspaceId)
    await ensureProjectBriefDocument(workspaceId, documentId)
    const pending = pendingProjectBriefRevisionCommands.get(documentId)
    if (pending !== undefined) {
      return submitProjectBriefRevision(
        workspaceId,
        documentId,
        pending.base_revision_id,
        content,
        actor,
        serialized,
      )
    }

    const document = await getProjectBriefDocument(workspaceId, documentId)

    if (document.current_revision_id !== null) {
      const current = await readProjectBriefFromDocument(workspaceId, documentId, actor)
      if (projectBriefContentsEqual(current.content, content)) {
        pendingProjectBriefDocumentCommands.delete(documentId)
        return current
      }
      return submitProjectBriefRevision(workspaceId, documentId, current.revisionId, content, actor, serialized)
    }

    return submitProjectBriefRevision(workspaceId, documentId, null, content, actor, serialized)
  }

  async function saveProjectBrief(
    workspaceIdInput: string,
    input: Readonly<ProjectBriefHomeInput>,
  ): Promise<CoreProjectBriefSnapshot> {
    const workspaceId = assertNonEmpty(workspaceIdInput, 'Workspace identity')
    const actor = authoritativeActor()
    const preflight = preflightProjectBriefInput(workspaceId, input)
    return persistProjectBrief(workspaceId, preflight.content, actor)
  }

  async function createProjectWithBrief(
    title: string,
    input: Readonly<ProjectBriefHomeInput>,
  ): Promise<CoreProjectBriefCreateResult> {
    const normalizedTitle = assertNonEmpty(title, 'Project title')
    const actor = authoritativeActor()
    let pending = pendingProjectBriefCreates.get(normalizedTitle)
    if (pending === undefined) {
      pending = {
        workspaceCommand: {
          schema: 'core-workspace-create-command/v1',
          operation_key: ids.next('operation'),
          workspace_id: ids.next('workspace'),
          workspace_kind: 'WritingProject',
          title: normalizedTitle,
        },
      }
      pendingProjectBriefCreates.set(normalizedTitle, pending)
    }

    const preflight = preflightProjectBriefInput(pending.workspaceCommand.workspace_id, input)
    if (pending.fingerprint === undefined) {
      pending.fingerprint = preflight.fingerprint
      pending.content = preflight.content
    } else if (pending.fingerprint !== preflight.fingerprint) {
      throw new Error('Core Project Brief creation is pending; retry the exact unchanged input')
    }

    if (pending.workspace === undefined) {
      let created: Readonly<CoreWorkspace>
      try {
        created = await gateway.request('workspace.create', pending.workspaceCommand)
      } catch (error: unknown) {
        if (isDefinitePreEffectRejection(error)) pendingProjectBriefCreates.delete(normalizedTitle)
        throw error
      }
      if (created.schema !== 'core-workspace/v1'
        || created.workspace_id !== pending.workspaceCommand.workspace_id
        || created.workspace_kind !== pending.workspaceCommand.workspace_kind
        || created.title !== pending.workspaceCommand.title) {
        throw new Error('Core Workspace create returned a different identity, kind, or title')
      }
      pending.workspace = projectFromWorkspace(created)
    }

    const project = pending.workspace
    try {
      const verifiedWorkspace = await gateway.request('workspace.get', {
        schema: 'core-workspace-get-query/v1', workspace_id: project.workspaceId,
      })
      if (verifiedWorkspace.schema !== 'core-workspace/v1'
        || verifiedWorkspace.workspace_id !== project.workspaceId
        || verifiedWorkspace.workspace_kind !== 'WritingProject'
        || verifiedWorkspace.title !== project.title) {
        throw new Error('Core Workspace verification crossed its exact identity, kind, or title')
      }
      pending.workspace = projectFromWorkspace(verifiedWorkspace)
      const content = pending.content
      if (content === undefined) throw new Error('Core Project Brief pending create lost its validated content')
      const brief = await persistProjectBrief(project.workspaceId, content, actor)
      pendingProjectBriefCreates.delete(normalizedTitle)
      return { workspaceId: project.workspaceId, project: pending.workspace, brief }
    } catch (error: unknown) {
      throw new CoreProjectBriefPartialCreateError(project.workspaceId, error)
    }
  }

  return {
    listProjects,
    createProject,
    createProjectWithBrief,
    deleteProject,
    createChapter,
    loadWorkbench,
    loadProjectBrief,
    saveProjectBrief,
    openChapter,
    saveChapter,
  }
}

export type CoreFlows = ReturnType<typeof createCoreFlows>
