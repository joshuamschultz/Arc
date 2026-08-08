import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from '@tanstack/react-query'
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from './api'
import type {
  AgentConnectorsResponse,
  ConnectorApproveResponse,
  ConnectorAuthResponse,
  ConnectorCatalogResponse,
  ConnectorDoctorResponse,
  ConnectorInstallResponse,
  ConnectorProbeResponse,
  ConnectorRemoveResponse,
  KeysResponse,
  KeyWriteResponse,
  PromptWriteResponse,
  RubricResponse,
  RubricUpdate,
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
  EventsResponse,
  PromptDetail,
  PromptListResponse,
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
  WorkflowDetail,
  WorkflowRunDetail,
  WorkflowRunsResponse,
  WorkflowsListResponse,
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

// Polls every 4s so live todo -> in_progress -> done transitions and newly
// dispatched tasks surface on the board without a manual refresh. The board's
// other driving query (roster) is near-static, so only tasks needs the poll.
export const useTeamTasks = () =>
  useQuery<TasksResponse>({
    queryKey: ['team', 'tasks'],
    queryFn: ({ signal }) => apiGet<TasksResponse>('/api/team/tasks', signal),
    refetchInterval: 4000,
  })

export interface PendingApproval {
  id: string
  agent_did: string
  agent_label: string
  tool: string
  legs: string[]
  call_hash: string
  status: string
  created_at: string
  expires_at: string
}
export interface ApprovalsResponse {
  approvals: PendingApproval[]
}

// Polls every 4s: agents park a pending row while blocked on a trifecta-
// completing call, so a new request must surface without a manual refresh.
export const useApprovals = () =>
  useQuery<ApprovalsResponse>({
    queryKey: ['approvals'],
    queryFn: ({ signal }) => apiGet<ApprovalsResponse>('/api/approvals', signal),
    refetchInterval: 4000,
  })

export interface GatedCapability {
  agent_id: string
  agent_label: string
  name: string
  kind: 'tool' | 'skill'
  status: 'deny' | 'new_sighting' | 'unsigned' | 'invalid' | 'error'
  path: string
  hash: string
  detail: string
}
export interface GatedResponse {
  gated: GatedCapability[]
}

// Polls every 4s: a capability is gated the moment the loader denies, first-
// sights, or fails to verify it, so newly quarantined tools/skills must surface
// without a manual refresh — same cadence as approvals.
export const useGatedCapabilities = () =>
  useQuery<GatedResponse>({
    queryKey: ['trust', 'gated'],
    queryFn: ({ signal }) => apiGet<GatedResponse>('/api/trust/gated', signal),
    refetchInterval: 4000,
  })

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

// Per-agent, per-file config editor (arcagent / arcllm / arcrun). Each file's
// top-level TOML tables come back as `sections`; the Settings page renders one
// editable cell per section and PATCHes a single section back.
export interface AgentConfigFileResponse {
  file: string
  sections: Record<string, unknown>
  mtime: number
}

export const useAgentConfigFile = (agentId: string | null, file: string) =>
  useQuery<AgentConfigFileResponse>({
    queryKey: ['agent', agentId, 'config', file],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/config/${file}`, signal),
    enabled: !!agentId,
  })

// System-level (fleet-wide `~/.arc`) config editor. Same shape as
// `useAgentConfigFile`, but the target is the user config root the per-agent
// files layer over — so the Settings page can edit fleet defaults, not just a
// single agent's override.
export const useSystemConfigFile = (file: string, enabled = true) =>
  useQuery<AgentConfigFileResponse>({
    queryKey: ['system', 'config', file],
    queryFn: ({ signal }) => apiGet(`/api/system-config/${file}`, signal),
    enabled,
  })

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

export const useAgentEvents = (agentId: string | null) =>
  useQuery<EventsResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'events'],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/knowledge/events`, signal),
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

export interface DeliveryChannel {
  target: string
  label: string
}
export interface AgentChannelsResponse {
  channels: DeliveryChannel[]
}
export const useAgentChannels = (agentId: string) =>
  useApiQuery<AgentChannelsResponse>(['agent', agentId, 'channels'], `/api/agents/${agentId}/channels`)

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

// COMP-010 — editable system prompts. List every prompt across packages; the
// detail hook is lazy (only when a prompt is selected) and carries the
// server-computed unified diff so the browser ships no diff library.
export const useAgentPrompts = (agentId: string) =>
  useApiQuery<PromptListResponse>(['agent', agentId, 'prompts'], `/api/agents/${agentId}/prompts`)

export const useAgentPromptDetail = (
  agentId: string,
  prompt: { package: string; name: string } | null,
) =>
  useQuery<PromptDetail>({
    queryKey: ['agent', agentId, 'prompt', prompt?.package, prompt?.name],
    queryFn: ({ signal }) =>
      apiGet(
        `/api/agents/${agentId}/prompts/${encodeURIComponent(prompt!.package)}/${encodeURIComponent(prompt!.name)}`,
        signal,
      ),
    enabled: !!prompt,
  })

// COMP-010 — structured rubric editor. The arcskill/judge_rubric prompt's body
// is YAML; this lazy hook (enabled only when that prompt is selected) fetches it
// parsed into dimensions so the drawer renders a form, not a textarea.
export const useRubric = (
  agentId: string,
  prompt: { package: string; name: string } | null,
) =>
  useQuery<RubricResponse>({
    queryKey: ['agent', agentId, 'rubric', prompt?.package, prompt?.name],
    queryFn: ({ signal }) =>
      apiGet(
        `/api/agents/${agentId}/prompts/${encodeURIComponent(prompt!.package)}/${encodeURIComponent(prompt!.name)}/rubric`,
        signal,
      ),
    enabled: !!prompt,
  })

// PUTs the structured edit through the SAME signed-overlay path as the prose
// editor; on success invalidates the rubric, the prose detail, and the list so
// every view reflects the new override.
export const useSaveRubric = (agentId: string) => {
  const queryClient = useQueryClient()
  return useMutation<
    PromptWriteResponse,
    Error,
    { prompt: { package: string; name: string }; update: RubricUpdate }
  >({
    mutationFn: ({ prompt, update }) =>
      apiPut(
        `/api/agents/${agentId}/prompts/${encodeURIComponent(prompt.package)}/${encodeURIComponent(prompt.name)}/rubric`,
        update,
      ),
    onSuccess: (_data, { prompt }) =>
      Promise.all([
        queryClient.invalidateQueries({
          queryKey: ['agent', agentId, 'rubric', prompt.package, prompt.name],
        }),
        queryClient.invalidateQueries({
          queryKey: ['agent', agentId, 'prompt', prompt.package, prompt.name],
        }),
        queryClient.invalidateQueries({ queryKey: ['agent', agentId, 'prompts'] }),
      ]),
  })
}

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

// --- SPEC-061 ArcFlow (COMP-020/023) — thin client over the workflow routes.
//
// Every mutation here is a direct 1:1 call to the COMP-023 route layer,
// which itself only relays to the (not-yet-merged) arcteam control plane —
// this file adds no business logic, only react-query plumbing.

export const useWorkflows = () =>
  useApiQuery<WorkflowsListResponse>(['workflows'], '/api/workflows')

export const useWorkflow = (id: string | null) =>
  useQuery<WorkflowDetail>({
    queryKey: ['workflow', id],
    queryFn: ({ signal }) => apiGet(`/api/workflows/${encodeURIComponent(id!)}`, signal),
    enabled: !!id,
  })

export const useWorkflowRuns = (id: string | null) =>
  useQuery<WorkflowRunsResponse>({
    queryKey: ['workflow', id, 'runs'],
    queryFn: ({ signal }) => apiGet(`/api/workflows/${encodeURIComponent(id!)}/runs`, signal),
    enabled: !!id,
  })

// Polling fallback only — the live view prefers the workflow's channel stream
// (`useWorkflowRunLiveStatus`) per DESIGN.md §8; this backfills the initial
// snapshot and covers a channel-less deployment.
export const useWorkflowRun = (runId: string | null, refetchIntervalMs?: number) =>
  useQuery<WorkflowRunDetail>({
    queryKey: ['workflow-run', runId],
    queryFn: ({ signal }) => apiGet(`/api/workflow-runs/${encodeURIComponent(runId!)}`, signal),
    enabled: !!runId,
    refetchInterval: refetchIntervalMs,
  })

export const useRequestSignature = (workflowId: string) => {
  const queryClient = useQueryClient()
  return useMutation<Dict, Error, void>({
    mutationFn: () =>
      apiPost(`/api/workflows/${encodeURIComponent(workflowId)}/request-signature`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['approvals'] })
      queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] })
    },
  })
}

// The prompt a node actually runs. Fetched only when a panel opens it — a
// bundle's bodies are not part of the definition payload.
export const useWorkflowFile = (workflowId: string, path: string | null) =>
  useQuery<{ path: string; content: string }>({
    queryKey: ['workflow-file', workflowId, path],
    queryFn: ({ signal }) =>
      apiGet(
        `/api/workflows/${encodeURIComponent(workflowId)}/file?path=${encodeURIComponent(path!)}`,
        signal,
      ),
    enabled: !!path,
  })

export const useWriteWorkflowFile = (workflowId: string) => {
  const queryClient = useQueryClient()
  return useMutation<Dict, Error, { path: string; content: string; expectedVersion: number }>({
    mutationFn: ({ path, content, expectedVersion }) =>
      apiPut(`/api/workflows/${encodeURIComponent(workflowId)}/file`, {
        path,
        content,
        expected_version: expectedVersion,
      }),
    onSuccess: (_d, vars) => {
      queryClient.invalidateQueries({ queryKey: ['workflow', workflowId] })
      queryClient.invalidateQueries({ queryKey: ['workflow-file', workflowId, vars.path] })
    },
  })
}

export const useCreateWorkflow = () => {
  const queryClient = useQueryClient()
  return useMutation<WorkflowDetail, Error, Dict>({
    mutationFn: (definition) => apiPost('/api/workflows', definition),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['workflows'] }),
  })
}

// `patch` carries whichever top-level keys changed — `nodes`/`edges` always
// replace the WHOLE array (one consistent contract for every node/edge
// mutation), `trigger`/`channel` replace that one field. Never the whole
// definition in one call. `expected_version` is the optimistic-concurrency
// token the control plane checks, not this hook.
export const usePatchWorkflow = (id: string) => {
  const queryClient = useQueryClient()
  return useMutation<WorkflowDetail, Error, { patch: Dict; expectedVersion: number }>({
    mutationFn: ({ patch, expectedVersion }) =>
      apiPatch(`/api/workflows/${encodeURIComponent(id)}`, {
        ...patch,
        expected_version: expectedVersion,
      }),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ['workflow', id] }),
        queryClient.invalidateQueries({ queryKey: ['workflows'] }),
      ]),
  })
}

export const useArchiveWorkflow = (id: string) => {
  const queryClient = useQueryClient()
  return useMutation<WorkflowDetail, Error, void>({
    mutationFn: () => apiPost(`/api/workflows/${encodeURIComponent(id)}/archive`),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ['workflow', id] }),
        queryClient.invalidateQueries({ queryKey: ['workflows'] }),
      ]),
  })
}

export const useUnarchiveWorkflow = (id: string) => {
  const queryClient = useQueryClient()
  return useMutation<WorkflowDetail, Error, void>({
    mutationFn: () => apiPost(`/api/workflows/${encodeURIComponent(id)}/unarchive`),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ['workflow', id] }),
        queryClient.invalidateQueries({ queryKey: ['workflows'] }),
      ]),
  })
}

export const useRunWorkflow = (id: string) => {
  const queryClient = useQueryClient()
  return useMutation<{ run_id: string }, Error, Dict | undefined>({
    mutationFn: (input) => apiPost(`/api/workflows/${encodeURIComponent(id)}/run`, input ?? {}),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['workflow', id, 'runs'] }),
  })
}

export const useCancelWorkflowRun = (runId: string) => {
  const queryClient = useQueryClient()
  return useMutation<WorkflowRunDetail, Error, void>({
    mutationFn: () => apiPost(`/api/workflow-runs/${encodeURIComponent(runId)}/cancel`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['workflow-run', runId] }),
  })
}

// COMP-018: gate resolution. `notes` matters only for `return_for_revision`
// (REQ-247) — the control plane, not this hook, enforces that.
export const useResolveGate = (taskId: string) => {
  const queryClient = useQueryClient()
  return useMutation<Dict, Error, { decision: string; notes?: string }>({
    mutationFn: ({ decision, notes }) =>
      apiPost(`/api/workflow-tasks/${encodeURIComponent(taskId)}/gate`, { decision, notes }),
    onSuccess: () => queryClient.invalidateQueries({ predicate: (q) => q.queryKey[0] === 'workflow-run' }),
  })
}

// --- Keys (SPEC-064) -------------------------------------------------------

const KEYS_KEY = ['keys']

// Fleet-wide provider keys from `~/.arc/.env`. The response carries presence
// only, so nothing here can cache, key, or render a credential.
export const useKeys = () => useApiQuery<KeysResponse>(KEYS_KEY, '/api/keys')

// The value travels in the body — never in the path, the query key, or the
// cache — and the caller drops it as soon as the write lands.
export const useSetKey = () => {
  const queryClient = useQueryClient()
  return useMutation<KeyWriteResponse, Error, { envVar: string; value: string }>({
    mutationFn: ({ envVar, value }) =>
      apiPut(`/api/keys/${encodeURIComponent(envVar)}`, { value }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS_KEY }),
  })
}

export const useClearKey = () => {
  const queryClient = useQueryClient()
  return useMutation<KeyWriteResponse, Error, string>({
    mutationFn: (envVar) => apiDelete(`/api/keys/${encodeURIComponent(envVar)}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS_KEY }),
  })
}

// --- Connectors (SPEC-064) -------------------------------------------------

const connectorsKey = (agentId: string | null) => ['agent', agentId, 'connectors']
const doctorKey = (agentId: string, instance: string) => [
  'agent',
  agentId,
  'connectors',
  instance,
  'doctor',
]

// What this deployment could connect. Bundles whose manifest would not parse
// come back under `unreadable` rather than failing the listing.
export const useConnectorCatalog = () =>
  useApiQuery<ConnectorCatalogResponse>(['connectors', 'catalog'], '/api/connectors/catalog')

export const useAgentConnectors = (agentId: string | null) =>
  useQuery<AgentConnectorsResponse>({
    queryKey: connectorsKey(agentId),
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${encodeURIComponent(agentId!)}/connectors`, signal),
    enabled: !!agentId,
  })

// Secrets are consumed by the route and dropped; the response names tools, not
// credentials, so nothing sensitive reaches the cache.
export const useInstallConnector = (agentId: string) => {
  const queryClient = useQueryClient()
  return useMutation<
    ConnectorInstallResponse,
    Error,
    { extension: string; instance: string; secrets: Record<string, string> }
  >({
    mutationFn: (body) => apiPost(`/api/agents/${encodeURIComponent(agentId)}/connectors`, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: connectorsKey(agentId) }),
  })
}

// Rotation. The response lists field names only.
export const useReauthConnector = (agentId: string, instance: string) => {
  const queryClient = useQueryClient()
  return useMutation<ConnectorAuthResponse, Error, Record<string, string>>({
    mutationFn: (secrets) =>
      apiPut(
        `/api/agents/${encodeURIComponent(agentId)}/connectors/${encodeURIComponent(instance)}/auth`,
        { secrets },
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: doctorKey(agentId, instance) }),
  })
}

// Opens a live connection, so it is a mutation (and operator-only server side).
// Its result is the row's live status — deliberately not cached.
export const useProbeConnector = (agentId: string, instance: string) =>
  useMutation<ConnectorProbeResponse, Error, void>({
    mutationFn: () =>
      apiPost(
        `/api/agents/${encodeURIComponent(agentId)}/connectors/${encodeURIComponent(instance)}/probe`,
      ),
  })

export const useConnectorDoctor = (agentId: string, instance: string, enabled: boolean) =>
  useQuery<ConnectorDoctorResponse>({
    queryKey: doctorKey(agentId, instance),
    queryFn: ({ signal }) =>
      apiGet(
        `/api/agents/${encodeURIComponent(agentId)}/connectors/${encodeURIComponent(instance)}/doctor`,
        signal,
      ),
    enabled,
  })

// Records the tool contract served right now (rug-pull defense, REQ-291).
export const useApproveConnector = (agentId: string, instance: string) => {
  const queryClient = useQueryClient()
  return useMutation<ConnectorApproveResponse, Error, void>({
    mutationFn: () =>
      apiPost(
        `/api/agents/${encodeURIComponent(agentId)}/connectors/${encodeURIComponent(instance)}/approve`,
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: doctorKey(agentId, instance) }),
  })
}

export const useRemoveConnector = (agentId: string) => {
  const queryClient = useQueryClient()
  return useMutation<ConnectorRemoveResponse, Error, string>({
    mutationFn: (instance) =>
      apiDelete(
        `/api/agents/${encodeURIComponent(agentId)}/connectors/${encodeURIComponent(instance)}`,
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: connectorsKey(agentId) }),
  })
}
