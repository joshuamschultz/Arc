import { useMutation, useQueryClient } from '@tanstack/react-query'
import { apiPost } from './api'

export const AGENT_TIERS = ['personal', 'enterprise', 'federal'] as const
export type AgentTier = (typeof AGENT_TIERS)[number]

export const DEFAULT_AGENT_MODEL = 'anthropic/claude-sonnet-4-5-20250929'

/** The only files an import may carry; the server rejects any other name. */
export const IMPORT_FILE_NAMES = ['identity.md', 'policy.md', 'context.md', 'pulse.md'] as const

const NAME_RULE = /^[a-z0-9][a-z0-9_-]{1,39}$/

/** Plain sentence when the name breaks the rule, otherwise an empty string. */
export const agentNameProblem = (name: string) =>
  NAME_RULE.test(name)
    ? ''
    : 'Use 2 to 40 characters: lowercase letters, digits, - or _, starting with a letter or digit.'

export interface NewAgentBody {
  name: string
  model?: string
  tier?: AgentTier
}

export interface NewAgentResult {
  agent_id: string
  name: string
  did: string
  team_registered: boolean
  notice: string | null
}

export type ImportAgentBody = NewAgentBody & { files: Record<string, string> }

/** Create or import an agent, then refresh the roster the Fleet page reads. */
export function useCreateAgent(mode: 'create' | 'import') {
  const queryClient = useQueryClient()
  const path = mode === 'create' ? '/api/agents' : '/api/agents/import'
  return useMutation<NewAgentResult, Error, NewAgentBody | ImportAgentBody>({
    mutationFn: (body) => apiPost<NewAgentResult>(path, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['roster'] }),
  })
}

/** Add an existing agent to team chat, then refetch its detail. */
export function useRegisterAgent(agentId: string) {
  const queryClient = useQueryClient()
  return useMutation<{ team_registered: boolean }, Error, void>({
    mutationFn: () => apiPost(`/api/agents/${encodeURIComponent(agentId)}/register`),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ['agent', agentId, 'detail'] }),
        queryClient.invalidateQueries({ queryKey: ['roster'] }),
      ]),
  })
}
