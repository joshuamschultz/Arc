import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { ApiError, apiGet, apiPost } from '@/lib/api'
import { useOperatorMode } from '@/hooks/use-operator-mode'

/** One card inside a proposed merge (arcmemory.entity_dedup.ProposalCard). */
export interface ProposalCard {
  slug: string
  name: string
  entity_type: string
  classification: string
  tags: string[]
  facts: number
}

/** One proposed merge (arcmemory.entity_dedup.DuplicateProposal). */
export interface DuplicateProposal {
  slugs: string[]
  survivor: string
  name: string
  entity_type: string
  classification: string
  basis: string
  cards: ProposalCard[]
}

interface ProposalsResponse {
  items: DuplicateProposal[]
}

type Action = 'merge' | 'reject'

const entitiesKey = (agentId: string) => ['agent', agentId, 'knowledge', 'entities']

/** Plain-language reason a merge or "Not the same" did not go through. */
function failureText(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) {
    return 'Not allowed. An operator role is required.'
  }
  if (error instanceof ApiError && error.status === 409) {
    return 'These cards cannot be one thing, so they were not merged.'
  }
  return 'That did not work. Try again.'
}

function ProposalRow({
  agentId,
  proposal,
  canAct,
}: {
  agentId: string
  proposal: DuplicateProposal
  canAct: boolean
}) {
  const client = useQueryClient()
  const act = useMutation({
    mutationFn: (action: Action) =>
      apiPost(`/api/agents/${agentId}/knowledge/entities/duplicates/${action}`, {
        slugs: proposal.slugs,
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: entitiesKey(agentId) }),
  })
  return (
    <li
      data-testid="duplicate-proposal"
      className="space-y-2 rounded-lg border border-border bg-muted/20 px-3 py-2"
    >
      <ul className="space-y-1">
        {proposal.cards.map((card) => (
          <li key={card.slug} className="flex flex-wrap items-baseline gap-2 text-sm">
            <span className="text-foreground">{card.name}</span>
            <span className="text-xs text-muted-foreground">
              {card.entity_type} · {card.classification} · {card.facts} fact
              {card.facts === 1 ? '' : 's'}
            </span>
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">
        Merged card: <span className="text-foreground">{proposal.name}</span> ·{' '}
        <span data-testid="proposal-type">{proposal.entity_type}</span> ·{' '}
        {proposal.classification}
      </p>
      {canAct && (
        <div className="flex gap-2">
          <Button size="sm" disabled={act.isPending} onClick={() => act.mutate('merge')}>
            Merge
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={act.isPending}
            onClick={() => act.mutate('reject')}
          >
            Not the same
          </Button>
        </div>
      )}
      {act.isError && (
        <p role="alert" className="text-xs text-destructive">
          {failureText(act.error)}
        </p>
      )}
    </li>
  )
}

/** "Review duplicates": cards Arc thinks are one real thing, for a person to judge.
 *  "Merge" folds them into one card (most specific type, every fact, the highest
 *  classification); "Not the same" is remembered so the pair is never proposed
 *  again. Hidden when there is nothing to review. The server is the real gate;
 *  operator mode only shows the buttons. */
export function EntityDuplicatesPanel({ agentId }: { agentId: string }) {
  const [open, setOpen] = useState(false)
  const [operatorMode] = useOperatorMode()
  const proposals = useQuery<ProposalsResponse>({
    queryKey: [...entitiesKey(agentId), 'duplicates'],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/knowledge/entities/duplicates`, signal),
    enabled: !!agentId,
  })
  const items = proposals.data?.items ?? []
  if (items.length === 0) return null

  return (
    <section className="rounded-lg border border-border bg-card px-3 py-2 shadow-xs">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="flex w-full cursor-pointer items-center justify-between text-left text-sm font-medium text-foreground"
      >
        <span>Review duplicates ({items.length})</span>
        <span className="text-xs text-muted-foreground">{open ? 'Hide' : 'Show'}</span>
      </button>
      {open && (
        <ul className="mt-2 space-y-2">
          {items.map((p) => (
            <ProposalRow
              key={p.slugs.join('+')}
              agentId={agentId}
              proposal={p}
              canAct={operatorMode}
            />
          ))}
        </ul>
      )}
    </section>
  )
}
