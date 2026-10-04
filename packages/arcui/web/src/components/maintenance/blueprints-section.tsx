import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ContextNote } from '@/components/hitl'
import { EmptyState, QueryState } from '@/components/states'
import { SectionCard } from '@/components/maintenance/section-card'
import { useBlueprints, useCreateFromBlueprint } from '@/lib/queries'
import { describeCreates, errorText } from '@/lib/maintenance'
import type { BlueprintCreateResponse, BlueprintSummary } from '@/lib/types'

function BlueprintCard({ blueprint, editable }: { blueprint: BlueprintSummary; editable: boolean }) {
  const create = useCreateFromBlueprint()
  const [name, setName] = useState('')
  const [done, setDone] = useState<BlueprintCreateResponse | null>(null)

  const submit = () =>
    create.mutate(
      { blueprint: blueprint.id, agentName: name.trim() },
      {
        onSuccess: (res) => {
          setDone(res)
          setName('')
        },
      },
    )

  return (
    <li className="space-y-2 rounded-lg border border-border bg-background p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-foreground">{blueprint.name}</h3>
        <span className="font-mono text-[11px] text-muted-foreground">v{blueprint.version}</span>
        <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
          {blueprint.signed ? 'Signed' : 'Not signed'}
        </span>
      </div>
      {blueprint.description && (
        <p className="text-xs text-muted-foreground">{blueprint.description}</p>
      )}
      <p className="text-xs text-foreground">
        <span className="font-medium">Would create:</span> {describeCreates(blueprint.creates)}.
      </p>
      {editable && (
        <div className="flex flex-wrap items-center gap-2">
          <Input
            className="h-8 w-48"
            placeholder="name for the new agent"
            aria-label={`Name for the new ${blueprint.name} agent`}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <Button size="sm" disabled={create.isPending || name.trim().length < 2} onClick={submit}>
            {create.isPending ? 'Creating…' : 'Create agent'}
          </Button>
        </div>
      )}
      {create.isError && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(create.error)}
        </p>
      )}
      {done && (
        <ContextNote tone="signed">
          <span role="status">
            Created{' '}
            <Link className="underline" to={`/agents/${done.agent_id}`}>
              {done.agent_id}
            </Link>
            .{done.notice ? ` ${done.notice}` : ''}
          </span>
        </ContextNote>
      )}
    </li>
  )
}

/** Agent blueprints: signed starting points. Creating one builds the agent exactly as
 *  the new-agent form does, then lays the blueprint's identity, prompts and skills over it. */
export function BlueprintsSection({ editable }: { editable: boolean }) {
  const blueprints = useBlueprints()
  return (
    <SectionCard
      title="Blueprints"
      description="Ready-made starting points for a new agent. Pick one, name the agent, and Arc builds it with the blueprint's identity, prompts and skills already signed."
    >
      <QueryState
        query={blueprints}
        isEmpty={(data) => data.blueprints.length === 0}
        empty={
          <EmptyState
            title="No blueprints found"
            description="No blueprint is installed on this computer."
          />
        }
      >
        {(data) => (
          <ul className="space-y-2">
            {data.blueprints.map((b) => (
              <BlueprintCard key={b.id} blueprint={b} editable={editable} />
            ))}
          </ul>
        )}
      </QueryState>
    </SectionCard>
  )
}
