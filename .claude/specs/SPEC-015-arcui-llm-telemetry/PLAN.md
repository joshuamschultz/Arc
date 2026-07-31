# PLAN: ArcUI LLM Telemetry (SPEC-015)

**Status**: COMPLETE
**Phases**: 5
**Total Tasks**: 36
**Completed**: 36/36
**Remaining**: 0

---

## Phase 1: ArcLLM Core — TraceStore + on_event (8 tasks) ✅

_Foundation. Everything else depends on these. No arcUI code yet._

- [x] **1.1** Create `TraceRecord` Pydantic model in `arcllm/trace_store.py` — all fields from SDD §2.1 (trace_id, timestamp, provider, model, request_body, response_body, phase_timings, cost_usd, duration_ms, tokens, status, error, event_type, prev_hash, record_hash). Frozen model. Unit tests for serialization, hash computation with jcs.
- [x] **1.2** Create `TraceStore` Protocol in `arcllm/trace_store.py` — 5 methods: `append()`, `query()`, `get()`, `verify_chain()`, `close()`. Type stubs only.
- [x] **1.3** Implement `JSONLTraceStore` — append-only JSONL with SHA-256 hash chain using `jcs` for canonical JSON. Daily rotation with tombstone records. `query()` with cursor pagination, provider/agent/status filters. Unit tests: append, query, rotation, chain verification.
- [x] **1.4** Add `on_event` and `trace_store` params to `load_model()` in `registry.py` — thread through config to TelemetryModule. Unit test: on_event fires after invoke, trace_store receives records.
- [x] **1.5** Modify `TelemetryModule.invoke()` to build `TraceRecord` — capture request bodies (messages, tools, kwargs), response bodies (content, tool_calls, usage), compute phase_timings placeholders (llm_call_ms from existing timer). Call `trace_store.append()` and `on_event()`. Unit tests.
- [x] **1.6** Add phase sub-timing to `TelemetryModule` — instrument prompt_assembly_ms (time before inner.invoke), llm_call_ms (existing), post_processing_ms (time after response). Emit as OTel child spans. Unit tests for timing accuracy.
- [x] **1.7** Raw body storage tier config — read `store_raw_bodies` from config. Default per tier: federal=True, enterprise=True, personal=False. When False, TraceRecord.request_body and response_body are None. Unit tests.
- [x] **1.8** Integration test: `load_model()` with `trace_store` + `on_event` → make 3 calls → verify JSONL file has 3 records → `verify_chain()` passes → `query()` returns all 3.

**Phase 1 gate**: `pytest tests/unit/test_trace_store.py tests/integration/test_trace_store_integration.py` passes. `ruff check` clean. `mypy --strict` clean on new files.

---

## Phase 2: ArcLLM Additions — CircuitBreaker + ConfigController + Budget API (7 tasks) ✅

_Completes all ArcLLM changes before touching arcUI._

- [x] **2.1** Create `CircuitBreakerModule` in `arcllm/modules/circuit_breaker.py` — state machine (CLOSED/OPEN/HALF_OPEN), configurable failure_threshold, cooldown_seconds, half_open_max_calls. Wraps inner LLMProvider. `get_state()` returns queryable dict. Unit tests for all state transitions.
- [x] **2.2** Wire `CircuitBreakerModule` into module stack in `registry.py` — new `circuit_breaker` param on `load_model()`. Positioned between RetryModule and TelemetryModule. Integration test: simulate failures → circuit opens → calls rejected → cooldown → half-open → recovery.
- [x] **2.3** CircuitBreaker emits events — on state transition, emit TraceRecord with `event_type="circuit_change"`, `event_data={provider, old_state, new_state, consecutive_failures}`. Flows through on_event callback. Unit test.
- [x] **2.4** Create `ConfigController` in `arcllm/config_controller.py` — `get_snapshot()`, `patch(updates, actor)`, `on_change(callback)`. Immutable `ConfigSnapshot` Pydantic model (frozen). Atomic swap on patch. Unit tests: get, patch, on_change fires, invalid patch rejected.
- [x] **2.5** ConfigController emits audit events — every `patch()` creates TraceRecord with `event_type="config_change"`, `event_data={actor, changes: {key: {old, new}}}`. Written to TraceStore. Unit test.
- [x] **2.6** Budget state queryable — add `get_budget_state()` to TelemetryModule that returns BudgetAccumulator state dict: `{scope, monthly_spend, daily_spend, monthly_limit, daily_limit, enforcement, alert_threshold_pct}`. Unit test.
- [x] **2.7** Integration test: full ArcLLM stack with CircuitBreaker + ConfigController + TraceStore + on_event → make calls, trigger circuit break, patch config, verify all events in JSONL.

**Phase 2 gate**: All ArcLLM tests pass. `ruff check` + `mypy --strict` clean. No regressions in existing tests.

---

## Phase 3: ArcUI Server — Python Backend (10 tasks) ✅

_Starlette server, WebSocket, REST API. No frontend yet (test with curl/wscat)._

- [x] **3.1** Scaffold `packages/arcui/` — update `pyproject.toml` with dependencies (starlette, uvicorn, jcs). Create package structure: `arcui/{__init__, server, auth, connection, event_buffer, aggregator}.py`, `arcui/routes/{ws, traces, config, stats, export}.py`. Empty stubs.
- [x] **3.2** Implement `ConnectionManager` — per-client `asyncio.Queue(maxsize=1000)`, register/unregister clients, `broadcast()` with queue-full handling (drop oldest). Unit tests.
- [x] **3.3** Implement `EventBuffer` — bounded `deque(maxlen=1000)`, 100ms flush loop via `asyncio.sleep(0.1)`, batches events and calls `ConnectionManager.broadcast()`. Unit tests.
- [x] **3.4** Implement `AuthMiddleware` — bearer token from TOML config, auto-generate if blank, two roles (viewer/operator). Middleware sets `request.state.role`. Unit tests for token validation, role assignment, missing token handling per tier.
- [x] **3.5** Implement REST routes — `GET /api/traces` (query TraceStore), `GET /api/traces/{trace_id}` (get single), `GET /api/config` (ConfigController.get_snapshot), `PATCH /api/config` (operator only, ConfigController.patch), `GET /api/circuit-breakers`, `GET /api/budget`, `GET /api/stats`, `GET /api/export` (CSV/JSON). Integration tests with httpx AsyncClient.
- [x] **3.6** Implement WebSocket route — `/ws` endpoint with first-message auth (5s timeout), subscribe to EventBuffer stream, heartbeat ping/pong. Integration test with websockets library.
- [x] **3.7** Implement `RollingAggregator` — BucketedWindow (1h/24h/7d), ingest TraceRecords, simple sorted-sample percentiles, `warm_start()` from TraceStore. Unit tests for bucketing accuracy, percentile computation.
- [x] **3.8** Implement `serve()` and `attach_llm()` — `serve(llm=model, host="127.0.0.1", port=8420)` one-liner. `attach_llm(instance, label)` wires on_event → EventBuffer + RollingAggregator. Starlette app factory. Integration test.
- [x] **3.9** Integration test: start server → attach mock LLM → make LLM calls → verify WS receives events → verify REST endpoints return correct data → verify export works.
- [x] **3.10** Add `cost_efficiency()` to `RollingAggregator` and `GET /api/cost-efficiency` route (REQ-015) — queries aggregated trace data per model within time window, computes $/token using real split input/output pricing from ArcLLM provider configs. Returns per-model efficiency ranking, cheapest model, most-used model, potential savings (USD + %). Unit tests for calculation accuracy, route integration test.

**Phase 3 gate**: `pytest tests/` for arcui passes. Server starts, curl hits all endpoints. `ruff check` + `mypy --strict` clean.

---

## Phase 4: ArcUI Frontend — Dashboard (8 tasks) ✅

_Port demo CSS/JS, connect to real WebSocket/REST._

- [x] **4.1** Port `arc-platform.css` to `arcui/static/assets/` — adapt demo design system. Added connection status banner styles, stale-data overlay styles, cost efficiency table styles, savings alert, resume live button.
- [x] **4.2** Create `ws-client.js` — `RobustWebSocket` class with exponential backoff + jitter, heartbeat ping/pong, outbound message queue, first-message auth, 4 connection states.
- [x] **4.3** Create `store.js` + `dom-batcher.js` + `formatters.js` + `connection-ui.js` — central state store (EventTarget), rAF batching, Intl.NumberFormat formatters (compact tokens, currency costs, tiered latency), escapeHTML, connection status banner with stale timer.
- [x] **4.4** Create `log-table.js` — LogTable class with node cap (300 rows), auto-scroll detection, "Resume live" button, DocumentFragment batch inserts.
- [x] **4.5** Create `index.html` — Telemetry page with 3 pill-nav tabs (Overview, Traces, Cost). Overview: 4 stat cards, token volume chart bars, cost breakdown by provider, circuit breaker table. Traces: filter dropdowns, trace table, expandable trace detail (request/response + span timeline). Cost: per-agent bars, per-provider bars, budget gauges, model efficiency table, optimization alert.
- [x] **4.5a** Cost efficiency UI components (REQ-015) — model efficiency table (sortable by $/token), cheapest vs most-used model comparison card, potential savings alert banner (shown when >20% savings possible), savings calculation display.
- [x] **4.6** Adapt `arc-shell.js` — shell renderer for sidebar nav and topbar. Wire to real data from Store. Only telemetry page active (other nav items disabled/grayed).
- [x] **4.7** Integration tests: dashboard HTML serves correctly, static CSS/JS files accessible, all content verified. Static file mount wired into server.py.

**Phase 4 gate**: Dashboard serves, static assets load, all 63 tests pass. Ruff + mypy clean.

---

## Phase 5: ArcAgent Bridge + Final Integration (3 tasks) ✅

_Wire into ArcAgent. Final polish and verification._

- [x] **5.1** Create `create_arcllm_bridge()` in `arcagent/core/agent.py` — maps TraceRecords to ModuleBus events (`llm:call_complete`, `llm:config_change`, `llm:circuit_change`). Uses `loop.create_task()` with `_pending` set. Unit test.
- [x] **5.2** Integration test: ArcAgent with ArcLLM + ArcUI — agent loads model with arcllm bridge → makes LLM calls → arcUI dashboard shows traces → ModuleBus receives events.
- [x] **5.3** Final verification — run full test suite across all three packages. Verify: `ruff check`, `mypy --strict`, `pytest --cov` with >=80% coverage on new code. Verify `serve(llm=model)` end-to-end works.

**Phase 5 gate**: All tests green. Coverage >=80%. Lint + type check clean. `serve()` one-liner works.

---

## Implementation Order Rationale

```
Phase 1 (TraceStore + on_event)
   ↓ everything depends on TraceRecord
Phase 2 (CircuitBreaker + ConfigController)
   ↓ server needs these to expose APIs
Phase 3 (ArcUI server backend)
   ↓ frontend needs WebSocket + REST to connect to
Phase 4 (Frontend dashboard)
   ↓ bridge needs both sides working
Phase 5 (ArcAgent bridge + final)
```

Each phase is independently testable. Phase boundaries are approval gates.

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `jcs` library has edge cases with Pydantic serialization | Medium | Medium | Unit test canonical JSON against known test vectors |
| DDSketch dependency adds complexity | Low | Low | Fall back to simple min/max/avg if DDSketch unavailable |
| Demo CSS is 1200 LOC, hard to trim | Medium | Low | Port full CSS, trim during cleanup pass |
| TelemetryModule modification affects existing tests | Medium | High | Run full arcllm test suite after every change |
| WebSocket auth timing in tests | Medium | Low | Use deterministic test clock |
