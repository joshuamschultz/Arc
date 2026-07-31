import { EmptyState, QueryState } from '@/components/states'
import { useAgentEvents } from '@/lib/queries'

/** Things that HAPPENED in the user's life — a meeting held, a sale closed, a
 *  call taken. Not a fact about someone and not a method: an occurrence, with
 *  the date it happened (distinct from when memory wrote it down), who was in
 *  it, and how it came out. Participants are entity slugs, so an event is also
 *  an edge in the shared graph. */
export function EventBrowser({ agentId }: { agentId: string }) {
  const events = useAgentEvents(agentId)

  return (
    <QueryState
      query={events}
      isEmpty={(d) => d.items.length === 0}
      empty={
        <EmptyState
          title="No events recorded yet"
          description="Nothing has been distilled as an occurrence. Events are minted by the nightly consolidation pass from the session conversation."
        />
      }
    >
      {(data) => (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {data.items.map((ev) => (
            <div
              key={ev.slug}
              className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4 shadow-xs transition-colors duration-150 hover:border-primary/25"
            >
              <div className="flex items-start justify-between gap-2">
                <h3 className="text-sm font-medium text-foreground">{ev.title}</h3>
                {ev.date && (
                  <span className="shrink-0 rounded-full border border-primary/20 bg-primary/10 px-2 py-0.5 font-mono text-[11px] tabular-nums text-primary">
                    {ev.date}
                  </span>
                )}
              </div>

              {ev.summary && <p className="text-sm text-foreground">{ev.summary}</p>}

              {ev.outcome && (
                <p className="text-xs text-muted-foreground">
                  <span className="text-muted-foreground/70">outcome:</span> {ev.outcome}
                </p>
              )}

              {ev.participants.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {ev.participants.map((slug) => (
                    <span
                      key={slug}
                      className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
                    >
                      {slug}
                    </span>
                  ))}
                </div>
              )}

              <div className="mt-auto flex items-center justify-between gap-2 pt-1 text-[11px] text-muted-foreground">
                {ev.event_type && (
                  <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px] uppercase tracking-wide">
                    {ev.event_type}
                  </span>
                )}
                {/* Recorded is the audit date, not the occurrence — kept visible so a
                    back-dated event is never mistaken for a late one. */}
                {ev.recorded && ev.recorded !== ev.date && (
                  <span className="font-mono text-[10px] text-muted-foreground/70">
                    recorded {ev.recorded}
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </QueryState>
  )
}
