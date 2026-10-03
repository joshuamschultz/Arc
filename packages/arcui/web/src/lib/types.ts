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

/** One labeled, expandable section of an LLM call's assembled prompt (H-049).
 *  Built server-side (arcui.prompt_sections) from the persisted request, in the
 *  operator-legible order; `body` is the raw section text for {@link LlmContent}. */
export interface PromptSection {
  key: string
  label: string
  body: string
  tokens: number
}

export interface Trace {
  [key: string]: unknown
  trace_id?: string
  // The actor's DID (H-008 joins/filters on this, never agent_label).
  agent?: string
  agent_label?: string
  agent_did?: string
  // Canonical identity for `agent`, resolved server-side (H-007/H-029) —
  // render with the shared `AgentIdentity` component instead of the raw
  // `agent`/`agent_label` strings. Absent when the roster provider isn't wired.
  identity?: AgentIdentityShape
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
  // H-049: the request re-presented as ordered, labeled prompt sections
  // (system prompt · identity · strategies · policies · tool list · skill list
  // · context · session data, then any other real section). Server-built read
  // projection over what was actually sent; absent when the body wasn't stored
  // (metadata-only / federal-encrypted default).
  prompt_sections?: PromptSection[]
  // H-028/H-029: what the call WAS — "inference" (chat/completion) or
  // "embedding" — classified from the recorded call type, never the model name.
  capability_class?: 'inference' | 'embedding'
  // The embed path's short caller label (e.g. "embed:consolidate",
  // "retrieve:recall"); undefined for a chat/completion call.
  operation?: string | null
  // Sub-job kind (workpad|distill|consolidate|eval|background) parsed off the
  // agent_label suffix or background origin; null for a plain agent call.
  job?: string | null
}

/**
 * Canonical agent identity — the ONE shape every screen renders, resolved
 * server-side (arcui.identity, H-007) by parsing the DID and joining the
 * roster's friendly name **by DID**. `host`/`platform`/`type`/`short_id`
 * are always populated (`"unknown"` when the DID itself is malformed or
 * absent, `""` for a structurally valid role DID with no hash segment);
 * `name` is `null` when no roster row matched this DID.
 */
export interface AgentIdentityShape {
  did: string
  host: string
  platform: string
  type: string
  short_id: string
  name: string | null
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
  /** H-040: runtime kind — "arcagent" (native) or a foreign harness name. */
  harness?: string
  identity?: AgentIdentityShape
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
  blocked_by_total?: number
  tags?: string[]
  tags_total?: number
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

/** The causal chain an audit row was written under (arctrust.causal, item 20). */
export interface AuditCausal {
  initiator?: string
  initiator_id?: string
  on_behalf_of?: string | null
  run_id?: string | null
  tool_call_id?: string | null
  llm_call_id?: string | null
  workflow_run_id?: string | null
  node_id?: string | null
  task_id?: string | null
  connection_id?: string | null
}

/** Ledger-wide counts: every mirrored record, not the page (GET /api/team/audit). */
export interface AuditTotals {
  total: number
  verified: number
  broken: number
}

export interface AuditEvent extends AuditCausal {
  [key: string]: unknown
  // The whole causal chain, and the same fields flattened as filterable columns.
  causal?: AuditCausal | null
  // Fingerprint of the key that signed this row's chain link.
  signer?: string | null
  // The chain this row belongs to (e.g. "audit-chain-arcui" is the operator's).
  chain?: string
  timestamp?: string
  ts?: string
  event_type?: string
  action?: string
  // Plain-language sentence for `action`, resolved server-side (H-022) —
  // e.g. "policy.evaluate" -> "Tool policy check". Render this, not `action`.
  action_label?: string
  // The resolved friendly name for `actor_did` (roster join, H-007/H-022) —
  // `null`/absent when the DID has no roster row (operator, role DID, …).
  actor?: string | null
  agent_id?: string
  // The canonical {AgentIdentity} shape (arcui.identity, H-007), joined by
  // DID server-side. Render with the shared `AgentIdentity` component rather
  // than re-deriving a name/host/type from the raw `actor_did` in React.
  identity?: AgentIdentityShape
  actor_did?: string
  target?: string
  // "Kind: value" for a namespaced target (`connector:github` -> "Connector:
  // github"); the bare target unchanged when it has no namespace.
  target_label?: string
  outcome?: string
  // Plain verdict word for `outcome` (e.g. "deny" -> "Denied"), resolved
  // server-side (H-022).
  decision?: string
  // The policy pipeline's (or emitter's) stated reason, lifted out of
  // `extra` server-side (H-022) — present on most allow/deny rows.
  reason?: string
  severity?: string
  seq?: number | string
  event_hash?: string
  prev_hash?: string
  signature?: string
  verified?: boolean | number
  request_id?: string
  extra?: Record<string, unknown>
}

// --- HTTP response envelopes (schemas.py, 1:1) -----------------------------

export interface ErrorResponse {
  error: string
}

export interface AgentsListResponse {
  agents: Agent[]
}

// Mirrors GET /api/auth/me (SPEC-057 REQ-043). `did` is null for an
// anonymous static-token session (viewer/operator token, no signed-in
// account) — callers that need a DID to act as this caller must check it.
export interface AuthMeResponse {
  authenticated: boolean
  anonymous: boolean
  role: string | null
  email: string | null
  did: string | null
  display_name?: string
  handle?: string
  expires_at?: string
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
  totals?: AuditTotals
}

/** POST /api/team/audit/reverify — what the re-walk found. */
export interface AuditReverifyResponse {
  events: number
  verified: number
  broken: number
}

export interface SessionEntry {
  sid: string
  path: string
  size: number
  mtime: number
  // True for the caller's current (rotation-aware) conversation — the session
  // the live chat writes to. After a /new this is the rotated generation.
  current?: boolean
}

export interface SessionsListResponse {
  sessions: SessionEntry[]
  // The caller's current session key; the live chat writes here. Lets a list
  // surface highlight/open the live conversation instead of a stale rotation.
  current_session_key?: string | null
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
  next_cursor?: string | null
  facets?: TaskBoardFacets | null
  projections?: Record<string, TaskBoardProjection> | null
}

export interface TaskBoardFacets {
  statuses: Record<string, number>
  priorities: Record<string, number>
  owners: Record<string, number>
  tags: Record<string, number>
  owners_truncated?: boolean
  tags_truncated?: boolean
  total: number
  blocked: number
  done_today: number
  avg_done_seconds: number | null
}

export interface TaskBoardProjection {
  blocked: boolean
  dependencies: Record<string, { id: string; title?: string | null; status?: TaskStatus | null }>
  dependency_total: number
  dependency_details_truncated?: boolean
  children: Pick<Task, 'id' | 'title' | 'status'>[]
  child_total: number
  child_done: number
  child_details_truncated?: boolean
}

export interface SchedulesResponse {
  schedules: Dict[]
}

export interface SkillsResponse {
  skills: Dict[]
}

// The ONE authoritative policy verdict (H-010, arcagent.summarize_tool_policy).
// The Identity tab and the Tools tab both render their headline label from
// this — never from a locally re-derived allow.length check — so they cannot
// disagree about what an empty allowlist means.
export interface ToolPolicySummary {
  state: 'default-allow' | 'deny-all' | 'explicit'
  allow: string[]
  deny: string[]
  label: string
}

export interface ToolsResponse {
  tools: Dict[]
  allowlist: string[]
  denylist: string[]
  policy_summary: ToolPolicySummary
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
  mime: string
}

export interface FileWriteResponse {
  path: string
  size: number
  mtime: number
  signature_stale: boolean
  message: string
}

export interface FileDeleteResponse {
  path: string
  protected: string | null
  message: string
}

// --- Prompts (COMP-010: editable system prompts) ---------------------------

// `rejected`: an override is present but fails the agent's signature check — the
// agent refuses to run, so nothing is effective (`effective` is empty).
export type PromptStatus = 'stock' | 'overridden' | 'rejected'

export interface PromptListItem {
  package: string
  name: string
  description: string
  status: PromptStatus
  rejection_reason?: string | null
}

export interface PromptListResponse {
  items: PromptListItem[]
}

export interface PromptDetail {
  package: string
  name: string
  description: string
  status: PromptStatus
  rejection_reason?: string | null
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

export interface PromptVersion {
  version: number
  sha256: string
  signer_did: string
  signed_at: string | null
  /** True when this version's bytes are what is live on disk now. */
  current: boolean
}

export interface PromptHistoryResponse {
  package: string
  name: string
  /** Newest first. */
  versions: PromptVersion[]
}

export interface PromptHistoryDiffResponse {
  package: string
  name: string
  from_label: string
  to_label: string
  diff: string
}

export interface PromptRevertResponse {
  package: string
  name: string
  reverted_from: number
  new_version: number
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
  rejection_reason?: string | null
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
  aliases: string[] // earlier names this entity was merged from (recall still finds them)
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

// --- Knowledge chunks (H-023 — arcmemory.operator browse_chunks/search_chunks) --

export interface ChunkRecord {
  chunk_id: string
  source: string // source_path (file, event, or ingested document)
  scope: string
  classification: string
  mtime: number | null
  score: number // rank-derived — not comparable across bm25/vec/recency at the raw level
  text: string // capped to ~500 chars regardless of result count
  truncated: boolean
}

export interface ChunkPage {
  items: ChunkRecord[]
  total: number
  limit: number
  offset: number
}

export type ChunkSearchMode = 'literal' | 'vector'

export interface ChunkSearchResponse {
  items: ChunkRecord[]
  mode: ChunkSearchMode
  degraded: boolean
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

export interface GatewayDestination {
  name: string
  platform: string
  agent_did: string
}
export interface GatewaysResponse {
  gateways: GatewayDestination[]
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
  // spawn_event — a sub-agent this run spawned
  child_did?: string | null
  role?: string | null
  depth?: number | null
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
  origin?: string | null // "background" for a self-wake; null/absent for a person-driven run
  job?: string | null // sub-job kind (workpad|distill|consolidate|eval|background); null for a plain agent run
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

/** One step of a procedure, with how many sessions have corroborated it. */
export interface ProcedureStep {
  text: string
  hits: number
}

export interface ProcedureCard {
  slug: string
  title: string
  when_to_use: string
  steps: ProcedureStep[]
  use_count: number
  revisions: number
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
  sha256: string // revision the operator opened
  editable: boolean // true when the file lives in an editable workspace root
  // Original path retained for provenance; edits use the signed revision route.
  write_root: 'workspace' | 'agent' | null
  write_path: string | null // relative to write_root
}

// --- Skill version timeline + diff (H-042: the reviewable diff-merge surface) ------

export interface SkillEvalCase {
  nodeid: string
  provenance: 'machine' | 'human' | 'curated'
  gate_type: 'exact_match' | 'assertions' | 'judge_rubric'
}

export interface SkillEvalCasesResponse {
  items: SkillEvalCase[]
}

export interface SkillVersionItem {
  candidate_id: string
  generation: number | null
  parent_id: string | null
  scores: Record<string, number>
  active: boolean
  body_hash: string | null
  tombstone: boolean
  ts: string | null
}

export interface SkillVersionsResponse {
  items: SkillVersionItem[]
}

export interface SkillVersionDiffResponse {
  a: string
  b: string
  diff: string
}

export interface SkillRollbackResponse {
  status: string
  skill_name: string
  from_candidate_id: string | null
  to_candidate_id: string
  warning: string
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

export type WorkflowStatus = 'draft' | 'signed' | 'archived' | 'unreadable'

export type WorkflowHealth = 'ok' | 'unsigned' | 'needs_resign' | 'unreadable'
export type WorkflowNodeKind = 'agent' | 'tool' | 'script' | 'router' | 'gate'
export type WorkflowRunStatus =
  'pending' | 'running' | 'waiting_gate' | 'done' | 'done_with_failures' | 'failed' | 'cancelled'
// Per-node run status. `skipped` = an untaken branch, sourced from the Run's
// path taken — with lazy materialization there are no task rows for unreached
// nodes, so this status is NEVER read from a task row for it.
export type WorkflowNodeStatus =
  | 'pending'
  | 'running'
  | 'waiting_gate'
  | 'done'
  | 'failed'
  | 'skipped'
  | 'cancelled'
  | 'routed'
  | 'materialized'
  | 'in_progress'
  | 'review'

export interface WorkflowNode {
  [key: string]: unknown
  id: string
  kind: WorkflowNodeKind
  needs?: string[]
  on_failure?: 'fail_run' | 'continue' | 'skip_dependents'
  when?: string | null
  agent?: string | null
  /** Tool nodes only: `false` when the tool's repeat call duplicates its effect. */
  idempotent?: boolean
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

/** The owner agent's scheduler row for a workflow's trigger. */
export interface WorkflowSchedule {
  agent_id: string
  schedule_id: string
  enabled: boolean
  disabled_reason?: 'operator' | 'breaker' | 'archived' | 'unapproved' | null
  disabled_at?: string | null
  next_fire_at?: string | null
  last_fired_at?: string | null
  last_outcome?: 'ok' | 'error' | 'start_unavailable' | 'missed' | null
  last_error?: string | null
}

export interface WorkflowSummary {
  [key: string]: unknown
  id: string
  /** `null` when the workflow has no schedule row on its owner agent. */
  schedule?: WorkflowSchedule | null
  // Display name; may be null when the definition has none — fall back to `id`
  // wherever the workflow is titled.
  name: string | null
  version: number
  status: WorkflowStatus
  trigger?: Dict | null
  last_run?: WorkflowLastRun | null
  /** Parse + signature health. `unreadable` rows have no detail page. */
  health?: WorkflowHealth
  /** The problem in plain words, when `health` is not ok. */
  health_detail?: string
  /** The one-click repair the card offers; there is never a command to copy. */
  health_fix_action?: WorkflowFixAction
}

/** What a workflow's repair button does: migrate the file format, or ask for a signature. */
export type WorkflowFixAction = '' | 'migrate' | 'sign'

/** One workflow's migration preview or result (`POST /api/workflows/:id/migrate`). */
export interface WorkflowMigration {
  workflow_id: string
  action: 'unchanged' | 'would_rewrite' | 'rewritten' | 'would_resign' | 'resigned' | 'refused'
  files: string[]
  nodes: string[]
  reason: string
}

export interface WorkflowDetail extends WorkflowSummary {
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  /** The agent handle that owns the workflow; nodes without an `agent` run as it. */
  owner?: string | null
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
  // Joins the EXISTING `/api/runs/{run_id}/timeline` (observe_run.py) — a
  // node's OWN per-dispatch execution trace, distinct from the workflow
  // run_id itself. See routes/workflows.py's naming note.
  task_run_id?: string | null
  /** The row a gate is resolved by — present on a node that has one. */
  task_id?: string | null
  kind?: string | null
  owner_did?: string | null
  started_at?: string | null
  completed_at?: string | null
  last_error?: string | null
  /** The router's chosen route id, on a `routed` node. */
  route?: string | null
  /** Why a node was skipped or cancelled (e.g. "upstream X failed: ..."). */
  reason?: string | null
  attempts?: number | null
  max_attempts?: number | null
  /** Tool nodes only: `false` when repeating the tool duplicates its side effect. */
  idempotent?: boolean
  /** Bounded value, a `{truncated, size_bytes, preview}` marker, or `{withheld}`. */
  input?: unknown
  output?: unknown
}

export interface WorkflowRunDetail {
  [key: string]: unknown
  last_error?: string | null
  run_id: string
  workflow_id: string
  version: number
  status: WorkflowRunStatus
  path_taken: string[]
  nodes: WorkflowRunNodeStatus[]
  started_at?: string | null
  ended_at?: string | null
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
  /** False when the bundle declares the field optional (`required = false`), so
   *  it may be left blank. Absent means required. */
  required?: boolean
  /** The allowed values; empty or absent means free text. */
  choices?: string[]
  /** The value used when the field is left blank. */
  default?: string
  /** What leaving this field blank costs. On the catalog it is the field's
   *  blank-warning text; on a connection's auth read it is non-empty only
   *  when the stored value IS blank. */
  warning?: string
  /** True for the OAuth refresh token: Connect fills it in, so the form never asks for it. */
  managed?: boolean
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
  knowledge_mode: 'source' | 'non_indexable'
  knowledge_reason: string
  secrets: ConnectorSecret[]
  host_requires: HostRequirement[]
  tools: ConnectorTool[]
  root: string
  /** False when Arc can never place this bundle's host program itself, so no Install
   *  button is offered and the page says so plainly. */
  auto_installable: boolean
  /** The sign-in app a one-click connect uses; empty unless the bundle is OAuth. */
  oauth_provider: string
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
  knowledge_mode: '' | 'source' | 'non_indexable'
  knowledge_reason: string
  approval: string
  agents: string[]
  status: ConnectionStatus
  /** `syncing` is derived server side from live sync leases. */
  display_status: ConnectionDisplayStatus
  reason_code: string | null
  reason_text: string | null
  action: ConnectionAction
  action_label: string
  last_checked_at: string | null
  last_success_at: string | null
  last_notice: ConnectionNotice | null
  connect_kind: ConnectionConnectKind
  /** The OAuth provider behind a `connect_kind` of `oauth`; empty otherwise. */
  oauth_provider: string
  /** A one-click connection whose sign-in app is not set up: the card's action opens
   *  that form first. */
  app_missing: boolean
  knowledge_sync: ConnectionKnowledgeSync[]
}

export type ConnectionStatus = 'unknown' | 'healthy' | 'needs_you' | 'error'
export type ConnectionDisplayStatus = ConnectionStatus | 'syncing'
export type ConnectionAction = 'none' | 'reconnect' | 'approve' | 'install_host' | 'wait'
export type ConnectionConnectKind = 'oauth' | 'token' | 'host_login' | 'none'

export interface ConnectionNotice {
  kind: 'needs_you' | 'error' | 'recovered'
  delivered: boolean
  channel: string
  at: string
}

export interface ConnectionKnowledgeSync {
  agent: string
  source_id: string
  state: string
  running: boolean
  last_synced_at: string | null
  pages: number
  error_code: string | null
}

export interface ConnectorActivation {
  agent: string
  status: 'applied' | 'activation_pending'
  revision: number
  tools: string[]
  detail: string
}

export interface ConnectorMutationResponse extends ConnectorInstance {
  activations: ConnectorActivation[]
}

/** Every connection this deployment has, and who holds each one — the answer to
 *  "who can reach what" for the whole fleet in a single read. */
export interface ConnectionsResponse {
  connections: ConnectorInstance[]
  extensions_roots: string[]
}

/** One connected account on the per-agent panel, plus its sync health.
 *  `needs_attention` (SPEC-082 COMP-008) is true when THIS agent's
 *  connected-data sync backed the source off after a terminal credential
 *  failure — a revoked/expired token no retry can clear, waiting on a human.
 *  Absent (missing/false) reads as healthy, never as an error. */
export interface AgentConnectorInstance extends ConnectorInstance {
  needs_attention?: boolean
}

/** What one agent can reach: the same rows, filtered to its grants.
 *  `mcp_door_enabled` (SPEC-082 COMP-008) is whether this agent's MCP server
 *  door is open. Default OFF and fails closed to false when unreadable. */
export interface AgentConnectorsResponse {
  instances: AgentConnectorInstance[]
  extensions_roots: string[]
  mcp_door_enabled?: boolean
}

export interface ConnectorInstallResponse {
  instance: string
  extension: string
  tools: string[]
  detail: string
  agents: string[]
}

/** One tool an MCP server advertised. The description is the server's own, untrusted text. */
export interface McpToolView {
  name: string
  description: string
  usable: boolean
  reason: string
}

/** What an MCP server offers (`POST /api/mcp-servers/preview`). Nothing was written. */
export interface McpPreviewResponse {
  tools: McpToolView[]
  suggested_tags: string[]
}

/** The operator's choice for one tool they are exposing. */
export interface McpToolChoice {
  classification: 'read_only' | 'state_modifying'
  capability_tags: string[]
  description?: string
}

/** How to reach an MCP server. Secrets are values typed here, never echoed back. */
export interface McpServerForm {
  name: string
  display?: string
  description?: string
  transport: 'http' | 'stdio'
  url?: string
  auth_header?: string
  auth_scheme?: string
  argv?: string[]
  env_refs?: Record<string, string>
  secrets: Record<string, string>
}

/** `POST /api/mcp-servers` result: names and a digest, never a credential. */
export interface McpServerAddedResponse {
  instance: string
  extension: string
  tools: string[]
  detail: string
  agents: string[]
  spec_sha256: string
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
  /** True when this connector is finished by an OAuth code exchange. */
  oauth?: boolean
  /** The provider consent URL to open (empty until the app key/secret are supplied). */
  authorize_url?: string
  /** Whether the account is connected right now, from the server's sign-in check. */
  sign_in?: ConnectorSignIn
  /** The host programs this connection runs through, and how each signs in. */
  hosts?: ConnectorHostAuthorization[]
}

/** One host program behind a connection. */
export interface ConnectorHostAuthorization {
  name: string
}

/** Start of a one-click OAuth connect. `redirect_mode` `none` means the provider
 *  shows a code on its own page instead of redirecting back to Arc. */
export interface OAuthBeginResponse {
  authorize_url: string
  state: string
  redirect_mode: 'callback' | 'none'
  expires_in: number
}

/** Finish an OAuth connect: the address the browser landed on, or the state and
 *  the code a provider with no redirect displays. */
export type OAuthCompleteBody = { redirect_url: string } | { state: string; code: string }

/** Whether Arc has an OAuth app (client id and secret) for a provider. The secret
 *  is never returned; `client_id_hint` is a masked id for recognition only. */
export interface OAuthCloudChoice {
  id: string
  label: string
}

export interface OAuthAppResponse {
  provider: string
  configured: boolean
  client_id_hint: string
  redirect_uri: string
  console_url: string
  /** The app needs its directory (tenant) ID (Microsoft Entra ID). */
  tenant_required?: boolean
  tenant_id?: string
  cloud?: string
  /** Clouds the bundle declares, default first; empty for a provider without clouds. */
  clouds?: OAuthCloudChoice[]
}

export interface OAuthAppBody {
  client_id: string
  client_secret: string
  tenant_id?: string
  cloud?: string
}

export interface ConnectorAuthResponse {
  instance: string
  updated: string[]
}

export interface ConnectorProbeResponse {
  reachable: boolean
  detail: string
  tools: ConnectorTool[]
  status: ConnectionStatus
  display_status: ConnectionDisplayStatus
  reason_code: string | null
  reason_text: string | null
  action: ConnectionAction
  action_label: string
  last_checked_at: string | null
  last_success_at: string | null
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
  /** What to offer next as a code, never prose naming a command. `restart_arc` means
   *  the program is placed but only a restarted Arc can see it; empty asks nothing. */
  action?: string
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
 *  as success, and never as a failure either. `expired` is a sign-in the
 *  provider stopped accepting; `not_installed` means the host program itself is
 *  missing. `command` is present when the
 *  login can only be completed by a person at a terminal. */
export type ConnectorSignIn = 'signed_in' | 'signed_out' | 'expired' | 'not_installed' | 'unknown'

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

// --- Connections data views (SPEC-073) --------------------------------------
//
// Read-only projections of an agent's connected data sources, surfaced as an
// offshoot of the Knowledge view. Sources, blob folders, and datastore tables
// are all `EntityRecord`s in the semantic store (typed by `entity_type`), so
// those responses reuse that existing shape rather than inventing a new one.

/** The operator-approved routing of a source to one-or-more homes
 *  (mirror of `arcmemory.types.SourceMapping`). */
export interface SourceMappingItem {
  source_id: string
  homes: string[]
}

export interface SourcesResponse {
  items: EntityRecord[]
}

/** One account/source known to the live connected-data coordinator.  This is
 * deliberately operational metadata only: it contains neither credentials nor
 * provider cursors/content. */
export interface ConnectedSourceItem {
  connection_id: string
  source_id: string
  source_kind: string
  label: string
  status: string
  detail: string
  pages: number
  bytes_processed: number
  error_code: string | null
  last_synced_at: string | null
  documents_indexed: number
  allowed_homes: string[]
  /** Which store the agent reads it from: its own copy, its own copy waiting to
   *  move into the shared store on the next sync, or the shared store. */
  lane: 'own' | 'migrating' | 'shared'
}

export interface ConnectedSourcesResponse {
  items: ConnectedSourceItem[]
  status?: string
}

/** One connection in the shared-store move preview (connections sweep J-K3). */
export interface SharedMigrationItem {
  connection_id: string
  status: string
  detail: string
  documents: number
  adopted: number
  deduplicated: number
  skipped: number
}

/** What the automatic move into the shared stores would do; changes nothing. */
export interface SharedMigrationPreview {
  dry_run: true
  items: SharedMigrationItem[]
}

/** Result of enabling the optional connected-data module for an existing agent. */
export interface ConnectedDataActivationResponse {
  status: 'activated'
  detail: string
}

/** A mapping awaiting the normal signed operator-approval flow. */
export interface MappingProposalItem {
  source_id: string
  homes: string[]
  status: 'not_staged' | 'pending' | 'approved' | 'denied' | 'expired'
  approval_id: string | null
  detail: string
  allowed_homes: string[]
}

export interface MappingProposalResponse {
  item: MappingProposalItem | null
}

export interface MappingStageResponse {
  item: MappingProposalItem
}

/** A selectable container inside a connection (for example Dropbox folder or
 * an email mailbox/label).  It is metadata only, never document content. */
export interface ConnectedResourceItem {
  resource_id: string
  label: string
  resource_kind: string
  selected: boolean
  detail: string
}

export interface ConnectedResourcesResponse {
  items: ConnectedResourceItem[]
}

/** A proposed profile fact. It remains unavailable to agent profile context
 * until an operator approves it. */
export interface ProfileReviewItem {
  fact_id: string
  profile_id: string
  field: string
  value: string
  kind: string
  status: string
  classification: string
  source_id: string
  external_id: string
  replaces_fact_id: string | null
}

export interface ProfileReviewsResponse {
  items: ProfileReviewItem[]
}

export interface ConnectedSyncStatus {
  connection_id: string
  status: string
  detail: string
  pages: number
  bytes_processed: number
  error_code: string | null
}

export interface ConnectedSyncStatusResponse {
  items: ConnectedSyncStatus[]
  status?: string
}

export interface MappingsResponse {
  items: SourceMappingItem[]
}

export interface MappingResponse {
  item: SourceMappingItem | null
}

export interface BlobFoldersResponse {
  items: EntityRecord[]
}

export interface DatastoreTablesResponse {
  items: EntityRecord[]
}

/** One document-search result: chunk text + a pointer back to the original
 *  object, never file bytes (mirror of `arcmemory.doc_index.DocHit`). */
export interface DocHitItem {
  chunk_id: string
  text: string
  pointer: string
  source_id: string
  score: number
  classification: string
  provenance: string[]
}

export interface DocumentsResponse {
  items: DocHitItem[]
}

/** One line of a verified collection index: a document or a child folder
 *  (mirror of `arcmemory.operator.CollectionIndexEntry`). `path` is
 *  source-relative; a folder's `path` is the `folder` argument that opens it. */
export interface CollectionIndexEntry {
  kind: 'document' | 'folder'
  path: string
  title: string
  summary: string
  classification: string
  digest: string
  /** Recursive document count (folders only). */
  count: number
}

/** A connected document source's verified OKF `index.md` — what's inside +
 *  purpose (mirror of `arcmemory.operator.CollectionIndexView`). Fail-closed:
 *  `markdown`/`entries` are populated ONLY when `verified` is true. */
export interface CollectionIndexView {
  source_id: string
  /** Source-relative folder this view lists (`''` is the source root). */
  folder: string
  present: boolean
  verified: boolean
  document_count: number
  entries: CollectionIndexEntry[]
  markdown: string
  error: string | null
  /** Operator-actionable recovery instruction, set when `verified` is false. */
  guidance: string | null
}

/** A live datastore read. `result` is the raw connector payload (a row, a list
 *  of rows, or null) — shape is source-defined, so it stays `unknown`. */
export interface DatastoreQueryResponse {
  result: unknown
}

/** One source's claim on a canonical item (mirror of `arcmemory.types.Provenance`). */
export interface ProvenanceItem {
  source: string
  external_id: string
  classification: string
}

export interface ProvenanceResponse {
  items: ProvenanceItem[]
}

/** Per-workspace index coverage — chunks indexed vs actually embedded
 *  (mirror of `arcmemory.status.WorkspaceVectors`). */
export interface WorkspaceVectorsItem {
  workspace: string
  indexed_chunks: number
  embedded_chunks: number
  insight_triggers: number
}

/** The honest semantic-channel probe (mirror of `arcmemory.status.SemanticStatus`). */
export interface IndexHealthItem {
  live: boolean
  vec_extension: boolean
  embedder_backend: string
  embedder_live: boolean
  embedder_dims: number | null
  detail: string
  degraded_reasons: string[]
  workspaces: WorkspaceVectorsItem[]
}

export interface IndexHealthResponse {
  item: IndexHealthItem
}

// --- H-027: fleet-shared knowledge (documents agents promoted into the
// signed fleet collection) ---------------------------------------------------

/** One search hit against the shared fleet collection. */
export interface SharedKnowledgeSearchHit {
  identifier: string
  title: string
  excerpt: string
}

export interface SharedKnowledgeSearchResponse {
  hits: SharedKnowledgeSearchHit[]
}

/** Why a saved credential key could not be placed (names only; never a value). */
export type CustodyReason =
  | 'undeclared'
  | 'custody_differs'
  | 'app_slot_differs'
  | 'app_pair_incomplete'
  | 'app_values_disagree'

/** `GET /api/custody`: what waits for the operator's answer. */
export interface CustodyStatus {
  state: 'ok' | 'needs_review' | 'blocked'
  /** Set when `state` is `blocked`: why the review could not even start. */
  code?: string
  keys: { key: string; reason: CustodyReason; kept: boolean }[]
  targets: { connection: string; field: string }[]
  affected_connections: string[]
}

/** One answer for one key. A drop carries the key typed back as confirmation. */
export type CustodyDecision =
  | { key: string; action: 'map'; connection: string; field: string }
  | { key: string; action: 'keep' }
  | { key: string; action: 'drop'; confirm: string }
