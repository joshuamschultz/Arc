// SPEC-087 COMP-013: the web client for app principals (operator) and a
// person's own delegations. Route shapes: packages/arcui/src/arcui/routes/principals.py.
//
// The issue reply carries the app's bearer secret exactly once. It is returned
// from `issuePrincipal` to the caller and nowhere else: no query, no mutation
// cache, no storage. Callers hold it in component state until dismissed.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost } from './api'

/** The closed scope catalogue (arctrust.principals.Scope). */
export const SCOPES = [
  'runs:start',
  'runs:read',
  'workflows:run',
  'workflows:event',
  'gates:resolve',
  'tasks:write',
  'inbox:send',
  'memory:write',
  'feedback:write',
  'events:subscribe',
] as const

/** Scopes that record a human decision; the only ones a person can delegate. */
export const DECISION_SCOPES = ['gates:resolve', 'tasks:write'] as const

/** A delegation lasts at most this long (server: MAX_DELEGATION_TTL_SECONDS). */
export const MAX_DELEGATION_HOURS = 8

export type PrincipalStatus = 'pending' | 'active' | 'revoked'

/** One app principal as the operator sees it. Never carries a secret. */
export interface AppPrincipal {
  id: string
  name: string
  did: string
  scopes: Record<string, string[]>
  agents: string[]
  subjects: string[]
  status: PrincipalStatus
  created_by: string
  approved_by: string | null
  last_used_at: string | null
  expires_at: string | null
  /** Present on a pending row: the approval card that activates it. */
  approval_id?: string
}

export interface IssuePrincipalBody {
  name: string
  public_key: string
  scopes: Record<string, string[]>
  subjects: string[]
  expires_at: string | null
}

export interface IssuePrincipalResult {
  principal: AppPrincipal
  /** The bearer secret. Shown once; never cached or stored. */
  secret: string
  status: PrincipalStatus
  approval_id?: string
}

export interface Delegation {
  id: string
  person_did: string
  principal_did: string
  scopes: string[]
  created_at: string
  expires_at: string
  revoked: boolean
}

export interface CreateDelegationBody {
  principal_did: string
  scopes: string[]
  ttl_seconds: number
}

const PRINCIPALS_KEY = ['principals']
const DELEGATIONS_KEY = ['me', 'delegations']

const principalPath = (id: string) => `/api/principals/${encodeURIComponent(id)}/revoke`
const delegationPath = (id: string) => `/api/me/delegations/${encodeURIComponent(id)}/revoke`

export const listPrincipals = (signal?: AbortSignal) =>
  apiGet<{ principals: AppPrincipal[] }>('/api/principals', signal)

export const issuePrincipal = (body: IssuePrincipalBody) =>
  apiPost<IssuePrincipalResult>('/api/principals', body)

export const revokePrincipal = (id: string) =>
  apiPost<{ principal: AppPrincipal }>(principalPath(id))

export const listDelegations = (signal?: AbortSignal) =>
  apiGet<{ delegations: Delegation[] }>('/api/me/delegations', signal)

export const createDelegation = (body: CreateDelegationBody) =>
  apiPost<Delegation>('/api/me/delegations', body)

export const revokeDelegation = (id: string) => apiPost<Delegation>(delegationPath(id))

/** Longest principal lifetime the tier allows, in days; null = no cap (personal).
 *  An unknown tier gets the strictest cap: the server is the authority, the
 *  form must never invite a request it will refuse. */
export function maxExpiryDays(tier: string): number | null {
  if (tier === 'personal') return null
  if (tier === 'enterprise') return 90
  return 30
}

export const usePrincipals = () =>
  useQuery<{ principals: AppPrincipal[] }>({
    queryKey: PRINCIPALS_KEY,
    queryFn: ({ signal }) => listPrincipals(signal),
    retry: false,
  })

/** Refresh the principal list after an issue (the issue itself bypasses the cache). */
export const useRefreshPrincipals = () => {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries({ queryKey: PRINCIPALS_KEY })
}

export const useRevokePrincipal = () => {
  const queryClient = useQueryClient()
  return useMutation<{ principal: AppPrincipal }, Error, string>({
    mutationFn: revokePrincipal,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PRINCIPALS_KEY }),
  })
}

export const useDelegations = () =>
  useQuery<{ delegations: Delegation[] }>({
    queryKey: DELEGATIONS_KEY,
    queryFn: ({ signal }) => listDelegations(signal),
    retry: false,
  })

function useDelegationMutation<TVars>(run: (vars: TVars) => Promise<Delegation>) {
  const queryClient = useQueryClient()
  return useMutation<Delegation, Error, TVars>({
    mutationFn: run,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: DELEGATIONS_KEY }),
  })
}

export const useCreateDelegation = () => useDelegationMutation(createDelegation)
export const useRevokeDelegation = () => useDelegationMutation(revokeDelegation)
