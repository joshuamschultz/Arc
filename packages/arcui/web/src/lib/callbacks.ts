// SPEC-088 COMP-018: the web client for the callback origin allowlist.
// Route shapes: packages/arcui/src/arcui/routes/work_event_callbacks.py.
// Operator session only; an add above personal tier is pending until an
// operator approves the `work_event_callback` approval row it queues.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost } from './api'

/** Clearances an entry may carry, low to high (arctrust.classification). */
export const CLEARANCES = ['UNCLASSIFIED', 'CUI', 'CONFIDENTIAL', 'SECRET', 'TOP_SECRET'] as const

export type CallbackStatus = 'pending' | 'active' | 'removed'

/** One allowlist entry: callbacks may go only to this exact origin. */
export interface CallbackEntry {
  id: string
  origin: string
  allow_private: boolean
  clearance: string
  status: CallbackStatus
  created_by: string
  approved_by: string | null
  created_at: string
}

export interface AddCallbackBody {
  origin: string
  allow_private: boolean
  clearance: string
}

export interface AddCallbackResult {
  callback: CallbackEntry
  status: CallbackStatus
  /** Present when the add is pending: the approval card that activates it. */
  approval_id?: string
}

const CALLBACKS_PATH = '/api/work-events/callbacks'
const CALLBACKS_KEY = ['work-events', 'callbacks']

/** Plain words for the refusals an operator can act on. */
const REFUSALS: Record<string, string> = {
  invalid_origin: 'Use an origin like https://hooks.example.com (no path).',
  invalid_callback: 'Use an origin like https://hooks.example.com (no path).',
  duplicate_origin: 'That origin is already on the list.',
  unknown_entry: 'That origin is no longer on the list.',
  already_removed: 'That origin was already removed.',
  operator_session_required: 'Only a signed-in operator can change callback origins.',
  account_authority_unavailable: 'The account authority is not reachable, so nothing was changed.',
  approval_queue_unavailable: 'The approval could not be queued, so the origin stays inactive.',
}

export const callbackRefusal = (e: unknown): string =>
  e instanceof Error ? (REFUSALS[e.message] ?? e.message) : 'Something went wrong.'

export const useCallbacks = () =>
  useQuery<{ callbacks: CallbackEntry[] }>({
    queryKey: CALLBACKS_KEY,
    queryFn: ({ signal }) => apiGet(CALLBACKS_PATH, signal),
    retry: false,
  })

function useCallbackMutation<TVars, TResult>(run: (vars: TVars) => Promise<TResult>) {
  const queryClient = useQueryClient()
  return useMutation<TResult, Error, TVars>({
    mutationFn: run,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: CALLBACKS_KEY }),
  })
}

export const useAddCallback = () =>
  useCallbackMutation((body: AddCallbackBody) => apiPost<AddCallbackResult>(CALLBACKS_PATH, body))

export const useRemoveCallback = () =>
  useCallbackMutation((id: string) =>
    apiPost<{ callback: CallbackEntry }>(`${CALLBACKS_PATH}/${encodeURIComponent(id)}/remove`),
  )
