import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from './api'
import type {
  AgentConnectorsResponse,
  ConnectionsResponse,
  ConnectorApproveResponse,
  ConnectorAuthResponse,
  ConnectorAuthStatusResponse,
  ConnectorAuthorizationResponse,
  ConnectorCatalogResponse,
  ConnectorDoctorResponse,
  ConnectorInstallResponse,
  ConnectorMutationResponse,
  ConnectorProbeResponse,
  ConnectorRemoveResponse,
  HostSetupResponse,
  KeysResponse,
  KeyWriteResponse,
  PromptWriteResponse,
  RubricResponse,
  RubricUpdate,
  AgentCapabilityInventory,
  AgentsListResponse,
  AuditEventsResponse,
  AuthMeResponse,
  BlobFoldersResponse,
  ChannelsResponse,
  ChunkPage,
  ChunkSearchMode,
  ChunkSearchResponse,
  ConnectedDataActivationResponse,
  ConnectedSourcesResponse,
  ConnectedResourcesResponse,
  ProfileReviewsResponse,
  ProfileReviewItem,
  GatewaysResponse,
  ConfigResponse,
  ConnectedSyncStatusResponse,
  DailyNoteDetail,
  DailyNotesResponse,
  DatastoreQueryResponse,
  DatastoreTablesResponse,
  Dict,
  DocumentsResponse,
  EntitiesResponse,
  FileReadResponse,
  FileWriteResponse,
  FilesTreeResponse,
  IdentityCostResponse,
  IndexHealthResponse,
  InsightsResponse,
  LinksResponse,
  MappingResponse,
  MappingProposalResponse,
  MappingStageResponse,
  MappingsResponse,
  MemoryPage,
  MemorySearchResponse,
  ProceduresResponse,
  ProvenanceResponse,
  SourcesResponse,
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
  Task,
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

function useApiQuery<T>(key: unknown[], path: string, enabled?: boolean): UseQueryResult<T> {
  return useQuery<T>({
    queryKey: key,
    queryFn: ({ signal }) => apiGet<T>(path, signal),
    enabled,
  })
}

// --- Fleet (team) ----------------------------------------------------------

export const useRoster = () => useApiQuery<AgentsListResponse>(['roster'], '/api/team/roster')

// Who is holding this session's token (SPEC-057 REQ-043) — the operator
// identity a channel-membership picker offers alongside the roster (H-019).
// Static-token sessions come back `anonymous: true` with `did: null`.
export const useMe = (enabled = true) =>
  useApiQuery<AuthMeResponse>(['auth', 'me'], '/api/auth/me', enabled)

// Polls every 4s so live todo -> in_progress -> done transitions and newly
// dispatched tasks surface on the board without a manual refresh. The board's
// other driving query (roster) is near-static, so only tasks needs the poll.
// `window` (H-004), when passed, scopes the count to tasks touched within it
// (Home's "today" card) — omitted, callers keep seeing the whole backlog
// (the Tasks board, "Needs you" review list).
export const useTeamTasks = (window?: string) =>
  useQuery<TasksResponse>({
    queryKey: ['team', 'tasks', window ?? 'all'],
    queryFn: ({ signal }) =>
      apiGet<TasksResponse>(
        `/api/team/tasks${window ? `?window=${window}` : ''}`,
        signal,
      ),
    refetchInterval: 4000,
  })

/** One prior tool call that lit a trifecta leg — mirrors the agent's ledger entry. */
export interface ApprovalProvenance {
  legs?: string[]
  tool?: string
  args?: string
  at?: string
}
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
  // SPEC-035 approval enrichment. `arguments` is the redacted, length-bounded
  // per-argument preview of the blocked call (WHAT/WHO/WHERE); `provenance`
  // records which prior tool calls lit each trifecta leg and when (WHY it fired);
  // `session_id` is the run/session the call belongs to. All bounded by the agent.
  arguments?: Record<string, string>
  provenance?: ApprovalProvenance[]
  session_id?: string
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
  status: 'deny' | 'new_sighting' | 'unsigned' | 'invalid' | 'error' | 'loaded'
  path: string
  hash: string
  detail: string
  /** DID of whoever signed the artifact on disk right now; '' when unsigned.
   *  A loaded capability may be signed by the AGENT itself (SPEC-033), which
   *  is what lets an operator spot one that has never been through them. */
  signer_did: string
}
export interface GatedResponse {
  gated: GatedCapability[]
}

// Polls every 4s: a capability is gated the moment the loader denies, first-
// sights, or fails to verify it, so newly quarantined tools/skills must surface
// without a manual refresh — same cadence as approvals.
//
// `includeLoaded` is part of the query key, not just the URL: the two views
// are different server responses, so sharing one cache entry would show the
// gated-only list for a beat after the toggle flips and let a mutation
// invalidate the wrong one. Both entries still invalidate together on the
// ['trust','gated'] prefix after an approve.
export const useGatedCapabilities = (includeLoaded: boolean) =>
  useQuery<GatedResponse>({
    queryKey: ['trust', 'gated', includeLoaded],
    queryFn: ({ signal }) =>
      apiGet<GatedResponse>(
        includeLoaded ? '/api/trust/gated?include_loaded=1' : '/api/trust/gated',
        signal,
      ),
    refetchInterval: 4000,
  })

/** A gated capability as it rides along in ``/api/home/needs`` — the raw
 * inventory row, with none of ``/api/trust/gated``'s ``signer_did``
 * enrichment (that read costs a sidecar lookup per row; Home only needs
 * enough to say WHAT is pending and WHERE to go review it). */
export interface HomeNeedsCapability {
  agent_id: string
  agent_label: string
  name: string
  kind: 'tool' | 'skill'
  status: 'deny' | 'new_sighting' | 'unsigned' | 'invalid' | 'error' | 'loaded'
  path: string
  hash: string
  detail: string
}

export interface HomeNeedsQueue<T> {
  count: number
  items: T[]
}

export interface HomeNeedsResponse {
  approvals: HomeNeedsQueue<PendingApproval>
  capabilities: HomeNeedsQueue<HomeNeedsCapability>
  review_tasks: HomeNeedsQueue<Task>
  total: number
}

// Polls every 4s, same cadence as the queues it aggregates (approvals, gated
// capabilities, tasks-in-review): Home's "Needs you" panel must not lag the
// pages an operator would land on after clicking through it. One request
// instead of three, run concurrently server-side (H-001) — see
// ``arcui.routes.home``.
export const useHomeNeeds = () =>
  useQuery<HomeNeedsResponse>({
    queryKey: ['home', 'needs'],
    queryFn: ({ signal }) => apiGet<HomeNeedsResponse>('/api/home/needs', signal),
    refetchInterval: 4000,
  })

export interface CapabilitySource {
  agent_id: string
  name: string
  kind: 'tool' | 'skill'
  path: string
  hash: string
  source: string
}

// Fetched per row only while its source disclosure is open (`enabled`), never
// folded into the gated list: that list polls every 4s, so carrying artifact
// text would re-ship every capability's executable source to every open
// dashboard continuously — and to operators who never asked to read it.
export const useCapabilitySource = (agentId: string, name: string, enabled: boolean) =>
  useQuery<CapabilitySource>({
    queryKey: ['trust', 'source', agentId, name],
    queryFn: ({ signal }) =>
      apiGet<CapabilitySource>(
        `/api/trust/source?agent_id=${encodeURIComponent(agentId)}&name=${encodeURIComponent(name)}`,
        signal,
      ),
    enabled,
    // Overrides the app-wide 10s staleTime: reopening the panel must re-read
    // what is on disk NOW, because the approve gate compares this hash to the
    // polled row hash and a cached body would otherwise keep an edited
    // artifact locked out until the cache aged out.
    staleTime: 0,
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
      apiGet(
        `/api/team/audit?limit=${limit}&target=${encodeURIComponent(`task:${taskId}`)}`,
        signal,
      ),
    enabled: !!taskId,
  })

// --- Messages / team chat --------------------------------------------------

export const useTeamChannels = () =>
  useApiQuery<ChannelsResponse>(['team', 'channels'], '/api/team/channels')

export const useTeamGateways = () =>
  useApiQuery<GatewaysResponse>(['team', 'gateways'], '/api/team/gateways')

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

export const useViewerConfig = () => useApiQuery<Dict>(['config'], '/api/config')

export const useArcllmConfig = () => useApiQuery<Dict>(['arcllm-config'], '/api/arcllm-config')

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

export const useChunks = (agentId: string | null, limit = 50, offset = 0) =>
  useQuery<ChunkPage>({
    queryKey: ['agent', agentId, 'knowledge', 'chunks', limit, offset],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/knowledge/chunks?limit=${limit}&offset=${offset}`, signal),
    enabled: !!agentId,
  })

export const useChunkSearch = (agentId: string | null, q: string, mode: ChunkSearchMode) =>
  useQuery<ChunkSearchResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'chunks', 'search', q, mode],
    queryFn: ({ signal }) =>
      apiGet(
        `/api/agents/${agentId}/knowledge/chunks?q=${encodeURIComponent(q)}&mode=${mode}`,
        signal,
      ),
    enabled: !!agentId && q.trim().length > 0,
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

// --- Graph viewer (H-016 — windowed neighborhood view) ----------------------

export interface GraphNode {
  id: string
  node_type: string
  classification: string
  metadata: Record<string, unknown>
}

export interface GraphEdge {
  src: string
  dst: string
  kind: string
  weight: number
  salience?: number
  last_hit?: string | null
  hits?: number
}

export interface GraphResponse {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

/** Windowed neighborhood view: `node=null` is the whole (capped) scope window;
 *  `node` re-centers the BFS window on a clicked node (neighborhood expand). */
export const useKnowledgeGraph = (agentId: string | null, node: string | null, hops = 1) =>
  useQuery<GraphResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'graph', node, hops],
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({ hops: String(hops) })
      if (node) params.set('node', node)
      return apiGet(`/api/agents/${agentId}/knowledge/graph?${params}`, signal)
    },
    enabled: !!agentId,
  })

// --- Connections data views (SPEC-073 — connected-source projections) -------
//
// Read-only offshoot of the Knowledge browser. Every hook is keyed under the
// same `['agent', agentId, 'knowledge', <slice>, ...]` prefix as the other
// knowledge reads, so an agent switch or a knowledge invalidation clears them
// together.

export const useSources = (agentId: string | null) =>
  useQuery<SourcesResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'sources'],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/knowledge/sources`, signal),
    enabled: !!agentId,
  })

/** Operational source inventory. Unlike semantic source entities, this is
 * populated as soon as an account is connected, before its first ingest. */
export const useConnectedSources = (agentId: string | null) =>
  useQuery<ConnectedSourcesResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'connected-sources'],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/knowledge/connected-sources`, signal),
    enabled: !!agentId,
    refetchInterval: 10_000,
  })

/** Operator mutation that enables the installed Knowledge module on an existing agent. */
export const useActivateConnectedData = (agentId: string | null) => {
  const client = useQueryClient()
  return useMutation<ConnectedDataActivationResponse, Error, void>({
    mutationFn: () => apiPost(`/api/agents/${agentId}/knowledge/connected-data/activate`),
    onSuccess: () =>
      client.invalidateQueries({
        queryKey: ['agent', agentId, 'knowledge', 'connected-sources'],
      }),
  })
}

export const useConnectedSyncStatus = (agentId: string | null) =>
  useQuery<ConnectedSyncStatusResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'sync-status'],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/knowledge/sync`, signal),
    enabled: !!agentId,
    refetchInterval: 10_000,
  })

export const useSourceMapping = (agentId: string | null, sourceId: string | null) =>
  useQuery<MappingResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'sources', sourceId, 'mapping'],
    queryFn: ({ signal }) =>
      apiGet(
        `/api/agents/${agentId}/knowledge/sources/${encodeURIComponent(sourceId!)}/mapping`,
        signal,
      ),
    enabled: !!agentId && !!sourceId,
  })

export const useMappingProposal = (agentId: string | null, sourceId: string | null) =>
  useQuery<MappingProposalResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'connected-sources', sourceId, 'mapping-proposal'],
    queryFn: ({ signal }) =>
      apiGet(
        `/api/agents/${agentId}/knowledge/connected-sources/${encodeURIComponent(sourceId!)}/mapping`,
        signal,
      ),
    enabled: !!agentId && !!sourceId,
  })

export const useStageSourceMapping = (agentId: string | null, sourceId: string | null) => {
  const client = useQueryClient()
  return useMutation<MappingStageResponse, Error, string[]>({
    mutationFn: (homes) =>
      apiPost(
        `/api/agents/${agentId}/knowledge/connected-sources/${encodeURIComponent(sourceId!)}/mapping`,
        { homes },
      ),
    onSuccess: () =>
      client.invalidateQueries({
        queryKey: ['agent', agentId, 'knowledge', 'connected-sources', sourceId, 'mapping-proposal'],
      }),
  })
}

export const useConnectedSourceAction = (agentId: string | null, sourceId: string | null) => {
  const client = useQueryClient()
  return useMutation<{ status: string; action: string; source_id: string }, Error, string>({
    mutationFn: (action) =>
      apiPost(
        `/api/agents/${agentId}/knowledge/sync/${encodeURIComponent(sourceId!)}/${action}`,
      ),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: ['agent', agentId, 'knowledge', 'connected-sources'] }),
        client.invalidateQueries({ queryKey: ['agent', agentId, 'knowledge', 'sync-status'] }),
      ]),
  })
}

export const useConnectedResources = (agentId: string | null, sourceId: string | null) =>
  useQuery<ConnectedResourcesResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'connected-sources', sourceId, 'resources'],
    queryFn: ({ signal }) =>
      apiGet(
        `/api/agents/${agentId}/knowledge/connected-sources/${encodeURIComponent(sourceId!)}/resources`,
        signal,
      ),
    enabled: !!agentId && !!sourceId,
  })

export const useSelectConnectedResources = (agentId: string | null, sourceId: string | null) => {
  const client = useQueryClient()
  return useMutation<ConnectedResourcesResponse, Error, string[]>({
    mutationFn: (resourceIds) =>
      apiPost(
        `/api/agents/${agentId}/knowledge/connected-sources/${encodeURIComponent(sourceId!)}/resources`,
        { resource_ids: resourceIds },
      ),
    onSuccess: () =>
      client.invalidateQueries({
        queryKey: ['agent', agentId, 'knowledge', 'connected-sources', sourceId, 'resources'],
      }),
  })
}

export const useProfileReviews = (
  agentId: string | null,
  status: string,
  sourceId: string,
) =>
  useQuery<ProfileReviewsResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'profile-reviews', status, sourceId],
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({ status })
      if (sourceId) params.set('source_id', sourceId)
      return apiGet(`/api/agents/${agentId}/knowledge/profile-reviews?${params}`, signal)
    },
    enabled: !!agentId,
  })

export const useResolveProfileReview = (agentId: string | null) => {
  const client = useQueryClient()
  return useMutation<ProfileReviewItem, Error, { factId: string; decision: 'approve' | 'decline' | 'undo' }>({
    mutationFn: ({ factId, decision }) =>
      apiPost(`/api/agents/${agentId}/knowledge/profile-reviews/${encodeURIComponent(factId)}/${decision}`),
    onSuccess: () => client.invalidateQueries({ queryKey: ['agent', agentId, 'knowledge', 'profile-reviews'] }),
  })
}

/** Resolve an approval through ArcUI's existing key-backed approval endpoint.
 * Connected-data mappings intentionally share this path: the source service
 * stages a hash-bound request, while only this operator route can sign it. */
export const useResolveApproval = (agentId: string | null, sourceId: string | null) => {
  const client = useQueryClient()
  return useMutation<Dict, Error, { approvalId: string; decision: 'approve' | 'deny' }>({
    mutationFn: ({ approvalId, decision }) =>
      apiPost(`/api/approvals/${encodeURIComponent(approvalId)}/${decision}`),
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({
          queryKey: ['agent', agentId, 'knowledge', 'connected-sources', sourceId, 'mapping-proposal'],
        }),
        client.invalidateQueries({ queryKey: ['approvals'] }),
      ]),
  })
}

export const useMappings = (agentId: string | null) =>
  useQuery<MappingsResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'mappings'],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/knowledge/mappings`, signal),
    enabled: !!agentId,
  })

export const useBlobFolders = (agentId: string | null, source?: string) =>
  useQuery<BlobFoldersResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'blob-folders', source ?? null],
    queryFn: ({ signal }) =>
      apiGet(
        `/api/agents/${agentId}/knowledge/blob-folders${source ? `?source=${encodeURIComponent(source)}` : ''}`,
        signal,
      ),
    enabled: !!agentId,
  })

export const useDatastoreTables = (agentId: string | null) =>
  useQuery<DatastoreTablesResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'datastore-tables'],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/knowledge/datastore-tables`, signal),
    enabled: !!agentId,
  })

export const useDocuments = (agentId: string | null, source: string, q: string) =>
  useQuery<DocumentsResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'documents', source, q],
    queryFn: ({ signal }) =>
      apiGet(
        `/api/agents/${agentId}/knowledge/documents?source=${encodeURIComponent(source)}&q=${encodeURIComponent(q)}`,
        signal,
      ),
    // A source with no query lists what it holds; a query filters that list.
    enabled: !!agentId && !!source,
  })

export const useDatastoreQuery = (
  agentId: string | null,
  source: string,
  op: string,
  table: string,
  args: Record<string, string>,
) =>
  useQuery<DatastoreQueryResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'datastore', source, op, table, args],
    queryFn: ({ signal }) => {
      const qs = new URLSearchParams({ source, op, table, ...args }).toString()
      return apiGet(`/api/agents/${agentId}/knowledge/datastore?${qs}`, signal)
    },
    enabled: !!agentId && !!source && !!table,
  })

export const useProvenance = (agentId: string | null, itemId: string | null) =>
  useQuery<ProvenanceResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'provenance', itemId],
    queryFn: ({ signal }) =>
      apiGet(
        `/api/agents/${agentId}/knowledge/provenance/${encodeURIComponent(itemId!)}`,
        signal,
      ),
    enabled: !!agentId && !!itemId,
  })

export const useIndexHealth = (agentId: string | null) =>
  useQuery<IndexHealthResponse>({
    queryKey: ['agent', agentId, 'knowledge', 'index-health'],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/knowledge/index-health`, signal),
    enabled: !!agentId,
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
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/knowledge/daily-notes/${day}`, signal),
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

export const useBudgets = () => useApiQuery<{ budgets: Dict[] }>(['budgets'], '/api/budget')

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
  embedding_cheapest_model: string | null
  embedding_potential_savings_usd: number
  embedding_potential_savings_pct: number
}
export const useCostEfficiency = (window = '24h') =>
  useApiQuery<CostEfficiencyResponse>(
    ['cost-efficiency', window],
    `/api/cost-efficiency?window=${window}`,
  )

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
  useApiQuery<SessionsListResponse>(
    ['agent', agentId, 'sessions'],
    `/api/agents/${agentId}/sessions`,
  )

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
  useApiQuery<SchedulesResponse>(
    ['agent', agentId, 'schedules'],
    `/api/agents/${agentId}/schedules`,
  )

export interface DeliveryChannel {
  target: string
  label: string
}
export interface AgentChannelsResponse {
  channels: DeliveryChannel[]
}
export const useAgentChannels = (agentId: string) =>
  useApiQuery<AgentChannelsResponse>(
    ['agent', agentId, 'channels'],
    `/api/agents/${agentId}/channels`,
  )

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

export interface DurableInboxThread extends Dict {
  thread_id: string
  subject?: string | null
  updated_at: string
  unread_count: number
  participants: Array<{
    participant_id: string
    display_name?: string | null
    role?: 'agent' | 'human' | 'service'
  }>
}

export interface DurableInboxMessage extends Dict {
  message_id: string
  thread_id: string
  body: string
  created_at: string
  sender: { participant_id: string; display_name?: string | null; role?: 'agent' | 'human' | 'service' }
  recipients?: Array<{
    participant_id: string
    display_name?: string | null
    role?: 'agent' | 'human' | 'service'
  }>
  attachments?: string[]
  reply_to_id?: string | null
}

export interface DurableInboxResponse {
  threads: DurableInboxThread[]
  next_cursor?: string | null
}

export interface DurableThreadResponse {
  messages: DurableInboxMessage[]
  handoffs: Dict[]
  next_cursor?: string | null
}

export interface DurableInboxSearchResponse {
  messages: DurableInboxMessage[]
}

export const useAgentInbox = (agentId: string) =>
  useQuery<DurableInboxResponse>({
    queryKey: ['agent', agentId, 'inbox'],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/inbox`, signal),
    refetchInterval: 4000,
  })

export const useAgentInboxThread = (agentId: string, threadId: string | null) =>
  useQuery<DurableThreadResponse>({
    queryKey: ['agent', agentId, 'inbox', threadId],
    queryFn: ({ signal }) => apiGet(`/api/agents/${agentId}/inbox/${threadId}`, signal),
    enabled: !!threadId,
  })

export const useAgentInboxSearch = (agentId: string, query: string) =>
  useQuery<DurableInboxSearchResponse>({
    queryKey: ['agent', agentId, 'inbox', 'search', query],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/inbox/search?q=${encodeURIComponent(query)}`, signal),
    enabled: query.trim().length > 0,
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
export const useRubric = (agentId: string, prompt: { package: string; name: string } | null) =>
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
        queryClient.invalidateQueries({
          queryKey: ['agent', agentId, 'prompts'],
        }),
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
  useApiQuery<PolicyStatsResponse>(
    ['agent', agentId, 'policy', 'stats'],
    `/api/agents/${agentId}/policy/stats`,
  )

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

// Operator-gated save through the SAME `PUT /files/read` chokepoint the read
// side above resolves — server-side this is direct workspace filesystem I/O
// (ADR-029), never the agent's write/edit tools, and every save (applied,
// denied, or errored) is audited (files_write.py). The viewer role never
// reaches the filesystem: a non-operator PUT is rejected server-side before
// any byte is touched, regardless of what this hook is wired to in the UI.
export const useSaveAgentFile = (agentId: string) => {
  const queryClient = useQueryClient()
  return useMutation<FileWriteResponse, Error, { path: string; content: string }>({
    mutationFn: ({ path, content }) =>
      apiPut(`/api/agents/${agentId}/files/read?path=${encodeURIComponent(path)}`, { content }),
    onSuccess: (_data, { path }) =>
      queryClient.invalidateQueries({ queryKey: ['agent', agentId, 'file', path] }),
  })
}

// --- SPEC-028: tool/code timeline, spawn lineage, per-identity cost --------

// Polls every 4s: a run's turns/tool_calls/status here are a snapshot, and the
// timeline drawer for the same run re-fetches fresh on every open — without a
// matching poll here, the list (and any header stats derived from it) freeze
// at first-load while the drawer underneath keeps moving, so the two disagree
// for the entire life of a long-running run.
// `window` (H-004/H-006), when passed, scopes the fold to runs from the last
// window (SQL-pushed cutoff) instead of the last N raw rows — Home's "today"
// card wants that; the ArcRun page and agent-detail runs tab omit it and keep
// seeing full recent history exactly as before.
export const useRuns = (window?: string) =>
  useQuery<RunsResponse>({
    queryKey: ['runs', window ?? 'all'],
    queryFn: ({ signal }) =>
      apiGet<RunsResponse>(`/api/runs${window ? `?window=${window}` : ''}`, signal),
    refetchInterval: 4000,
  })

export const useRunTimeline = (runId: string | null) =>
  useQuery<RunTimelineResponse>({
    queryKey: ['run', runId, 'timeline'],
    queryFn: ({ signal }) => apiGet(`/api/runs/${encodeURIComponent(runId!)}/timeline`, signal),
    enabled: !!runId,
    refetchInterval: 4000,
  })

export const useRunRecalls = (runId: string | null) =>
  useQuery<AuditEventsResponse>({
    queryKey: ['run', runId, 'recalls'],
    queryFn: ({ signal }) => apiGet(`/api/runs/${encodeURIComponent(runId!)}/recalls`, signal),
    enabled: !!runId,
  })

export const useSpawnTree = (root: string | null) =>
  useApiQuery<SpawnTreeResponse>(
    ['spawn-tree', root],
    `/api/spawn-tree${root ? `?root=${encodeURIComponent(root)}` : ''}`,
  )

export const useIdentityCost = (window = '24h') =>
  useApiQuery<IdentityCostResponse>(
    ['stats', 'by-identity', window],
    `/api/stats/by-identity?window=${window}`,
  )

// --- SPEC-061 ArcFlow (COMP-020/023) — thin client over the workflow routes.
//
// Every mutation here is a direct 1:1 call to the COMP-023 route layer,
// which itself only relays to the (not-yet-merged) arcteam control plane —
// this file adds no business logic, only react-query plumbing.

// `includeArchived` is part of the query key, not just the URL: archived-on and
// archived-off are different server responses, so a shared cache entry would
// flash the wrong list for a beat after the toggle flips. Both still invalidate
// together on the ['workflows'] prefix after any mutation.
export const useWorkflows = (includeArchived = false) =>
  useApiQuery<WorkflowsListResponse>(
    ['workflows', includeArchived],
    includeArchived ? '/api/workflows?include_archived=true' : '/api/workflows',
  )

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
    mutationFn: () => apiPost(`/api/workflows/${encodeURIComponent(workflowId)}/request-signature`),
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
      queryClient.invalidateQueries({
        queryKey: ['workflow-file', workflowId, vars.path],
      })
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
      apiPost(`/api/workflow-tasks/${encodeURIComponent(taskId)}/gate`, {
        decision,
        notes,
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({
        predicate: (q) => q.queryKey[0] === 'workflow-run',
      }),
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
    mutationFn: ({ envVar, value }) => apiPut(`/api/keys/${encodeURIComponent(envVar)}`, { value }),
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

// --- Connections and grants (SPEC-064) -------------------------------------
//
// Two scopes, and the keys say which. A CONNECTION belongs to the deployment —
// one account, one credential, connected once — so every verb below is keyed by
// instance with no agent in it. A GRANT is the agent-scoped thing, and it is the
// only thing that decides access, so granting and revoking invalidate BOTH the
// deployment listing and the per-agent one: those are two views of one fact, and
// a stale second view is how an operator concludes a revoke did not work.

const CONNECTIONS_KEY = ['connections']
const agentConnectorsKey = (agentId: string | null) => ['agent', agentId, 'connectors']
const doctorKey = (instance: string) => ['connections', instance, 'doctor']
const authStatusKey = (instance: string) => ['connections', instance, 'auth-status']

const connectionPath = (instance: string, verb = '') =>
  `/api/connections/${encodeURIComponent(instance)}${verb}`

// What this deployment could connect. Bundles whose manifest would not parse
// come back under `unreadable` rather than failing the listing.
export const useConnectorCatalog = () =>
  useApiQuery<ConnectorCatalogResponse>(['connectors', 'catalog'], '/api/connectors/catalog')

// Every connection and who holds it — the whole "who can reach what" question.
export const useConnections = () =>
  useApiQuery<ConnectionsResponse>(CONNECTIONS_KEY, '/api/connections')

// The other direction: what one agent can reach. Same rows, its grants only.
export const useAgentConnectors = (agentId: string | null) =>
  useQuery<AgentConnectorsResponse>({
    queryKey: agentConnectorsKey(agentId),
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${encodeURIComponent(agentId!)}/connectors`, signal),
    enabled: !!agentId,
  })

// Every view of who holds what, refreshed together after a change.
const useGrantInvalidator = () => {
  const queryClient = useQueryClient()
  return () => {
    queryClient.invalidateQueries({ queryKey: CONNECTIONS_KEY })
    queryClient.invalidateQueries({ queryKey: ['agent'] })
  }
}

// Secrets are consumed by the route and dropped; the response names tools and
// grantees, not credentials, so nothing sensitive reaches the cache.
export const useInstallConnector = () => {
  const invalidate = useGrantInvalidator()
  return useMutation<
    ConnectorInstallResponse,
    Error,
    {
      extension: string
      instance: string
      agents: string[]
      secrets: Record<string, string>
    }
  >({
    mutationFn: (body) => apiPost('/api/connections', body),
    onSuccess: invalidate,
  })
}

// Hand one connected account to more agents. Nothing is copied — the credential
// stays at its one coordinate and every grantee reads it through the same store.
export const useGrantConnection = (instance: string) => {
  const invalidate = useGrantInvalidator()
  return useMutation<ConnectorMutationResponse, Error, string[]>({
    mutationFn: (agents) => apiPost(connectionPath(instance, '/grant'), { agents }),
    onSuccess: invalidate,
  })
}

export const useRevokeConnection = (instance: string) => {
  const invalidate = useGrantInvalidator()
  return useMutation<ConnectorMutationResponse, Error, string[]>({
    mutationFn: (agents) => apiDelete(connectionPath(instance, '/grant'), { agents }),
    onSuccess: invalidate,
  })
}

// Rotation. The response lists field names only. One write serves every agent
// granted this connection, because there is only ever one copy of the credential.
//
// Invalidates the auth read (`useConnectorAuthorization`) alongside doctor: that
// query is what decides an OAuth connector's `authorize_url` ("Add the app key
// and secret first…") and what fills a rotation form's non-sensitive fields the
// next time it opens. Leaving it cached meant an operator who had just saved a
// working app key/secret still saw "Add the app key and secret first" — keys
// that were genuinely saved, reported back to the UI as not saved.
export const useReauthConnector = (instance: string) => {
  const queryClient = useQueryClient()
  return useMutation<ConnectorAuthResponse, Error, Record<string, string>>({
    mutationFn: (secrets) => apiPut(connectionPath(instance, '/auth'), { secrets }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: doctorKey(instance) })
      queryClient.invalidateQueries({ queryKey: ['connections', instance, 'auth'] })
      queryClient.invalidateQueries({ queryKey: authStatusKey(instance) })
    },
  })
}

// Opens a live connection, so it is a mutation (and operator-only server side).
// Its result is the row's live status — deliberately not cached.
export const useProbeConnector = (instance: string) =>
  useMutation<ConnectorProbeResponse, Error, void>({
    mutationFn: () => apiPost(connectionPath(instance, '/probe')),
  })

export const useConnectorDoctor = (instance: string, enabled: boolean) =>
  useQuery<ConnectorDoctorResponse>({
    queryKey: doctorKey(instance),
    queryFn: ({ signal }) => apiGet(connectionPath(instance, '/doctor'), signal),
    enabled,
  })

// Records the tool contract served right now (rug-pull defense, REQ-291).
export const useApproveConnector = (instance: string) => {
  const queryClient = useQueryClient()
  return useMutation<ConnectorApproveResponse, Error, void>({
    mutationFn: () => apiPost(connectionPath(instance, '/approve')),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: doctorKey(instance) }),
  })
}

// Installs the host binaries a bundle declares, so an operator never has to
// open a terminal. Keyed by extension, not instance: the prerequisite belongs
// to the bundle and is missing before any instance exists.
export const useHostSetup = (extension: string) => {
  const queryClient = useQueryClient()
  return useMutation<HostSetupResponse, Error, void>({
    mutationFn: () => apiPost(connectionPath(extension, '/host-setup')),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['connectors', 'catalog'] }),
  })
}

// What one connected instance was configured with. Read when a rotation form
// opens, so the non-sensitive fields can be shown filled in rather than blank.
// Nothing here can carry a credential: the server populates a value only for a
// field the bundle declared non-sensitive.
export const useConnectorAuthorization = (instance: string, enabled: boolean) =>
  useQuery<ConnectorAuthorizationResponse>({
    queryKey: ['connections', instance, 'auth'],
    queryFn: ({ signal }) => apiGet(connectionPath(instance, '/auth'), signal),
    enabled,
    retry: false,
  })

// Is the host binary behind this connection signed in? Only meaningful for
// bundles that declare no secrets — those hold their own credential.
export const useConnectorAuthStatus = (instance: string, enabled: boolean) =>
  useQuery<ConnectorAuthStatusResponse>({
    queryKey: authStatusKey(instance),
    queryFn: ({ signal }) => apiGet(connectionPath(instance, '/auth-status'), signal),
    enabled,
    retry: false,
  })

// Runs the bundle's login command. `token` is optional — some binaries take one
// non-interactively; the rest answer with the command a person must run.
export const useAuthorizeConnector = (instance: string) => {
  const queryClient = useQueryClient()
  return useMutation<ConnectorAuthStatusResponse, Error, { token?: string }>({
    mutationFn: (body) => apiPost(connectionPath(instance, '/authorize'), body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: authStatusKey(instance) }),
  })
}

// Native OAuth connect: exchange the one-time authorization code for a durable
// refresh token, server-side. The operator opens the authorize URL, pastes the
// code here, and this finishes the sign-in — no token is ever typed or stored
// short-lived. Operator-only server side.
export const useCompleteOauth = (instance: string) => {
  const queryClient = useQueryClient()
  return useMutation<ConnectorAuthStatusResponse, Error, { code: string }>({
    mutationFn: (body) => apiPost(connectionPath(instance, '/oauth'), body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: authStatusKey(instance) })
      queryClient.invalidateQueries({ queryKey: ['connections', instance, 'auth'] })
    },
  })
}

// Disconnects the account for everyone: its credential, its definition, and
// every grant on it. There is no per-agent removal, because a connection left
// standing for one agent and gone for another is a credential nobody thinks is live.
export const useRemoveConnector = () => {
  const invalidate = useGrantInvalidator()
  return useMutation<ConnectorRemoveResponse, Error, string>({
    mutationFn: (instance) => apiDelete(connectionPath(instance)),
    onSuccess: invalidate,
  })
}
