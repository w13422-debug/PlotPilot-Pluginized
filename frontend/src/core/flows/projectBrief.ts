import projectBriefSchema from './projectBrief.schema.json' with { type: 'json' }
import { canonicalJson, sha256Hex, utf8 } from '../../contracts/canonical.ts'
import { parseJsonSchema, type JsonSchema } from '../../contracts/schema-ingress.ts'
import type {
  ProjectBriefContent,
  ProjectBriefHomeInput,
  ProjectBriefLengthTier,
} from '../../contracts/types.ts'

export const PROJECT_BRIEF_DOCUMENT_TYPE = 'plotpilot.project-brief' as const
export const PROJECT_BRIEF_PAYLOAD_SCHEMA = 'plotpilot.project-brief/v1' as const
export const PROJECT_BRIEF_TITLE = 'Project Brief' as const
export const PROJECT_BRIEF_MAX_SERIALIZED_BYTES = 8_388_608

const PREFLIGHT_WORKSPACE_ID = 'project-brief-preflight'
const TARGET_WORDS_BY_TIER: Readonly<Record<ProjectBriefLengthTier, number>> = {
  short: 300_000,
  standard: 1_000_000,
  epic: 3_000_000,
}

type UnknownRecord = Record<string, unknown>

function asRecord(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : Object.create(null) as UnknownRecord
}

function assertWorkspaceId(value: unknown): string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(value)) {
    throw new Error('Project Brief workspace_id must be a Core identity')
  }
  return value
}

function assertProjectBriefSemantics(content: Readonly<ProjectBriefContent>): void {
  const length = content.length
  const target = length.use_custom
    ? length.custom_chapters * length.custom_words_per_chapter
    : TARGET_WORDS_BY_TIER[length.tier]
  if (!Number.isSafeInteger(target) || target < 1 || content.target_words !== target) {
    throw new Error('Project Brief target_words does not match its declared length')
  }
}

function serializedByteLength(serialized: string): number {
  return utf8(serialized).byteLength
}

/** Strictly validates closed persisted content and returns a deep-frozen copy. */
export function parseProjectBriefContent(value: unknown, expectedWorkspaceId?: string): Readonly<ProjectBriefContent> {
  const content = parseJsonSchema<ProjectBriefContent>(
    value,
    projectBriefSchema as JsonSchema,
    PROJECT_BRIEF_PAYLOAD_SCHEMA,
  )
  assertProjectBriefSemantics(content)
  if (expectedWorkspaceId !== undefined && content.workspace_id !== assertWorkspaceId(expectedWorkspaceId)) {
    throw new Error('Project Brief content crossed its expected Workspace identity')
  }
  return content
}

/** Serializes only content that passed the closed v1 codec and byte-size bound. */
export function serializeProjectBriefContent(value: unknown, expectedWorkspaceId?: string): string {
  const content = parseProjectBriefContent(value, expectedWorkspaceId)
  const serialized = JSON.stringify(content)
  if (serialized === undefined || serializedByteLength(serialized) > PROJECT_BRIEF_MAX_SERIALIZED_BYTES) {
    throw new Error('Project Brief serialized content exceeds the frozen revision limit')
  }
  return serialized
}

/** Reads untrusted Revision content as closed UTF-8 JSON. */
export function parseSerializedProjectBriefContent(serialized: unknown, expectedWorkspaceId?: string): Readonly<ProjectBriefContent> {
  if (typeof serialized !== 'string') throw new Error('Project Brief Revision content must be a string')
  if (serializedByteLength(serialized) > PROJECT_BRIEF_MAX_SERIALIZED_BYTES) {
    throw new Error('Project Brief serialized content exceeds the frozen revision limit')
  }
  let value: unknown
  try {
    value = JSON.parse(serialized)
  } catch {
    throw new Error('Project Brief Revision content is not valid JSON')
  }
  return parseProjectBriefContent(value, expectedWorkspaceId)
}

/** Builds the only v1 payload shape from the current Home controls without dropping fields. */
export function buildProjectBriefContent(workspaceIdInput: string, input: Readonly<ProjectBriefHomeInput> | unknown): Readonly<ProjectBriefContent> {
  const workspaceId = assertWorkspaceId(workspaceIdInput)
  const raw = asRecord(input)
  const useCustom = raw.useCustomLength === true
  const tier = raw.lengthTier
  const chapters = raw.customChapters
  const words = raw.customWordsPerChapter
  const targetWords = useCustom
    && typeof chapters === 'number'
    && typeof words === 'number'
    && Number.isSafeInteger(chapters)
    && Number.isSafeInteger(words)
    ? chapters * words
    : typeof tier === 'string' && raw.useCustomLength === false
      ? TARGET_WORDS_BY_TIER[tier as ProjectBriefLengthTier]
      : undefined

  return parseProjectBriefContent({
    workspace_id: workspaceId,
    premise: raw.premise,
    genres: { genre: raw.genre },
    target_words: targetWords,
    structure: {
      story_structure: raw.storyStructure,
      pacing_control: raw.pacingControl,
      writing_style: raw.writingStyle,
      special_requirements: raw.specialRequirements,
    },
    market: { world_preset: raw.worldPreset },
    length: {
      tier,
      use_custom: raw.useCustomLength,
      custom_chapters: chapters,
      custom_words_per_chapter: words,
    },
  }, workspaceId)
}

/** Validates all Home-derived values before a Workspace effect and freezes them for retry. */
export function preflightProjectBriefInput(input: Readonly<ProjectBriefHomeInput> | unknown): Readonly<{
  fingerprint: string
  content: ProjectBriefContent
}> {
  const content = buildProjectBriefContent(PREFLIGHT_WORKSPACE_ID, input)
  return Object.freeze({
    fingerprint: serializeProjectBriefContent(content, PREFLIGHT_WORKSPACE_ID),
    content,
  })
}

/** Rebinds a preflighted immutable intent to the verified Core Workspace identity. */
export function bindProjectBriefWorkspace(
  preflightContent: Readonly<ProjectBriefContent>,
  workspaceIdInput: string,
): Readonly<ProjectBriefContent> {
  const workspaceId = assertWorkspaceId(workspaceIdInput)
  return parseProjectBriefContent({ ...preflightContent, workspace_id: workspaceId }, workspaceId)
}

export function projectBriefContentsEqual(left: Readonly<ProjectBriefContent>, right: Readonly<ProjectBriefContent>): boolean {
  return canonicalJson(left) === canonicalJson(right)
}

/** project-brief: + lowercase SHA-256 of the UTF-8 Core Workspace identity. */
export async function projectBriefDocumentId(workspaceIdInput: string): Promise<string> {
  const workspaceId = assertWorkspaceId(workspaceIdInput)
  return `project-brief:${await sha256Hex(utf8(workspaceId))}`
}
