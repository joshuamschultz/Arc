import { useId, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  PackageCheck,
  Wrench,
  Sparkles,
  Check,
  X,
  FileCode,
  Eye,
  EyeOff,
  ShieldCheck,
  ShieldOff,
} from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { Button } from '@/components/ui/button'
import { QueryState, EmptyState } from '@/components/states'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import {
  useCapabilitySource,
  useGatedCapabilities,
  type GatedCapability,
} from '@/lib/queries'
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
  loaded: 'loaded',
}

// Severity coloring: an active denial or a verification error is red; a first
// sighting or a merely-unsigned artifact is amber (needs a look, not alarm); an
// invalid signature is neutral — the artifact is simply not trusted as-is. A
// loaded capability is green: nothing is wrong with it, it is only here because
// the operator asked to see what they could re-sign.
const STATUS_CLASS: Record<GatedCapability['status'], string> = {
  deny: 'border-destructive/30 bg-destructive/10 text-destructive',
  error: 'border-destructive/30 bg-destructive/10 text-destructive',
  new_sighting:
    'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  unsigned:
    'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  invalid: 'border-border bg-muted/40 text-muted-foreground',
  loaded:
    'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
}

// Said once so the tooltip, the accessible description, and the visible hint all
// carry the same sentence: a greyed-out button that explains nothing is the
// rubber-stamp risk wearing a different coat.
const APPROVE_LOCKED = 'Review the source before signing.'

/** The artifact text itself — rendered as text, never as markup.
 *
 * This is executable source fetched from an artifact the loader has REFUSED to
 * trust. It goes in a `<pre>` and nowhere near `dangerouslySetInnerHTML`: the
 * whole point of the panel is to show the operator what the code says, not to
 * let the code say it (LLM05).
 */
function SourceBody({
  query,
  expectedHash,
}: {
  query: ReturnType<typeof useCapabilitySource>
  expectedHash: string
}) {
  if (query.isPending)
    return <p className="px-2.5 py-2 text-xs text-muted-foreground">Loading source…</p>
  if (query.isError || !query.data)
    return (
      <p className="px-2.5 py-2 text-xs text-destructive">
        {query.error instanceof ApiError ? query.error.message : 'Could not read the source'}
      </p>
    )
  return (
    <>
      <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words px-2.5 py-2 font-mono text-[11px] leading-relaxed text-foreground">
        {query.data.source}
      </pre>
      {query.data.hash !== expectedHash && (
        <p className="border-t border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-700 dark:text-amber-400">
          This artifact changed after it was read. Close and reopen to review what is on disk now.
        </p>
      )}
    </>
  )
}

function GatedCard({ c }: { c: GatedCapability }) {
  const queryClient = useQueryClient()
  const [operatorMode] = useOperatorMode()
  const [busy, setBusy] = useState<'approve' | 'disapprove' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const source = useCapabilitySource(c.agent_id, c.name, open)
  const panelId = useId()
  const hintId = useId()

  // REQ-322's gate. Approving authorizes THIS artifact to execute inside the
  // agent, so the action unlocks only while the source on screen is the source
  // that would be signed. Deriving that from the rendered hash — rather than
  // remembering "the panel was opened once" — means an artifact edited
  // mid-review re-locks the button on the next 4s poll instead of letting a
  // stale reading stand in for a fresh one. Disapprove is never gated:
  // withdrawing trust is always safe.
  //
  // The gate applies unchanged to re-signing. If anything it matters MORE
  // there: the operator already trusts this capability, which is exactly the
  // state of mind that rubber-stamps a file someone swapped underneath it.
  const reviewed = open && source.data?.hash === c.hash

  // A row that already carries a signature is a re-sign, and re-signing has a
  // different consequence from a first approval — it moves trust off whoever
  // signed it before, usually the agent itself. Saying so on the button is the
  // cheapest place to put that.
  const signed = c.signer_did !== ''

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

          {/* Who signed the bytes on disk. `status` cannot answer this: a
              capability the AGENT signed itself loads exactly like one the
              operator signed, and telling those apart is the whole reason an
              operator would re-sign. Full DID on hover — the row shows the
              prefix so a wall of them stays scannable. */}
          <div className="mt-1.5 flex flex-wrap items-center gap-x-1.5 text-xs">
            {signed ? (
              <>
                <ShieldCheck className="size-3.5 text-muted-foreground" />
                <span className="text-muted-foreground">signed by</span>
                <span className="font-mono text-foreground/80" title={c.signer_did}>
                  {shortId(c.signer_did, 28)}
                </span>
              </>
            ) : (
              <>
                <ShieldOff className="size-3.5 text-muted-foreground" />
                <span className="text-muted-foreground">unsigned</span>
              </>
            )}
          </div>
          {c.detail && <p className="mt-2 text-xs text-muted-foreground">{c.detail}</p>}

          {/* Any authed role may read the artifact — a viewer who cannot read
              it cannot review it. Only an operator may act on what they read. */}
          <div className="mt-3 overflow-hidden rounded-md border border-border">
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
              aria-controls={panelId}
              className="flex w-full items-center gap-2 bg-muted/30 px-2.5 py-1.5 text-left hover:bg-muted/50"
            >
              <FileCode className="size-3.5 text-muted-foreground" />
              <span className="text-xs font-medium text-foreground">
                {open ? 'Hide source' : 'Review source'}
              </span>
              <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                {open ? '▾' : '▸'}
              </span>
            </button>
            {open && (
              <div id={panelId} className="border-t border-border bg-muted/10">
                <SourceBody query={source} expectedHash={c.hash} />
              </div>
            )}
          </div>

          {operatorMode ? (
            <div className="mt-3 flex flex-col gap-1.5">
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  onClick={() => resolve('approve')}
                  disabled={busy !== null || !reviewed}
                  title={reviewed ? undefined : APPROVE_LOCKED}
                  aria-describedby={reviewed ? undefined : hintId}
                >
                  <Check className="size-3.5" /> {signed ? 'Re-sign' : 'Approve'}
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
              {!reviewed && (
                <p id={hintId} className="text-xs text-muted-foreground">
                  {APPROVE_LOCKED}
                </p>
              )}
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

/** Switches the page between the attention queue and the full inventory.
 *
 * Deliberately a filter on one page rather than a second page: an operator
 * hunting for something to re-sign is doing the same job on the same rows, and
 * splitting it in two would mean the "sign this" control lived in two places.
 */
function IncludeLoadedToggle({
  on,
  onChange,
}: {
  on: boolean
  onChange: (next: boolean) => void
}) {
  return (
    <Button
      type="button"
      variant={on ? 'default' : 'outline'}
      size="sm"
      aria-pressed={on}
      onClick={() => onChange(!on)}
      title="Show capabilities that already load, so a hand-edited or agent-signed one can be signed with the operator key."
    >
      {on ? <Eye className="size-3.5" /> : <EyeOff className="size-3.5" />}
      {on ? 'Showing all capabilities' : 'Showing gated only'}
    </Button>
  )
}

export function GatedCapabilitiesPage() {
  // Lowest sufficient rung: a view filter nothing outside this page reads. It
  // is not persisted on purpose — the gated queue is what an operator should
  // land on, so the wider view has to be chosen each visit.
  const [includeLoaded, setIncludeLoaded] = useState(false)
  const gated = useGatedCapabilities(includeLoaded)

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Gated"
        description={
          includeLoaded
            ? 'Every tool and skill, gated or loaded — sign or re-sign any of them with the operator key.'
            : 'Tools and skills the loader quarantined pending operator trust.'
        }
        actions={
          <>
            <IncludeLoadedToggle on={includeLoaded} onChange={setIncludeLoaded} />
            <OperatorModeToggle />
          </>
        }
      />
      <div className="flex-1 overflow-auto p-6">
        <QueryState
          query={gated}
          isEmpty={(data) => data.gated.length === 0}
          empty={
            <EmptyState
              icon={<PackageCheck className="size-7" />}
              title="No gated capabilities"
              description="Tools and skills held back by signing or policy checks will appear here. Switch to all capabilities to sign one that already loads."
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
