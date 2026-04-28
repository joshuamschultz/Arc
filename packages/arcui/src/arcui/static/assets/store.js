/* ============================================================
   ArcUI — Central State Store (EventTarget-based)
   ============================================================ */

class Store extends EventTarget {
  constructor() {
    super();
    this._stats = { request_count: 0, total_tokens: 0, total_cost: 0, latency_avg: 0,
                    latency_p50: 0, latency_p95: 0, latency_p99: 0, latency_min: 0,
                    latency_max: 0, model_stats: {}, provider_counts: {}, agent_counts: {} };
    this._traces = [];
    this._circuitBreakers = [];
    this._budgets = [];
    this._costEfficiency = null;
    this._config = null;
    this._lastUpdate = 0;
    // Scheduler events (layer === "scheduler"). Bounded ring buffer of last 5.
    // Populated by handleEventBatch routing schedule:* events from the bus.
    this._scheduleEvents = [];
  }

  get stats() { return this._stats; }
  get traces() { return this._traces; }
  get circuitBreakers() { return this._circuitBreakers; }
  get budgets() { return this._budgets; }
  get costEfficiency() { return this._costEfficiency; }
  get config() { return this._config; }
  get lastUpdate() { return this._lastUpdate; }
  get scheduleEvents() { return this._scheduleEvents; }

  updateStats(data) {
    Object.assign(this._stats, data);
    this._lastUpdate = Date.now();
    this.dispatchEvent(new CustomEvent('stats', { detail: this._stats }));
  }

  setTraces(traces) {
    this._traces = traces;
    this.dispatchEvent(new CustomEvent('traces', { detail: traces }));
  }

  addTraceEvent(event) {
    this._traces.unshift(event);
    if (this._traces.length > 500) this._traces.length = 500;
    this._lastUpdate = Date.now();
    this.dispatchEvent(new CustomEvent('trace-event', { detail: event }));
  }

  setCircuitBreakers(cbs) {
    this._circuitBreakers = cbs;
    this.dispatchEvent(new CustomEvent('circuit-breakers', { detail: cbs }));
  }

  setBudgets(budgets) {
    this._budgets = budgets;
    this.dispatchEvent(new CustomEvent('budgets', { detail: budgets }));
  }

  setCostEfficiency(data) {
    this._costEfficiency = data;
    this.dispatchEvent(new CustomEvent('cost-efficiency', { detail: data }));
  }

  setConfig(config) {
    this._config = config;
    this.dispatchEvent(new CustomEvent('config', { detail: config }));
  }

  handleEventBatch(batch) {
    const events = batch.events || [];
    for (const evt of events) {
      if (!evt) continue;
      // Scheduler-layer events go to their own ring buffer.
      if (evt.layer === 'scheduler') {
        this.addScheduleEvent(evt);
        continue;
      }
      // LLM-layer events carry trace fields (model, tokens, cost) and feed
      // the Recent LLM Calls table. Other layers (agent, run) are lifecycle
      // events without trace fields — they'd render as empty rows, so drop
      // them from the trace stream. (Future: route them to a dedicated
      // activity feed if/when that view exists.)
      if (evt.layer === 'llm') {
        // Unpack event.data into the flat shape LogTable expects when the
        // event is a call_complete (the only LLM event that carries the
        // full trace payload).
        const data = evt.data || {};
        const traceRow = {
          ...data,
          agent_label: data.agent_label || evt.agent_name || data.agent || '',
          timestamp: data.timestamp || evt.timestamp,
        };
        this.addTraceEvent(traceRow);
      }
      // Other layers intentionally dropped from the trace table.
    }
  }

  addScheduleEvent(event) {
    this._scheduleEvents.unshift(event);
    if (this._scheduleEvents.length > 5) this._scheduleEvents.length = 5;
    this._lastUpdate = Date.now();
    this.dispatchEvent(new CustomEvent('schedule-event', { detail: event }));
  }
}

window.ArcStore = new Store();
