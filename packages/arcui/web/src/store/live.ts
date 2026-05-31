import { create } from 'zustand'
import type { AggregateStats, Trace, UIEvent, Dict } from '@/lib/types'

const TRACE_CAP = 500
const RUN_CAP = 500
const SCHEDULE_CAP = 5

interface LiveState {
  stats: AggregateStats
  /** LLM-layer call rows (ArcLLM). Newest first, capped. */
  traces: Trace[]
  /** Run-layer events (ArcRun). Newest first, capped. */
  runEvents: UIEvent[]
  /** Scheduler-layer events. Newest first, capped. */
  scheduleEvents: UIEvent[]
  lastUpdate: number

  setStats: (stats: AggregateStats) => void
  setTraces: (traces: Trace[]) => void
  addTrace: (trace: Trace) => void
  addRunEvent: (event: UIEvent) => void
  addScheduleEvent: (event: UIEvent) => void
  /** Route a `{events:[...]}` batch (or single wrapped event) by layer. */
  handleEventBatch: (batch: { events?: UIEvent[] }) => void
}

const EMPTY_STATS: AggregateStats = {
  request_count: 0,
  total_tokens: 0,
  total_cost: 0,
  latency_avg: 0,
  model_stats: {},
  provider_counts: {},
  agent_counts: {},
}

/**
 * Live telemetry pushed over `/ws`. Snapshots (initial + paginated) come from
 * react-query against REST; this store layers the live stream on top. The
 * `llm` / `run` split mirrors the ArcLLM / ArcRun sections.
 */
export const useLiveStore = create<LiveState>((set) => ({
  stats: EMPTY_STATS,
  traces: [],
  runEvents: [],
  scheduleEvents: [],
  lastUpdate: 0,

  setStats: (stats) => set({ stats, lastUpdate: Date.now() }),
  setTraces: (traces) => set({ traces }),

  addTrace: (trace) =>
    set((s) => ({
      traces: [trace, ...s.traces].slice(0, TRACE_CAP),
      lastUpdate: Date.now(),
    })),

  addRunEvent: (event) =>
    set((s) => ({
      runEvents: [event, ...s.runEvents].slice(0, RUN_CAP),
      lastUpdate: Date.now(),
    })),

  addScheduleEvent: (event) =>
    set((s) => ({
      scheduleEvents: [event, ...s.scheduleEvents].slice(0, SCHEDULE_CAP),
      lastUpdate: Date.now(),
    })),

  handleEventBatch: ({ events }) => {
    if (!events?.length) return
    for (const evt of events) {
      if (!evt) continue
      if (evt.layer === 'scheduler') {
        useLiveStore.getState().addScheduleEvent(evt)
      } else if (evt.layer === 'run') {
        useLiveStore.getState().addRunEvent(evt)
      } else if (evt.layer === 'llm') {
        const data = (evt.data || {}) as Dict
        const row: Trace = {
          ...data,
          agent_label:
            (data.agent_label as string) || evt.agent_name || (data.agent as string) || '',
          timestamp: (data.timestamp as string) || evt.timestamp,
        }
        useLiveStore.getState().addTrace(row)
      }
      // `agent` / `team` lifecycle events carry no trace fields — dropped.
    }
  },
}))
