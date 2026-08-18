import type { ReactNode } from 'react'
import {
  Boxes,
  CalendarDays,
  GitBranch,
  Layers,
  Lightbulb,
  ListChecks,
  NotebookPen,
  Share2,
  Sparkles,
} from 'lucide-react'
import { FileTree } from '@/components/file-tree'
import { JsonBlock } from '@/components/json-block'
import { fmtNumber } from '@/lib/format'
import type { KnowledgeResponse } from '@/lib/queries'

/** Uppercase micro-label + optional hint above each overview section. */
export function SectionLabel({
  children,
  hint,
}: {
  children: ReactNode
  hint?: ReactNode
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <h2 className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        <span aria-hidden className="h-3 w-[2px] rounded-full bg-primary/70" />
        {children}
      </h2>
      {hint && <span className="text-[11px] text-muted-foreground/70">{hint}</span>}
    </div>
  )
}

/** Coerce an unknown scalar to a display number, defaulting to 0. */
function asCount(v: unknown): number {
  if (typeof v === 'number') return v
  const n = Number(v)
  return Number.isFinite(n) ? n : 0
}

/** A memory type shown as a big count that deep-links into its browser tab. */
const MEMORY_TILES: {
  key: string
  label: string
  tab: string
  icon: ReactNode
}[] = [
  { key: 'episodic', label: 'Raw stream', tab: 'memories', icon: <Layers className="size-4" /> },
  { key: 'entities', label: 'Entities', tab: 'entities', icon: <Boxes className="size-4" /> },
  { key: 'insights', label: 'Insights', tab: 'insights', icon: <Lightbulb className="size-4" /> },
  {
    key: 'procedures',
    label: 'Procedures',
    tab: 'procedures',
    icon: <ListChecks className="size-4" />,
  },
  { key: 'events', label: 'Events', tab: 'events', icon: <CalendarDays className="size-4" /> },
  {
    key: 'daily_notes',
    label: 'Daily notes',
    tab: 'daily-notes',
    icon: <NotebookPen className="size-4" />,
  },
]

/** A stat tile that navigates to a browser tab when clicked. */
function StatTileLink({
  onClick,
  label,
  children,
}: {
  onClick: () => void
  label: string
  children: ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={`View ${label}`}
      className="group block rounded-lg text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 [&>div]:group-hover:border-primary/40"
    >
      {children}
    </button>
  )
}

/** A tight one-line stat tile: icon + big number + micro-label, minimal chrome.
 *  Sized so a full row of six fits without wrapping. */
function CompactStat({
  label,
  value,
  icon,
}: {
  label: string
  value: ReactNode
  icon: ReactNode
}) {
  return (
    <div className="flex items-center gap-2.5 rounded-lg border border-border bg-card px-3 py-2 transition-colors hover:border-foreground/15">
      <span className="shrink-0 text-muted-foreground/70 transition-colors group-hover:text-primary">
        {icon}
      </span>
      <div className="min-w-0">
        <div className="font-display text-lg font-bold leading-none tabular-nums tracking-tight text-foreground">
          {value}
        </div>
        <div className="truncate text-[9px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          {label}
        </div>
      </div>
    </div>
  )
}

/**
 * The Knowledge overview: a summary-first hero of memory counts that deep-link
 * into each browser, the graph size, and the agent's workspace tree. Presentation
 * only — the sub-tab browsers own detail.
 */
export function KnowledgeOverview({
  data,
  agentId,
  onNavigate,
}: {
  data: KnowledgeResponse
  agentId: string
  onNavigate: (tab: string) => void
}) {
  const memory = data.memory ?? {}
  const graph = data.graph ?? {}
  const context = data.context ?? {}

  const totalMemories = MEMORY_TILES.reduce((sum, t) => sum + asCount(memory[t.key]), 0)

  return (
    <div className="space-y-8">
      <section className="space-y-3">
        <SectionLabel hint={`${fmtNumber(totalMemories)} memories in all`}>
          Knowledge base
        </SectionLabel>
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
          {MEMORY_TILES.map((t) => (
            <StatTileLink key={t.key} label={t.label} onClick={() => onNavigate(t.tab)}>
              <CompactStat label={t.label} value={fmtNumber(asCount(memory[t.key]))} icon={t.icon} />
            </StatTileLink>
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <SectionLabel hint="Entities and their links">Knowledge graph</SectionLabel>
        <div className="grid grid-cols-2 gap-2 sm:max-w-xs">
          <StatTileLink label="graph entities" onClick={() => onNavigate('entities')}>
            <CompactStat
              label="Nodes"
              value={fmtNumber(asCount(graph.nodes))}
              icon={<Share2 className="size-4" />}
            />
          </StatTileLink>
          <StatTileLink label="graph links" onClick={() => onNavigate('entities')}>
            <CompactStat
              label="Links"
              value={fmtNumber(asCount(graph.edges))}
              icon={<GitBranch className="size-4" />}
            />
          </StatTileLink>
        </div>
      </section>

      <section className="space-y-3">
        <SectionLabel hint="Files in the agent's home">Workspace</SectionLabel>
        <div className="rounded-lg border border-border bg-card p-3">
          <FileTree agentId={agentId} />
        </div>
      </section>

      {/* Raw store payload kept for operators who want the exact numbers. */}
      <details className="group rounded-lg border border-border bg-card">
        <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-2.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground transition-colors hover:text-foreground">
          <Sparkles className="size-3.5 text-signed" />
          Raw summary payload
        </summary>
        <div className="px-4 pb-4">
          <JsonBlock value={{ context, graph, memory }} className="max-h-72" />
        </div>
      </details>
    </div>
  )
}
