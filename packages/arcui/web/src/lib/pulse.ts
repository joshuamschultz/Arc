// Shared shapes and paths for the pulse panel and its add/edit form.

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
