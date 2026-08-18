import { useNavigate } from 'react-router-dom'
import { GitBranch, Zap } from 'lucide-react'
import { StatusChip } from '@/components/ai'
import { SignedSeal } from '@/components/hitl'
import { relativeTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { WorkflowStatus, WorkflowSummary } from '@/lib/types'

/* ---------------------------------------------------------------------------
 * Workflows list presentation. Distinctive, summary-first cards for a business
 * operator scanning named multi-agent workflows — lifecycle state (draft /
 * signed / archived) read at a glance, the tan seal felt on signed ones, and
 * the last run's real activity shown with the shared StatusChip. Token-driven,
 * flat. See REDESIGN.md §5–6, §12.2.
 * ------------------------------------------------------------------------- */

const LIFECYCLE: Record<WorkflowStatus, { label: string; className: string }> = {
  draft: {
    label: 'Draft',
    className: 'border-status-warning/30 bg-status-warning/10 text-status-warning',
  },
  signed: {
    label: 'Signed',
    className: 'border-signed/30 bg-signed/10 text-signed',
  },
  archived: {
    label: 'Archived',
    className: 'border-border bg-muted/40 text-muted-foreground',
  },
}

/** The workflow's own lifecycle state as a quiet, plain-language chip. */
export function WorkflowLifecycle({ status }: { status: WorkflowStatus }) {
  const l = LIFECYCLE[status] ?? LIFECYCLE.draft
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center rounded-md border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.08em]',
        l.className,
      )}
    >
      {l.label}
    </span>
  )
}

/** One counted total in the summary strip. */
function SummaryTile({
  label,
  value,
  signed = false,
}: {
  label: string
  value: number
  signed?: boolean
}) {
  return (
    <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-card p-3">
      <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        {label}
      </span>
      <span
        className={cn(
          'font-display text-[20px] font-extrabold leading-none tabular-nums tracking-tight',
          signed ? 'text-signed' : 'text-foreground',
        )}
      >
        {value}
      </span>
    </div>
  )
}

/** Summary-first count strip above the workflow grid. */
export function WorkflowSummaryStrip({ workflows }: { workflows: WorkflowSummary[] }) {
  const signed = workflows.filter((w) => w.status === 'signed').length
  const drafts = workflows.filter((w) => w.status === 'draft').length
  const archived = workflows.filter((w) => w.status === 'archived').length
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      <SummaryTile label="Total" value={workflows.length} />
      <SummaryTile label="Signed" value={signed} signed />
      <SummaryTile label="Drafts" value={drafts} />
      <SummaryTile label="Archived" value={archived} />
    </div>
  )
}

/** A distinctive card for one workflow — links through to `/workflows/:id`. */
export function WorkflowCard({ w }: { w: WorkflowSummary }) {
  const navigate = useNavigate()
  const lastRun = w.last_run
  const triggerType =
    w.trigger != null ? String((w.trigger as Record<string, unknown>).type ?? 'manual') : 'manual'

  return (
    <button
      type="button"
      onClick={() => navigate(`/workflows/${encodeURIComponent(w.id)}`)}
      className="group flex flex-col gap-3 rounded-lg border border-border bg-card p-4 text-left transition-colors hover:border-foreground/15 hover:bg-muted/30"
    >
      <div className="flex items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-md bg-primary/12 text-primary">
          <GitBranch className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate font-display text-[15px] font-bold tracking-tight text-foreground">
              {w.name || w.id}
            </span>
            {w.status === 'signed' && (
              <span className="shrink-0">
                <SignedSeal />
              </span>
            )}
          </div>
          <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <span className="font-mono tabular-nums">v{w.version}</span>
            {w.name && (
              <>
                <span aria-hidden>·</span>
                <span className="truncate font-mono">{w.id}</span>
              </>
            )}
          </div>
        </div>
        <WorkflowLifecycle status={w.status} />
      </div>

      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Zap className="size-3 shrink-0 text-muted-foreground/70" />
        <span className="capitalize">{triggerType.replace(/_/g, ' ')}</span>
        <span className="text-muted-foreground/50">trigger</span>
      </div>

      <div className="flex items-center justify-between gap-2 border-t border-border pt-3">
        <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Last run
        </span>
        {lastRun ? (
          <div className="flex items-center gap-2">
            <StatusChip value={lastRun.status} />
            {lastRun.ended_at && (
              <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
                {relativeTime(lastRun.ended_at)}
              </span>
            )}
          </div>
        ) : (
          <span className="text-xs text-muted-foreground/70">Never run</span>
        )}
      </div>
    </button>
  )
}
