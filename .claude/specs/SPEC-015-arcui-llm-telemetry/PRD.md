# PRD: ArcUI LLM Telemetry (SPEC-015)

## 1. Problem Statement

ArcLLM calls are invisible. Users of the ArcMAS stack (ArcLLM + ArcRun + ArcAgent) have no way to:
- See what LLM calls are happening in real-time
- Inspect request/response bodies for debugging
- Monitor costs per provider and per agent
- Detect provider health issues (timeouts, rate limits)
- View or change LLM configuration at runtime
- Audit LLM call history for compliance

The demo UI proves the concept works. This spec makes it real.

## 2. Users & Personas

| Persona | Role | Primary Use |
|---------|------|------------|
| **Developer** | Building with ArcLLM | Debug LLM calls, inspect traces, tune parameters |
| **Operator** | Running ArcMAS fleet | Monitor costs, health, circuit breakers. Adjust config. |
| **Auditor** | Compliance review | Export audit logs, verify tamper-evident chain, review call history |
| **Dashboard viewer** | Wall display / read-only | Live stream on a monitor, no config access |

## 3. Requirements

### 3.1 ArcLLM Changes (Core)

#### REQ-001: TraceStore Protocol + JSONL Implementation
- `TraceStore` Protocol with 4 methods: `append()`, `query()`, `verify_chain()`, `close()`
- `JSONLTraceStore` implementation: append-only, SHA-256 hash chain, daily rotation
- `TraceRecord` Pydantic model with: trace_id, timestamp, provider, model, messages (request body), response (content, tool_calls, usage, stop_reason), phase_timings, cost_usd, duration_ms, agent_label, budget_scope, status, error, prev_hash, record_hash
- Raw body storage configurable per tier (federal: always, enterprise: default on, personal: default off)
- Rotation tombstone records for cross-file chain continuity
- Use `jcs` (RFC 8785) for canonical JSON serialization

#### REQ-002: on_event Callback
- New optional `on_event` parameter on `load_model()`: `Callable[[TraceRecord], None] | None`
- Fires after every `invoke()` completes (success or error)
- Fires OUTSIDE any locks (matching ArcRun's EventBus pattern)
- Zero overhead when not set (no trace record created if no callback AND no TraceStore)
- TraceStore and on_event are independent — either, both, or neither can be active

#### REQ-003: ConfigController
- `ConfigController` class in ArcLLM for runtime get/set of model configuration
- Immutable snapshot + atomic reference swap pattern
- Readable fields: model, temperature, max_tokens, budget limits, failover chain
- Writable fields (operator role): temperature, max_tokens, daily budget limit
- Every mutation emits audit event (TraceRecord with type=config_change)
- `GET /api/config` and `PATCH /api/config` exposed by arcUI server

#### REQ-004: CircuitBreakerModule
- New module in ArcLLM module stack: `CircuitBreakerModule`
- Per-provider state machine: CLOSED → OPEN (after N consecutive failures) → HALF_OPEN (probe after cooldown) → CLOSED (on success)
- Configurable: failure_threshold (default 5), cooldown_seconds (default 30), half_open_max_calls (default 1)
- State transitions emitted as events via on_event
- State queryable: `GET /api/circuit-breakers`

#### REQ-005: Span Sub-Phase Timing
- TelemetryModule records sub-phase timings: `phase_timings: dict[str, float]`
- Phases: prompt_assembly_ms, token_estimation_ms, llm_call_ms, tool_execution_ms, post_processing_ms
- Also emitted as OTel child spans under the existing telemetry span
- arcUI renders as horizontal span timeline bar

#### REQ-006: Budget State API
- BudgetAccumulator state queryable via ConfigController
- `GET /api/budget` returns per-scope: monthly_spend, daily_spend, monthly_limit, daily_limit, enforcement, alert_threshold_pct
- arcUI displays budget gauges (spent/limit ratio) per scope

### 3.2 ArcUI Package (New)

#### REQ-007: Starlette Server
- `packages/arcui/` subpackage with Starlette + uvicorn
- Static file serving for HTML/CSS/JS from `arcui/static/`
- WebSocket endpoint at `/ws` for real-time event streaming
- REST endpoints at `/api/*` for queries and config
- Default bind: `127.0.0.1:8420` (configurable in TOML)
- Entry point: `arcui.serve(llm=model)` one-liner or `attach_llm(instance, label)`

#### REQ-008: WebSocket Real-Time Stream
- ConnectionManager with per-client `asyncio.Queue` (bounded, maxsize=1000)
- 100ms batched flush via EventBuffer (bounded deque)
- First-message auth: client sends `{"type":"auth","token":"..."}`, server validates within 5s
- Graceful shutdown: close all connections with code 1001
- Heartbeat ping/pong for silent TCP drop detection

#### REQ-009: REST API Endpoints
- `GET /api/traces` — paginated trace history with cursor, filters (provider, agent, status)
- `GET /api/traces/{trace_id}` — single trace with full request/response bodies
- `GET /api/stats` — rolling window aggregates (1h/24h/7d)
- `GET /api/config` — current LLM configuration
- `PATCH /api/config` — update config (operator role, bearer token required)
- `GET /api/circuit-breakers` — provider circuit breaker states
- `GET /api/budget` — per-scope budget state
- `GET /api/export` — CSV/JSON export of filtered traces

#### REQ-010: Authentication & Authorization
- Bearer token from TOML config (auto-generated if blank)
- Two roles: viewer (read-only endpoints), operator (read + write)
- Viewer: GET endpoints, WebSocket subscribe
- Operator: all viewer + PATCH config, export
- Federal tier: mTLS required in addition to token

#### REQ-011: Telemetry Dashboard (HTML/CSS/JS)
- Port demo CSS design system (`arc-platform.css`) to `arcui/static/assets/`
- Port shell JS (`arc-shell.js`) adapted for real WebSocket data
- Telemetry page with 4 pill-nav tabs: Overview, Traces, Replay, Cost
- **Overview tab**: 4 stat cards (calls 24h, total tokens, avg latency with P95/P99, cost 24h), token volume chart (hourly bars), cost breakdown by provider (progress bars), provider circuit breaker table
- **Traces tab**: filterable table (provider, agent, status dropdowns), expandable trace detail (request/response code blocks + span timeline bar), Export button
- **Cost tab**: per-agent cost breakdown (bar chart), per-provider cost (progress bars), budget gauges per scope, model cost efficiency insights ($/token ranking, potential savings)

#### REQ-015: Model Cost Efficiency Insights
- Cost tab section calculating per-model $/token efficiency from TraceStore records
- Show: (1) most cost-efficient model (lowest $/token), (2) most-utilized model (highest request volume), (3) potential savings if migrating heavy-use models to cheapest viable alternative
- Uses real split input/output pricing from ArcLLM provider configs (not flat-rate approximation)
- Alert when optimization opportunity exceeds 20% potential savings
- Export-friendly data for budget justification
- Inspired by Mission Control competitive analysis (2026-03-01)

#### REQ-012: Rolling Window Aggregation
- Server-side BucketedWindow: 1h (per-minute), 24h (per-hour), 7d (per-day)
- Metrics per bucket: count, sum, min, max, DDSketch for P50/P95/P99
- Double-buffer for lock-free reads
- Warm-start from JSONL on server startup
- ~2MB total memory footprint

#### REQ-013: Frontend Patterns
- RobustWebSocket class: exponential backoff + jitter, heartbeat, outbound queue
- DOMBatcher: requestAnimationFrame batching, DocumentFragment for bulk inserts
- Store: EventTarget + CustomEvent central state, scoped subscribers
- LogTable: node cap 200-300 rows, auto-scroll detection, "Resume live" button
- Number formatting: pre-allocated Intl.NumberFormat (compact tokens, currency costs, tiered latency)
- Connection UX: 4 visual states, stale-data overlay after 30s, live "Xs ago" timer
- escapeHTML() on all untrusted content (agent names, trace content)

### 3.3 ArcAgent Bridge

#### REQ-014: create_arcllm_bridge()
- New function in ArcAgent matching `create_arcrun_bridge()` pattern
- Maps ArcLLM on_event TraceRecords to ModuleBus events: `llm:call_complete`, `llm:error`, `llm:config_change`, `llm:circuit_change`
- Uses `loop.create_task()` with `_pending` set for GC prevention

## 4. Non-Requirements (Explicit Exclusions)

- **Replay**: Session-based call replay is an ArcAgent concern. arcUI can VIEW raw bodies but does not re-send calls. Day 2.
- **Multi-page dashboard**: Only the telemetry page is day 1. Settings, agents, tools, etc. are future pages.
- **SQLiteStore**: Protocol-ready but JSONL only for day 1. SQLite added when needed.
- **CLI wrapper**: REST API is the CLI (`curl + jq`). Thin CLI wrapper is day 2.
- **NATS discovery**: Multiple `attach_llm()` calls with labels. Auto-discovery via NATS is day 2.

## 5. Success Criteria

1. `arcui.serve(llm=model)` starts server on :8420 and shows live telemetry in browser
2. Every LLM call appears in the trace table within 200ms
3. Trace detail shows full request/response bodies and span timeline
4. Circuit breaker state visible per provider
5. Budget gauges show spend vs limit per scope
6. Config changes via PATCH reflect immediately in dashboard
7. `verify_chain()` passes on all stored JSONL traces
8. Token auth works (viewer can't PATCH, operator can)
9. Export produces valid CSV/JSON matching displayed filters
10. All new code passes: `ruff check`, `mypy --strict`, `pytest` with >=80% coverage
11. Cost tab shows per-model $/token efficiency ranking and potential savings calculation

## 6. Compliance Notes

| Requirement | NIST Control | Implementation |
|-------------|-------------|----------------|
| Every LLM call audited | AU-2, AU-12 | TraceStore auto-append on every invoke() |
| Tamper-evident logs | AU-9 | SHA-256 hash chain with rotation tombstones |
| Retention | AU-11 | Configurable, federal enforces 90d+1yr |
| Config change audit | CM-3 | Every PATCH logged as TraceRecord type=config_change |
| Access control | AC-3, AC-6 | Bearer token, two roles, mTLS for federal |
| Transport security | SC-8 | TLS 1.2+ default, mTLS federal tier |
