import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { apiGet } from './api'
import type {
  AgentsListResponse,
  AuditEventsResponse,
  ConfigResponse,
  Dict,
  FileReadResponse,
  FilesTreeResponse,
  IdentityCostResponse,
  PolicyBulletsResponse,
  PolicyResponse,
  PolicyStatsResponse,
  RunsResponse,
  RunTimelineResponse,
  SchedulesResponse,
  SessionReplayResponse,
  SessionsListResponse,
  SkillsResponse,
  SpawnTreeResponse,
  StatsResponse,
  TasksResponse,
  TeamPolicyStatsResponse,
  TeamToolsSkillsResponse,
  ToolsResponse,
  Trace,
  TracesResponse,
} from './types'

// Shared react-query helpers for every page. Keys are arrays so live updates
// (Phase 2 file-change bus) can target invalidations precisely; this is the
// server-query-first seam that keeps the DB migration transparent (plan
// §storage-evolution).

function useApiQuery<T>(key: unknown[], path: string): UseQueryResult<T> {
  return useQuery<T>({
    queryKey: key,
    queryFn: ({ signal }) => apiGet<T>(path, signal),
  })
}

// --- Fleet (team) ----------------------------------------------------------

export const useRoster = () =>
  useApiQuery<AgentsListResponse>(['roster'], '/api/team/roster')

export const useTeamTasks = () =>
  useApiQuery<TasksResponse>(['team', 'tasks'], '/api/team/tasks')

export const useTeamToolsSkills = () =>
  useApiQuery<TeamToolsSkillsResponse>(['team', 'tools-skills'], '/api/team/tools-skills')

export const useTeamPolicyBullets = () =>
  useApiQuery<PolicyBulletsResponse>(['team', 'policy', 'bullets'], '/api/team/policy/bullets')

export const useTeamPolicyStats = () =>
  useApiQuery<TeamPolicyStatsResponse>(['team', 'policy', 'stats'], '/api/team/policy/stats')

export const useTeamAudit = (filter?: string, limit = 100) =>
  useApiQuery<AuditEventsResponse>(
    ['team', 'audit', filter ?? 'all', limit],
    `/api/team/audit?limit=${limit}${filter ? `&filter=${filter}` : ''}`,
  )

// --- Messages / team chat --------------------------------------------------

export const useTeamChannels = () =>
  useApiQuery<{ channels: Array<string | Dict> }>(['team', 'channels'], '/api/team/channels')

export interface ChannelMessagesResponse {
  channel: string
  messages: Dict[]
  next_after_seq: number | null
}
// Initial channel history. Live updates arrive over the read-only `/ws/team`
// stream (see `useTeamStream`), so this is a one-shot backfill — the 5s DB poll
// it used to carry is gone (SPEC-031 F3 / REQ-062).
export const useChannelMessages = (name: string | null) =>
  useQuery<ChannelMessagesResponse>({
    queryKey: ['team', 'channel', name],
    queryFn: ({ signal }) =>
      apiGet(`/api/team/channels/${encodeURIComponent(name!)}/messages?limit=100`, signal),
    enabled: !!name,
  })

// --- Config (settings) -----------------------------------------------------

export const useViewerConfig = () =>
  useApiQuery<Dict>(['config'], '/api/config')

export const useArcllmConfig = () =>
  useApiQuery<Dict>(['arcllm-config'], '/api/arcllm-config')

// --- Knowledge -------------------------------------------------------------

export interface KnowledgeResponse {
  agent_id: string
  agent_did?: string
  context?: Record<string, unknown>
  memory?: Record<string, unknown>
  workspace?: Record<string, unknown>
  graph?: Record<string, unknown>
}

export const useKnowledge = (agentId: string | null) =>
  useQuery<KnowledgeResponse>({
    queryKey: ['knowledge', agentId],
    queryFn: ({ signal }) => apiGet(`/api/knowledge/${agentId}`, signal),
    enabled: !!agentId,
  })

// --- ArcLLM (LLM layer) ----------------------------------------------------

export interface LlmStats extends Dict {
  request_count: number
  total_tokens: number
  total_cost: number
  error_count: number
  retry_count: number
  latency_avg: number
  latency_p50: number
  latency_p95: number
  model_stats: Dict
  provider_counts: Dict
  provider_costs: Dict
  agent_perf: Dict
}

export const useLlmStats = (window = '7d') =>
  useApiQuery<LlmStats>(['llm-stats', window], `/api/stats?window=${window}`)

export interface TimeseriesBucket extends Dict {
  request_count: number
  total_tokens: number
  total_cost: number
  latency_avg: number
}
export interface TimeseriesResponse {
  window: string
  buckets: TimeseriesBucket[]
}
export const useTimeseries = (window = '24h') =>
  useApiQuery<TimeseriesResponse>(['timeseries', window], `/api/stats/timeseries?window=${window}`)

export const useCircuitBreakers = () =>
  useApiQuery<{ circuit_breakers: Dict[] }>(['circuit-breakers'], '/api/circuit-breakers')

export const useBudgets = () =>
  useApiQuery<{ budgets: Dict[] }>(['budgets'], '/api/budget')

export interface PerformanceResponse {
  window: string
  models: Dict[]
  agents: Dict[]
}
export const usePerformance = (window = '7d') =>
  useApiQuery<PerformanceResponse>(['performance', window], `/api/performance?window=${window}`)

export interface CostEfficiencyResponse {
  window: string
  models: Dict[]
  cheapest_model: string | null
  most_used_model: string | null
  potential_savings_usd: number
  potential_savings_pct: number
}
export const useCostEfficiency = (window = '24h') =>
  useApiQuery<CostEfficiencyResponse>(['cost-efficiency', window], `/api/cost-efficiency?window=${window}`)

export const useTraces = (limit = 200) =>
  useApiQuery<TracesResponse>(['traces', limit], `/api/traces?limit=${limit}`)

export const useTraceDetail = (traceId: string | null) =>
  useQuery<Trace>({
    queryKey: ['trace', traceId],
    queryFn: ({ signal }) => apiGet(`/api/traces/${traceId}`, signal),
    enabled: !!traceId,
  })

// --- Per-agent (agent detail, Phase 6) -------------------------------------

export const useAgent = (agentId: string) =>
  useApiQuery<Dict>(['agent', agentId, 'detail'], `/api/agents/${agentId}`)

export const useAgentTraces = (agentId: string, limit = 200) =>
  useApiQuery<TracesResponse>(
    ['agent', agentId, 'traces', limit],
    `/api/agents/${agentId}/traces?limit=${limit}`,
  )

export const useAgentSessions = (agentId: string) =>
  useApiQuery<SessionsListResponse>(['agent', agentId, 'sessions'], `/api/agents/${agentId}/sessions`)

export const useAgentStats = (agentId: string, window = '24h') =>
  useApiQuery<StatsResponse>(
    ['agent', agentId, 'stats', window],
    `/api/agents/${agentId}/stats?window=${window}`,
  )

export const useAgentTimeseries = (agentId: string, window = '24h') =>
  useApiQuery<TimeseriesResponse>(
    ['agent', agentId, 'timeseries', window],
    `/api/stats/timeseries?window=${window}&agent_id=${encodeURIComponent(agentId)}`,
  )

export const useAgentTasks = (agentId: string) =>
  useApiQuery<TasksResponse>(['agent', agentId, 'tasks'], `/api/agents/${agentId}/tasks`)

export const useAgentSchedules = (agentId: string) =>
  useApiQuery<SchedulesResponse>(['agent', agentId, 'schedules'], `/api/agents/${agentId}/schedules`)

export const useAgentSessionReplay = (agentId: string, sid: string, page = 1) =>
  useApiQuery<SessionReplayResponse>(
    ['agent', agentId, 'session', sid, page],
    `/api/agents/${agentId}/sessions/${sid}?page=${page}`,
  )

export const useAgentSkills = (agentId: string) =>
  useApiQuery<SkillsResponse>(['agent', agentId, 'skills'], `/api/agents/${agentId}/skills`)

export const useAgentTools = (agentId: string) =>
  useApiQuery<ToolsResponse>(['agent', agentId, 'tools'], `/api/agents/${agentId}/tools`)

export const useAgentPolicy = (agentId: string) =>
  useApiQuery<PolicyResponse>(['agent', agentId, 'policy'], `/api/agents/${agentId}/policy`)

export const useAgentPolicyStats = (agentId: string) =>
  useApiQuery<PolicyStatsResponse>(['agent', agentId, 'policy', 'stats'], `/api/agents/${agentId}/policy/stats`)

export const useAgentConfig = (agentId: string) =>
  useApiQuery<ConfigResponse>(['agent', agentId, 'config'], `/api/agents/${agentId}/config`)

export const useAgentFilesTree = (agentId: string, path = '') =>
  useApiQuery<FilesTreeResponse>(
    ['agent', agentId, 'files', path],
    `/api/agents/${agentId}/files/tree${path ? `?path=${encodeURIComponent(path)}` : ''}`,
  )

export const useAgentFileRead = (agentId: string, path: string | null) =>
  useQuery<FileReadResponse>({
    queryKey: ['agent', agentId, 'file', path],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/files/read?path=${encodeURIComponent(path!)}`, signal),
    enabled: !!path,
  })

// --- SPEC-028: tool/code timeline, spawn lineage, per-identity cost --------

export const useRuns = () => useApiQuery<RunsResponse>(['runs'], '/api/runs')

export const useRunTimeline = (runId: string | null) =>
  useQuery<RunTimelineResponse>({
    queryKey: ['run', runId, 'timeline'],
    queryFn: ({ signal }) => apiGet(`/api/runs/${encodeURIComponent(runId!)}/timeline`, signal),
    enabled: !!runId,
  })

export const useSpawnTree = (root: string | null) =>
  useApiQuery<SpawnTreeResponse>(
    ['spawn-tree', root],
    `/api/spawn-tree${root ? `?root=${encodeURIComponent(root)}` : ''}`,
  )

export const useIdentityCost = (window = '24h') =>
  useApiQuery<IdentityCostResponse>(['stats', 'by-identity', window], `/api/stats/by-identity?window=${window}`)
