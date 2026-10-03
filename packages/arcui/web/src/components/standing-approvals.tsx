import { Infinity as InfinityIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useAgent, useRevokeStandingGrant, useStandingGrants, type StandingGrant } from '@/lib/queries'

/* ---------------------------------------------------------------------------
 * Standing approvals — the operator's "Always allow" answers for one agent
 * (SPEC-035 OQ-3, ruled 2026-10-03). Each row says what it covers (the
 * combination and the destination), who granted it, when, and how often the
 * agent has used it. Revoke takes effect on the agent's next call.
 * ------------------------------------------------------------------------- */

const LEG_LABEL: Record<string, string> = {
  private_data: 'Private data',
  external_comms: 'External comms',
  untrusted_input: 'Untrusted input',
}

function legs(composition: string[]): string {
  return composition.map((leg) => LEG_LABEL[leg] ?? leg.replace(/_/g, ' ')).join(' + ')
}

function shortDid(did: string): string {
  return did.split(/[:/]/).pop() || did
}

function GrantRow({ grant, operatorMode }: { grant: StandingGrant; operatorMode: boolean }) {
  const revoke = useRevokeStandingGrant()
  return (
    <li className="flex flex-wrap items-start gap-3 px-4 py-3 text-xs">
      <div className="min-w-0 flex-1 space-y-1">
        <div className="text-foreground">
          <span className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono">
            {grant.tool}
          </span>{' '}
          <span className="text-muted-foreground">to</span>{' '}
          <span className="font-medium">{grant.destination || 'no outside destination'}</span>
        </div>
        <div className="text-muted-foreground">{legs(grant.composition)}</div>
        <div className="text-[11px] text-muted-foreground">
          Granted by <span className="font-mono">{shortDid(grant.granted_by)}</span>
          {grant.granted_at && <> on {grant.granted_at.slice(0, 16).replace('T', ' ')}</>} · used{' '}
          <span className="tabular-nums">{grant.use_count}</span>{' '}
          {grant.use_count === 1 ? 'time' : 'times'}
        </div>
      </div>
      {operatorMode && (
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            className="text-destructive hover:text-destructive"
            disabled={revoke.isPending}
            onClick={() => revoke.mutate(grant.id)}
          >
            Revoke
          </Button>
          {revoke.error && <span className="text-destructive">{revoke.error.message}</span>}
        </div>
      )}
    </li>
  )
}

/** The Standing approvals list for one agent, shown on its Policy tab. */
export function StandingApprovals({ agentId }: { agentId: string }) {
  const agent = useAgent(agentId)
  const did = typeof agent.data?.did === 'string' ? agent.data.did : null
  const grants = useStandingGrants(did)
  const [operatorMode] = useOperatorMode()
  const rows = grants.data?.grants ?? []

  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="flex items-center gap-2 border-b border-border px-4 py-2.5">
        <InfinityIcon className="size-4 text-muted-foreground" />
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Standing approvals
        </h3>
      </header>
      {rows.length === 0 ? (
        <p className="px-4 py-3 text-xs text-muted-foreground">
          None. When you answer an approval with “Always allow”, it is listed here until you
          revoke it.
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {rows.map((grant) => (
            <GrantRow key={grant.id} grant={grant} operatorMode={operatorMode} />
          ))}
        </ul>
      )}
    </section>
  )
}
