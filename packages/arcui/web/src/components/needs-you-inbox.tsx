import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { CalendarClock, HeartPulse, PackageCheck, Plug, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { DiffBlock } from '@/components/pulse-panel'
import { ApiError } from '@/lib/api'
import { agentLabel, grantName } from '@/lib/agent-names'
import { humanizeInterval } from '@/lib/schedule-format'
import { useApprovePulseCheck } from '@/lib/pulse'
import {
  useApproveAgentSchedule,
  useConnections,
  useHomeNeeds,
  useRoster,
  type HomeNeedsPulse,
  type HomeNeedsSchedule,
} from '@/lib/queries'

const MAX_NAMED_AGENTS = 3

/** Display names for grant names ("josh_agent" -> "Olivia"), "+N more" past three. */
function agentNames(grants: string[], names: Map<string, string>): string {
  if (grants.length === 0) return 'No agent granted'
  const shown = grants.slice(0, MAX_NAMED_AGENTS).map((g) => names.get(g) ?? g)
  const extra = grants.length - shown.length
  return extra > 0 ? `${shown.join(', ')} +${extra} more` : shown.join(', ')
}

/** What a person calls a connection: "Confluence", or "Google (work)" when the
 *  instance is not just the extension itself. */
function connectionName(c: { instance: string; extension: string; extension_display_name: string }) {
  const base = c.extension_display_name || c.extension
  return c.instance === c.extension || c.instance === base ? base : `${base} (${c.instance})`
}

const errorText = (e: unknown) => (e instanceof ApiError ? e.message : 'The request failed')

function InboxRow({
  icon,
  agent,
  title,
  why,
  detail,
  children,
  testId,
}: {
  icon: ReactNode
  agent: string
  title: ReactNode
  why: ReactNode
  detail?: ReactNode
  children: ReactNode
  testId: string
}) {
  return (
    <li
      data-testid={testId}
      className="flex min-w-0 max-w-full flex-col gap-3 rounded-lg border border-border bg-card p-3.5 sm:flex-row sm:items-start"
    >
      <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted/60 text-status-warning">
        {icon}
      </span>
      <div className="min-w-0 flex-1 space-y-1">
        <div className="text-sm font-semibold text-foreground">{agent}</div>
        <div className="text-sm text-foreground">{title}</div>
        <div className="text-xs text-muted-foreground">{why}</div>
        {detail}
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2">{children}</div>
    </li>
  )
}

function ActionError({ message }: { message: string | null }) {
  return message ? (
    <p role="alert" className="w-full text-sm text-destructive">
      {message}
    </p>
  ) : null
}

function PulseRow({ item, operatorMode }: { item: HomeNeedsPulse; operatorMode: boolean }) {
  const approve = useApprovePulseCheck(item.agent_id)
  const [error, setError] = useState<string | null>(null)
  return (
    <InboxRow
      testId={`needs-pulse-${item.agent_id}-${item.check}`}
      icon={<HeartPulse className="size-4" />}
      agent={item.agent_label}
      title={
        <>
          Pulse check {item.check} · {humanizeInterval(item.interval_minutes * 60).toLowerCase()}
        </>
      }
      detail={<DiffBlock diff={`+action: ${item.action}`} />}
      why={
        item.changed
          ? 'Changed since you last approved it. It will not run until you approve.'
          : 'Never approved. It will not run until you approve.'
      }
    >
      {operatorMode && (
        <Button
          size="sm"
          disabled={approve.isPending}
          aria-label={`Approve ${item.check}`}
          onClick={() => {
            setError(null)
            approve.mutate(
              { check: item.check, definition_digest: item.definition_digest },
              { onError: (e) => setError(errorText(e)) },
            )
          }}
        >
          {approve.isPending ? 'Approving…' : 'Approve'}
        </Button>
      )}
      <ActionError message={error} />
    </InboxRow>
  )
}

function ScheduleRow({ item, operatorMode }: { item: HomeNeedsSchedule; operatorMode: boolean }) {
  const approve = useApproveAgentSchedule()
  const [error, setError] = useState<string | null>(null)
  return (
    <InboxRow
      testId={`needs-schedule-${item.agent_id}-${item.schedule_id}`}
      icon={<CalendarClock className="size-4" />}
      agent={item.agent_label}
      title={
        <>
          Schedule {item.name}
        </>
      }
      why="No signed approval yet. It cannot fire until you approve."
    >
      {operatorMode && (
        <Button
          size="sm"
          disabled={approve.isPending}
          aria-label={`Approve schedule ${item.name}`}
          onClick={() => {
            setError(null)
            approve.mutate(item, { onError: (e) => setError(errorText(e)) })
          }}
        >
          {approve.isPending ? 'Approving…' : 'Approve'}
        </Button>
      )}
      <ActionError message={error} />
    </InboxRow>
  )
}

/**
 * The operator's one "Needs you" inbox. Pulse checks and schedules approve
 * inline through their own routes; connections that need you, new tool
 * contracts and standing grants link to the one screen that owns the action.
 * Tool and mapping approvals render below this on the Approvals page.
 */
export function NeedsYouInbox({ operatorMode }: { operatorMode: boolean }) {
  const needs = useHomeNeeds().data
  const names = new Map(
    (useRoster().data?.agents ?? []).map((a) => [grantName(a), agentLabel(a)] as const),
  )
  const connections = (useConnections().data?.connections ?? []).filter(
    (c) => c.display_status === 'needs_you',
  )
  const pulse = needs?.pulse.items ?? []
  const schedules = needs?.schedules.items ?? []
  const capabilities = needs?.capabilities.count ?? 0

  return (
    <section aria-label="Needs you" className="space-y-2.5">
      <ul className="flex flex-col gap-2.5">
        {pulse.map((p) => (
          <PulseRow key={`${p.agent_id}:${p.check}`} item={p} operatorMode={operatorMode} />
        ))}
        {schedules.map((s) => (
          <ScheduleRow key={`${s.agent_id}:${s.schedule_id}`} item={s} operatorMode={operatorMode} />
        ))}
        {connections.map((c) => (
          <InboxRow
            key={c.instance}
            testId={`needs-connection-${c.instance}`}
            icon={<Plug className="size-4" />}
            agent={agentNames(c.agents, names)}
            title={
              <>
                Connection {connectionName(c)} needs you
              </>
            }
            why={c.reason_text ?? 'Reconnect it so agents can use it.'}
          >
            <Link
              to="/connections"
              className="rounded-md border border-border bg-secondary px-3 py-1.5 text-xs font-semibold"
            >
              {c.action_label || 'Open connection'}
            </Link>
          </InboxRow>
        ))}
        {capabilities > 0 && (
          <InboxRow
            testId="needs-capabilities"
            icon={<PackageCheck className="size-4" />}
            agent="Your fleet"
            title={`${capabilities} new tool or skill contract${capabilities === 1 ? '' : 's'} to review`}
            why="A tool or skill cannot load until you sign it."
          >
            <Link
              to="/gated"
              className="rounded-md border border-border bg-secondary px-3 py-1.5 text-xs font-semibold"
            >
              Review
            </Link>
          </InboxRow>
        )}
      </ul>
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <ShieldCheck className="size-3.5" />
        Always-allow rules you set live on each agent&rsquo;s page, under Standing approvals.
        <Link to="/agents" className="underline">
          Open Fleet
        </Link>
      </p>
    </section>
  )
}
