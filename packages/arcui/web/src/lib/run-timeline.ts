import type { TimelineEntry } from '@/lib/types'

// Shared run-timeline fold — used by the run drawer and the Run River. Lives in
// its own module so both can import it without a component file exporting a
// non-component (react-refresh).

const CODE_EXEC_TOOLS = new Set(['execute_python', 'execute'])

// An embedder call (memory retrieval) is NOT the reasoning model — it turns text
// into vectors to search memory. It rides the same llm_call telemetry as a
// reasoning turn, so the trace mislabelled it "the agent thought about what to do
// next". An embedder never emits completion tokens AND its model name is one of
// the well-known embedding families, so the pair is an unambiguous tell.
const EMBED_MODEL_RX = /(embed|minilm|bge|gte|e5|nomic)/i

function isEmbeddingCall(item: LlmItem): boolean {
  return item.tokensOut === 0 && EMBED_MODEL_RX.test(item.model)
}

/** snake/dot_case → Title Case, for names we have no friendly label for. */
export function prettifyName(name: string): string {
  return name
    .replace(/[._]+/g, ' ')
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

// Plain-language descriptions so a non-technical viewer can read a trace. Keyed
// by the raw tool / run-event name; anything unmapped falls back to prettifyName.
const TOOL_DESCRIPTIONS: Record<string, string> = {
  search_similar_entity: 'Searched memory for related entities',
  list_procedures: 'Listed saved procedures',
  read_card: 'Read a memory card',
  write_card: 'Saved a memory card',
  recall: 'Recalled from memory',
  remember: 'Saved to memory',
  memory_search: 'Searched memory',
  read: 'Read a file',
  write: 'Wrote a file',
  edit: 'Edited a file',
  bash: 'Ran a shell command',
  execute_python: 'Ran Python code',
  execute: 'Ran code',
  web_search: 'Searched the web',
  web_extract: 'Read a web page',
  send_message: 'Sent a message',
  notify_user: 'Notified the operator',
}

const EVENT_DESCRIPTIONS: Record<string, string> = {
  'run.start': 'Run started',
  'run.end': 'Run ended',
  'turn.start': 'Turn started',
  'turn.end': 'Turn ended',
  'strategy.selected': 'Chose a strategy',
  'strategy.select': 'Chose a strategy',
  'strategy.selection.complete': 'Chose a strategy',
}

/** A human title + one-line description for a trace step, for the operator who
 *  does not know the tool names. */
export function describeAction(item: Item): { title: string; description: string } {
  if (item.kind === 'llm') {
    if (isEmbeddingCall(item)) {
      return {
        title: item.model,
        description: 'Memory lookup — turned text into vectors to search memory (not a reasoning step)',
      }
    }
    return { title: item.model, description: 'Model call — the agent thought about what to do next' }
  }
  if (item.kind === 'context') {
    return { title: 'Context prep', description: 'What the agent gathered before its first model turn' }
  }
  if (item.kind === 'run') {
    const key = item.name.toLowerCase()
    if (key === 'run.not_started') {
      const reason = typeof item.extra?.reason === 'string' ? item.extra.reason : null
      return { title: 'Run did not start', description: reason ?? 'The run ended before the model loop' }
    }
    if (key === 'strategy.selected' || key === 'strategy.select') {
      const strategy = typeof item.extra?.strategy === 'string' ? item.extra.strategy : null
      if (strategy) {
        return { title: prettifyName(item.name), description: `Chose the "${strategy}" strategy` }
      }
    }
    if (key === 'strategy.selection.complete') {
      const selected = typeof item.extra?.selected === 'string' ? item.extra.selected : null
      if (selected) return { title: prettifyName(item.name), description: `Selected: ${selected}` }
    }
    return { title: prettifyName(item.name), description: EVENT_DESCRIPTIONS[key] ?? 'Run event' }
  }
  if (item.kind === 'spawn') {
    const tail = (item.childDid.split('/').pop() ?? item.childDid).slice(0, 8)
    const delegated = item.childDid.includes('delegate')
    return {
      title: `${delegated ? 'Delegated to' : 'Spawned'} sub-agent ${tail}`,
      description: `A child agent this run started${item.outcome ? ` · ${item.outcome}` : ''}`,
    }
  }
  const key = item.name.toLowerCase()
  const base = TOOL_DESCRIPTIONS[key] ?? `Ran the ${item.name} tool`
  const via = item.activatedSkill ? ` · used skill "${item.activatedSkill}"` : ''
  return { title: prettifyName(item.name), description: base + via }
}

// A tool call pairs its start (carrying input) with its end/error (carrying
// output). LLM and run markers pass through as their own items.
export interface ToolItem {
  kind: 'tool'
  ts?: string | null
  name: string
  isCode: boolean
  input: unknown
  output: unknown
  status: string
  latency_ms?: number | null
  implicit?: boolean
  activatedSkill?: string | null
  skillActivated?: boolean
  requestId?: string | null
}
export interface LlmItem {
  kind: 'llm'
  ts?: string | null
  model: string
  tokensIn: number
  tokensOut: number
  latency_ms?: number | null
  traceId?: string | null
  requestId?: string | null
  agentLabel?: string | null
  costUsd?: number | null
  provider?: string | null
  cacheReadTokens?: number | null
  cacheWriteTokens?: number | null
}
export interface RunItem {
  kind: 'run'
  ts?: string | null
  name: string
  // Small scalar detail the backend carries on a lifecycle run_event — e.g.
  // `strategy` on `strategy.selected`, so the trace can say WHICH strategy.
  extra?: Record<string, unknown> | null
  // Terminal outcome on a run_event — `failed` on `run.not_started`.
  outcome?: string | null
}

/** One-line cache summary for a model call: "cache read 900 · write 0 · hit 90%".
 *  Anthropic's prompt_tokens EXCLUDE cached tokens, so the prompt total is
 *  tokensIn + read + write; OpenAI-style providers INCLUDE them, so the total is
 *  tokensIn. Null cache fields read as "-". */
export function cacheSummary(item: LlmItem): string {
  const { cacheReadTokens: read, cacheWriteTokens: write } = item
  if (read == null && write == null) return 'cache -'
  const r = read ?? 0
  const w = write ?? 0
  const total = item.provider === 'anthropic' ? item.tokensIn + r + w : item.tokensIn
  const hit = total > 0 ? `${Math.min(100, Math.round((r / total) * 100))}%` : '-'
  return `cache read ${r} · write ${w} · hit ${hit}`
}

export interface RetrievalCandidate {
  source_kind: string
  source: string
  title: string
  path?: string
  score: number
  classification: string
  snippet: string
  included: boolean
  reason: string
  tokens?: number
}
export interface RetrievalStep {
  name: string
  latency_ms: number
  status: string
  found?: number
}
export interface PrefixTier {
  sha256: string
  tokens: number
}
export interface RetrievalPrep {
  status: string
  reason?: string
  query: string
  latency_ms: number
  budget_ms: number
  top_k: number
  token_cap: number
  tokens_injected: number
  steps: RetrievalStep[]
  items: RetrievalCandidate[]
}
// Everything the harness did before the first model turn, folded from the
// `strategy.selected` + `context.*` run_events into one step group.
export interface ContextPrepItem {
  kind: 'context'
  ts?: string | null
  strategy?: {
    strategy: string
    reason: string
    latency_ms: number
    selected_by?: 'only' | 'model' | 'fallback'
  } | null
  system?: {
    cached: boolean
    tokens: number
    sha256: string
    tiers?: { session: PrefixTier; run: PrefixTier }
  } | null
  retrieval?: RetrievalPrep | null
  session?: { turns: number; tokens: number } | null
  skipped: { step: string; reason: string }[]
}
export interface SpawnItem {
  kind: 'spawn'
  ts?: string | null
  childDid: string
  role?: string | null
  outcome?: string | null
}
export type Item = ToolItem | LlmItem | RunItem | SpawnItem | ContextPrepItem

const isRunEvent = (e: TimelineEntry, name: string) => e.kind === 'run_event' && e.name === name
const isContextRow = (e: TimelineEntry) =>
  e.kind === 'run_event' && typeof e.name === 'string' && e.name.startsWith('context.')

function hasContextPrep(entries: TimelineEntry[]): boolean {
  return entries.some(isContextRow)
}

function newContextPrep(): ContextPrepItem {
  return { kind: 'context', strategy: null, system: null, retrieval: null, session: null, skipped: [] }
}

/** Move one run_event into the prep group. Only the first `strategy.selected`
 *  joins it — later strategy picks stay as their own steps. */
function absorbIntoContextPrep(prep: ContextPrepItem, e: TimelineEntry): boolean {
  const extra = (e.extra ?? {}) as Record<string, unknown>
  if (isRunEvent(e, 'strategy.selected') && !prep.strategy) {
    prep.strategy = extra as unknown as ContextPrepItem['strategy']
    return true
  }
  if (!isContextRow(e)) return false
  if (e.name === 'context.system') prep.system = extra as unknown as ContextPrepItem['system']
  else if (e.name === 'context.retrieval') prep.retrieval = extra as unknown as RetrievalPrep
  else if (e.name === 'context.session') prep.session = extra as unknown as ContextPrepItem['session']
  else if (e.name === 'context.skipped') {
    prep.skipped.push({ step: String(extra.step ?? ''), reason: String(extra.reason ?? '') })
  }
  return true
}

/**
 * Fold raw timeline rows into display items, pairing tool start/end by name.
 * `runIsLive` gates a still-open tool item's status: an unmatched `start` reads
 * "running" only while its own run is live, else "stale".
 */
export function mergeTimeline(entries: TimelineEntry[], runIsLive: boolean): Item[] {
  const items: Item[] = []
  const pending = new Map<string, ToolItem[]>()
  const prep = hasContextPrep(entries) ? newContextPrep() : null
  let prepPlaced = false

  for (const e of entries) {
    if (prep && absorbIntoContextPrep(prep, e)) {
      if (!prepPlaced) {
        prep.ts = e.ts
        items.push(prep)
        prepPlaced = true
      }
      continue
    }
    if (e.kind === 'tool_event') {
      const name = e.tool_name ?? '—'
      if (e.phase === 'start') {
        const item: ToolItem = {
          kind: 'tool',
          ts: e.ts,
          name,
          isCode: CODE_EXEC_TOOLS.has(name),
          input: e.extra?.args ?? null,
          output: null,
          status: 'running',
          implicit: e.extra?.implicit === true,
          requestId: e.request_id,
        }
        items.push(item)
        const q = pending.get(name) ?? []
        q.push(item)
        pending.set(name, q)
      } else {
        const q = pending.get(name)
        const target = q?.shift()
        const out = e.extra?.result ?? null
        const status = e.outcome === 'error' || e.phase === 'error' ? 'error' : 'ok'
        const activatedSkill = (e.extra?.activated_skill as string | undefined) ?? null
        const skillActivated = e.extra?.skill_activated as boolean | undefined
        if (target) {
          target.output = out
          target.status = status
          target.latency_ms = e.latency_ms
          target.activatedSkill = activatedSkill
          target.skillActivated = skillActivated
        } else {
          items.push({
            kind: 'tool',
            ts: e.ts,
            name,
            isCode: CODE_EXEC_TOOLS.has(name),
            input: null,
            output: out,
            status,
            latency_ms: e.latency_ms,
            implicit: e.extra?.implicit === true,
            activatedSkill,
            skillActivated,
            requestId: e.request_id,
          })
        }
      }
    } else if (e.kind === 'spawn_event') {
      items.push({
        kind: 'spawn',
        ts: e.ts,
        childDid: e.child_did ?? '—',
        role: e.role,
        outcome: e.outcome,
      })
    } else if (e.kind === 'llm_call') {
      items.push({
        kind: 'llm',
        ts: e.ts,
        model: e.model ?? '—',
        tokensIn: e.prompt_tokens ?? 0,
        tokensOut: e.completion_tokens ?? 0,
        latency_ms: e.latency_ms,
        traceId: e.record_id ?? null,
        requestId: e.request_id,
        agentLabel: e.agent_label,
        costUsd: e.cost_usd,
        provider: e.provider ?? null,
        cacheReadTokens: e.cache_read_tokens ?? null,
        cacheWriteTokens: e.cache_write_tokens ?? null,
      })
    } else {
      items.push({
        kind: 'run',
        ts: e.ts,
        name: e.name ?? 'event',
        extra: e.extra ?? null,
        outcome: e.outcome ?? null,
      })
    }
  }
  if (!runIsLive) {
    for (const item of items) {
      if (item.kind === 'tool' && item.status === 'running') item.status = 'stale'
    }
  }
  return items
}
