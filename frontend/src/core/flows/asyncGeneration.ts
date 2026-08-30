export interface ScopedGenerationToken {
  readonly generation: number
  readonly scope: string
}

/**
 * Small fail-closed guard for async UI work. Starting or invalidating a
 * generation makes every previously captured response ineligible to commit.
 */
export class ScopedRequestGeneration {
  private generation = 0
  private scope: string | null = null

  begin(scope: string): ScopedGenerationToken {
    this.generation += 1
    this.scope = scope
    return { generation: this.generation, scope }
  }

  capture(): ScopedGenerationToken | null {
    return this.scope === null ? null : { generation: this.generation, scope: this.scope }
  }

  invalidate(): void {
    this.generation += 1
    this.scope = null
  }

  isCurrent(token: Readonly<ScopedGenerationToken>): boolean {
    return token.generation === this.generation && token.scope === this.scope
  }
}

export function reconcilePartialDeleteSelection(
  selectedIds: readonly string[],
  succeededIds: ReadonlySet<string>,
  authoritativeIds: ReadonlySet<string>,
): string[] {
  return [...new Set(selectedIds)].filter(id => !succeededIds.has(id) && authoritativeIds.has(id))
}
