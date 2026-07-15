import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { PackageCheck, Wrench, Sparkles, Check, X } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { Button } from '@/components/ui/button'
import { QueryState, EmptyState } from '@/components/states'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useGatedCapabilities, type GatedCapability } from '@/lib/queries'
import { apiPost, ApiError } from '@/lib/api'
import { shortId } from '@/lib/format'
import { cn } from '@/lib/utils'

// Human-readable gloss for the loader's gate reasons — the operator shouldn't
// need to know the internal verdict tokens to make the call.
const STATUS_LABEL: Record<GatedCapability['status'], string> = {
  deny: 'denied',
  new_sighting: 'new sighting',
  unsigned: 'unsigned',
  invalid: 'invalid signature',
  error: 'error',
}

// Severity coloring: an active denial or a verification error is red; a first
// sighting or a merely-unsigned artifact is amber (needs a look, not alarm); an
// invalid signature is neutral — the artifact is simply not trusted as-is.
const STATUS_CLASS: Record<GatedCapability['status'], string> = {
  deny: 'border-destructive/30 bg-destructive/10 text-destructive',
  error: 'border-destructive/30 bg-destructive/10 text-destructive',
  new_sighting:
    'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  unsigned:
    'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  invalid: 'border-border bg-muted/40 text-muted-foreground',
}

function GatedCard({ c }: { c: GatedCapability }) {
  const queryClient = useQueryClient()
  const [operatorMode] = useOperatorMode()
  const [busy, setBusy] = useState<'approve' | 'disapprove' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const resolve = async (decision: 'approve' | 'disapprove') => {
    setBusy(decision)
    setError(null)
    try {
      await apiPost(`/api/trust/${decision}`, { agent_id: c.agent_id, name: c.name })
      await queryClient.invalidateQueries({ queryKey: ['trust', 'gated'] })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : `Could not ${decision}`)
      setBusy(null)
    }
  }

  const KindIcon = c.kind === 'skill' ? Sparkles : Wrench

  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
      <div className="flex items-start gap-3">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-primary/15 text-foreground [&>svg]:size-4">
          <KindIcon />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="font-semibold text-foreground">{c.name}</span>
            <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] font-medium capitalize text-muted-foreground">
              {c.kind}
            </span>
            <span
              className={cn(
                'rounded-sm border px-1.5 py-0.5 text-[11px] font-medium',
                STATUS_CLASS[c.status],
              )}
            >
              {STATUS_LABEL[c.status]}
            </span>
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
            <span className="font-mono text-foreground/80">{shortId(c.hash)}</span>
            <span className="truncate font-mono text-muted-foreground/70">{c.path}</span>
          </div>
          {c.detail && <p className="mt-2 text-xs text-muted-foreground">{c.detail}</p>}

          {operatorMode ? (
            <div className="mt-3 flex items-center gap-2">
              <Button size="sm" onClick={() => resolve('approve')} disabled={busy !== null}>
                <Check className="size-3.5" /> Approve
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => resolve('disapprove')}
                disabled={busy !== null}
                className="text-destructive hover:text-destructive"
              >
                <X className="size-3.5" /> Disapprove
              </Button>
              {error && <span className="text-xs text-destructive">{error}</span>}
            </div>
          ) : (
            <p className="mt-3 text-xs italic text-muted-foreground/80">
              Enable operator mode to approve or disapprove.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}

// Preserve first-seen order of agents while bucketing their gated items, so the
// grouping is stable across the 4s poll rather than reshuffling each refetch.
function groupByAgent(gated: GatedCapability[]): [string, GatedCapability[]][] {
  const groups = new Map<string, GatedCapability[]>()
  for (const c of gated) {
    const bucket = groups.get(c.agent_label)
    if (bucket) bucket.push(c)
    else groups.set(c.agent_label, [c])
  }
  return [...groups.entries()]
}

export function GatedCapabilitiesPage() {
  const gated = useGatedCapabilities()

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Gated"
        description="Tools and skills the loader quarantined pending operator trust."
        actions={<OperatorModeToggle />}
      />
      <div className="flex-1 overflow-auto p-6">
        <QueryState
          query={gated}
          isEmpty={(data) => data.gated.length === 0}
          empty={
            <EmptyState
              icon={<PackageCheck className="size-7" />}
              title="No gated capabilities"
              description="Tools and skills held back by signing or policy checks will appear here."
            />
          }
        >
          {(data) => (
            <div className="mx-auto flex max-w-3xl flex-col gap-6">
              {groupByAgent(data.gated).map(([agentLabel, items]) => (
                <div key={agentLabel} className="flex flex-col gap-3">
                  <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    {agentLabel}
                  </h2>
                  {items.map((c) => (
                    <GatedCard key={`${c.agent_id}:${c.name}`} c={c} />
                  ))}
                </div>
              ))}
            </div>
          )}
        </QueryState>
      </div>
    </div>
  )
}
