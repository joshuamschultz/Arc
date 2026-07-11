import { EmptyState, QueryState } from '@/components/states'
import { useAgentProcedures } from '@/lib/queries'

/** How-to cards promoted by repetition (U3): a repeated action-sequence becomes
 *  a numbered procedure. `use_count` is the promotion signal — how many times
 *  the sequence has recurred/been reused. */
export function ProcedureBrowser({ agentId }: { agentId: string }) {
  const procedures = useAgentProcedures(agentId)

  return (
    <QueryState
      query={procedures}
      isEmpty={(d) => d.items.length === 0}
      empty={
        <EmptyState
          title="No procedures promoted yet"
          description="This agent hasn't repeated an action sequence enough times to promote a how-to card."
        />
      }
    >
      {(data) => (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {data.items.map((proc) => (
            <div
              key={proc.slug}
              className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4"
            >
              <div className="flex items-start justify-between gap-2">
                <h3 className="text-sm font-medium text-foreground">{proc.title}</h3>
                <span className="shrink-0 rounded bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                  ×{proc.use_count}
                </span>
              </div>
              {proc.when_to_use && (
                <p className="text-xs text-muted-foreground">
                  <span className="text-muted-foreground/70">when to use:</span> {proc.when_to_use}
                </p>
              )}
              {proc.steps.length > 0 && (
                <ol className="list-decimal space-y-1 pl-4 text-sm text-foreground">
                  {proc.steps.map((step, i) => (
                    <li key={i}>{step}</li>
                  ))}
                </ol>
              )}
              <div className="mt-auto flex items-center justify-between pt-1 text-[11px] text-muted-foreground">
                <span className="font-mono">{proc.slug}</span>
                <span className="rounded bg-muted/40 px-1.5 py-0.5">{proc.classification}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </QueryState>
  )
}
