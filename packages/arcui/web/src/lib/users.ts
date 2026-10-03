import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost, apiPut } from './api'

export type UserRole = 'viewer' | 'operator'

export interface ArcUser {
  id: string
  email: string
  did: string
  handle: string
  display_name: string | null
  roles: string[]
  pairings: unknown
  disabled: boolean
  created_at: number
}

/** A one-time link the operator hands to a person (invite or password reset). */
export interface OneTimeLink {
  link_path: string
  expires_at: number
  email: string
  role: UserRole
}

const USERS_KEY = ['users']

const userPath = (email: string, action: string) =>
  `/api/users/${encodeURIComponent(email)}/${action}`

/** Full link for a `link_path` such as `/#invite=<token>`. */
export const absoluteLink = (linkPath: string) => `${window.location.origin}${linkPath}`

export const useUsers = () =>
  useQuery<{ users: ArcUser[] }>({
    queryKey: USERS_KEY,
    queryFn: ({ signal }) => apiGet<{ users: ArcUser[] }>('/api/users', signal),
    retry: false,
  })

/** Builds a mutation that refreshes the people list once it lands. */
function useUsersMutation<TVars, TResult>(run: (vars: TVars) => Promise<TResult>) {
  const queryClient = useQueryClient()
  return useMutation<TResult, Error, TVars>({
    mutationFn: run,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: USERS_KEY }),
  })
}

export interface AddUserBody {
  email: string
  password: string
  role: UserRole
  display_name?: string
}

export const useAddUser = () =>
  useUsersMutation((body: AddUserBody) => apiPost<{ user: ArcUser }>('/api/users', body))

export const useInviteUser = () =>
  useUsersMutation((body: { email: string; role: UserRole }) =>
    apiPost<OneTimeLink>('/api/users/invites', body),
  )

export const useChangeRole = () =>
  useUsersMutation(({ email, role }: { email: string; role: UserRole }) =>
    apiPut<{ user: ArcUser }>(userPath(email, 'role'), { role }),
  )

export const useSetDisabled = () =>
  useUsersMutation(({ email, disabled }: { email: string; disabled: boolean }) =>
    apiPost<{ user: ArcUser }>(userPath(email, disabled ? 'disable' : 'enable')),
  )

export const useResetLink = () =>
  useUsersMutation((email: string) => apiPost<OneTimeLink>(userPath(email, 'reset-link')))
