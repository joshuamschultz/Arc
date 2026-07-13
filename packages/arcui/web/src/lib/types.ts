// TypeScript mirror of the arcui wire contract (types.py + schemas.py).
//
// Many backend payloads are `dict[str, Any]` passthroughs (traces, agents,
// bullets, tasks…). We model the envelopes exactly and give the inner objects
// permissive interfaces: the fields the UI reads are typed-but-optional, and
// an index signature keeps unknown keys flowing through. This is deliberately
// storage-agnostic — when arcllm/arcrun move to a database, these shapes hold
// as long as the JSON does.

export type Dict = Record<string, unknown>

// --- Domain shapes (permissive: known fields typed, rest passthrough) -------

export interface Trace {
  [key: string]: unknown
  trace_id?: string
  agent?: string
  agent_label?: string
  agent_did?: string
  provider?: string
  model?: string
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  // O2: prompt-cache accounting per call. None when the provider reported none.
  cache_read_tokens?: number | null
  cache_write_tokens?: number | null
  prompt_tokens?: number | null
  completion_tokens?: number | null
  duration_ms?: number
  cost_usd?: number
  status?: string
  timestamp?: string
  tools?: unknown
  request?: unknown
  response?: unknown
}

export interface Agent {
  [key: string]: unknown
  agent_id?: string
  name?: string
  display_name?: string
  did?: string
  org?: string
  type?: string
  model?: string
  provider?: string
  online?: boolean
  color?: string
  role_label?: string
  hidden?: boolean
  workspace_path?: string
}

export interface PolicyBullet {
  [key: string]: unknown
  id?: string
  text?: string
  score?: number
  uses?: number
  reviewed?: string
  created?: string
  source?: string
  retired?: boolean
  agent_id?: string
}

export type TaskStatus = 'backlog' | 'todo' | 'in_progress' | 'review' | 'done' | 'failed'
export type TaskPriority = 'low' | 'medium' | 'high' | 'critical'

// Mirrors arcstore.tasks.Task (SPEC-056). `agent_id` is stamped onto the
// fleet `/api/team/tasks` rows only (resolved owner_did -> roster agent_id);
// absent on the per-agent `/api/agents/{id}/tasks` rows.
export interface Task {
  [key: string]: unknown
  id: string
  title: string
  description?: string
  status?: TaskStatus
  priority?: TaskPriority
  owner_did?: string | null
  creator_did?: string
  parent_id?: string | null
  run_id?: string | null
  blocked_by?: string[]
  tags?: string[]
  metadata?: Dict
  output?: Dict | null
  resolution?: string | null
  created_at?: string | null
  updated_at?: string | null
  agent_id?: string | null
  // Lifecycle reliability + review gate (SPEC-056 Phases 1–3).
  started_at?: string | null
  completed_at?: string | null
  duration_seconds?: number | null
  attempts?: number
  max_attempts?: number
  last_error?: string | null
  timeout_seconds?: number | null
  next_attempt_at?: string | null
  cancel_requested?: boolean
  requires_review?: boolean
}

export interface AuditEvent {
  [key: string]: unknown
  timestamp?: string
  event_type?: string
  action?: string
  actor?: string
  agent_id?: string
  decision?: string
  severity?: string
}

// --- HTTP response envelopes (schemas.py, 1:1) -----------------------------

export interface ErrorResponse {
  error: string
}

export interface AgentsListResponse {
  agents: Agent[]
}

export interface TracesResponse {
  traces: Trace[]
  cursor?: string | null
}

export interface StatsResponse {
  stats: Dict
  window: string
}

export interface AuditEventsResponse {
  events: AuditEvent[]
}

export interface SessionEntry {
  sid: string
  path: string
  size: number
  mtime: number
}

export interface SessionsListResponse {
  sessions: SessionEntry[]
}

export interface SessionReplayResponse {
  sid: string
  page: number
  page_size: number
  total: number
  messages: Dict[]
}

export interface TasksResponse {
  tasks: Task[]
}

export interface SchedulesResponse {
  schedules: Dict[]
}

export interface SkillsResponse {
  skills: Dict[]
}

export interface ToolsResponse {
  tools: Dict[]
  allowlist: string[]
  denylist: string[]
}

export interface PolicyResponse {
  raw: string
  bullets: PolicyBullet[]
}

export interface PolicyBulletsResponse {
  bullets: PolicyBullet[]
}

export interface PolicyStatsResponse {
  total: number
  active: number
  retired: number
  avg_score: number
}

export interface TeamPolicyStatsResponse extends PolicyStatsResponse {
  per_agent: Dict[]
}

export interface TeamToolsSkillsResponse {
  skills: Dict[]
  tools: Dict[]
}

export interface FilesTreeEntry {
  path: string
  type: string
  size: number
  mtime: number
}

export interface FilesTreeResponse {
  root: string
  entries: FilesTreeEntry[]
}

export interface FileReadResponse {
  path: string
  size: number
  mtime: number
  content: string
  content_type: string
}

export interface FileWriteResponse {
  path: string
  size: number
  mtime: number
  signature_stale: boolean
  message: string
}

// --- Knowledge (arcmemory.operator facade, COMP-001/002/003) ---------------

export interface MemoryRecord {
  entry_id: string
  scope: string
  kind: string
  text: string
  classification: string
  created: string
  salience: number
  importance: number // 1..10 projection of salience
  recency: number // 0..1 decay indicator
  source: string
  entities: string[]
}

export interface MemoryPage {
  items: MemoryRecord[]
  total: number
  limit: number
  offset: number
}

export interface EntityRecord {
  slug: string
  name: string
  entity_type: string
  classification: string
  confidence: number
  importance: number // 1..10 projection of confidence
  source: string
  links_to: string[]
  facts: string[]
  tags: string[]
}

export interface LinkRecord {
  source_id: string
  target_id: string
  target_type: string // "entity" | "cue"
  kind: string // "link" | "assoc" | "tagged"
  weight: number
}

export interface LinksResponse {
  items: LinkRecord[]
}

export interface EntitiesResponse {
  items: EntityRecord[]
}

export interface Recall {
  source: string
  content: string
  score: number
  kind: string
  confidence: string
  classification: string
  verify_first: boolean
}

export interface MemorySearchResponse {
  items: Recall[]
  query: string
}

export type MutationStatus = 'applied' | 'error'

export interface MutationResult {
  status: MutationStatus
  operation: string
  actor_did: string
  entry_id?: string | null
  error?: string | null
}

export interface MutationResponse {
  status: MutationStatus
  results: MutationResult[]
}

// --- Capabilities (arcagent.capabilities.inventory, COMP-007/008/009) ------

export interface CapabilityInventoryItem {
  kind: string // "skill" | "tool"
  name: string
  version: string
  description: string
  source_root: string // "builtins" | "builtins-skills" | "global" | "global-skills" | "agent" | "agent-skills" | "workspace" | "workspace-skills"
  status: string // verbatim loader verdict — never re-derived client-side
  status_detail: string
}

export interface RuntimeToolItem {
  name: string
  description: string
  classification: string
  transport: string
}

export interface AgentCapabilityInventory {
  items: CapabilityInventoryItem[]
  runtime: boolean
  runtime_tools: RuntimeToolItem[]
}

// --- Channels (arcteam, COMP-005/006) ---------------------------------------

export interface Channel {
  name: string
  description: string
  members: string[]
  created: string
  clearance: string
}

export interface ChannelsResponse {
  channels: Channel[]
}

export interface ConfigResponse {
  config: Dict
  raw: string
  mtime: number
}

export interface ExportTracesResponse {
  traces: Trace[]
  count: number
}

export interface ControlResponseEnvelope {
  response: Dict
}

// --- Aggregate stats (passthrough dict; common keys for the UI) ------------

export interface AggregateStats {
  [key: string]: unknown
  request_count?: number
  total_tokens?: number
  total_cost?: number
  latency_avg?: number
  latency_p50?: number
  latency_p95?: number
  latency_p99?: number
  model_stats?: Dict
  provider_counts?: Dict
  agent_counts?: Dict
}

// --- SPEC-028: tool/code timeline, spawn lineage, per-identity cost --------

export interface TimelineEntry {
  kind: string // run_event | tool_event | llm_call
  ts?: string | null
  request_id?: string | null
  record_id?: string | null // arcstore row id — for an llm_call this is its trace_id
  // tool_event
  tool_name?: string | null
  phase?: string | null
  outcome?: string | null
  latency_ms?: number | null
  args_digest?: string | null
  args_size?: number | null
  result_digest?: string | null
  result_size?: number | null
  // tool bodies (present only when raw capture is on): { args, result }
  extra?: Record<string, unknown> | null
  // run_event
  name?: string | null
  // llm_call
  model?: string | null
  agent_label?: string | null
  cost_usd?: number | null
  prompt_tokens?: number | null
  completion_tokens?: number | null
}

export interface RunTimelineResponse {
  run_id: string
  timeline: TimelineEntry[]
}

export interface SpawnNode {
  did: string
  role?: string | null
  depth?: number | null
  outcome?: string | null
  children: SpawnNode[]
}

export interface SpawnTreeResponse {
  tree: SpawnNode
}

export interface IdentityCost {
  identity: string
  request_count: number
  error_count: number
  total_tokens: number
  total_cost: number
}

export interface IdentityCostResponse {
  window: string
  identities: IdentityCost[]
}

// A run = one user-question→final-response cycle (one arcrun run_id), folded
// from its run/tool/llm spool rows on read.
export interface RunSummary {
  run_id: string
  agent: string
  actor_did?: string | null
  started_at?: string | null
  ended_at?: string | null
  duration_ms?: number | null
  turns: number
  tool_calls: number
  llm_calls: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cost_usd: number
  status: string // completed | running | error
}

export interface RunsResponse {
  runs: RunSummary[]
}

// --- Curated memory layer (U3/U4 — insights, procedures, daily notes) -------
// These mirror arcmemory's glass-box cards (Insight/Procedure/DaySummary),
// surfaced read-only by the knowledge routes so the curated layer — not the
// raw episodic stream — is the headline of the Knowledge view.

export interface InsightCard {
  id: string
  statement: string
  trigger: string
  cues: string[]
  instances: string[]
  confidence: number
  classification: string
}

export interface ProcedureCard {
  slug: string
  title: string
  when_to_use: string
  steps: string[]
  use_count: number
  classification: string
}

export interface DailyNoteMeta {
  day: string // YYYY-MM-DD
  classification: string
}

export interface DailyNoteDetail {
  day: string
  timeline: string[]
  discussions: string[]
  decisions: string[]
  people: string[]
  goals: string[]
  tasks: string[]
  classification: string
}

export interface InsightsResponse {
  items: InsightCard[]
}

export interface ProceduresResponse {
  items: ProcedureCard[]
}

export interface DailyNotesResponse {
  items: DailyNoteMeta[]
}

// --- Capability detail drawers (U5/U6 — skill SKILL.md + tool source) -------

export interface SkillDetail {
  name: string
  version: string
  description: string
  source_root: string
  source_path: string
  status: string
  status_detail: string
  content: string // SKILL.md body
  editable: boolean // true when the file lives in an editable workspace root
  // Save target for the existing `PUT /files/read` route (null when read-only).
  write_root: 'workspace' | 'agent' | null
  write_path: string | null // relative to write_root
}

export interface ToolDetail {
  name: string
  transport: string
  classification: string
  description: string
  source_path: string
  content: string // the @tool Python source (empty for unresolvable builtins)
  editable: boolean // true for agent/workspace-authored tools; false for builtins
  write_root: 'workspace' | 'agent' | null
  write_path: string | null // relative to write_root
}
