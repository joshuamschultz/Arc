// Shared (fleet) knowledge: the typed list, one document with provenance, the
// operator demote, and the per-agent "Share to fleet" + decisions history.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost } from './api'

/** Singular kind — used by both the fleet list and the agent card routes. */
export type SharedKind = 'insight' | 'procedure' | 'entity'

export interface SharedContributor {
  did: string
  display: string
}

export interface SharedDemotion {
  demoted_by: string
  reason: string
  demoted_at: string
}

export interface SharedDocument {
  identifier: string
  title: string
  kind: SharedKind
  classification: string
  tags: string[]
  owner_did: string
  owner_display: string
  contributors: SharedContributor[]
  promoted_at: string | null
  excerpt: string
  demotion: SharedDemotion | null
}

export interface SharedCounts {
  all: number
  insight: number
  procedure: number
  entity: number
  demoted: number
}

export interface SharedListResponse {
  documents: SharedDocument[]
  counts: SharedCounts
}

export interface SharedProvenance {
  contributor_did: string
  contributor_display: string
  source_ref: string
  digest: string
  kind: SharedKind
  decision: 'classifier_promote' | 'operator_promote' | 'direct'
  confidence: number | null
  classifier_version: string | null
  decided_by: string | null
  promoted_at: string | null
}

export interface SharedDetail {
  identifier: string
  title: string
  kind: SharedKind
  content: string
  classification: string
  tags: string[]
  contributors: SharedContributor[]
  promoted_at: string | null
  provenance: SharedProvenance[]
}

export interface DemoteResponse {
  identifier: string
  demoted_by: string
  reason: string
  demoted_at: string
}

export interface ShareResponse {
  status: string
  shared_ref: string | null
}

/** One verified ledger row for a card (no content). */
export interface CardDecision {
  decision: string
  evaluated_at: string
  label: string | null
  confidence: number | null
  classifier_version: string | null
  decided_by: string | null
  reason: string | null
}

export const useSharedList = (kind: SharedKind | null, includeDemoted: boolean) => {
  const params = new URLSearchParams()
  if (kind) params.set('kind', kind)
  if (includeDemoted) params.set('include_demoted', '1')
  const qs = params.toString()
  return useQuery<SharedListResponse>({
    queryKey: ['team', 'knowledge', 'shared', 'list', kind, includeDemoted],
    queryFn: ({ signal }) => apiGet(`/api/team/knowledge/shared${qs ? `?${qs}` : ''}`, signal),
  })
}

export const useSharedDetail = (identifier: string | null) =>
  useQuery<SharedDetail>({
    queryKey: ['team', 'knowledge', 'shared', 'detail', identifier],
    queryFn: ({ signal }) =>
      apiGet(`/api/team/knowledge/shared/${encodeURIComponent(identifier ?? '')}`, signal),
    enabled: !!identifier,
  })

export function useDemoteShared(identifier: string) {
  const queryClient = useQueryClient()
  return useMutation<DemoteResponse, Error, string>({
    mutationFn: (reason) =>
      apiPost(`/api/team/knowledge/shared/${encodeURIComponent(identifier)}/demote`, { reason }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['team', 'knowledge', 'shared'] }),
  })
}

const cardPath = (agentId: string, kind: SharedKind, itemId: string) =>
  `/api/agents/${agentId}/knowledge/${kind}/${encodeURIComponent(itemId)}`

export function useShareToFleet(agentId: string, kind: SharedKind, itemId: string) {
  const queryClient = useQueryClient()
  return useMutation<ShareResponse, Error, void>({
    mutationFn: () => apiPost(`${cardPath(agentId, kind, itemId)}/share`, {}),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ['team', 'knowledge', 'shared'] }),
        queryClient.invalidateQueries({
          queryKey: ['agent', agentId, 'card-decisions', kind, itemId],
        }),
      ]),
  })
}

export const useCardDecisions = (
  agentId: string,
  kind: SharedKind,
  itemId: string,
  enabled: boolean,
) =>
  useQuery<{ decisions: CardDecision[] }>({
    queryKey: ['agent', agentId, 'card-decisions', kind, itemId],
    queryFn: ({ signal }) => apiGet(`${cardPath(agentId, kind, itemId)}/decisions`, signal),
    enabled,
  })
