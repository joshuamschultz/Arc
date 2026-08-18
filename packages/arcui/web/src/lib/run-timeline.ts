import type { TimelineEntry } from '@/lib/types'

// Shared run-timeline fold — used by the run drawer and the Run River. Lives in
// its own module so both can import it without a component file exporting a
// non-component (react-refresh).

const CODE_EXEC_TOOLS = new Set(['execute_python', 'execute'])

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
}
export interface LlmItem {
  kind: 'llm'
  ts?: string | null
  model: string
  tokensIn: number
  tokensOut: number
  latency_ms?: number | null
  traceId?: string | null
}
export interface RunItem {
  kind: 'run'
  ts?: string | null
  name: string
}
export type Item = ToolItem | LlmItem | RunItem

/**
 * Fold raw timeline rows into display items, pairing tool start/end by name.
 * `runIsLive` gates a still-open tool item's status: an unmatched `start` reads
 * "running" only while its own run is live, else "stale".
 */
export function mergeTimeline(entries: TimelineEntry[], runIsLive: boolean): Item[] {
  const items: Item[] = []
  const pending = new Map<string, ToolItem[]>()

  for (const e of entries) {
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
          })
        }
      }
    } else if (e.kind === 'llm_call') {
      items.push({
        kind: 'llm',
        ts: e.ts,
        model: e.model ?? '—',
        tokensIn: e.prompt_tokens ?? 0,
        tokensOut: e.completion_tokens ?? 0,
        latency_ms: e.latency_ms,
        traceId: e.record_id ?? null,
      })
    } else {
      items.push({ kind: 'run', ts: e.ts, name: e.name ?? 'event' })
    }
  }
  if (!runIsLive) {
    for (const item of items) {
      if (item.kind === 'tool' && item.status === 'running') item.status = 'stale'
    }
  }
  return items
}
