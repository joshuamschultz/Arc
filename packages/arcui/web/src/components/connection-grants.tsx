import { Check, Loader2, Plus, TriangleAlert } from 'lucide-react'
import { agentLabel, grantName } from '@/lib/agent-names'
import { useGrantConnection, useRevokeConnection } from '@/lib/queries'
import type { Agent } from '@/lib/types'
import { cn } from '@/lib/utils'

const CHIP =
  'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium transition-colors'

const HELD = 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'

const NOT_HELD = 'border-dashed border-border bg-transparent text-muted-foreground/70'

/**
 * Every agent in the fleet, as one chip each: filled when it holds this
 * connection, outlined when it does not.
 *
 * Showing the agents that do NOT hold it is the whole design. A list of holders
 * alone answers "who has jira" and leaves "who doesn't" to be worked out from
 * another screen, and the operator's question was both at once — *"maybe 2
 * agents can access jira and 2 don't have the connection"*. Here that is one
 * row: two filled, two outlined.
 *
 * For an operator each chip is also the control: one click grants, one click
 * revokes. A viewer sees exactly the same picture and cannot change it, because
 * a grant is an access-control decision and the server refuses one from a viewer
 * whatever this renders.
 */
export function AgentGrantChips({
  instance,
  agents,
  holders,
  operatorMode,
}: {
  instance: string
  agents: Agent[]
  holders: string[]
  operatorMode: boolean
}) {
  const grant = useGrantConnection(instance)
  const revoke = useRevokeConnection(instance)
  const busy = grant.isPending || revoke.isPending
  const error = grant.error ?? revoke.error
  const activations = grant.data?.activations ?? revoke.data?.activations ?? []

  // A grant naming an agent this deployment no longer has: kept visible rather
  // than filtered out, because an invisible grant is one nobody revokes.
  const known = new Set(agents.map(grantName))
  const orphaned = holders.filter((name) => !known.has(name))

  const toggle = (name: string, held: boolean) => {
    if (!operatorMode || busy) return
    if (held) revoke.mutate([name])
    else grant.mutate([name])
  }

  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-1.5">
        {agents.map((agent) => {
          const name = grantName(agent)
          const held = holders.includes(name)
          return (
            <button
              key={name}
              type="button"
              disabled={!operatorMode || busy}
              onClick={() => toggle(name, held)}
              title={
                operatorMode
                  ? held
                    ? `Revoke ${instance} from ${agentLabel(agent)}`
                    : `Grant ${instance} to ${agentLabel(agent)}`
                  : `${agentLabel(agent)} ${held ? 'can' : 'cannot'} use ${instance}`
              }
              className={cn(
                CHIP,
                held ? HELD : NOT_HELD,
                operatorMode && !busy
                  ? 'cursor-pointer hover:border-foreground/40'
                  : 'cursor-default',
              )}
            >
              {held ? <Check className="size-3" /> : <Plus className="size-3 opacity-60" />}
              {agentLabel(agent)}
            </button>
          )
        })}
        {orphaned.map((name) => (
          <span
            key={name}
            className={cn(
              CHIP,
              'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400',
            )}
            title="Granted to a name this deployment has no agent for"
          >
            {name} — unknown agent
          </span>
        ))}
        {busy && <Loader2 className="size-3.5 animate-spin text-muted-foreground" />}
      </div>
      {holders.length === 0 && (
        // Deny by default is the correct behaviour and a baffling one to meet by
        // accident: the connection is connected, probed, and working, and no
        // agent can see it. Saying so here is the difference between a deliberate
        // state and an operator wondering what they did wrong.
        <p className="flex items-center gap-1.5 text-[11px] text-amber-700 dark:text-amber-400">
          <TriangleAlert className="size-3.5 shrink-0" />
          No agent can use this yet
          {operatorMode ? ' — pick one above.' : '. An operator has to grant it.'}
        </p>
      )}
      {error && <p className="text-[11px] text-destructive">{error.message}</p>}
      {(grant.isSuccess || revoke.isSuccess) && !busy && (
        <p className="text-[11px] text-muted-foreground">
          {activations.every((activation) => activation.status === 'applied')
            ? 'Applied to the running agent.'
            : 'Queued durably; the owning agent applies it when it is running.'}
        </p>
      )}
    </div>
  )
}
