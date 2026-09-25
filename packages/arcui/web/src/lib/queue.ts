import { apiGet, apiPost, apiPut } from './api'

export type QueueState =
  | 'queued'
  | 'running'
  | 'cancel_requested'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'timed_out'
  | 'outcome_unknown'

export interface QueueJob {
  call_id: string
  tenant_id: string
  owner_id: string
  agent_id: string | null
  session_id: string | null
  run_id: string | null
  state: QueueState
  version: number
  created_at: number
  updated_at: number
  provider_scope: string | null
  attempt_id: string | null
}

export interface QueuePage {
  jobs: QueueJob[]
  next_cursor: string | null
}

export interface QueueLimits {
  max_concurrent: number
  max_queued: number
  wait_timeout: number
  history_limit: number
}

export interface QueueControl {
  revision: number
  paused: boolean
  limits: QueueLimits
}

export interface QueueCancellation {
  status: 'requested' | 'confirmed' | 'conflict' | 'unavailable'
  job: QueueJob | null
}

export interface QueueFilters {
  owner_id?: string
  state?: QueueState
  cursor?: string
  limit?: number
}

export function getQueueJobs(filters: QueueFilters = {}): Promise<QueuePage> {
  const query = new URLSearchParams()
  if (filters.owner_id) query.set('owner_id', filters.owner_id)
  if (filters.state) query.set('state', filters.state)
  if (filters.cursor) query.set('cursor', filters.cursor)
  query.set('limit', String(filters.limit ?? 50))
  return apiGet(`/api/queue/jobs?${query}`)
}

export function getQueueControl(): Promise<QueueControl> {
  return apiGet('/api/queue/control')
}

export function setQueuePaused(paused: boolean, expectedRevision: number): Promise<QueueControl> {
  return apiPost(`/api/queue/${paused ? 'pause' : 'resume'}`, {
    expected_revision: expectedRevision,
  })
}

export function setQueueLimits(limits: QueueLimits, expectedRevision: number): Promise<QueueControl> {
  return apiPut('/api/queue/limits', { ...limits, expected_revision: expectedRevision })
}

export function cancelQueueJob(callId: string, expectedVersion: number): Promise<QueueCancellation> {
  return apiPost('/api/queue/cancel', { call_id: callId, expected_version: expectedVersion })
}
