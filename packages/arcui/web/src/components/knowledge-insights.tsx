import { EmptyState, QueryState } from '@/components/states'
import { useAgentInsights } from '@/lib/queries'
import { fmtPercent } from '@/lib/format'
import { cn } from '@/lib/utils'

/** Confidence badge — same 0..1 tone ramp as the entity/memory importance badges. */
function ConfidenceBadge({ confidence }: { confidence: number }) {
  const tone =
    confidence >= 0.8
      ? 'bg-status-online/15 text-status-online'
      : confidence >= 0.4
        ? 'bg-status-warning/15 text-status-warning'
        : 'bg-muted text-muted-foreground'
  return (
    <span className={cn('rounded-full px-2 py-0.5 font-mono text-[11px] tabular-nums', tone)}>
      {fmtPercent(confidence)}
    </span>
  )
}

/** The curated glass-box centerpiece: minted pattern/thesis cards (U3). Each card
 *  leads with the statement — the abstraction itself — with the trigger (the
 *  mechanism-level situation it fires on) as a subtle secondary line and the
 *  cue vocabulary as small chips. */
export function InsightBrowser({ agentId }: { agentId: string }) {
  const insights = useAgentInsights(agentId)

  return (
    <QueryState
      query={insights}
      isEmpty={(d) => d.items.length === 0}
      empty={
        <EmptyState
          title="No insights minted yet"
          description="This agent hasn't generalized any patterns from its episodes."
        />
      }
    >
      {(data) => (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {data.items.map((insight) => (
            <div
              key={insight.id}
              className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4 shadow-xs transition-colors duration-150 hover:border-primary/25"
            >
              <div className="flex items-start justify-between gap-2">
                <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                  {insight.id}
                </span>
                <ConfidenceBadge confidence={insight.confidence} />
              </div>
              <p className="text-sm font-medium text-foreground">{insight.statement}</p>
              {insight.trigger && (
                <p className="text-xs text-muted-foreground">
                  <span className="text-muted-foreground/70">when:</span> {insight.trigger}
                </p>
              )}
              {insight.cues.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {insight.cues.map((cue) => (
                    <span
                      key={cue}
                      className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[11px] text-muted-foreground"
                    >
                      {cue}
                    </span>
                  ))}
                </div>
              )}
              <div className="mt-auto flex items-center justify-between pt-1 text-[11px] text-muted-foreground">
                <span className="tabular-nums">
                  {insight.instances.length} instance{insight.instances.length === 1 ? '' : 's'}
                </span>
                <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                  {insight.classification}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </QueryState>
  )
}
