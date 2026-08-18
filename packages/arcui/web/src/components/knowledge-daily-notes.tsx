import { useState } from 'react'
import { EmptyState, ErrorState, LoadingRows, QueryState } from '@/components/states'
import { useAgentDailyNote, useAgentDailyNotes, useRoster } from '@/lib/queries'
import { cn } from '@/lib/utils'
import type { DailyNoteDetail } from '@/lib/types'

const _WIKI_LINK = /\[\[([^\]]+)\]\]/g

/** The agent a note belongs to — its display name and roster color (a dot). */
interface NoteAgent {
  name: string
  color: string
}

type NoteSource = 'script' | 'skill' | 'agent'

/** Chip palette for the note's source: script=blue, skill=violet, agent=emerald. */
const _SOURCE_STYLES: Record<NoteSource, string> = {
  script: 'border-blue-500/30 bg-blue-500/10 text-blue-400',
  skill: 'border-violet-500/30 bg-violet-500/10 text-violet-400',
  agent: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400',
}

/**
 * Classify what produced a curated daily note. The on-disk note carries no source
 * field (only `day` + `classification`), so we read an optional `source`/`origin`
 * if the store ever stamps one and otherwise fall back to `agent` — consolidation
 * (the agent's own sleep pass) is what authors these rollups today.
 */
function noteSource(meta: object): NoteSource {
  const m = meta as { source?: string; origin?: string }
  const raw = String(m.source ?? m.origin ?? '').toLowerCase()
  if (raw === 'script' || raw === 'skill' || raw === 'agent') return raw
  return 'agent'
}

/** A small color-coded chip naming the note's source (script / skill / agent). */
function SourceTag({ source }: { source: NoteSource }) {
  return (
    <span
      className={cn(
        'shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide',
        _SOURCE_STYLES[source],
      )}
    >
      {source}
    </span>
  )
}

/** A dot + name chip identifying the owning agent, tinted with its roster color. */
function AgentTag({ agent }: { agent: NoteAgent }) {
  return (
    <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
      <span
        aria-hidden
        className="size-1.5 rounded-full"
        style={{ background: agent.color || 'var(--primary)' }}
      />
      {agent.name}
    </span>
  )
}

/** Render a bullet's text, lightly styling `[[slug]]` wiki-links inline. */
function Bullet({ text }: { text: string }) {
  const parts = text.split(_WIKI_LINK)
  // String.split with a capturing group interleaves plain text and captured
  // groups: [before, slug, between, slug, after, ...] — odd indices are links.
  return (
    <li className="text-sm text-foreground">
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <span
            key={i}
            className="rounded-sm border border-primary/20 bg-primary/10 px-1 py-0.5 font-mono text-[11px] text-primary"
          >
            {part}
          </span>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </li>
  )
}

const _SECTIONS: [keyof Omit<DailyNoteDetail, 'day' | 'classification'>, string][] = [
  ['timeline', 'Timeline'],
  ['discussions', 'Discussions'],
  ['decisions', 'Decisions'],
  ['people', 'People'],
  ['goals', 'Goals'],
  ['tasks', 'Tasks'],
]

function DayDetail({ agentId, day, agent }: { agentId: string; day: string; agent: NoteAgent }) {
  const query = useAgentDailyNote(agentId, day)
  if (query.isLoading) return <LoadingRows rows={8} />
  if (query.isError) return <ErrorState error={query.error} />
  if (!query.data) return null
  const detail = query.data

  const sections = _SECTIONS.filter(([field]) => detail[field].length > 0)

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2 text-xs text-muted-foreground">
        <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-foreground">
          {detail.day}
        </span>
        <AgentTag agent={agent} />
        <SourceTag source={noteSource(detail)} />
        <span className="ml-auto rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
          {detail.classification}
        </span>
      </div>
      <div className="flex-1 space-y-5 overflow-auto p-4">
        {sections.length === 0 ? (
          <p className="text-xs text-muted-foreground">No sections recorded for this day.</p>
        ) : (
          sections.map(([field, heading]) => (
            <section key={field} className="space-y-1.5">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                {heading}
              </h3>
              <ul className="list-disc space-y-1 pl-4">
                {detail[field].map((bullet, i) => (
                  <Bullet key={i} text={bullet} />
                ))}
              </ul>
            </section>
          ))
        )}
      </div>
    </div>
  )
}

/** Two-pane daily-notes browser (U4): left = days newest-first, right = the
 *  selected day's curated sections (Timeline/Discussions/Decisions/People/
 *  Goals/Tasks), following FileTree's left-list/right-detail layout. */
export function DailyNotesBrowser({ agentId }: { agentId: string }) {
  const notes = useAgentDailyNotes(agentId)
  const roster = useRoster()
  const [selected, setSelected] = useState<string | null>(null)

  const match = (roster.data?.agents ?? []).find((a) => a.agent_id === agentId)
  const agent: NoteAgent = {
    name: match?.display_name || match?.name || agentId,
    color: typeof match?.color === 'string' ? match.color : '',
  }

  return (
    <QueryState
      query={notes}
      isEmpty={(d) => d.items.length === 0}
      empty={
        <EmptyState
          title="No daily notes yet"
          description="This agent hasn't accumulated a curated daily rollup."
        />
      }
    >
      {(data) => {
        // Newest day is the default selection until the user picks one.
        const active = selected ?? data.items[0]?.day ?? null
        return (
        <div className="grid h-[560px] grid-cols-[minmax(180px,240px)_1fr] overflow-hidden rounded-lg border border-border bg-card shadow-xs">
          <div className="overflow-auto border-r border-border bg-card/60 p-2">
            <div className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Days
            </div>
            <ul>
              {data.items.map((meta) => (
                <li key={meta.day}>
                  <button
                    type="button"
                    onClick={() => setSelected(meta.day)}
                    className={cn(
                      'relative flex w-full cursor-pointer items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors duration-150 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60',
                      active === meta.day
                        ? 'bg-primary/8 text-foreground before:absolute before:inset-y-1 before:left-0 before:w-[2px] before:rounded-full before:bg-primary'
                        : 'text-muted-foreground',
                    )}
                  >
                    <span className="font-mono text-xs">{meta.day}</span>
                    <span className="flex shrink-0 items-center gap-1">
                      <SourceTag source={noteSource(meta)} />
                      <span className="rounded-full border border-border bg-muted/40 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                        {meta.classification}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
          <div className="overflow-hidden">
            {active ? (
              <DayDetail agentId={agentId} day={active} agent={agent} />
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                Select a day to view
              </div>
            )}
          </div>
        </div>
        )
      }}
    </QueryState>
  )
}
