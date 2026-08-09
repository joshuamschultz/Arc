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

// --- Prompts (COMP-010: editable system prompts) ---------------------------

export type PromptStatus = 'stock' | 'overridden'

export interface PromptListItem {
  package: string
  name: string
  description: string
  status: PromptStatus
}

export interface PromptListResponse {
  items: PromptListItem[]
}

export interface PromptDetail {
  package: string
  name: string
  description: string
  status: PromptStatus
  stock: string
  effective: string
  diff: string // server-computed unified diff (stock -> effective)
}

export interface PromptWriteResponse {
  package: string
  name: string
  signer_did: string
  sha256: string
  message: string
}

export interface PromptResetResponse {
  package: string
  name: string
  message: string
}

// --- Structured rubric editor (arcskill/judge_rubric) ----------------------
// The rubric prompt's body is YAML, not prose; the drawer edits it as a form.

export interface RubricDimension {
  checklist: string[]
  anti_inflation: string
}

export interface RubricResponse {
  package: string
  name: string
  status: PromptStatus
  dimensions: Record<string, RubricDimension>
}

export interface RubricUpdate {
  dimensions: Record<string, RubricDimension>
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

export interface LifeEventCard {
  slug: string
  title: string
  date: string // when it HAPPENED (YYYY-MM-DD)
  recorded: string // when memory wrote it down
  event_type: string
  participants: string[] // entity slugs
  summary: string
  outcome: string
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

export interface EventsResponse {
  items: LifeEventCard[]
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

// --- SPEC-061 ArcFlow (COMP-020/023) -----------------------------------------
//
// The arcteam control plane (COMP-021) that owns these shapes is a concurrent,
// not-yet-merged workstream — like the Python route module's Protocol, these
// are permissive "passthrough" interfaces (known fields typed, rest flows
// through) rather than a tight contract, so this dashboard degrades gracefully
// if the real shape adds fields rather than 500ing on an unrecognized key.

export type WorkflowStatus = 'draft' | 'signed' | 'archived'
export type WorkflowNodeKind = 'agent' | 'tool' | 'script' | 'router' | 'gate'
export type WorkflowRunStatus =
  'pending' | 'running' | 'waiting_gate' | 'done' | 'failed' | 'cancelled'
// Per-node run status. `skipped` = an untaken branch; `looping` = an
// in-progress loop iteration. Both are sourced from the Run's path taken —
// with lazy materialization there are no task rows for unreached nodes, so
// this status is NEVER read from a task row for those two states.
export type WorkflowNodeStatus =
  'pending' | 'running' | 'waiting_gate' | 'done' | 'failed' | 'skipped' | 'looping'

export interface WorkflowNode {
  [key: string]: unknown
  id: string
  kind: WorkflowNodeKind
  needs?: string[]
  join?: string | null
  when?: string | null
  loop_back_to?: string | null
  max_iterations?: number | null
  agent?: string | null
}

export interface WorkflowEdge {
  [key: string]: unknown
  from: string
  to: string
}

export interface WorkflowVersion {
  [key: string]: unknown
  version: number
  signer?: string | null
  reason?: string | null
  created_at?: string
}

export interface WorkflowLastRun {
  [key: string]: unknown
  run_id: string
  status: WorkflowRunStatus
  ended_at?: string | null
}

export interface WorkflowSummary {
  [key: string]: unknown
  id: string
  name: string
  version: number
  status: WorkflowStatus
  trigger?: Dict | null
  last_run?: WorkflowLastRun | null
}

export interface WorkflowDetail extends WorkflowSummary {
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  channel?: string | null
  versions?: WorkflowVersion[]
}

export interface WorkflowsListResponse {
  workflows: WorkflowSummary[]
}

// Exact wire shape SDD COMP-002 specifies — also modelled server-side as
// `arcui.routes.workflows.WorkflowFieldError` (Pydantic, `extra="forbid"`).
export interface WorkflowFieldError {
  node_id: string
  field: string
  error: string
  observed?: unknown
  admissible?: unknown[] | null
}

export interface WorkflowErrorsResponse {
  errors: WorkflowFieldError[]
}

export interface WorkflowRunSummary {
  [key: string]: unknown
  run_id: string
  status: WorkflowRunStatus
  started_at?: string
  ended_at?: string | null
}

export interface WorkflowRunsResponse {
  runs: WorkflowRunSummary[]
}

export interface WorkflowRunNodeStatus {
  [key: string]: unknown
  node_id: string
  status: WorkflowNodeStatus
  iteration?: number | null
  max_iterations?: number | null
  // Joins the EXISTING `/api/runs/{run_id}/timeline` (observe_run.py) — a
  // node's OWN per-dispatch execution trace, distinct from the workflow
  // run_id itself. See routes/workflows.py's naming note.
  task_run_id?: string | null
  /** The row a gate is resolved by — present on a node that has one. */
  task_id?: string | null
  kind?: string | null
}

export interface WorkflowRunDetail {
  [key: string]: unknown
  run_id: string
  workflow_id: string
  version: number
  status: WorkflowRunStatus
  path_taken: string[]
  nodes: WorkflowRunNodeStatus[]
}

export type GateDecision = 'approve' | 'fail_run' | 'return_for_revision'

/** `ApiError.errors` arrives as untyped `Record<string, unknown>[]` (the
 * shared HTTP client has no workflow-specific knowledge); this is the one
 * place that casts it back to the typed wire contract for rendering. */
export function asWorkflowFieldErrors(
  errors?: Array<Record<string, unknown>>,
): WorkflowFieldError[] {
  return (errors ?? []) as unknown as WorkflowFieldError[]
}

// --- Connection surfaces (SPEC-064) ----------------------------------------

/** One provider arcllm knows about. `present` is the whole answer: the API
 *  carries no value, prefix, length, or hash, so a surface built on this
 *  cannot leak a key (D-583). */
export interface KeyEntry {
  provider: string
  env_var: string
  required: boolean
  present: boolean
}

export interface KeysResponse {
  keys: KeyEntry[]
}

/** Result of a key write or clear. The value is never echoed back. */
export interface KeyWriteResponse {
  env_var: string
  present: boolean
  removed?: boolean
}

/** A credential a bundle declares; `prompt` is the operator-facing ask. */
/** One value the connect form asks for. `sensitive` is the bundle's own word for
 *  whether it is a credential: an API token is, a base URL and an account address
 *  are not, and masking those hid the one thing an operator needed to check.
 *  It defaults to true server-side, so a field that says nothing stays masked.
 *  `value` is what is configured now, and the server populates it only for a
 *  non-sensitive field — a credential is never read back out of the store. */
export interface ConnectorSecret {
  name: string
  prompt: string
  sensitive: boolean
  value: string
}

/** A binary (or similar) the bundle needs on this host. `satisfied` is this
 *  host's answer, not the manifest's: absent when the server does not check,
 *  in which case the surface knows only that the bundle declares it. */
export interface HostRequirement {
  name: string
  instruction: string
  satisfied?: boolean
}

export interface ConnectorTool {
  name: string
  description: string
  classification: string
  capability_tags: string[]
}

export interface CatalogBundle {
  /** The coordinate: what every request is keyed by, and what a path is built from. */
  name: string
  /** What the product calls itself — `1Password`, not `onepassword`. Never blank:
   *  a bundle declaring none falls back to the coordinate server-side. */
  display_name: string
  version: string
  description: string
  attachment: string
  tier_floor: string
  approval_default: string
  secrets: ConnectorSecret[]
  host_requires: HostRequirement[]
  tools: ConnectorTool[]
  root: string
}

/** A bundle on the search path whose manifest would not parse — surfaced
 *  rather than silently dropped. */
export interface UnreadableBundle {
  name: string
  reason: string
}

export interface ConnectorCatalogResponse {
  available: CatalogBundle[]
  unreadable: UnreadableBundle[]
}

/** One connected account. `agents` is the grant list and the only thing that
 *  decides access: an agent not named here gets no verb and no path to the
 *  credential, so a row rendered without it says nothing about who can use it. */
export interface ConnectorInstance {
  instance: string
  extension: string
  /** What to call `extension` in front of a person. Falls back to the coordinate. */
  extension_display_name: string
  approval: string
  agents: string[]
}

/** Every connection this deployment has, and who holds each one — the answer to
 *  "who can reach what" for the whole fleet in a single read. */
export interface ConnectionsResponse {
  connections: ConnectorInstance[]
  extensions_roots: string[]
}

/** What one agent can reach: the same rows, filtered to its grants. */
export interface AgentConnectorsResponse {
  instances: ConnectorInstance[]
  extensions_roots: string[]
}

export interface ConnectorInstallResponse {
  instance: string
  extension: string
  tools: string[]
  detail: string
  agents: string[]
}

/** Rotation result — field names only, never values. */
/** How one connected instance is authorised. `credentials` is the same field list
 *  the catalog carries, except that a non-sensitive field arrives with the value
 *  that is configured now — which is what lets a rotation form show an operator the
 *  URL they already set instead of making them retype it blind. A sensitive field
 *  always arrives empty; the server never reads one out of the store. */
export interface ConnectorAuthorizationResponse {
  instance: string
  extension: string
  extension_display_name: string
  credentials: ConnectorSecret[]
  reachable: boolean
  detail: string
}

export interface ConnectorAuthResponse {
  instance: string
  updated: string[]
}

export interface ConnectorProbeResponse {
  reachable: boolean
  detail: string
  tools: ConnectorTool[]
}

/** One row of `arc connector doctor`. `status` is the CLI's vocabulary. */
export interface DoctorCheck {
  check: string
  status: string
  detail: string
}

export interface ConnectorDoctorResponse {
  checks: DoctorCheck[]
}

export interface ConnectorApproveResponse {
  instance: string
  approved: string[]
}

export interface ConnectorRemoveResponse {
  instance: string
  removed_secrets: string[]
  removed_config: boolean
  removed_state: boolean
}

/** Result of asking the server to install a bundle's host prerequisites.
 *  A refusal is a 200 with `installed: false` — the reason and the steps a
 *  person would run instead are both operator-facing text. */
export interface HostSetupResponse {
  installed: boolean
  detail: string
  manual_steps?: string
}

/** Sign-in state of a connector that holds its own credentials (no declared
 *  secrets — the host binary owns the token).
 *
 *  `sign_in` and `reachable` are two questions. `reachable` is "does this
 *  connection answer at all"; `sign_in` is "is this account connected". They
 *  used to be one field, taken from the probe, and a `dbxcli` that had never
 *  been signed in was drawn with a green tick reading "Signed in — dbxcli
 *  version: 3.7.1", because `dbxcli version` runs fine with no credential.
 *  `unknown` means the bundle declares no way to check: it must never be drawn
 *  as success, and never as a failure either. `command` is present when the
 *  login can only be completed by a person at a terminal. */
export type ConnectorSignIn = 'signed_in' | 'signed_out' | 'unknown'

export interface ConnectorAuthStatusResponse {
  sign_in: ConnectorSignIn
  reachable: boolean
  detail: string
  command?: string
}

/** An install refused for a missing host prerequisite returns 400 with
 *  `unsatisfied_host` alongside `error`; this reads it back off the decoded
 *  error body so the operator sees the instruction, not a generic failure. */
export function asUnsatisfiedHost(body?: Record<string, unknown>): HostRequirement[] {
  const raw = body?.unsatisfied_host
  return Array.isArray(raw) ? (raw as HostRequirement[]) : []
}
