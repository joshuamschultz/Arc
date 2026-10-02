export const SOURCE_FILTERS = ['builtin', 'agent', 'extension', 'module'] as const
export type SourceFilter = (typeof SOURCE_FILTERS)[number]
export type TypeFilter = 'all' | 'tools' | 'skills'

export interface CapabilityFilters {
  agent: string
  type: TypeFilter
  /** Selected source chips. Empty means no source filter is applied. */
  sources: ReadonlySet<SourceFilter>
}

interface ToolLike {
  agents?: string[]
  source?: string
}
interface SkillLike {
  agent_id?: string
  source?: string
}

// A missing OR empty source is an agent-origin row ("??" would keep "").
const sourceOf = (row: { source?: string }): string => row.source || 'agent'

const sourceMatches = (row: { source?: string }, sources: ReadonlySet<SourceFilter>): boolean =>
  sources.size === 0 || sources.has(sourceOf(row) as SourceFilter)

/** Apply the Agent / Type / Source filters. The type filter empties the
 *  excluded kind so the count cards always equal what the tables show. */
export function filterCapabilities<T extends ToolLike, S extends SkillLike>(
  tools: T[],
  skills: S[],
  { agent, type, sources }: CapabilityFilters,
): { tools: T[]; skills: S[] } {
  return {
    tools:
      type === 'skills'
        ? []
        : tools.filter(
            (t) => (agent === 'all' || (t.agents ?? []).includes(agent)) && sourceMatches(t, sources),
          ),
    skills:
      type === 'tools'
        ? []
        : skills.filter((s) => (agent === 'all' || s.agent_id === agent) && sourceMatches(s, sources)),
  }
}
