// Shared shapes and paths for the pulse panel and its add/edit form.

import { useMutation, useQueryClient } from '@tanstack/react-query'
import { apiPost } from '@/lib/api'

export type PulseCheckDraft = {
  name: string
  interval_minutes: number
  action: string
  /** Set when editing: the digest the operator saw, so a stale edit is refused. */
  definition_digest?: string
  /** Set when accepting an agent proposal: the server clears it on save. */
  proposal?: string
}

export const pulsePath = (agentId: string) => `/api/agents/${encodeURIComponent(agentId)}/pulse`
export const pulseKey = (agentId: string) => ['agents', agentId, 'pulse']

/**
 * Approve one pulse check exactly as the operator reviewed it. The digest is the
 * one the row displayed, so an edit since is refused server-side. Used by the
 * pulse panel and the needs-you inbox: one approve route, one invalidation.
 */
export const useApprovePulseCheck = (agentId: string) => {
  const queryClient = useQueryClient()
  return useMutation<unknown, Error, { check: string; definition_digest: string }>({
    mutationFn: (body) => apiPost(`${pulsePath(agentId)}/approve`, body),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: pulseKey(agentId) }),
        queryClient.invalidateQueries({ queryKey: ['home', 'needs'] }),
      ]),
  })
}
