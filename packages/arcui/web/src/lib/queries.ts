import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { apiGet } from './api'
import type {
  AgentCapabilityInventory,
  AgentsListResponse,
  AuditEventsResponse,
  ChannelsResponse,
  ConfigResponse,
  DailyNoteDetail,
  DailyNotesResponse,
  Dict,
  EntitiesResponse,
  FileReadResponse,
  FilesTreeResponse,
  IdentityCostResponse,
  InsightsResponse,
  LinksResponse,
  MemoryPage,
  MemorySearchResponse,
  ProceduresResponse,
  SkillDetail,
  ToolDetail,
  PolicyBulletsResponse,
  PolicyResponse,
  PolicyStatsResponse,
  RunsResponse,
  RunTimelineResponse,
  SchedulesResponse,
  SessionReplayResponse,
  SessionsListResponse,
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

// A task's activity timeline (FR-12) — the audit chain filtered to
// `target == "task:<id>"`, newest first.
export const useTaskActivity = (taskId: string | null, limit = 100) =>
  useQuery<AuditEventsResponse>({
    queryKey: ['task', taskId, 'activity'],
    queryFn: ({ signal }) =>
      apiGet(`/api/team/audit?limit=${limit}&target=${encodeURIComponent(`task:${taskId}`)}`, signal),
    enabled: !!taskId,
  })

// --- Messages / team chat --------------------------------------------------

export const useTeamChannels = () =>
  useApiQuery<ChannelsResponse>(['team', 'channels'], '/api/team/channels')

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

// --- Knowledge browser (COMP-002/003 — memories, entities, links) ----------

export const useAgentMemories = (agentId: string | null, limit = 50, offset = 0) =>
  useQuery<MemoryPage>({
    queryKey: ['agent', agentId, 'knowledge', 'memories', limit, offset],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/knowledge/memories?limit=${limit}&offset=${offset}`, signal),
    enabled: !!agentId,
  })

export const useMemorySearch = (agentId: string | null, q: string) =>
  useQuery<MemorySearchResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'memories', 'search', q],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/knowledge/memories?q=${encodeURIComponent(q)}`, signal),
    enabled: !!agentId && q.trim().length > 0,
  })

export const useMemoryLinks = (agentId: string | null, entryId: string | null) =>
  useQuery<LinksResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'memories', entryId, 'links'],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/knowledge/memories/${entryId}/links`, signal),
    enabled: !!agentId && !!entryId,
  })

export const useEntities = (agentId: string | null) =>
  useQuery<EntitiesResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'entities'],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/knowledge/entities`, signal),
    enabled: !!agentId,
  })

export const useEntityLinks = (agentId: string | null, slug: string | null) =>
  useQuery<LinksResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'entities', slug, 'links'],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/knowledge/entities/${slug}/links`, signal),
    enabled: !!agentId && !!slug,
  })

// --- Curated memory layer (U3/U4 — insights, procedures, daily notes) -------

export const useAgentInsights = (agentId: string | null) =>
  useQuery<InsightsResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'insights'],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/knowledge/insights`, signal),
    enabled: !!agentId,
  })

export const useAgentProcedures = (agentId: string | null) =>
  useQuery<ProceduresResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'procedures'],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/knowledge/procedures`, signal),
    enabled: !!agentId,
  })

export const useAgentDailyNotes = (agentId: string | null) =>
  useQuery<DailyNotesResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'daily-notes'],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/knowledge/daily-notes`, signal),
    enabled: !!agentId,
  })

export const useAgentDailyNote = (agentId: string | null, day: string | null) =>
  useQuery<DailyNoteDetail>({
    queryKey: ['agent', agentId, 'knowledge', 'daily-notes', day],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/knowledge/daily-notes/${day}`, signal),
    enabled: !!agentId && !!day,
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

export const useAgentTools = (agentId: string) =>
  useApiQuery<ToolsResponse>(['agent', agentId, 'tools'], `/api/agents/${agentId}/tools`)

// COMP-008 — the loader's own verdict mirror (skills + capability tools across
// all four scan roots, plus the live runtime tool list when the agent is
// loaded). Replaces the old per-kind `/skills` glob-scan hook (REQ-093/096).
export const useAgentCapabilities = (agentId: string) =>
  useApiQuery<AgentCapabilityInventory>(
    ['agent', agentId, 'capabilities'],
    `/api/agents/${agentId}/capabilities`,
  )

// U5/U6 — SKILL.md body / tool source for the detail drawers. Lazy: only
// fetched when a row is selected (skillName/toolName non-null).
export const useAgentSkillDetail = (agentId: string, skillName: string | null) =>
  useQuery<SkillDetail>({
    queryKey: ['agent', agentId, 'skill', skillName],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/skills/${encodeURIComponent(skillName!)}/detail`, signal),
    enabled: !!skillName,
  })

export const useAgentToolDetail = (agentId: string, toolName: string | null) =>
  useQuery<ToolDetail>({
    queryKey: ['agent', agentId, 'tool', toolName],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/tools/${encodeURIComponent(toolName!)}/detail`, signal),
    enabled: !!toolName,
  })

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
