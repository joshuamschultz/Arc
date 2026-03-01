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
  }

  get stats() { return this._stats; }
  get traces() { return this._traces; }
  get circuitBreakers() { return this._circuitBreakers; }
  get budgets() { return this._budgets; }
  get costEfficiency() { return this._costEfficiency; }
  get config() { return this._config; }
  get lastUpdate() { return this._lastUpdate; }

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
      this.addTraceEvent(evt);
    }
  }
}

window.ArcStore = new Store();
