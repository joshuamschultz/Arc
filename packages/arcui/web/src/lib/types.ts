// TypeScript mirror of the arcui wire contract (types.py + schemas.py).
//
// Many backend payloads are `dict[str, Any]` passthroughs (traces, agents,
// bullets, tasks…). We model the envelopes exactly and give the inner objects
// permissive interfaces: the fields the UI reads are typed-but-optional, and
// an index signature keeps unknown keys flowing through. This is deliberately
// storage-agnostic — when arcllm/arcrun move to a database, these shapes hold
// as long as the JSON does.

export type Dict = Record<string, unknown>

// --- Live events (types.py: UIEvent) ---------------------------------------

export type EventLayer = 'llm' | 'run' | 'agent' | 'team' | 'scheduler'

export interface UIEvent {
  layer: EventLayer
  event_type: string
  agent_id: string
  agent_name: string
  source_id: string
  timestamp: string
  data: Dict
  sequence: number
}

export interface EventBatch {
  type: 'event_batch'
  events: UIEvent[]
}

export interface FileChangeMessage {
  type: 'file_change'
  agent_id: string
  event_type: string
  path: string
  payload?: Dict
}

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
  degraded?: boolean
  color?: string
  role_label?: string
  hidden?: boolean
  workspace_path?: string
}

export interface PolicyBullet {
  [key: string]: unknown
  text?: string
  score?: number
  uses?: number
  created?: string
  retired?: boolean
  agent_id?: string
}

export interface Task {
  [key: string]: unknown
  id?: string
  subject?: string
  status?: string
  owner?: string
  agent_id?: string
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
