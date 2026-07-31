# Decisions Log

All design decisions across features and phases.

---

## Multi-Agent UI Architecture — Build Decisions (2026-03-03)

**Phase**: build | **Status**: complete | **Total decisions**: 24 (15 user, 9 auto-applied)
**Priority framework**: simplicity > security > scalability > compliance
**Brainstorm**: inline conversation — covered current architecture analysis, 4 approach options, user decisions on topology/transport/layer-monitoring/control-plane
**Prior build**: arcui-llm-telemetry (2026-03-01) — 27 decisions covering initial single-agent UI

### Summary

ArcUI evolves from an embedded single-agent dashboard to a standalone multi-agent control plane. UI runs as its own process (`arc ui start`), agents connect via WebSocket (`/api/agent/connect`), push events from 4 layers (llm, run, agent, team), and receive control commands on the same connection. UIReporter module in arcagent bridges all internal events to the UI. Three token types (viewer, operator, agent) with server-side identity binding. Per-agent aggregators + global rollup. Full control plane via REST proxy to agent WebSocket. Clean break from embedded mode.

### Architecture Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-001 | Agent-to-UI encryption in transit | TLS 1.2+ minimum | security | Federal: mTLS. Enterprise: TLS. Personal: optional (localhost). `auto-applied: federal-mandate` (NIST 800-52r2, SC-8) |
| D-002 | Connection lifecycle audit events | All state changes audited | compliance | Same across tiers. `auto-applied: federal-mandate` (NIST AU-2) |
| D-003 | Agent identity verification | Verifiable identity on connect | security | Federal: DID + signed challenge. Enterprise/Personal: token. `auto-applied: federal-mandate` (NIST IA-2/IA-8) |
| D-004 | Agent discovery/connection | Explicit TOML config `[ui]` section with url and token | simplicity | Federal: validates wss:// for non-localhost. Same otherwise. |
| D-005 | Disconnect resilience | Reconnect + buffer. Exponential backoff (1s→60s cap), bounded deque (1000), flush on reconnect | simplicity | Federal: disconnect/reconnect audited. |
| D-006 | UI ↔ arcteam messaging | Agents relay their own team messages through UI WebSocket | simplicity | No direct arcteam dependency in arcui. |
| D-007 | Event layer taxonomy | 4 layers: llm, run, agent, team | simplicity | Maps 1:1 to packages. |
| D-008 | UIReporter ownership | arcagent module at `arcagent/modules/ui_reporter/` | simplicity | Opt-in via config. Follows existing module pattern. |
| D-009 | UI process launch | New `arc ui start` CLI command. Standalone process. | simplicity | `--ui` on agent means "connect to UI". Old embedded mode removed. |
| D-010 | Control plane transport | Bidirectional WebSocket. Same connection for events and control. | simplicity | Federal: control commands signed. |
| D-011 | UI agent registry | In-memory. Agents reconnect on UI restart. No persistence. | simplicity | Federal: connection events in separate audit log. |
| D-012 | Browser event filtering | Server-side subscription filters. Browser sends subscribe message with agents/layers/teams. | scalability | Federal: subscription changes audited. |

### Data Model Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-013 | UIEvent schema | Flat Pydantic envelope: layer, event_type, agent_id, agent_name, source_id, timestamp, data, sequence | simplicity | Federal: may add classification field. |
| D-014 | Agent registration schema | Identity + capabilities: DID, name, model, provider, team, tools, modules, workspace, meta dict | simplicity | Federal: workspace path redacted. |
| D-015 | Control message schema | Core set + extensible: action, target, data, request_id. Actions: steer, cancel, config, ping, shutdown. Response correlation via request_id. | simplicity | Federal: includes operator identity. |

### API Design Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-016 | Rate limiting | All endpoints rate-limited | security | `auto-applied: federal-mandate` (NIST SC-5) |
| D-017 | Agent WS path | `/api/agent/connect`. Browser stays at `/ws`. | simplicity | |
| D-018 | Browser REST for agents | Thin proxy: POST to UI REST → UI forwards to agent WS → returns response. Endpoints: /api/agents, /api/agents/{id}, /api/agents/{id}/control | simplicity | Federal: operator role for control. |
| D-019 | Auth model | Three token types: viewer_token, operator_token, agent_token. Clear privilege matrix. | security | Federal: DID challenge for agents. |

### Observability Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-020 | UI operations traced | OpenTelemetry spans on all UI operations | compliance | `auto-applied: federal-mandate` (NIST AU-12) |
| D-021 | Multi-agent aggregation | Per-agent sub-aggregators + global rollup. Reuses RollingAggregator class. | simplicity | |

### Audit & Compliance (all auto-applied)

| # | Decision | Choice | Mandate |
|---|----------|--------|---------|
| D-022 | Registration audited | AU-2 | `auto-applied: federal-mandate` |
| D-023 | Control commands audited | AU-2 | `auto-applied: federal-mandate` |
| D-024 | Auth failures audited | AU-2 | `auto-applied: federal-mandate` |
| D-025 | Subscription changes audited (federal) | AU-6 | `auto-applied: federal-mandate` |
| D-026 | Tamper-evident UI audit log | AU-9 | `auto-applied: federal-mandate` |

### Security Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-027 | No secrets in events | Sanitized payloads | security | `auto-applied: federal-mandate` (NIST IA-5) |
| D-028 | Control requires operator role | Role-gated | security | `auto-applied: federal-mandate` (NIST AC-3) |
| D-029 | WS idle timeout | 5-min timeout with heartbeat keepalive | security | `auto-applied: federal-mandate` (NIST SC-10) |
| D-030 | Agent token scope | Shared agent_token with server-side binding. Server stamps agent_id on events. | simplicity | Federal: upgrades to DID. |

### Integration Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-031 | UIReporter ↔ arcllm | Chain into TelemetryModule.on_event. Same pattern as agent.py:1273. | simplicity | No arcllm changes. |
| D-032 | UIReporter ↔ arcrun | Subscribe to ModuleBus events (existing arcrun bridge). | simplicity | No arcrun changes. |

### Performance Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-033 | Max agent connections | 100 default, configurable. ~60KB per agent. 429 on exceed. | scalability | |

### Extensibility Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-034 | Transport abstraction | UITransport Protocol now. WebSocketTransport implementation. NATSTransport later. | simplicity | |

### Testing Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| T-1 | Test strategy | Unit (70%) + integration with InMemoryTransport (20%) + E2E WebSocket (10%) | simplicity | |

### Deployment Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-035 | Migration from embedded | Clean break. Remove embedded code. `--ui` becomes "connect to UI". | simplicity | |

### UI/UX Decisions

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-036 | Dashboard layout | Agent sidebar + layer tabs (All/LLM/Run/Agent/Team). Stat cards + event stream. | simplicity | |

---

## ArcUI LLM Telemetry — Build Decisions (2026-03-01)

**Phase**: build | **Status**: complete | **Total decisions**: 27 (21 user, 6 auto-applied)
**Priority framework**: simplicity > security > scalability > compliance
**Brainstorm**: inline conversation (no file — brainstorm covered architecture, persistence, control plane)

### Summary

ArcUI is a new subpackage (`packages/arcui/`) providing browser-based monitoring and control for ArcLLM (and eventually the full ArcMAS stack). Architecture: Starlette + uvicorn serves static vanilla HTML/CSS/JS + WebSocket + REST endpoints. ArcLLM gains three new capabilities: TraceStore (JSONL default, SQLite optional, hash-chained for tamper evidence), `on_event` callback (matching ArcRun's pattern), and ConfigController (runtime get/set). arcUI attaches to live ArcLLM instances via `attach_llm()`, collects events, computes server-side rolling aggregates, and streams to browser over WebSocket. Two auth roles (viewer/operator). Default telemetry page shows live stream + stat cards + cost breakdowns.

### Research Insights (from /deepen — 6 parallel agents)

#### Architecture & Integration Patterns (Codebase Analysis)

**Existing code to match/reuse:**
- `arcrun/events.py:EventBus` — Hash chain with `GENESIS_PREV_HASH = "0"*64`, `_canonical_bytes()` uses `json.dumps(sort_keys=True, separators=(",",":"))`, SHA-256 via `hashlib`. Thread-safe with `threading.Lock`. `on_event` callback fires OUTSIDE the lock. `verify_chain()` does three-part check: self-hash recompute, prev-hash linkage, sequence contiguity.
- `arcrun/loop.py` — `on_event: Callable[[Event], None]` wired at `_build_state()`. Use same signature for ArcLLM.
- `arcllm/modules/telemetry.py:TelemetryModule` — Computes duration_ms, cost_usd, all usage fields. `BudgetAccumulator` with `deduct()`, `check_limits()`. Shared registry keyed by `budget_scope`.
- `arcllm/modules/base.py:BaseModule` — `_inner` chain pattern, `_span()` for OTel spans. New TraceStore hooks should wrap similarly.
- `arcagent/core/module_bus.py:ModuleBus` — Async pub/sub, priority-grouped dispatch, `EventContext` with veto. Bridge maps arcrun events to agent events.
- `arcagent/core/agent.py:create_arcrun_bridge()` — Maps `tool.start→agent:pre_tool`, etc. Uses `loop.create_task()` with `_pending` set for GC. Same pattern for `create_arcllm_bridge()`.

**Key insight**: The on_event callback pattern is already proven in ArcRun. ArcLLM's implementation should mirror it exactly — optional param on `load_model()`, fires after state mutation, never blocks the caller.

#### Data Model — JSONL Hash Chain Best Practices

**Canonical JSON**: Use `jcs` library (RFC 8785) for deterministic JSON serialization instead of `json.dumps(sort_keys=True)`. RFC 8785 handles Unicode normalization, number serialization edge cases (e.g., `-0` → `0`, `1e2` → `100`), and key ordering more rigorously than stdlib.

**Performance**: `orjson` is 3-10x faster than stdlib `json` for serialization. `orjsonl` provides streaming JSONL append/read. Consider for high-throughput traces.

**Rotation continuity**: Use rotation tombstone records — last record in a day's file contains `{"type":"rotation","next_file":"traces-2026-03-02.jsonl","chain_hash":"<last_hash>"}`. Next file's first record references this. Unbroken chain across files without separate state file.

**Alternative to chain-state.json**: The tombstone approach is more robust than a separate pointer file because chain-state.json can get out of sync if process crashes between writing a trace and updating the pointer. With tombstones, the rotation record IS part of the chain.

**NIST AU-9 compliance checklist**: (1) append-only file mode, (2) hash chain with cryptographic binding, (3) manifest sidecar for quick integrity verification, (4) rotation with chain continuity, (5) alerting on chain break detection.

#### API Design — Starlette WebSocket Patterns

**ConnectionManager pattern**: Per-client `asyncio.Queue` (bounded, maxsize=1000) instead of direct `websocket.send()`. Prevents slow clients from blocking the event loop. If queue is full, drop oldest or disconnect.

**EventBuffer**: Bounded `collections.deque(maxlen=N)` for 100ms batched flush. Accumulate events, flush in a single WebSocket frame via `asyncio.sleep(0.1)` loop.

**mTLS with uvicorn**: `uvicorn.Config(ssl_keyfile=..., ssl_certfile=..., ssl_ca_certs=..., ssl_cert_reqs=ssl.CERT_REQUIRED)` for federal tier. Starlette middleware validates client cert DN.

**Graceful shutdown**: Register `app.on_shutdown` handler that closes all WebSocket connections with code 1001 ("going away"), waits for drain, then stops the event loop.

#### Performance — Rolling Window Aggregation

**BucketedWindow design**: Three resolutions — 1h at per-minute buckets (60 slots), 24h at per-hour buckets (24 slots), 7d at per-day buckets (7 slots). Each bucket stores: count, sum, min, max, DDSketch for percentiles.

**DDSketch for streaming percentiles**: P50/P95/P99 with bounded relative error (~1%) using ~2KB memory per sketch. Total memory for all three windows with 6 metrics each: ~2MB. Much better than keeping all raw values.

**Double-buffer for lock-free reads**: Write buffer + read buffer. Writer always appends to write buffer. Periodic swap (atomic reference swap) makes write buffer the new read buffer. Readers never block writers.

**Warm-start from JSONL**: On server startup, scan current day's JSONL file to rebuild in-memory windows. Server restarts invisible — aggregates immediately accurate.

#### Security — Runtime Config Hot-Reload

**ConfigStore pattern**: Immutable config snapshot + atomic reference swap. `Pydantic.model_copy(update={...})` creates new frozen config object. Single `config_ref` atomic swap ensures readers see consistent snapshot.

**Three propagation strategies**: (1) Direct — ConfigController holds reference, callers read on each use (simplest). (2) Callback — `on_config_change(key, callback)` handlers. (3) Event-based — emit via ModuleBus/EventBus.

**Recommendation**: Start with Direct. Add Callback for hot-reload of specific settings (budget limits). Event-based only when ArcAgent modules need to react.

**Audit trail**: Every config mutation emits OTel span + TraceRecord with: who (token identity), what (key path + old value + new value), when (timestamp), where (source IP).

#### UI/UX — Vanilla JS Dashboard Patterns

**RobustWebSocket**: Reconnection class with exponential backoff + jitter, heartbeat ping/pong for silent TCP drop detection, outbound message queue during disconnection, `navigator.onLine` pre-check. Max retries configurable.

**DOMBatcher**: Accumulate-and-flush — never write DOM in WebSocket message handler. All mutations batched through single `requestAnimationFrame` per frame. `DocumentFragment` for bulk row inserts (single reflow).

**Store pattern**: Central state via `EventTarget` + `CustomEvent`. Scoped subscribers per UI section (traces panel, status bar, agent selector). Avoids full re-render on every state change.

**LogTable**: Node cap (200-300 rows) with `deleteRow(0)`. Auto-scroll via `scrollHeight - scrollTop - clientHeight < 4`. "Resume live" sticky button when user scrolls up.

**Number formatting**: Pre-allocated `Intl.NumberFormat` objects — compact notation for tokens, currency for costs, tiered latency ms/s/m. `escapeHTML()` for all untrusted content.

**WebSocket auth**: First-message pattern — connection opens, client sends `{"type":"auth","token":"..."}`, server validates within 5s timeout, sends `auth_ok` or closes with 4001. Token embedded in server-rendered HTML.

**Connection UX**: Four visual states (CONNECTED/CONNECTING/RECONNECTING/DISCONNECTED). Stale-data overlay on metric cards after 30s silence. Live "Xs ago" timer during reconnection. Keep last-known data visible rather than clearing.

**CSS charts**: `width` % on flex children = repaint only (fast). `conic-gradient` for gauges = repaint only but NOT CSS-animatable. Limit `will-change` to <10 elements. Switch to Canvas if >20 elements updating >5/sec.

---

### Auto-Applied (Federal Mandates)

| # | Decision | Mandated Answer | Citation |
|---|----------|----------------|----------|
| A1 | Trace storage integrity | SHA-256 hash chain, append-only, tamper-evident | NIST 800-53 AU-9 |
| A2 | Audit every LLM call | Every `invoke()` generates a TraceRecord, no opt-out | NIST 800-53 AU-2, AU-12 |
| A3 | Retention period | Configurable. Federal: 90d online + 1yr archive minimum | NIST 800-53 AU-11, FedRAMP |
| A4 | Transport encryption | TLS 1.2+ for arcUI server. mTLS optional (federal tier) | NIST 800-52r2, SC-8 |
| A5 | Config mutations audited | Every config change via control plane logged as audit event | NIST 800-53 AU-2, CM-3 |
| A6 | Default bind address | `127.0.0.1` (localhost only). Explicit opt-in to expose | NIST 800-53 SC-7 |

### Architecture

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-037 | TraceStore location | ArcLLM / ArcUI / Shared utils | ArcLLM | Data belongs closest to where it's generated. Traces persist without arcUI running. |
| D-038 | TraceStore backend design | Protocol + impls / Single class / JSONL-only | Protocol + implementations (JSONLStore, SQLiteStore) | Follows ArcLLM's adapter pattern. We know SQLite is coming. 4-method Protocol is minimal. |
| D-039 | Real-time event hook | on_event callback / Internal EventBus / OTel SpanProcessor | `on_event` callback on `load_model()` | Matches ArcRun's pattern. Optional, zero overhead when unused. ArcAgent bridge forwards to ModuleBus. |
| D-040 | ConfigController location | ArcLLM / ArcUI / ArcAgent | ArcLLM | Owner provides API. Standalone ArcLLM users also benefit. |
| D-041 | Attach API | attach_llm(instance) / Auto-discover / Config-driven | `attach_llm(instance)` explicit API | Explicit, typed, debuggable. Works standalone and inside ArcAgent. One-liner shortcut: `serve(llm=model)`. |
| D-042 | WebSocket protocol | JSON type field / MessagePack / JSON-RPC 2.0 | JSON messages with `type` field | Matches ArcRun Event structure. Debuggable in devtools. No client deps. |
| D-043 | Multi-LLM handling | Multiple attach_llm() / Agent registry / NATS discovery | Multiple `attach_llm()` calls with labels | Explicit, typed, scales linearly. Agent auto-discovery layered later. |

### Data Model

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-044 | TraceRecord fields | Full telemetry / Minimal / Full + raw bodies | **Full telemetry + raw bodies** | Everything TelemetryModule computes PLUS serialized request (messages, tools, params) and response (content, usage, stop_reason). Federal requires full visibility into every LLM call. Raw bodies enable trace detail view in arcUI. Configurable: personal tier can omit raw bodies to save disk. |
| D-045 | JSONL rotation | Daily / Size-based / No rotation | Daily rotation | Aligns with budget reset. Date-based retention trivial. chain-state.json for continuity. |
| D-046 | Trace file location | Workspace-relative / XDG / Configurable | Workspace-relative `{workspace}/traces/` with configurable override | Co-locates with sessions. Configurable for shared fleet directories. |
| D-047 | Hash chain across rotations | chain-state.json / Self-referencing records / Separate chain log | chain-state.json carries last hash | Simple pointer file. Unbroken chain across file boundaries. |

### API Design

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-048 | Endpoint structure | Single WS + static + REST / Multiple WS / Pure WS | Single WS `/ws` + static `/` + REST `/api/*` | Minimal routing surface. WS for real-time, REST for queries. |
| D-049 | Historical query API | Filter params / GraphQL / POST body | GET with query params + cursor pagination | Cacheable, shareable, curl-friendly. Inline aggregates. |
| D-050 | Day 1 authentication | Bearer token / No auth / mTLS | Bearer token from TOML | Day 1 auth without certificate infra. Auto-generated if blank. Federal upgrades to mTLS. |

### Observability

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-051 | arcUI self-telemetry | Minimal OTel / None / Full OTel + Prometheus | Minimal OTel spans | Dog-fooding. HTTP requests, WS connections, queries, config mutations. Same tracer pattern. |

### Audit & Compliance

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-052 | Audit target for config mutations | Same TraceStore / Separate audit log / ArcAgent audit | Same TraceStore, different event type | One store, one chain, one verify_chain(). Type field distinguishes LLM calls from admin actions. |

### Security

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-053 | Authorization model | Two roles / Single role / RBAC | Two roles: viewer (read-only) + operator (read + write) | Least-privilege. Dashboard on wall = viewer. Person at keyboard = operator. |
| D-054 | Data redaction | No redaction / Configurable / Classification-aware | No redaction — trust the viewer role | TraceRecords are operational metadata, not secrets. Access control is the right gate. |

### Integration

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-055 | ArcAgent integration | Optional module / Direct bus sub / Shared queue | Optional arcui_bridge module | Decoupled — works LLM-only or full-stack. attach_agent() wires everything. |
| D-056 | CLI mode | Not day 1 / Day 1 / No CLI | Not day 1, design for it | REST API is the CLI (curl + jq). Thin wrapper trivial to add later. |

### Performance

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-057 | Event stream backpressure | Server buffer + batch / Client filter / Sampling | Server-side buffering with 100ms batched flush | Bounded deque, batched sends. All events still stored. Bandwidth-efficient. |
| D-058 | Aggregation strategy | Server rolling windows / Client compute / Pre-computed in store | Server-side rolling windows (1h/24h/7d) | O(1) per event. Client gets pre-computed stats. In-memory counters. |

### Extensibility

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-059 | Frontend framework | Vanilla HTML/CSS/JS / Lit / HTMX | Vanilla HTML/CSS/JS | Matches demo. Zero build step. Ships in wheel. No node in a Python project. |
| D-060 | Page extensibility | Convention-ready / Plugin API / Fixed | Convention-ready (HTML file + PAGES entry) | Convention is the plugin system. API when modules prove they need custom UI. |

### Testing

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-061 | Test strategy | Python + Playwright / Python only / Python + Jest | pytest (unit/integration) + Playwright (E2E) | Both Python tools. Playwright already in arcagent deps. Full stack confidence. |

### Deployment

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-062 | Packaging | Monorepo subpackage / Separate repo / Built into arcagent | Subpackage in Arc monorepo (`packages/arcui/`) | Same pattern as siblings. Cross-package testing. Static files in wheel. |

### UI/UX

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-063 | Telemetry default view | Live stream + stats / Historical analytics / Per-agent split | Live stream + stats | Shows the pulse immediately. Errors visible at a glance. Historical/per-agent are tabs. |
| D-064 | Telemetry tab structure | Single page / Tabbed views / Separate pages | Four pill-nav tabs: Overview, Traces, Replay, Cost | Matches demo. Overview = live stream + stats. Traces = filterable table + detail expand. Replay = session-level (ArcAgent concern, not day 1). Cost = per-agent + per-provider breakdowns. |

### Observability (additions)

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-065 | Span timeline sub-phases | Total duration only / Sub-phase timing / Full OTel child spans | Sub-phase timing in TraceRecord + OTel child spans | TelemetryModule needs to emit timing for: prompt assembly, token estimation, LLM API call, tool execution, post-processing. TraceRecord stores `phase_timings: dict[str, float]` (ms). arcUI renders as horizontal span bar matching demo. OTel child spans for collector integration. |
| D-066 | Circuit breaker module | No circuit breaker / Per-provider state machine / External service | New CircuitBreakerModule in ArcLLM module stack | RetryModule handles per-call retries but doesn't track provider health state. CircuitBreakerModule wraps adapter: tracks consecutive failures, transitions CLOSED→OPEN (trip after N failures)→HALF_OPEN (probe after cooldown)→CLOSED (on success). Per-provider state. Emits state transitions as events. arcUI displays provider circuit state table. |
| D-067 | Budget state visibility | OTel only / Queryable API / Both | Queryable via ConfigController + OTel spans | BudgetAccumulator already tracks monthly_spend, daily_spend, limits, enforcement. ConfigController exposes `GET /api/budget` returning per-scope spend vs limits. arcUI displays budget gauges (spent/limit) per scope. Alerts at threshold_pct. |
| D-068 | Per-agent cost breakdown | Client-side compute / Server-side aggregate / Pre-computed in TraceStore | Server-side rolling window keyed by agent label | BudgetAccumulator scopes map to agents. Rolling window aggregation (decision #22) adds agent dimension. arcUI cost tab shows per-agent cost bar chart matching demo layout. |

### Data Model (additions)

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-069 | Raw body storage | Always store / Configurable / Never store | Configurable per tier, default ON | Federal: always store (full audit trail, NIST AU-3). Enterprise: default on, can disable. Personal: default off (saves disk), opt-in. TraceRecord gains `request_body: dict | None` and `response_body: dict | None`. Serialized as JSON within JSONL record. |
| D-070 | Filter/export on trace table | No filters / Provider + Agent filters / Full filter set | Provider filter + Agent filter + Status filter + Export CSV/JSON | Matches demo. Dropdowns for provider and agent. Status filter (all/success/error/timeout). Export button for compliance reporting (NIST AU-6 review support). |

### UI/UX (additions — 2026-03-01, from Mission Control competitive analysis)

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-071 | Model cost efficiency insights | No insights / Simple cheapest-model display / Full optimization analysis | Cost optimization section in Cost tab | Calculate per-model $/token efficiency from TraceStore records. Show: (1) most cost-efficient model (lowest $/token), (2) most-utilized model (highest request volume), (3) potential savings if migrating heavy-use models to cheapest viable alternative. ArcLLM already has split input/output pricing per provider config — use actual rates, not flat estimates. Alert when optimization opportunity exceeds 20% potential savings. Export-friendly for budget justification. Inspired by Mission Control's optimizer, but ours uses real split pricing instead of their flat-rate approximation. |

_Note: Context window utilization tracking was evaluated but deferred — ArcLLM is stateless per-call. Context accumulation is an ArcAgent/ArcRun session concern, not ArcUI day 1._

### Tier Behavior Summary

| Capability | Personal | Enterprise | Federal |
|-----------|----------|------------|---------|
| TraceStore hash chain | Optional (warn if off) | Required (warn) | Required (block) |
| Raw body storage | Off (opt-in) | On (opt-out) | Always on (no opt-out) |
| Retention enforcement | No enforcement | Warn at expiry | 90d online + 1yr archive enforced |
| Auth for viewer | Open (no token) | Token required | Token + mTLS required |
| Auth for operator | Token required | Token required | Token + mTLS required |
| Config mutation audit | Logged | Logged + alerted | Logged + alerted + signed |
| Bind address | Configurable | Default localhost | Localhost only (override requires explicit config) |
| TLS | Optional | Recommended | Required (mTLS) |
| Circuit breaker | Optional module | Enabled by default | Required (block calls to tripped providers) |
| Budget enforcement | Warn only | Block at limit | Block at limit + alert + audit |

---

## Convention-Driven Prompt Injection — Build Decisions (2026-02-27)

**Phase**: build | **Status**: complete | **Total decisions**: 16 (15 user, 1 auto-applied)
**Priority framework**: simplicity > security > scalability > compliance
**Brainstorm**: `.claude/brainstorms/2026-02-27-convention-driven-prompt-injection.md`

### Summary

Two auto-injected catalogs in the system prompt: **tool catalog** (arcagent core, on `ToolRegistry`) and **team roster** (messaging module, from `EntityRegistry`). Both use the `agent:assemble_prompt` bus event. Tool catalog uses invalidate-on-register caching. Team roster uses TTL-based refresh (60s). All rendering is dynamic — iterate model fields, render non-empty values as XML. New fields on `RegisteredTool` or `Entity` automatically appear in the prompt. Section key for team is `sections['teams']`.

### Auto-Applied (Federal Mandates)

| # | Decision | Mandated Answer | Citation |
|---|----------|----------------|----------|
| D-072 | Audit prompt catalog rebuilds | Log `prompt.tools_catalog_rebuilt` and `prompt.roster_rebuilt` events | NIST 800-53 AU-2 |

### Architecture

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-073 | Tool catalog formatter location | ToolRegistry method / Standalone class / Mirror SkillRegistry | `format_for_prompt()` on ToolRegistry | Registry knows its own data. Same pattern as SkillRegistry but lives on ToolRegistry directly. |
| D-074 | Prompt format | Markdown / XML / Mixed | XML tags for both catalogs | Consistent with SkillRegistry's XML format. Structured, parseable. |
| D-075 | RegisteredTool new fields | RegisteredTool only / Add to decorator too | Add `when_to_use`, `example`, `category` to both RegisteredTool and @native_tool decorator | Ergonomic — module authors set metadata inline at registration time. |
| D-076 | Tool catalog cache strategy | Invalidate on register / Bus events / No cache | Invalidate on `register()` | `register()` is the single entry point for all tools (startup, reload, mid-session). One line: `self._prompt_cache = None`. Always fresh, zero overhead. |
| D-077 | Team roster cache strategy | TTL / Rebuild every turn / File watcher | TTL-based refresh (60s default) | Entity data lives on disk, written by other processes. No hook into their writes. TTL bounds staleness. Different from tools because different data ownership. |
| D-078 | Section ordering | Alphabetical / Explicit priority | Alphabetical | messaging → skills → tools is reasonable. Identity first, context last. Don't over-engineer the middle. |
| D-079 | Built-in tools in catalog | All / Exclude builtins / Minimal builtins | All tools, no exceptions | Everything through `register()` appears. Consistent. Built-in tools can have `when_to_use` too. |
| D-080 | Section key for team context | `sections['messaging']` / `sections['teams']` | `sections['teams']` | Will grow to include more team-based injections beyond messaging. Future-proof name. |

### Data Model

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-081 | Entity fields in roster | name+id+roles+caps+status / name+id only / all fields | All non-empty fields | Convention-driven: whatever's on the model appears in the prompt. Add a field to Entity, it auto-renders. |
| D-082 | Specific new fields on Entity | description / when_to_contact / none | None needed | Convention handles it. Any field set on Entity renders automatically. Design fields as needed, not upfront. |
| D-083 | Renderer approach | Dynamic (model_dump) / Static (known fields) | Dynamic field iteration | `model_dump(exclude_defaults=True)` for Pydantic, `dataclasses.fields()` for dataclasses. New fields appear in prompt without code changes. |

### Security

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-084 | Metadata sanitization | XML-escape / Trust registered data | XML-escape all string values | Defense in depth. Matches SkillRegistry pattern. Prevents prompt injection via tool descriptions from extensions. |

### Extensibility

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-085 | Preamble configurability | Hardcoded / Workspace file / TOML | Configurable via TOML | Default hardcoded, overridable in `[tools]` config. Lets operators customize the guidance text per deployment. |
| D-086 | Roster TTL configuration | MessagingConfig / Hardcoded | `roster_ttl_seconds` in MessagingConfig | Configured via `[modules.messaging.config]` in TOML. Keeps config with the owning module. |

### Integration

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|
| D-087 | Overlap with API tool schemas | Complementary / Enriched-only / Replace | Complementary | API schemas = WHAT (parameters). Prompt catalog = WHEN/WHY (guidance). Different concerns, both valuable. |

### Tier Variations

| Decision | Federal | Enterprise | Personal |
|----------|---------|------------|----------|
| Audit (12) | Required — hard error if audit fails | Required — warn on failure | Optional — info-level log |
| Sanitization (13) | Required — all values escaped | Required | Required (defense in depth at all tiers) |
| Preamble (14) | May include compliance notice | Default preamble | Default preamble |

### Research Insights (Deepen — 2026-02-27)

Research conducted across: Anthropic engineering posts, OWASP cheat sheets, Elastic Security Labs, Palo Alto Unit 42, arXiv papers (2024-2026), Claude Code system prompts (reverse-engineered), OpenAI/Google/LangChain framework docs, and production codebase analysis.

#### Tool Description Best Practices

**Anthropic's own guidance** (Advanced Tool Use, 2025): "Prioritize descriptions over examples" — clear when-to-use prose outperforms example-heavy descriptions. Adding tool use examples improved parameter handling accuracy from 72% to 90%, but description quality matters more than quantity.

**Claude Code's pattern** (validated in production): Each tool has purpose statement + usage context + constraints + explicit "When NOT to Use" sections. The negative boundary ("Do NOT use for inventory queries") is a 2025-2026 innovation that significantly reduces false positives. Our `when_to_use` field should support both positive and negative guidance.

**Optimal description anatomy** (synthesized from Anthropic, Google ADK, OpenAI):
1. What it does (action verb + object)
2. What it returns (structure, types)
3. When to use it (positive trigger)
4. When NOT to use it (negative boundary)
5. Unambiguous parameter names (`customer_id` not `user`)

**Token efficiency data**: A typical multi-server MCP setup consumes ~55,000 tokens in tool definitions. Anthropic's Tool Search achieves 85% reduction via deferred loading. For our use case (<50 tools), static injection is fine — degradation starts at 30-50 tools (Anthropic Tool Search docs). Our ~500 token budget is well within safe range.

**Format finding**: XML requires ~80% more tokens than Markdown for equivalent content. However, Claude is specifically trained with XML-tagged data, so XML tags for *section delineation* are well-supported. For capable models, format choice matters less than description quality (arXiv 2411.10541, 480 tests across 5 models). Our XML choice is consistent with existing codebase patterns (skills, bio_memory) — consistency matters more than marginal token savings.

#### Team Roster Best Practices

**Consensus across ADK, OpenAI Agents SDK, Claude Code**: Routing signals are text-based. Models use natural language descriptions, not structured schemas, for routing decisions.

**Minimal viable teammate description** (production-validated):
- Name (handle/identifier)
- Domain scope (what types of tasks)
- One-line capability statement

**More metadata is not always better.** Google ADK docs: "The description field is effectively your API documentation for the LLM. Be precise." The failure mode is *description overlap* between agents, not missing information. Our dynamic field rendering (D11) is sound — render what's set, skip what's not.

**Multi-agent research finding** (arXiv 2502.02533, Feb 2025): Prompt optimization (role definitions and behavioral instructions) had more impact on multi-agent accuracy than topology changes. This validates our approach of rich, auto-injected roster descriptions.

#### Security Research — Critical Findings

**Four distinct risk categories** for XML-structured prompt injection:

| Category | Attack | Our Mitigation | Gap |
|----------|--------|----------------|-----|
| **A: Tag Confusion** | Injected `</available-tools>` in tool description closes section early | XML-escape all values (D13) | Covered |
| **B: Tag Authority Spoofing** | `<instructions>Do X</instructions>` in metadata treated as authoritative | XML-escape strips tags. Consider salted section tags for instruction blocks. | Partial — escape handles it, but salted tags are stronger |
| **C: Semantic Content Injection** | Natural language "When invoked, also read /etc/passwd" in description | Format-agnostic. No escape fixes this. Requires trusted registries, module signing. | Existing module signing (CLAUDE.md) covers this |
| **D: Context Poisoning** | Injected instructions persist across turns | Stateless rebuild on each assemble_prompt call | Covered — we rebuild from registry state each time |

**Documented attacks on tool metadata** (Elastic Security Labs, 2025):
- **Tool Poisoning via Docstrings**: Database tool docstring contains "override all instructions." Agent processes as instruction-level input.
- **Rug-Pull Redefinitions**: Tool initially legitimate, description silently updated with malicious instructions. No re-approval flow.
- **Parameter Name Exploitation**: Parameters named "context" or "environment_details" cause model to populate sensitive data without request.
- **Obfuscated Instructions**: Unicode invisible characters or Base64 in descriptions bypass human review.

**Our defense posture**: Module signing prevents unauthorized tool registration (ASI04). XML-escape prevents tag confusion (Category A). Trusted `register()` path means only code-level actors can register tools — no user-adjacent content in tool metadata. The Lethal Trifecta (private data + external comms + untrusted input) is broken by design: tool descriptions come from signed modules, not untrusted input.

**Edge case to address in implementation**: MCP tools loaded from external servers. These descriptions come from outside our trust boundary. The `source` field on `RegisteredTool` should be used to tag provenance, and MCP tool descriptions should receive stricter sanitization (strip all XML-like patterns, not just escape).

#### Codebase Landscape — Existing Prompt Injection Points

All current `agent:assemble_prompt` subscribers and their patterns:

| Module | Section Key | Format | Priority | Pattern |
|--------|-------------|--------|----------|---------|
| Core (identity.md) | `identity` | Markdown | First (hardcoded) | File read |
| Core (context.md) | `context` | Markdown | Last (hardcoded) | File read |
| Skills | `skills` | XML | 90 | `format_for_prompt()` on registry |
| Policy | `policy` | Markdown | 100 | File read (policy.md) |
| Planning | `planning` | Markdown | 100 | Dynamic (pending tasks) |
| Memory | `notes` + `memory_guidance` | Markdown | 100 | Dynamic |
| Bio Memory | `memory_context` | XML | 100 | Dynamic (context builder) |
| Messaging | `messaging` → `teams` | Markdown → XML | 50 | Dynamic (will add roster) |

**New sections to add**:
- `tools` — Tool catalog (XML, on ToolRegistry, follows skills pattern)
- `teams` — Rename from `messaging`, add roster alongside existing messaging context

The skills pattern (`format_for_prompt()` with XML, subscribed at priority 90) is the exact model. Tools should follow identically.

#### Implementation Considerations

1. **`when_to_use` should support negative guidance**: "Use for X. Do NOT use for Y." This is the highest-impact pattern from 2025-2026 production systems.
2. **MCP tool provenance**: Tag `source="mcp:{server_name}"` on MCP tools. Consider stripping XML-like patterns from MCP descriptions (beyond just escaping) since they cross a trust boundary.
3. **Cache prompt string, not formatted lines**: Store the final joined string, not the list of lines. One string comparison for cache hit check.
4. **Alphabetical ordering within XML**: Sort tools by name for deterministic output. Aids prompt caching (cache_control breakpoints work best with stable prefixes).
5. **Budget monitoring**: Log token count of each catalog rebuild. Alert if tool catalog exceeds 1000 tokens (indicates tool sprawl). This is observability, not enforcement.

#### Sources

- [Anthropic Advanced Tool Use](https://www.anthropic.com/engineering/advanced-tool-use)
- [Anthropic Effective Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Anthropic Prompt Injection Defenses](https://www.anthropic.com/research/prompt-injection-defenses)
- [Anthropic Tool Search Docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
- [Anthropic XML Tags Docs](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/use-xml-tags)
- [Elastic Security Labs — MCP Attack Vectors](https://www.elastic.co/security-labs/mcp-tools-attack-defense-recommendations)
- [Palo Alto Unit 42 — MCP Sampling Attacks](https://unit42.paloaltonetworks.com/model-context-protocol-attack-vectors/)
- [OWASP LLM Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
- [arXiv 2411.10541 — Prompt Formatting Impact](https://arxiv.org/html/2411.10541v1)
- [arXiv 2502.02533 — Multi-Agent Design Optimization](https://arxiv.org/abs/2502.02533)
- [arXiv 2512.23557 — Trustworthy Agentic AI Framework](https://arxiv.org/html/2512.23557v1)
- [arXiv 2601.17548 — Prompt Injection on Coding Assistants](https://arxiv.org/html/2601.17548v1)
- [Google ADK Multi-Agent Patterns](https://google.github.io/adk-docs/agents/multi-agents/)
- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/agents/)
- [Claude Code System Prompts (Piebald AI)](https://github.com/Piebald-AI/claude-code-system-prompts)
- [AWS Prescriptive Guidance — Prompt Injection](https://docs.aws.amazon.com/prescriptive-guidance/latest/llm-prompt-engineering-best-practices/best-practices.html)
- [Airia — Lethal Trifecta](https://airia.com/ai-security-in-2026-prompt-injection-the-lethal-trifecta-and-how-to-defend/)

---

## ArcTeam Memory — Build Decisions (2026-02-21)

**Phase**: build | **Status**: complete | **Total decisions**: 31 (26 user, 5 auto-applied)
**Priority framework**: simplicity > security > scalability > compliance
**Design doc**: `packages/arcagent/.claude/ARC-Memory-System-v2.1-Final.md` (Section 7)

### Summary

Team memory is the shared knowledge graph — "Confluence for the team." Wiki-linked markdown entity files, searchable via BM25 with adaptive graph traversal. `TeamMemoryService` is a standalone service in arcteam, framework-agnostic (usable by arcagent, langchain, crewai, etc.). arcagent connects via a thin Module Bus adapter. Consolidation is LLM-driven (via arcllm), triggered automatically by the first agent to start a session when consolidation is due.

### Auto-Applied (Federal Mandates)

| # | Decision | Mandated Answer | Citation |
|---|----------|----------------|----------|
| D-088 | Audit trail on team memory operations | Every read/write/search logged | NIST 800-53 AU-2 |
| D-089 | Audit log integrity | Chained HMAC (existing AuditLogger) | NIST 800-53 AU-9 |
| D-090 | Data classification labeling on stored memory | Required on all entities | NIST 800-53 RA-2 |
| D-091 | Encryption at rest for memory store | AES-256 (tier-gated) | NIST 800-53 SC-28, FIPS 140-3 |
| D-092 | Memory content in transit between agents | mTLS required | NIST 800-53 SC-8 |

### Architecture

#### D-093: Team Memory Core Concept
**Decision**: Shared knowledge base — team-level persistent store with shared context, notes, entity index. Members contribute, coordinator curates. Independent from per-agent memory.
**Priority**: Simplicity — cleanest separation of concerns, no coupling to arcagent memory internals.
**Alternatives**: Message-derived memory (rejected: implicit, hard to debug), Federated agent memory (rejected: couples to agent internals), Hybrid (rejected: too complex for Phase 1).
**Tiers**: All tiers same.

#### D-094: Storage Location
**Decision**: Reuse existing `StorageBackend` for decisions JSONL. New `MemoryStorage` layer for entity markdown files.
**Priority**: Simplicity — consistent with messaging pattern for structured data, proper markdown handling for entities.
**Alternatives**: All through StorageBackend (rejected: muddies JSON protocol with markdown), All new storage (rejected: duplicates existing patterns).
**Tiers**: All tiers identical. Encryption at rest handled by policy layer.

#### D-095: Storage Format
**Decision**: Entity files are actual `.md` files on disk with YAML frontmatter. New `MemoryStorage` class handles markdown+YAML I/O. `StorageBackend` stays for messaging/JSON only.
**Priority**: Simplicity — matches design doc exactly, human-readable, git-diffable.
**Alternatives**: Adapt StorageBackend for markdown (rejected: muddies protocol), JSON with markdown rendering (rejected: breaks "read the file" philosophy).
**Tiers**: Federal adds encryption-at-rest wrapper.

#### D-096: Service Shape
**Decision**: Single `TeamMemoryService` class parallel to `MessagingService`. Methods for entities, playbooks, decisions, search. Consolidation is a separate `ConsolidationEngine` class.
**Priority**: Simplicity — discoverable API, one place to look.
**Alternatives**: Split into EntityGraph + PlaybookStore + DecisionLog (rejected: too many classes), Plugin architecture (rejected: over-abstracted for a file store).
**Tiers**: Same class all tiers. Policy layer gates writes.

#### D-097: Consolidation Trigger
**Decision**: First agent to start a session checks `.last_consolidated` timestamp file. If stale (>24h configurable), runs consolidation with file lock to prevent concurrent runs. Zero daemon, zero cron.
**Priority**: Simplicity — no background processes, no scheduling infrastructure, self-healing.
**Alternatives**: Post-session hook (rejected: "last agent" detection is fragile), Activity-based escalation (rejected: more complex for marginal benefit).
**Tiers**: Federal — consolidation mandatory, blocks session start until complete. Enterprise — runs async, agent proceeds. Personal — optional via config.

#### D-098: LLM Integration
**Decision**: Direct arcllm dependency. arcllm handles provider routing, budgets, model selection. arcteam config has optional `consolidation_model` — if set, passed to arcllm; otherwise arcllm default.
**Priority**: Simplicity — no protocol abstraction layer. arcllm IS the abstraction.
**Alternatives**: Callable protocol (rejected: unnecessary when arcllm already handles routing), Event-based (rejected: over-engineered).
**Tiers**: All tiers use arcllm. Federal gets FIPS-compliant providers via arcllm's tier config.

#### D-099: Promotion Gate Location
**Decision**: `TeamMemoryService.promote()` method in arcteam. Agent decides to promote, calls the method, arcteam validates/audits/writes.
**Priority**: Simplicity — one method call, clear ownership, audit trail in arcteam.
**Alternatives**: Message-based (rejected: async, harder to confirm), Bridge in arcagent (rejected: arcagent shouldn't understand team storage format).
**Tiers**: Federal — requires classification label, blocks without it. Enterprise — warns if missing. Personal — no enforcement.

#### D-100: Wiki-Link Resolution
**Decision**: `_index.json` manifest maps entity_id to relative path. O(1) lookup. Rebuilt on write/delete.
**Priority**: Simplicity — fast, deterministic, already in design doc.
**Alternatives**: Flat namespace (rejected: loses organizational structure), Glob search (rejected: O(n) per resolve).
**Tiers**: Federal adds integrity checksum on index load.

### Search

#### D-101: Search Strategy
**Decision**: Grep + adaptive link traversal. Max hops configurable (default 3). Each hop evaluated — if BM25 score drops below threshold, traversal stops for that branch. Prevents flooding context with irrelevant traversals.
**Priority**: Simplicity — grep is zero deps, adaptive stopping prevents wasted tokens.
**Alternatives**: FTS5 (deferred to later phase), Pluggable backend protocol (rejected: premature abstraction).
**Tiers**: Federal audit-logs every search query + results returned.

#### D-102: Hop Relevance Scoring
**Decision**: BM25 (Okapi BM25) scoring for traversal relevance. Lightweight index of entity files, scored per hop. Below threshold stops that branch. Handles document length variation well.
**Priority**: Simplicity — well-understood algorithm, ~50 lines or rank-bm25 lib, better than raw grep without LLM cost.
**Alternatives**: TF-IDF (rejected: BM25 handles doc length better), LLM per hop (rejected: too expensive), Jaccard (rejected: weaker at term importance).
**Tiers**: All tiers same scoring.

#### D-103: Search Response Shape
**Decision**: `list[SearchResult]` Pydantic models with entity_id, path, snippet, score, hops, entity_type, tags. Caller formats for injection.
**Priority**: Simplicity — clean data boundary, caller has full control.
**Tiers**: Federal adds `classification` field to SearchResult.

### Data Model

#### D-104: Index Schema
**Decision**: Rich _index.json — includes id, path, type, tags, links_to, linked_from, summary snippet, last_updated, status. Enables graph traversal and consolidation cluster selection from index alone.
**Priority**: Scalability — fewer file reads during search and consolidation.
**Tiers**: Federal adds `classification` field per entry.

#### D-105: Entity Type to Directory Mapping
**Decision**: entity_type maps to subdirectory. Generic defaults (person, organization, project, domain, process). Custom types configurable via TOML.
**Priority**: Simplicity — small generic set, extensible.
**Alternatives**: Flat directory (rejected: loses organization), User-defined freeform (rejected: unpredictable).
**Tiers**: All tiers same.

#### D-106: Playbooks & Decisions Storage
**Decision**: Playbooks are entities (entity_type='playbook'), stored as markdown, searchable, consolidatable. Decisions are append-only JSONL via existing StorageBackend.
**Priority**: Simplicity — two storage modes matching two write patterns.
**Tiers**: Federal — decisions JSONL gets chained HMAC. Enterprise/Personal — no chain enforcement.

### Observability & Telemetry

#### D-107: Event Flow
**Decision**: Audit-only for Phase 1. Memory operations log to AuditLogger. No messaging events. Agents discover updates on next retrieval. Messaging events deferred to Phase 3/NATS.
**Priority**: Simplicity — zero coupling between memory and messaging systems.
**Tiers**: Federal requires audit. Enterprise/Personal same.

#### D-108: Telemetry Scope
**Decision**: Full telemetry from start — AuditLogger for compliance, structured logging for operations, OpenTelemetry spans for distributed tracing. arcteam gets OTEL dependency.
**Priority**: Security — full observability required for federal. "Can this be audited?"
**Tiers**: Federal — all required, 100% sampling. Enterprise — configurable sampling. Personal — audit optional, OTEL opt-in.

### Security & Compliance

#### D-109: Classification Access Control
**Decision**: Agent has max_classification in config. TeamMemoryService checks entity classification against agent clearance on every read/search. Entities above clearance are invisible.
**Priority**: Security — zero-trust, least-privilege. NIST 800-53 AC-3.
**Tiers**: Federal — hard block. Enterprise — warn + block. Personal — classification ignored.

#### D-110: Promotion Validation
**Decision**: promote() validates schema (Pydantic), classification label presence. UNCLASSIFIED auto-approved. CUI+ queued for human approval (async). All promotions audit-logged.
**Priority**: Security — human-in-the-loop for sensitive data entering shared knowledge graph (OWASP ASI-09).
**Tiers**: Federal — human approval for CUI+ required. Enterprise — configurable. Personal — all auto-approved.

#### D-111: Approval Queue
**Decision**: CUI+ promotion requests sent as messages to `memory-approval` channel via existing MessagingService. Approval/rejection sent as reply messages.
**Priority**: Simplicity — reuses existing messaging infrastructure, no new storage mechanism.
**Tiers**: Federal — channel always exists, promotions blocked until approved. Enterprise — configurable. Personal — no channel.

### Integration

#### D-112: Agent Wiring
**Decision**: `TeamMemoryService` in arcteam is fully standalone — any framework calls it directly. For arcagent, a thin `TeamMemoryBridge` module hooks Module Bus events and delegates to TeamMemoryService. arcteam has zero knowledge of arcagent.
**Priority**: Simplicity + Scalability — clean separation, future-proof for langchain/crewai.
**Tiers**: All tiers same wiring.

#### D-113: Concurrency
**Decision**: Reads are lock-free. Writes use `fcntl.flock` per entity file. Consolidation uses global `.consolidation.lock`. Same pattern as existing StorageBackend.
**Priority**: Simplicity — zero new deps, proven pattern, self-cleaning.
**Tiers**: All tiers same.

### Performance

#### D-114: Index Freshness
**Decision**: Lazy rebuild with dirty flag. Writes touch `.dirty` marker. Next read checks and rebuilds if dirty. Amortizes cost to readers.
**Priority**: Simplicity + Performance — clean separation of write and index concerns.
**Tiers**: All tiers same.

#### D-115: Budget Enforcement
**Decision**: arcteam enforces per-entity file budget (800 tokens) during writes and consolidation. Retrieval budget (3000 tokens) is caller's responsibility. Clean ownership split.
**Priority**: Simplicity — each package enforces what it owns.
**Note**: Aligned with arcagent brainstorm RT-5 (retrieval budget enforcement).
**Tiers**: All tiers same.

### Extensibility

#### D-116: Disabled Behavior
**Decision**: Null Object pattern. When `enabled = false`, service returns empty results, no-op on writes. Callers never need conditionals.
**Priority**: Simplicity — zero error handling needed in callers.
**Tiers**: All tiers same.

### Testing

#### D-117: LLM Testing Approach
**Decision**: Fixture-based pre-recorded LLM responses per test scenario. Mock LLM matches prompts to fixtures. Small set of integration tests hit real LLM (marked slow, CI-optional).
**Priority**: Simplicity — deterministic, fast, reproducible, zero LLM cost.
**Tiers**: All tiers same.

#### D-118: Test Strategy
**Decision**: 70% unit / 20% integration / 10% e2e. Security tests in dedicated directory. Matches CLAUDE.md quality gates.
**Priority**: Compliance — matches established project standards (>=80% line, >=75% branch, >=90% core).
**Tiers**: All tiers same.

### Open Questions
- None. All categories addressed.

### Related Design Doc
- `packages/arcagent/.claude/ARC-Memory-System-v2.1-Final.md` — Full ARC Memory System design (agent + team)

### Research Insights (via /deepen)

**Date**: 2026-02-21
**Topics**: 6 parallel research areas (BM25, wiki-link traversal, YAML/markdown I/O, LLM consolidation, classification access control, file lock concurrency)
**Sources**: rank-bm25 docs, NIST 800-53 rev5 (AC-3, AU-2, RA-2), python-frontmatter, Okapi BM25 literature, fcntl man pages, existing codebase patterns (StorageBackend, Bio-Memory /deepen)

---

#### BM25 Search Research Insights

**rank-bm25 is the right choice.** Pure Python, ~50 LOC internally, zero compiled deps. API: `BM25Okapi(corpus)` where corpus is `list[list[str]]` (tokenized docs). `get_scores(query_tokens)` returns numpy array of scores. `get_top_n(query_tokens, documents, n)` returns ranked results. Memory: ~1KB per doc for 1000 docs = ~1MB total. Negligible.

**Threshold selection is empirical, not universal.** BM25 scores are not normalized (range depends on corpus). Best practice: use relative threshold, not absolute. **Recommended**: take the max score from initial grep results, set branch-stopping threshold at `0.3 * max_score`. This adapts to query specificity. An absolute threshold (e.g., 1.0) fails because BM25 scores scale with IDF — rare terms score high, common terms score low.

**Rebuild vs persist.** For <1000 markdown files, rebuild on search is fast (<50ms). Persisting the BM25 index adds complexity (pickle/JSON serialization, invalidation on writes) for minimal gain. **Recommended**: rebuild per search call, lazy-load tokenized corpus from dirty-flag-gated cache. Cache the tokenized corpus (not scores), invalidate on dirty flag.

**Markdown preprocessing matters.** Strip YAML frontmatter before indexing body text. Strip markdown syntax (`#`, `*`, `**`, `` ` ``). Keep wiki-link text (`[[entity-name]]` → `entity-name`). Remove code blocks entirely (they pollute term frequencies with variable names). Use simple tokenization: lowercase, split on whitespace + punctuation, no stemming needed for entity-name matching.

**Edge cases:**
- Empty query → return empty, don't score (division by zero in IDF)
- Single-token query → BM25 still works, but consider exact-match boost
- Very long documents → BM25 naturally handles via length normalization (k1=1.5, b=0.75 defaults are good)
- Code-heavy docs → strip code blocks, index only prose sections

**rank-bm25 vs from-scratch.** rank-bm25 is 47 lines of core code. Writing from scratch saves a dependency but gains nothing. The library handles edge cases (empty corpus, zero-length docs) that a hand-roll might miss. **Recommendation**: use rank-bm25 with `BM25Okapi` (not `BM25Plus` or `BM25L`). Okapi is the standard.

---

#### Wiki-Link Graph Traversal Research Insights

**Regex for wiki-links.** Pattern: `r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]'` — captures `entity_id` in group 1 and optional `display_text` in group 2. Edge cases to handle: (1) skip matches inside fenced code blocks (``` regions), (2) skip matches inside inline code (`` ` ``), (3) handle escaped brackets `\[\[` gracefully. Best approach: strip code blocks before regex, not with negative lookbehind (complex, fragile).

**BFS is correct for relevance-bounded traversal.** BFS explores closest neighbors first — when you stop a branch at low relevance, you've already covered the most connected nodes. DFS would dive deep into one branch before visiting close neighbors, wasting budget on distant irrelevant nodes. **Recommended**: BFS with per-node scoring.

**Bidirectional traversal strategy.** Forward links (links_to) represent "this entity references that." Backlinks (linked_from) represent "that entity references this." For search: traverse forward links first (author → their projects), then backlinks (project → who else works on it). In practice, traverse both in BFS, treating the graph as undirected. The BM25 threshold handles relevance — direction matters less than content match.

**Cycle detection.** Simple `visited: set[str]` is sufficient. For 1000 entities × 5 links avg = 5000 edges, the visited set is trivially small. No need for Tarjan's or Floyd's. **Worst-case BFS with max_hops=3**: 1 + 5 + 25 + 125 = 156 nodes visited (with no deduplication). With deduplication and early stopping, typically 10-30 nodes.

**Adaptive stopping implementation:**
```
threshold = 0.3 * max_initial_score
queue = [(entity_id, hop_count) for each initial result]
while queue:
    entity_id, hops = queue.popleft()
    if hops >= max_hops or entity_id in visited: continue
    visited.add(entity_id)
    score = bm25.score(entity_content, query)
    if score < threshold: continue  # prune this branch
    results.append((entity_id, score, hops))
    for linked_id in entity.links_to + entity.linked_from:
        queue.append((linked_id, hops + 1))
```

**Link consistency on write.** When entity A adds `[[B]]`:
1. A's frontmatter gets `links_to: [B]`
2. B's frontmatter gets `linked_from: [A]` added
3. Both updates happen in same write transaction (or rebuild index with dirty flag)

**Simpler alternative**: don't maintain `linked_from` in individual files. Compute backlinks from `_index.json` which already stores all `links_to` relationships. O(N) scan of index, but only during search — not on every write. **Recommended**: this approach. It eliminates write-time cross-file updates entirely.

---

#### YAML Frontmatter + Markdown I/O Research Insights

**python-frontmatter is reliable.** `frontmatter.load(path)` returns `Post` object with `.metadata` (dict) and `.content` (str). `frontmatter.dumps(post)` serializes back. Handles multi-line strings, lists, dates, Unicode. One dependency (PyYAML). Battle-tested in static site generators.

**Pydantic validation pattern:**
```python
raw = frontmatter.load(path)
meta = EntityMetadata.model_validate(raw.metadata)  # Pydantic v2
content = raw.content
```
This gives you type-safe metadata + raw markdown body in two lines.

**Atomic write — match existing FileBackend pattern.** The existing `storage.py:113-123` already does `tempfile.mkstemp + os.replace`. MemoryStorage should use the identical pattern:
```python
fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    os.replace(tmp, path)
except BaseException:
    if os.path.exists(tmp): os.unlink(tmp)
    raise
```

**Token counting.** tiktoken is accurate but requires downloading tokenizer data and adds a dependency. For budget enforcement on entity files (800 token limit), a **word-count heuristic is sufficient**: `len(content.split()) * 1.3 ≈ tokens`. The 1.3 multiplier accounts for subword tokenization. For consolidation prompts where exact counts matter, use arcllm's token counting if available. **Recommendation**: word-count heuristic for write-time budget checks, exact counting deferred to consolidation.

**Frontmatter edge cases:**
- Special characters in YAML: strings with `:`, `#`, `{`, `}` must be quoted. python-frontmatter handles this automatically via PyYAML's `safe_dump`.
- Multi-line strings: use YAML literal blocks (`|`) for summaries. python-frontmatter handles via `default_flow_style=False`.
- Lists in frontmatter: `links_to: [entity-a, entity-b]` — PyYAML handles natively.
- Dates: use ISO 8601 strings, not YAML date type (avoids timezone issues).

**Index rebuild without reading full bodies.** python-frontmatter has no "metadata-only" mode. Two options:
1. Read first ~20 lines until `---` closing delimiter (fast, ~0.1ms/file)
2. Use regex to extract between `---` delimiters without full parse

**Recommended**: option 1 — read file, split on first two `---`, parse only the YAML block. Skip body entirely. For 1000 files: ~100ms total.

**Encoding.** Always `encoding="utf-8"`. No BOM handling needed — python-frontmatter strips BOM if present. Normalize newlines to `\n` on write (avoid CRLF in cross-platform scenarios).

---

#### LLM Consolidation Engine Research Insights

**arcllm integration pattern.** Call `load_model(model_name)` from arcllm to get an adapter. Use adapter.invoke() with structured messages. If `consolidation_model` is set in team memory config, pass it to `load_model()`. Otherwise, `load_model()` uses arcllm's default routing.

**Entity rewrite prompt pattern (from Bio-Memory research):**
```
You are updating a knowledge base entity file.

Current file:
{current_content}

New information from recent sessions:
{new_episodes}

Rules:
1. Integrate new facts into the existing structure
2. For each fact you drop, state why (superseded, redundant, or contradicted)
3. Preserve all wiki-links [[entity-id]] that still reference valid entities
4. Stay under {budget} words (~{token_budget} tokens)
5. Do not invent facts not present in the source material
6. Output ONLY the updated markdown body (no frontmatter)
```

**Batch processing.** Sequential is safer for Phase 1. Parallel introduces coordination complexity (two entities referencing each other during simultaneous rewrite). **Recommended**: process entities sequentially, prioritized by: (1) staleness (oldest `last_updated` first), (2) number of pending episodes, (3) link count (highly-connected entities first).

**Crash safety — write-ahead manifest.**
```
1. Write manifest: pending_entities.json = [entity_a, entity_b, ...]
2. For each entity: rewrite → atomic write → remove from manifest
3. On restart: read manifest → re-process remaining entities
4. On complete: delete manifest, update .last_consolidated timestamp
```
This matches the existing FileBackend pattern. The manifest is the crash recovery checkpoint.

**Cost estimation.** Per entity rewrite: ~500 input tokens (current) + ~500 (episodes) + ~200 (prompt) = ~1200 input + ~800 output. At $3/M input, $15/M output (Claude Sonnet): ~$0.016/entity. For 100 entities: ~$1.60 per consolidation. **Recommendation**: log estimated cost before running, configurable max-cost-per-consolidation.

**Diff-based consolidation.** Only send entities with pending episodes (flagged during promote()). Track `last_consolidated_at` per entity in frontmatter. Compare against episode timestamps. Skip unchanged entities. From Bio-Memory research: content-hash gating gives 80-90% reduction.

**Link discovery.** Two-phase approach from Bio-Memory research: (1) entity rewrite discovers links within episode context naturally, (2) optional graph pass discovers cross-domain links by showing entity summaries to LLM. Phase 2 is expensive — defer to Phase 2 of implementation.

**Validation of LLM output.** Before writing:
1. Parse output as markdown (no syntax errors)
2. Check word count against budget (reject if >110% of budget)
3. Verify no frontmatter in output (prompt says "no frontmatter")
4. Extract wiki-links from output, verify all reference valid entities in index
5. If validation fails: retry once with explicit error, then skip entity and log warning

---

#### Classification Access Control Research Insights

**US Government classification hierarchy (codified):**
```python
class Classification(IntEnum):
    UNCLASSIFIED = 0
    CUI = 1          # Controlled Unclassified Information (NIST SP 800-171)
    CONFIDENTIAL = 2
    SECRET = 3
    TOP_SECRET = 4
```
CUI is not a classification level per se — it's a handling category for unclassified info that requires safeguarding. In practice, it sits between UNCLASSIFIED and CONFIDENTIAL for access control purposes. Sub-levels (TS//SCI, TS//SAP) exist but are handled as TOP_SECRET for our purposes.

**NIST 800-53 AC-3 (Access Enforcement):**
- "The system enforces approved authorizations for logical access to information and system resources"
- Requires: (1) defined access control policy, (2) enforcement mechanism, (3) audit of enforcement decisions
- For file-based: every read/search must check agent clearance vs entity classification. Denied access must be audit-logged (AU-2 cross-reference).

**Mixed-classification search results.** NIST guidance: **filter silently, log the denial.** Do not inform the requesting agent that higher-classified results exist (that itself is information leakage — "there IS something classified about this topic"). Return count of results found, not count of results filtered. The audit log captures the filtered results for compliance review.

**Classification inheritance during traversal.** An UNCLASSIFIED entity linking to a CUI entity does NOT inherit CUI classification. However, during graph traversal, if following a link would lead to a CUI entity and the agent lacks CUI clearance, that branch is pruned. The link itself (the fact that a connection exists) is at the classification of the linking entity. **Recommendation**: prune at traversal time, don't propagate classification upward.

**Downgrade/declassification.** Requires human approval at all tiers. Classification can only be lowered, never raised automatically (an agent cannot classify something as SECRET — only a human can). Audit trail must record: who downgraded, when, from what to what, authorization reference.

**Tier-gated enforcement pattern:**
```python
def check_access(entity_cls: Classification, agent_cls: Classification, tier: str) -> bool:
    if tier == "personal":
        return True  # no enforcement
    if entity_cls <= agent_cls:
        return True
    if tier == "enterprise":
        logger.warning("Access denied: %s > %s", entity_cls, agent_cls)
    # federal: silent deny + audit
    audit_log.log("access_denied", entity=entity_id, agent=agent_id, reason="classification")
    return False
```

**Data spillage detection.** Two approaches:
1. **Write-time**: when promoting content, scan for patterns (CUI markings, classification banners) in content destined for lower-classified entities. Regex-based.
2. **Consolidation-time**: LLM prompt includes instruction "flag any content that appears to be classified higher than {entity_classification}."
**Recommendation**: regex patterns at write-time (zero cost), LLM check during consolidation (amortized cost).

---

#### File Lock Concurrency Research Insights

**fcntl.flock behavior.** On macOS and Linux: advisory locks (processes must cooperate). `LOCK_EX` blocks until acquired. **Auto-releases on file descriptor close AND on process death.** This means crashed processes don't leave stale locks — the kernel cleans up. Threads sharing an fd share the lock (no intra-process exclusion). This is fine for asyncio since we use `asyncio.to_thread()` which runs in a thread pool — each thread gets its own fd via `open()`.

**NFS caveat.** `flock()` does NOT work on NFS (some implementations silently succeed without locking). For NFS: use `fcntl.lockf()` instead. For our use case (local filesystem): `flock` is correct.

**Per-entity-file lock pattern (matching existing FileBackend).** Lock the entity file itself — no separate `.lock` files needed. The existing `storage.py:136-143` pattern is correct:
```python
with open(entity_path, "ab") as f:
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    try:
        # write via tempfile + os.replace
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
```
For new entity files (file doesn't exist yet): create parent dir, then lock on the tempfile or use directory-level lock briefly. **Simpler**: use `open(entity_path, "a+b")` which creates the file if missing.

**asyncio.to_thread safety.** Two coroutines in the same process calling `await asyncio.to_thread(write_with_lock, path)` will run in separate threads. Each thread opens its own fd → gets its own lock → flock serializes them correctly. No deadlock risk because each lock is on a single file. **Deadlock possible only if**: one thread holds lock A and waits for lock B while another holds B and waits for A. Our pattern is one lock per operation → no deadlock.

**Lock timeout.** `flock(LOCK_NB)` returns immediately with `BlockingIOError` if lock unavailable. **Recommended pattern**:
```python
for attempt in range(max_retries):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        await asyncio.sleep(0.1 * (attempt + 1))
else:
    raise TimeoutError(f"Could not acquire lock on {path}")
```
Default: 5 retries × backoff = ~1.5s max wait. If a write takes >1.5s something is seriously wrong.

**Consolidation global lock.** `.consolidation.lock` file with `flock(LOCK_EX | LOCK_NB)`. If lock unavailable → another process is consolidating → skip. This is the "first agent wins" pattern from D-097.

**_index.json rebuild atomicity.** Same tempfile+rename pattern. Multiple writers setting dirty flag simultaneously is fine — flag is idempotent (file exists = dirty). Rebuild reads all entity frontmatter, writes new index atomically. If two processes rebuild simultaneously: both produce correct output, last writer wins (os.replace is atomic). No corruption possible.

---

#### Cross-Cutting Insights

**Alignment with Bio-Memory /deepen.** The Bio-Memory research (already in this log) directly supports several arcteam-memory decisions:
- Frontmatter-first search (Data Model Insights) validates our two-pass approach
- Crash safety via `os.replace() + os.fsync()` for single files, write-ahead manifest for multi-file (Integration Insights)
- Content-hash gating for 80-90% consolidation cost reduction (Performance Insights)
- Layered defense model (Security Insights) maps to our classification + audit approach
- BFS traversal with visited set matches Zep/Graphiti patterns

**New risks discovered:**
1. **BM25 threshold gaming.** An attacker who understands the scoring model could craft entity content to always score high (keyword stuffing). Mitigation: per-entity content validation at write time, word frequency caps.
2. **Consolidation prompt injection.** Malicious content in entity files becomes part of consolidation prompts. Mitigation: randomized boundary markers per consolidation run (from Bio-Memory research), content sanitization before prompt construction.
3. **Classification downgrade via consolidation.** If entity A (CUI) is consolidated and rewritten, the LLM might produce output without CUI markers. Mitigation: frontmatter classification is NEVER changed by consolidation — only body content is rewritten. Classification changes require human approval (D-110).
4. **Index poisoning.** Corrupt `_index.json` could route queries to wrong entities. Mitigation: integrity checksum on index (federal tier), rebuild from source files on checksum mismatch.

**Implementation priority (based on research):**
1. MemoryStorage (YAML+markdown I/O) — foundation everything depends on
2. Entity model + _index.json — data layer
3. BM25 search + grep — retrieval
4. Wiki-link traversal — graph layer
5. Promotion gate + classification — security layer
6. ConsolidationEngine — LLM integration
7. TeamMemoryBridge (arcagent adapter) — integration layer

---

## Feature: Scheduling / Heartbeat / Cron MVP

**Date**: 2026-02-16
**Source Design**: `packages/arcagent/docs/arcagent-design-v3.md` Section 4
**Goal**: Build the scheduling system that lets agents manage their own recurring work

### Decisions

| # | Category | Decision | Choice | Rationale |
|---|----------|----------|--------|-----------|
| D-119 | Architecture | Where does the scheduler live? | **Module (via Module Bus)** | Keeps core under LOC budget. Opt-in via config. Lives in `arcagent/modules/scheduler/`. |
| D-120 | Scope | Which schedule types ship? | **All three: interval + cron + once** | Full ScheduleEntry from design doc. Complete from day one. |
| D-121 | Data Model | Schedule storage format? | **Pydantic models + JSON file** | `workspace/schedules.json`. Atomic writes (tmp+rename). Git-diffable, auditable, zero infrastructure. |
| D-122 | Execution | What happens when a schedule fires? | **agent.run(prompt) with new session** | Each fire creates a fresh session, runs prompt through full agent loop (tools, memory, context). |
| D-123 | Agent Tools | Schedule management tools? | **Full CRUD: create/list/update/cancel** | Agent fully owns its schedule lifecycle. 4 tools exposed to LLM. |
| D-124 | Constraints | Active hours and limits? | **Active hours + timeout. No max_retries.** | Active hours critical for heartbeats. arcllm handles LLM retries. Schedule-level retries deferred. |
| D-125 | Concurrency | Overlapping executions? | **Queue and run sequentially** | FIFO queue. Nothing missed, no concurrency issues. |
| D-126 | Lifecycle | When does the scheduler run? | **Standalone daemon (`arc agent serve`)** | Agent stays warm in-process. Schedules fire even when nobody is chatting. Fresh session per execution. |
| D-127 | Config | Where are schedules defined? | **Runtime only (schedules.json)** | No TOML seeds. Agent creates via tools or operator edits JSON. Single source of truth. |
| D-128 | Audit | What audit trail? | **Module Bus events + metadata update** | Emit schedule:fired/completed/failed/skipped. Update ScheduleEntry metadata. Flows to existing telemetry. |
| D-129 | Dependencies | Cron expression parser? | **croniter** | Proven library. Handles DST, leap years, edge cases. Used by Airflow, Celery. |
| D-130 | Testing | Testing strategy? | **Unit (frozen time) + integration (mock LLM)** | Full scheduling logic coverage plus end-to-end with actual agent loop and mock provider. |

### Research Insights (via /deepen)

**Enriched**: 2026-02-16 | **Sources**: 3 parallel research agents (asyncio patterns, codebase analysis, security edge cases)

#### D1 — Module Architecture: Integration Patterns from Codebase

The existing module system has a precise DI contract. A scheduler module must follow:

1. **MODULE.yaml** at `arcagent/modules/scheduler/MODULE.yaml` — entry_point must start with `arcagent.modules.` (enforced at `module_loader.py:147`, ASI-04 protection)
2. **Constructor DI** — parameters must match the `available` dict in `module_loader.py:190-196`: `config`, `eval_config`, `llm_config`, `telemetry`, `workspace`
3. **Config lookup** — `getattr(ctx.config, manifest.name, None)` means `ArcAgentConfig.scheduler: SchedulerConfig` must exist as a field
4. **Module Protocol** — must satisfy `Module` Protocol: `name` property, `async startup(ctx)`, `async shutdown()`
5. **TOML enablement** — requires `[modules.scheduler]` with `enabled = true`

The memory module (`markdown_memory.py`) is the closest analog — same constructor pattern, same startup pattern (subscribe to bus events + register tools).

#### D3 — JSON Storage: Atomic Write Patterns

**Critical pattern**: `os.replace()` is atomic on all platforms. Temp file must be in same directory (same filesystem).

```python
fd, tmp_path = tempfile.mkstemp(dir=filepath.parent, prefix=f".{filepath.name}.", suffix=".tmp")
os.write(fd, json_bytes)
os.fsync(fd)  # Force to disk before rename
os.close(fd)
os.replace(tmp_path, filepath)  # Atomic swap
```

**TOCTOU risk**: Read-modify-write on `schedules.json` is NOT atomic even with `os.replace()`. Since our scheduler is single-process sequential, this is safe for MVP. If we ever go multi-process, need `fcntl.flock()` or advisory locks.

**Edge case from Celery Beat**: Race condition between schedule sync and schedule update caused production crashes (django-celery-beat #158). Our sequential queue design avoids this.

#### D4 — Execution: agent.run() Interface

From codebase analysis, `agent.run(task)` returns `LoopResult`:
- `content: str | None` — final response
- `turns: int` — number of loop iterations
- `tool_calls_made: int` — total tool invocations
- `tokens_used: dict` — token breakdown
- `cost_usd: float` — dollar cost
- `events: list` — event history

This gives us everything needed for schedule metadata: `last_result`, `run_count`, cost tracking per execution.

#### D5 — Tools: Registration Pattern

Tools use `RegisteredTool` with `**kwargs` execute (NOT `(args, ctx)` like arcrun):

```python
RegisteredTool(
    name="schedule_create",
    description="Create a new schedule",
    input_schema={...},  # JSON Schema
    transport=ToolTransport.NATIVE,
    execute=self._handle_create,  # async **kwargs
)
ctx.tool_registry.register(tool)
```

Register during `startup()`, same as memory module's `_register_search_tool()`.

#### D6 — Constraints: Resource Exhaustion Guardrails

**Critical security finding**: Autonomous scheduled agents are a prime target for resource exhaustion attacks.

Recommended guardrails (from OWASP LLM10 + agentic security research):

| Guardrail | Threshold | Rationale |
|-----------|-----------|-----------|
| Max schedules per agent | 50 | Prevents schedule bombing |
| Minimum interval | 300s (5 min) | Prevents token drain |
| Prompt max length | 500 chars | Limits injection surface |
| Token budget per execution | 10,000 | Prevents runaway sessions |
| Execution timeout | 120s | Unattended must complete quickly |
| Loop detection window | 5 actions | Same tool+args 2x = kill |

**Circuit breaker**: After 3 consecutive failures, open circuit for 5 minutes. Emit `schedule:circuit_open` event.

#### D7 — Sequential Queue: asyncio.Queue Pattern

`asyncio.Queue` with `asyncio.wait_for()` is confirmed race-condition free by CPython maintainers. Pattern:

```python
item = await asyncio.wait_for(self.queue.get(), timeout=1.0)  # Won't lose items
# ... process ...
self.queue.task_done()  # Signals completion for queue.join()
```

Use `queue.join()` during graceful shutdown to drain pending work.

#### D8 — Daemon: Graceful Shutdown Pattern

**Do NOT use `asyncio.run()`** — it auto-cancels tasks without control. Instead:

```python
loop = asyncio.new_event_loop()
for sig in (signal.SIGTERM, signal.SIGINT):
    loop.add_signal_handler(sig, handle_signal)
loop.run_until_complete(startup())
loop.run_forever()  # Until shutdown
loop.run_until_complete(shutdown())
loop.close()
```

**Memory management**: Use `asyncio.BoundedSemaphore` to limit concurrent operations. Monitor with `psutil` health checks every 60s. Alert at 500MB.

**CLI pattern**: Click sync command wraps async function. Follows `arc agent run` pattern at `arccli/agent.py`.

#### D10 — Audit: NIST 800-53 AU-2/AU-3 Compliance

**AU-3 requires 7 fields** in every audit record:

1. **Event type** — `schedule.created`, `schedule.fired`, `schedule.completed`, `schedule.failed`
2. **Timestamp** — ISO 8601 with timezone (NTP-synced)
3. **Location** — component name + process ID
4. **Source** — agent DID + originating service
5. **Outcome** — SUCCESS, FAILURE, DENIED, ERROR
6. **Associated identities** — agent DID, tools invoked, human user (if applicable)
7. **Classification** — data sensitivity level

**Additional requirements**:
- Tamper-evident records (SHA-256 hash per record)
- SIEM integration for AU-6 automated analysis
- Alert on: any DENIED outcome, 3+ failures in 5 min, budget exceeded, circuit breaker trip
- 1-year retention minimum (federal), 7 years for sensitive systems

We already have OpenTelemetry + `AgentTelemetry` — emit structured events through existing pipeline.

#### D11 — croniter: DST Gotcha

**Critical bug**: croniter computes wrong next execution during DST transitions. Fix:

1. Always use `pytz.timezone(tz_name).localize()` — never `datetime(tzinfo=tz)`
2. After any timedelta math, call `tz.normalize()` to handle DST transitions
3. Use tolerance window (30-60s) for scheduler drift
4. Store all times in UTC, convert to local TZ only for cron evaluation

#### Security: Prompt Injection via Schedules

**Three-layer defense** (from OWASP AI Agent Security Cheat Sheet):

1. **Syntactic**: Length limits, character allowlist `[a-zA-Z0-9\s\-_.,;:]`, format validation
2. **Semantic**: Detect injection patterns (`ignore previous`, `disregard`, `system:`, URLs, email addresses)
3. **Provenance**: Tag origin (`user-created` vs `agent-generated` vs `external-data`), require human approval for external-data schedules

Log original unsanitized input separately from sanitized version for forensics.

#### Security: Execution Deduplication

Each schedule fire should generate a unique execution ID (UUID). Pattern from AWS EventBridge:
- `execution_id = f"{schedule_id}:{fire_timestamp_unix}"`
- Check dedup table before executing
- Prevents double-fires if scheduler restarts mid-cycle

### Key Design Principles

- **Agent self-schedules**: The agent writes both the WHEN and the WHAT. It creates cron/interval/once entries with the prompt describing what to do.
- **Module, not core**: Scheduling is opt-in. Module Bus participant. Doesn't bloat the nucleus.
- **Daemon model**: `arc agent serve` runs the scheduler as a long-running process. Agent stays warm. Each schedule fire is an independent `agent.run(prompt)` with a fresh session.
- **Sequential queue**: Overlapping fires queue up. No concurrent executions. FIFO order.
- **Zero infrastructure**: JSON file storage. No database. No message queue. Just files and asyncio.

### Components to Build

1. **`arcagent/modules/scheduler/`** — Module with MODULE.yaml
   - `scheduler.py` — Core scheduler engine (asyncio timer loop, cron evaluation, queue)
   - `models.py` — Pydantic models (ScheduleEntry, ScheduleMetadata, ActiveHours)
   - `store.py` — JSON file persistence (load/save with atomic writes)
   - `tools.py` — 4 agent-facing tools (create/list/update/cancel)

2. **`arcagent/core/config.py`** — Add SchedulerConfig to ArcAgentConfig

3. **`arccli`** — Add `arc agent serve` command

4. **Tests**
   - `tests/unit/modules/scheduler/` — Frozen-time unit tests
   - `tests/integration/` — End-to-end with mock LLM

### Architecture Diagram

```
arc agent serve
    |
    v
ArcAgent (warm, long-running)
    |
    v
SchedulerModule (Module Bus)
    |
    +-- loads workspace/schedules.json
    +-- evaluates timing (croniter for cron, timedelta for interval, datetime for once)
    +-- checks active_hours
    |
    v (when schedule fires)
    |
    +-- emit schedule:fired event
    +-- queue execution
    +-- agent.run(prompt) with fresh session
    +-- emit schedule:completed/failed event
    +-- update metadata (last_run, last_result, run_count)
    +-- persist to schedules.json
```

---

## Feature: Recursive Agent Spawning (ArcRun v1)

**Date**: 2026-02-16
**Source**: `.claude/brainstorms/2026-02-16-recursive-agent-spawning.md`
**Goal**: Enable ArcRun to recursively spawn child agent loops for task decomposition and parallel execution

### Decisions

| # | Category | Decision | Choice | Rationale |
|---|----------|----------|--------|-----------|
| D-131 | Architecture | How does the model express decomposition intent? | **Spawn as a tool (Claude Code pattern)** | No new strategies needed. Model calls `spawn_task` like any tool within the react loop. Simplest extension of existing architecture. |
| D-132 | Architecture | Where does the spawn tool live? | **ArcRun built-in + overridable** | Lives in `arcrun/builtins/spawn.py`. ArcRun is standalone — spawn works without ArcAgent. ArcAgent can override with a richer version (identity, permissions). Same pattern as `execute_python`. |
| D-133 | Architecture | Spawn tool API (model arguments) | **task + system_prompt + tools** | `spawn_task(task, system_prompt, tools)`. Model specializes child role and restricts capabilities. `max_turns` inherits from parent. Strategy not exposed — child uses same selection logic. |
| D-134 | Data Model | How do depth and budgets live on RunState? | **Flat fields** | `depth`, `max_depth`, `parent_run_id`, `token_budget`, `cost_budget` as flat fields on RunState. No special classes. A child is just another `run()` call with depth + 1. |
| D-135 | Security | Shared memory between children? | **Strictly nothing** | Complete isolation. Fresh state per child. Results flow up via LoopResult only. Blast radius containment. |
| D-136 | Security | Cross-child communication? | **Parent only** | No sibling-to-sibling. Clean tree structure: parent spawns, children return results, parent aggregates. |
| D-137 | Performance | Resource budget splitting | **No enforcement in v1** | Depth limit only. Budget fields exist on RunState for observability but aren't enforced. Add enforcement later with real usage data. |
| D-138 | Performance | Parallel vs sequential spawning | **Parallel via multiple tool calls** | When model emits multiple `spawn_task` calls in one turn, run concurrently via `asyncio.gather`. Model naturally expresses parallelism. Requires react loop change for concurrent tool execution. |
| D-139 | Integration | NATS for distributed execution? | **In-process asyncio (v1)** | Children run as asyncio tasks in same process. Design interfaces so NATS is a drop-in later. |
| D-140 | Security | Identity inheritance | **Minimal: run_id lineage only** | Unique `run_id` per child with `parent_run_id` for correlation. DID/auth is ArcAgent's concern when it overrides the spawn tool. |
| D-141 | Error Handling | Child failure behavior | **Error string as tool result** | Same as any failed tool. Parent gets `"Error: child failed — {reason}"` and decides how to proceed. Consistent, no special handling. |
| D-142 | Observability | Event propagation | **Bubble up with prefix** | Child events propagate to parent bus with `child.{run_id}.` prefix. Full observability across the tree. |
| D-143 | Testing | Testing strategy | **Mock model + real spawn** | Mock model that emits `spawn_task` tool calls, with real nested `run()` calls. Tests the full pipeline. |
| D-144 | Scope | V1 scope | **Confirmed MVP** | See scope section below. |

### V1 Scope

**In scope:**
- `spawn_task` built-in tool in `arcrun/builtins/spawn.py`
- Flat depth/budget fields on `RunState`
- `depth` and `max_depth` params on `run()` / `run_async()`
- Depth enforcement (reject spawn if `depth >= max_depth`)
- Event bubbling from child to parent with `child.{run_id}.` prefix
- In-process asyncio execution
- Parallel spawn via multiple tool calls in one turn
- `parent_run_id` on `RunState` for lineage tracking

**Deferred to v2+:**
- NATS distributed execution
- Resource budget enforcement (tokens, cost, time)
- Identity/DID inheritance (ArcAgent override)
- Cross-child communication
- Persistent agents
- Team strategy (coordinated multi-agent with roles)

### Key Design Principles

- **Spawn is a tool, not a strategy** — The react loop doesn't change. It gains a powerful tool.
- **ArcRun is standalone** — Spawn works without ArcAgent. `pip install arcrun` gets you spawning.
- **A child is just another run()** — No special classes. Recursion via flat fields (depth, parent_run_id).
- **Complete isolation** — No shared state between children. Results flow up only.
- **Full observability** — Event bubbling gives parent complete visibility into child execution.

### Components to Build

1. **`arcrun/builtins/spawn.py`** — Built-in spawn tool
2. **`arcrun/state.py`** — Add flat fields (depth, max_depth, parent_run_id, budgets)
3. **`arcrun/loop.py`** — Add depth/max_depth params to run()/run_async()
4. **`arcrun/strategies/react.py`** — Parallel tool execution for spawn calls
5. **Tests** — Mock model + real spawn, depth limits, event bubbling, parallel, errors

### Architecture Diagram

```
run(model, tools, prompt, task, depth=0, max_depth=3)
    |
    v
ReactStrategy (react loop)
    |
    +-- model decides to call spawn_task(task="research X")
    |
    v
spawn_task tool (builtins/spawn.py)
    |
    +-- checks depth < max_depth
    +-- creates child EventBus (bubbles to parent)
    +-- calls run(model, tools, child_prompt, child_task, depth=depth+1)
    |
    v
Child ReactStrategy (independent react loop)
    |
    +-- uses inherited model + tools (or subset)
    +-- can itself call spawn_task (if depth allows)
    +-- returns LoopResult
    |
    v
Parent receives LoopResult.content as tool result string
    |
    +-- continues react loop with child's answer
```

---

## Feature: CDP Browser Module

**Date**: 2026-02-16
**Source**: `.claude/brainstorms/2026-02-16-cdp-browser-module.md`
**Goal**: General-purpose browser interaction module using Chrome DevTools Protocol — agents can navigate, click, type, fill forms, take screenshots, and complete multi-step web tasks (e.g., booking a flight)

### Decisions

| # | Category | Decision | Choice | Rationale |
|---|----------|----------|--------|-----------|
| D-145 | Architecture | CDP client library | **cdp-use (browser-use's CDP layer)** | Type-safe Python CDP bindings auto-generated from Chrome's protocol spec. No agent/LLM baggage from browser-use. Direct CDP over WebSocket. |
| D-146 | Architecture | Module internal structure | **Tool-per-file** | Each tool (navigate, click, type, screenshot, etc.) in its own file. BrowserModule wires them together. Most granular, easiest to test individually. |
| D-147 | Architecture | Tool API surface | **Fine-grained tools** | Individual tools: browser_navigate, browser_click, browser_type, browser_screenshot, browser_read_page, browser_fill_form, browser_execute_js, browser_handle_dialog. LLM picks exactly what it needs. |
| D-148 | Architecture | Element selection strategy | **Hybrid: accessibility-first, CSS fallback** | Primary: accessibility tree snapshot (role + name). Fallback: CSS selectors when accessibility labels are missing. Semantic and robust. |
| D-149 | Architecture | Page state representation | **Accessibility snapshot (structured)** | Return accessibility tree as structured text with role, name, value, and numeric ref IDs for targeting. Compact, LLM-friendly. |
| D-150 | Integration | CDP connection management | **Launch + connect** | Module can optionally launch a headless Chrome process and connect via CDP WebSocket. Self-contained. |
| D-151 | Integration | Tool registration | **Auto-register on module load** | BrowserModule subscribes to agent:startup event, registers all browser tools via ToolRegistry. Tools appear automatically when module is enabled. |
| D-152 | Security | URL access control | **Dual-mode (configurable)** | Config setting: mode = 'allowlist' or 'denylist'. Allowlist for restricted/federal environments, denylist for open ones. Clear security posture per deployment. |
| D-153 | Security | JavaScript execution control | **Enabled by default, toggle in config** | JS execution available as a tool. Config toggle: security.allow_js_execution = true/false. Simple on/off. |
| D-154 | Security | Credential/cookie handling | **Configurable persistence** | Option to persist cookies/sessions to encrypted store for multi-step workflows. Ephemeral by default, opt-in persistence. |
| D-155 | Performance | Timeouts | **Per-tool defaults at registration (ArcRun enforces)** | Set appropriate timeout_seconds per RegisteredTool (navigate=30s, click=5s, screenshot=10s). ArcRun's existing asyncio.wait_for handles enforcement. No duplicate timeout logic. |
| D-156 | Performance | Screenshot format | **PNG base64 inline** | Return as base64-encoded PNG directly in tool result. Vision-capable LLMs process inline. No file management. |
| D-157 | Testing | Testing strategy | **Mock CDP at WebSocket level** | Unit tests mock CDP WebSocket with canned responses. Integration tests use real headless Chrome. Standard test pyramid. |
| D-158 | Configuration | Config schema | **Flat under [modules.browser]** | All config under [modules.browser] with sub-tables for security, timeouts, connection. Matches existing module config pattern. |
| D-159 | Events | Module bus events | **Full action events** | Emit for every action: browser.navigated, browser.clicked, browser.typed, browser.screenshot_taken, browser.js_executed, browser.dialog_handled, browser.connected, browser.disconnected, browser.error. Rich audit stream. |

### Key Design Principles

- **Just tools for ArcRun**: Browser actions are registered tools. ArcRun's loop calls them like any other tool. No special browser-aware logic in the loop.
- **cdp-use for protocol**: Type-safe, auto-generated CDP bindings. No Playwright/Selenium abstraction layer.
- **Security is configurable**: URL allowlist/denylist, JS execution toggle, cookie persistence — all in TOML config under [modules.browser.security].
- **Accessibility-first selection**: Elements identified by accessibility tree (role + name), not fragile CSS selectors.
- **Full audit trail**: Every browser action emits a module bus event. Combined with ToolRegistry's existing audit, complete observability.

### Components to Build

1. **`arcagent/modules/browser/`** — Module directory
   - `MODULE.yaml` — Module manifest
   - `__init__.py` — Public API
   - `config.py` — Pydantic config (BrowserConfig, BrowserSecurityConfig)
   - `errors.py` — Browser-specific errors
   - `browser_module.py` — Module wiring (startup, shutdown, tool registration, events)
   - `cdp_client.py` — CDP connection management (launch Chrome, connect WebSocket)
   - `accessibility.py` — Accessibility tree snapshot and element resolution
   - `tools/` — Tool-per-file directory
     - `navigate.py` — browser_navigate, browser_go_back, browser_go_forward, browser_reload
     - `interact.py` — browser_click, browser_type, browser_select, browser_hover
     - `form.py` — browser_fill_form
     - `read.py` — browser_read_page, browser_get_element_text
     - `screenshot.py` — browser_screenshot
     - `javascript.py` — browser_execute_js
     - `dialog.py` — browser_handle_dialog
     - `cookies.py` — browser_get_cookies, browser_set_cookies
     - `download.py` — browser_download_file
   - `cli.py` — CLI commands for testing/debugging

2. **`arcagent/core/config.py`** — Add BrowserConfig to ArcAgentConfig

3. **Tests**
   - `tests/unit/modules/browser/` — Mock CDP WebSocket tests per tool
   - `tests/integration/` — Real headless Chrome tests

### Architecture Diagram

```
ArcAgent
    |
    v
BrowserModule (Module Bus participant)
    |
    +-- subscribes to agent:startup → registers tools
    +-- subscribes to agent:shutdown → closes CDP connection
    |
    v
CDPClient (cdp-use)
    |
    +-- launches headless Chrome (optional)
    +-- connects via CDP WebSocket
    +-- manages page session
    |
    v
Tools (registered in ToolRegistry)
    |
    +-- browser_navigate(url) → CDP Page.navigate
    +-- browser_click(ref) → CDP DOM + Input
    +-- browser_type(ref, text) → CDP Input.dispatchKeyEvent
    +-- browser_screenshot() → CDP Page.captureScreenshot
    +-- browser_read_page() → CDP Accessibility.getFullAXTree
    +-- browser_execute_js(expression) → CDP Runtime.evaluate
    +-- browser_handle_dialog(action) → CDP Page.handleJavaScriptDialog
    +-- browser_fill_form(fields) → compound: find elements + type
    +-- browser_get_cookies() → CDP Network.getCookies
    +-- browser_download_file(url) → CDP Page.setDownloadBehavior + navigate
    |
    v
ArcRun (loop) — calls tools via ToolRegistry wrapper
    |
    +-- pre_tool event (policy check)
    +-- execute tool (with timeout)
    +-- post_tool event
    +-- audit event
```

### Research Insights (via /deepen)

**Enriched**: 2026-02-16 | **Sources**: 5 parallel research agents (cdp-use API, accessibility tree, headless Chrome, browser security, codebase patterns)

#### D1 — CDP Library: cdp-use Assessment

**Critical finding**: `cdp-use` is NOT on PyPI — it's generated locally from Chrome's protocol spec by cloning the browser-use repo and running `python -m cdp_use.generator`. It's a code-generation tool, not a published package.

**API surface**: Provides `CDPClient` with async context manager:
```python
async with CDPClient("ws://localhost:9222/devtools/browser/...") as cdp:
    await cdp.send.Page.navigate({"url": "https://example.com"})
    targets = await cdp.send.Target.getTargets()
```

Two main interfaces: `cdp.send` for commands (Page.navigate, DOM.querySelector), `cdp.register` for event registration (Runtime.consoleAPICalled).

**Alternative approaches** (if cdp-use proves too immature):
- **python-cdp** (PyPI: `python-cdp`) — async CDP client with type wrappers, version 0.3.0
- **PyCDP** (PyPI: `chrome-devtools-protocol`) — full typed CDP bindings, version 0.4.0
- **Raw WebSocket** — `websockets` + JSON, maximum control, protocol JSON from `https://chromedevtools.github.io/devtools-protocol/`

**Recommendation**: Start with cdp-use but wrap it behind an internal `CDPClient` protocol/interface so we can swap implementations without touching tool code.

#### D4/D5 — Accessibility Tree: CDP Methods and Patterns

**CDP Accessibility domain methods** (all experimental):

| Method | Purpose | Key Params |
|--------|---------|------------|
| `getFullAXTree` | Fetch entire accessibility tree | `depth`, `frameId` |
| `getPartialAXTree` | Subtree from a node | `backendNodeId`, `nodeId`, `objectId` |
| `queryAXTree` | Search by role/name | `accessibleName`, `role`, `backendNodeId` |
| `getRootAXNode` | Get root node only | `frameId` |
| `getChildAXNodes` | Children of a node | `id`, `frameId` |

**AXNode structure** (each node in the tree):
```
nodeId: str              # Unique AX ID
role: AXValue            # "button", "textbox", "link", etc.
name: AXValue            # Accessible name ("Submit", "Email")
value: AXValue           # Current value (for inputs)
description: AXValue     # Accessible description
backendDOMNodeId: int     # Maps to DOM node for interaction
parentId: str            # Parent AX node
childIds: list[str]      # Children
ignored: bool            # Whether this is accessibility-hidden
properties: list         # Additional ARIA properties
frameId: str             # Frame context (for iframes)
```

**Element interaction via backendDOMNodeId**: The `backendDOMNodeId` on each AXNode maps directly to the DOM node. To click/type:
1. `Accessibility.getFullAXTree()` → get tree with backendDOMNodeIds
2. Assign sequential ref IDs to interactive elements
3. Agent requests action by ref ID
4. Resolve ref → backendDOMNodeId → `DOM.resolveNode(backendNodeId=...)` → objectId
5. `DOM.getBoxModel(backendNodeId=...)` → get coordinates
6. `Input.dispatchMouseEvent(x, y)` for click, `Input.dispatchKeyEvent()` for type

**Edge cases**:
- **iframes**: Each iframe has its own execution context. Must use `frameId` parameter in getFullAXTree
- **Shadow DOM**: CDP can pierce shadow DOM via `DOM.describeNode(pierce=true)`
- **Dynamic content**: Tree may change between snapshot and interaction. Use `DOM.resolveNode` to validate element still exists
- **Elements without labels**: Fall back to CSS selector via `DOM.querySelector`

**Performance**: `getFullAXTree` on complex pages can be slow (100-500ms). Use `depth` parameter to limit tree depth. `queryAXTree` is faster for targeted lookups.

#### D6 — Chrome Launch: Essential Flags and Process Management

**Essential headless Chrome flags**:
```
--headless=new                  # New headless mode (Chrome 112+)
--remote-debugging-port=0       # CDP port (0 = auto-assign)
--no-first-run                  # Skip first-run UI
--no-default-browser-check      # Skip browser check
--disable-background-networking # No background network activity
--disable-extensions            # No extensions
--disable-sync                  # No Google Sync
--metrics-recording-only        # Minimal telemetry
--disable-default-apps          # No default apps
--mute-audio                    # No audio
--no-sandbox                    # Required in containers (NOT for production)
--disable-dev-shm-usage         # Use /tmp instead of /dev/shm (Docker fix)
--disable-gpu                   # No GPU (headless)
```

**CDP endpoint discovery**: After launch, Chrome writes the WebSocket URL to stderr. Also available via:
```
GET http://localhost:{port}/json/version
→ { "webSocketDebuggerUrl": "ws://localhost:{port}/devtools/browser/{id}" }
```

Using `--remote-debugging-port=0` auto-assigns an available port — parse it from Chrome's stderr output to avoid port conflicts.

**Process management pattern**:
```python
import asyncio
import subprocess
import signal

proc = await asyncio.create_subprocess_exec(
    chrome_path, *flags,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
# Parse WebSocket URL from stderr
# Connect CDP client
# On shutdown: proc.terminate(), await proc.wait()
```

**Graceful shutdown**: Send SIGTERM, wait 5s, then SIGKILL. Use process groups (`os.setpgrp()`) to kill Chrome and all child processes.

**Zombie process prevention**: Always `await proc.wait()` after termination. Register an `atexit` handler as a safety net.

**Container considerations**:
- `/dev/shm` is 64MB by default in Docker — Chrome crashes. Use `--disable-dev-shm-usage` or mount larger tmpfs
- `--no-sandbox` required when running as root in containers
- Consider `chrome-headless-shell` image (smaller, purpose-built for automation)

#### D8/D9/D10 — Browser Security Controls

**URL filtering implementation**:
- **Domain-based matching**: Compare against list of allowed/denied domains using `urllib.parse.urlparse(url).hostname`
- **Pattern matching**: Support glob patterns (`*.example.com`) and exact matches
- **Pre-navigation check**: Validate URL BEFORE calling `Page.navigate()`. Also subscribe to `Page.frameNavigated` to catch client-side redirects
- **Protocol restriction**: Default deny `file://`, `chrome://`, `chrome-extension://` schemes. Only allow `http://` and `https://`

**JavaScript execution security**:
- `Runtime.evaluate` runs in the page's main world — full access to DOM, cookies, localStorage
- `Page.createIsolatedWorld` creates a separate JS context — cannot see page variables but CAN see the same DOM
- For maximum safety, use isolated worlds. The code can observe the DOM but cannot access `document.cookie`, `localStorage`, or page-scoped JS variables
- **Risk**: Even isolated worlds can still mutate the DOM. True sandboxing requires additional Chrome flags (`--disable-web-security` is the opposite — never use it)

**Cookie encryption pattern**:
```python
from cryptography.fernet import Fernet
key = Fernet.generate_key()  # Store in vault, NOT filesystem
f = Fernet(key)
encrypted = f.encrypt(json.dumps(cookies).encode())
# Persist encrypted blob
# Decrypt at session start
```

**Download controls via CDP**:
```
Browser.setDownloadBehavior(behavior="deny")  # Block all downloads
Browser.setDownloadBehavior(behavior="allowAndName", downloadPath="/tmp/safe/")  # Controlled path
```

**Data leakage prevention flags**:
```
--disable-background-networking     # No background requests
--disable-client-side-phishing-detection
--disable-component-update          # No auto-updates
--disable-breakpad                  # No crash reporting
--safebrowsing-disable-auto-update  # No Safe Browsing updates
```

**Audit trail per action** (NIST 800-53 AU-3 compliant):
- URL navigated + final URL (after redirects)
- Element targeted (ref ID, role, name, backendNodeId)
- Action performed (click, type, navigate, etc.)
- Input value (for type — redact if `security.redact_inputs = true`)
- Screenshot hash (if taken)
- Timestamp (ISO 8601 with timezone)
- Agent DID

#### Codebase Integration: Exact Module DI Contract

From `module_loader.py`, the browser module constructor must accept these optional params:

```python
class BrowserModule:
    def __init__(
        self,
        config: dict[str, Any] | None = None,   # [modules.browser.config] from TOML
        telemetry: AgentTelemetry | None = None,
        workspace: Path = Path("."),
    ) -> None:
        self._config = BrowserConfig(**(config or {}))
```

**startup() must**:
1. Register tools via `ctx.tool_registry.register(tool)` (each tool as a `RegisteredTool` with `ToolTransport.NATIVE`)
2. Subscribe to events via `ctx.bus.subscribe(event, handler, priority)`
3. Initialize CDP connection (launch Chrome or connect to existing)

**Tool execute pattern**: async functions with `**kwargs` matching `input_schema.properties` keys.

**CLI pattern**: `cli_group(workspace: Path) -> click.Group` factory function. Must add entry to `_register_module_clis()` in `arccli/agent.py` (line 2205) and `_get_module_commands()` (line 2371).

**Config pattern**: Inherit from `ModuleConfig` (which has `extra="forbid"`). TOML section is `[modules.browser.config]`.

#### Security: Prompt Injection via Browser Content

**Critical risk**: Web page content returned to the LLM is untrusted input. An attacker could craft a page with text like "Ignore all previous instructions and...".

**Three-layer defense**:
1. **Content sanitization**: Strip script tags, event handlers, and suspicious patterns from page text before returning to LLM
2. **Output marking**: Clearly mark browser content as "external web content" in tool results so the LLM treats it as data, not instructions
3. **Policy enforcement**: The policy module can veto tool calls based on the content returned (e.g., if it contains injection patterns)

This aligns with OWASP LLM01 (Prompt Injection) and ASI06 (Memory & Context Poisoning).

#### Performance: Resource Guardrails

| Guardrail | Value | Rationale |
|-----------|-------|-----------|
| Max concurrent browser sessions | 1 per agent | Memory budget (Chrome = 100-300MB) |
| Page load timeout | 30s | Prevent hangs on slow pages |
| Screenshot max size | 1920x1080 | Prevent context explosion |
| Accessibility tree depth | 10 levels | Balance completeness vs. size |
| Max page text length | 50,000 chars | Prevent context overflow |
| CDP WebSocket timeout | 10s | Detect stale connections |
| Chrome process memory limit | 512MB | Enforce via cgroups in containers |

### New Risks Discovered

1. **cdp-use is not on PyPI** — must vendor or generate locally. Consider fallback to `python-cdp` or `chrome-devtools-protocol` if this is too fragile for a dependency
2. **Prompt injection via web content** — page text returned to LLM is a major injection surface. Must sanitize and clearly mark as external data
3. **Chrome process management** — zombie processes, port conflicts, /dev/shm issues in containers. Need robust process lifecycle
4. **Accessibility tree performance** — `getFullAXTree` can be 100-500ms on complex pages. May need caching or partial tree queries
5. **Redirect-based URL bypass** — validating the initial URL is insufficient; must also check after navigation completes (redirects could land on blocked domains)

---

## Feature: Telegram Messaging Module

**Date**: 2026-02-16
**Source**: `.claude/brainstorms/2026-02-16-agent-messaging.md` (deepened)
**Goal**: Bidirectional text messaging between human (phone) and ArcAgent via Telegram Bot API

### Decisions

| # | Category | Decision | Choice | Rationale |
|---|----------|----------|--------|-----------|
| D-160 | Architecture | Where does Telegram integration live? | **ArcAgent module** (`arcagent/modules/telegram/`) | Follows existing module convention (MODULE.yaml, Module protocol, ModuleLoader). Same pattern as memory, policy, scheduler. Removable without touching core. |
| D-161 | Architecture | Inbound message transport? | **Long polling only** | No HTTPS, no domain, no reverse proxy needed. Works on Mac and AWS. Proactive outbound via `send_message()` works regardless. OpenClaw also defaults to polling. |
| D-162 | Architecture | Module lifecycle? | **Module owns the polling loop** | `TelegramModule.startup()` starts polling as background asyncio task. `shutdown()` stops it. Same pattern as scheduler's `_timer_loop()`. Self-contained. |
| D-163 | Architecture | How does module access agent.chat()? | **Deferred binding via callback** | `set_agent_chat_fn(agent.chat)` wired by agent.py after startup. Same pattern as scheduler's `set_agent_run_fn()`. Zero coupling to agent internals. Module removable without core changes. |
| D-164 | Data Model | Session management? | **Resume last session, /new for fresh start** | Following OpenClaw's model. Cron-triggered runs get isolated sessions. Current session_id persisted in `{workspace}/telegram/state.json`. |
| D-165 | Data Model | Chat model? | **1 bot = 1 user = 1 chat** | Single-user day-1. `allowed_chat_ids` allowlist in config. No mapping store needed. Module tracks current active session_id. |
| D-166 | Integration | Long message handling? | **Smart split at paragraph boundaries** | Double-newline first, sentence boundaries second, hard-split at 4096 as fallback. Sequential messages. |
| D-167 | Integration | Response formatting? | **Plain text only** | No parse mode. Agent output sent as-is. Zero formatting bugs. Can add HTML later. |
| D-168 | Integration | Processing acknowledgment? | **Typing indicator only** | Send TYPING chat action before processing. Expires after ~5s. No "Processing..." messages cluttering chat. |
| D-169 | Security | Bot token storage? | **Environment variable** | `ARCAGENT_TELEGRAM_BOT_TOKEN`. Doesn't touch filesystem. Production can use vault-injected env vars. |
| D-170 | Security | Unauthorized user handling? | **chat_id allowlist in config** | `allowed_chat_ids` list in arcagent.toml. Unauthorized messages silently ignored. Rejection logged to telemetry for audit. |
| D-171 | Performance | Concurrent message handling? | **Sequential (asyncio.Queue)** | Queue inbound messages, process one at a time. Prevents session state race conditions. Same approach as OpenClaw's per-chat sequencing. |
| D-172 | Testing | Testing strategy? | **Mock bot API + real agent** | Unit tests mock python-telegram-bot's Bot class. Integration tests use real ArcAgent with mocked Telegram. Separate test bot for manual E2E. |
| D-173 | Deployment | Module activation? | **Auto-start in serve mode** | Module detects serve mode at startup. If telegram.enabled=true and agent is serving, starts polling. Dormant in run/chat modes. |

### Key Design Principles

- **Module, not core**: Telegram is opt-in. Module Bus participant. Removable without touching nucleus.
- **Polling simplicity**: No HTTPS, no webhooks, no domain. Just start the agent and text it.
- **ArcAgent is the brain**: Telegram is just transport. All intelligence stays in agent.chat().
- **Deferred binding**: Module gets agent.chat() callback after startup, same proven pattern as scheduler.
- **Sequential processing**: One message at a time via asyncio.Queue. No race conditions.

### Components to Build

1. **`arcagent/modules/telegram/`** — Module directory
   - `MODULE.yaml` — Module manifest (entry_point, dependencies)
   - `__init__.py` — TelegramModule class (Module protocol)
   - `bot.py` — Telegram bot setup, polling loop, message handlers
   - `config.py` — TelegramConfig (Pydantic model)

2. **`arcagent/core/config.py`** — Add TelegramConfig under modules

3. **`arcagent/core/agent.py`** — Wire `set_agent_chat_fn()` for Telegram module (same pattern as scheduler)

4. **Tests**
   - `tests/unit/modules/telegram/` — Mock bot API tests
   - `tests/integration/` — Real agent + mocked Telegram

### Architecture Diagram

```
arc agent serve
    |
    v
ArcAgent (warm, long-running)
    |
    v
TelegramModule (Module Bus participant)
    |
    +-- startup(): start polling loop (asyncio.create_task)
    +-- set_agent_chat_fn(agent.chat) ← wired by agent.py
    +-- subscribes to schedule:completed for proactive notifications
    |
    v (inbound message from Telegram)
    |
    +-- verify chat_id in allowlist
    +-- send TYPING chat action
    +-- queue message in asyncio.Queue
    +-- process sequentially: await agent_chat_fn(text, session_id=current_session)
    +-- smart-split result.content at paragraph boundaries
    +-- send response(s) via bot.send_message()
    |
    v (proactive / cron notification)
    |
    +-- schedule:completed event fires on Module Bus
    +-- TelegramModule handler extracts result
    +-- send notification to stored chat_id
```

### Telegram Bot Commands

| Command | Action |
|---------|--------|
| `/start` | Register chat, store chat_id, create first session |
| `/new` | Start fresh session (new session_id) |
| `/status` | Show current session info, agent status |
| Free text | Route to agent.chat() |

### Config Schema

```toml
[modules.telegram]
enabled = true
priority = 100

[modules.telegram.config]
allowed_chat_ids = []          # Empty = accept none. Must configure.
poll_interval = 1.0            # Seconds between getUpdates calls
max_message_length = 4096      # Telegram limit
```

Token via environment: `ARCAGENT_TELEGRAM_BOT_TOKEN`

### Open Questions

None — all decisions resolved through interactive build session.

### Related Solutions

- Scheduler module (same deferred binding pattern, same asyncio task lifecycle)
- OpenClaw Telegram integration (architecture reference)

---

## Feature: ArcRun Phase 4 — Hardening

**Date**: 2026-02-21
**Source**: `packages/arcrun/.claude/steering/roadmap.md` (Phase 4)
**Goal**: Container sandbox, event integrity, adversarial testing, concurrent spawn performance, NIST 800-53 documentation

> **Research Enhancement Summary** (2026-02-21): Enriched with 5 parallel research agents covering Docker SDK patterns, SHA-256 hash chain implementation, adversarial testing techniques, NIST 800-53 control mapping, and codebase integration analysis. Key findings: 3 real bugs discovered (mutable Event data, no seccomp on execute, no PID limits), 38 NIST controls mapped (21 fully implemented, 14 partial, 3 planned), complete implementation patterns for container factory and hash chain. Ready for `/specify`.

### Decisions

| # | Category | Decision | Choice | Rationale |
|---|----------|----------|--------|-----------|
| D-174 | Integration | Container runtime for isolation sandbox | **Docker SDK with configurable socket** | Works with both Docker and Podman via socket config. Docker SDK (docker-py) is mature. Podman implements Docker's API. Zero extra LOC for dual runtime. Podman is superior for fed/enterprise (rootless, SELinux native, FIPS mode) but Docker SDK is the portable abstraction. |
| D-175 | Architecture | What does the container sandbox wrap? | **CodeExec only** | Only `make_execute_tool()` (model-generated code) runs in containers. User-provided `Tool.execute` stays in-process — caller trusts their own tools. Model-generated code is the RCE threat vector. |
| D-176 | Architecture | How does container sandbox integrate? | **New factory: `make_contained_execute_tool()`** | Separate factory function. Existing `make_execute_tool()` unchanged. Caller explicitly opts into container isolation. Zero changes to existing code. Same pattern as execute vs spawn. |
| D-177 | Security | Default container constraints | **Maximum lockdown** | No network, read-only root FS, tmpfs /tmp (64MB), 256MB memory cap, 50% CPU, drop ALL capabilities, no-new-privileges, 64 PID limit. Model code can compute and write to /tmp. Cannot call APIs, write to FS, fork-bomb, OOM, or escape. Caller can relax. |
| D-178 | Security | Event integrity mechanism | **SHA-256 hash chain** | Each event includes sequence number + hash of (previous_hash + event_data). Blockchain-like tamper-evidence. If any event modified or deleted, chain breaks. ~20-30 LOC. No crypto keys needed (integrity, not authentication). Maps to NIST AU-9, AU-10. |
| D-179 | Architecture | Event verification API | **Method on LoopResult** | `result.verify_integrity()` returns bool. Optional `detailed=True` for VerifyResult with chain metadata. Natural home — caller already has the result. ~5 LOC public API. |
| D-180 | Testing | Adversarial test scope | **Comprehensive (8 categories)** | Prompt injection, path traversal, resource exhaustion, event chain tampering, tool parameter injection, spawn depth bomb, steering injection, timing attacks. Covers OWASP LLM01, ASI02, ASI05, AU-9. |
| D-181 | Testing | Adversarial test location | **Dedicated `tests/security/` directory** | One file per attack category (8 files). Matches project test structure (unit/, integration/, security/, performance/). Run independently: `pytest tests/security/`. |
| D-182 | Performance | Concurrent spawn testing approach | **Stress tests in `tests/security/test_timing_attacks.py`** | 10+ parallel spawns, nested parallel spawns, spawn+cancel race, spawn+steer race. Uses asyncio.gather + mock models with controlled delays. No new production code. |
| D-183 | Architecture | NIST 800-53 documentation format | **Standalone `docs/security/` directory** | `nist-800-53-mapping.md` + `threat-model.md` + `adversarial-tests.md`. Each control entry includes: control ID, title, arcrun feature, code reference, test evidence. Separate from README. |
| D-184 | Security | NIST control mapping scope | **Full audit (all applicable controls)** | Map every NIST 800-53 control arcrun touches (estimated 30-40+). Most thorough for FedRAMP authorization. Includes controls enabled by Phase 4 additions (SC-4, SC-39, AU-9, AU-10, SC-7, AC-25, SA-8, CA-8). |
| D-185 | Dependencies | Docker SDK dependency strategy | **Optional: `pip install arcrun[container]`** | Lazy import with helpful error message if not installed. Zero impact on existing users. `docker>=7.0` in `[project.optional-dependencies]`. |
| D-186 | Architecture | LOC budget revision | **1,400 LOC for Phase 4** | Current: ~1,221 LOC (post-spawn). Phase 4 adds ~120-160 LOC (container factory + hash chain). Spawn was bigger than estimated (+421 LOC vs Phase 3 budget). Accept and adjust. Log in ADR. |
| D-187 | Security | Container image management | **Caller specifies, no auto-pull** | Image name is required param. arcrun never pulls images or makes network calls. Operator pre-stages approved images. Air-gap safe for SCIF/disconnected environments. |

### Key Design Principles

- **Container sandbox is opt-in**: Existing code unchanged. New factory for container-isolated code execution.
- **Event integrity is always-on**: Hash chain computed on every emit. Verification optional but chain is always built.
- **Adversarial tests are comprehensive**: 8 attack categories covering OWASP LLM + Agentic top 10 threats.
- **NIST documentation is thorough**: Full control mapping for FedRAMP readiness.
- **No network calls from arcrun. Ever.**: Container images pre-staged by operator. No auto-pull.

### Components to Build

1. **`arcrun/builtins/contained_execute.py`** — Container-isolated code execution factory (~80-100 LOC)
2. **`arcrun/events.py`** — Add hash chain to EventBus (~20-30 LOC)
3. **`arcrun/types.py`** — Add `sequence`, `prev_hash`, `event_hash` to Event; `verify_integrity()` to LoopResult (~20 LOC)
4. **`tests/security/`** — 8 adversarial test files
5. **`docs/security/`** — NIST mapping, threat model, adversarial test catalog

### Architecture Diagram

```
make_contained_execute_tool(image="python:3.11-slim", ...)
    |
    v
Tool.execute(params, ctx)
    |
    +-- docker.from_env() or docker.DockerClient(base_url=socket)
    +-- client.containers.run(
    |       image=image,
    |       command=["python", "/tmp/script.py"],
    |       network_disabled=True,
    |       read_only=True,
    |       mem_limit="256m",
    |       pids_limit=64,
    |       cap_drop=["ALL"],
    |       tmpfs={"/tmp": "size=64m"},
    |       auto_remove=True,
    |   )
    +-- return {"stdout": ..., "stderr": ..., "exit_code": ...}

EventBus.emit()
    |
    +-- sequence = len(self._events)
    +-- prev_hash = self._events[-1].event_hash if events else "genesis"
    +-- event_hash = sha256(prev_hash + canonical(event_data))
    +-- Event(type, timestamp, run_id, data, sequence, prev_hash, event_hash)
```

### Research Insights (via /deepen)

**Enriched**: 2026-02-21 | **Sources**: 5 parallel research agents (Docker SDK patterns, SHA-256 hash chain, adversarial testing, NIST 800-53 mapping, codebase integration analysis)

#### D1/D3 — Container Sandbox: Docker SDK Implementation Patterns

**Factory pattern** — `make_contained_execute_tool()` follows the same factory signature as `make_execute_tool()`:

```python
def make_contained_execute_tool(
    *,
    image: str,                          # Required — caller specifies, no auto-pull
    timeout_seconds: float = 30,
    max_output_bytes: int = 65536,
    socket: str | None = None,           # Auto-detect: Docker or Podman
    mem_limit: str = "256m",
    cpu_period: int = 100_000,
    cpu_quota: int = 50_000,             # 50% of one core
    pids_limit: int = 64,
    tmpfs_size: str = "64m",
    network_disabled: bool = True,
    read_only: bool = True,
) -> Tool:
```

**Socket auto-detection** (Docker vs Podman):
```python
def _detect_socket() -> str:
    candidates = [
        os.environ.get("DOCKER_HOST", ""),
        f"unix:///run/user/{os.getuid()}/podman/podman.sock",  # Rootless Podman
        "unix:///var/run/docker.sock",                          # Docker default
        "unix:///var/run/podman/podman.sock",                   # Rootful Podman
    ]
    for sock in candidates:
        if sock and Path(sock.replace("unix://", "")).exists():
            return sock
    raise SandboxUnavailableError("No container runtime socket found")
```

**Code injection via tar** (avoids bind mounts — more secure):
```python
import tarfile, io
def _inject_code_via_tar(container, code: str) -> None:
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        data = code.encode("utf-8")
        info = tarfile.TarInfo(name="script.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    container.put_archive("/tmp", tar_stream)
```

**Seccomp profile** — Restrict syscalls to compute-only operations. Block: `mount`, `umount`, `ptrace`, `keyctl`, `pivot_root`, `reboot`, `kexec_load`, `unshare`, `setns`, `clone` (with CLONE_NEWUSER). Allow: `read`, `write`, `open`, `close`, `mmap`, `brk`, `stat`, `fstat`, `exit_group`, and other standard compute syscalls.

**Error hierarchy**:
- `SandboxUnavailableError` — No runtime found (Docker not installed, socket not accessible)
- `SandboxTimeoutError` — Container exceeded timeout
- `SandboxOOMError` — Container killed by OOM (exit code 137)
- `SandboxRuntimeError` — Script execution failed (non-zero exit)

**Podman compatibility notes**:
- Podman implements Docker's API — `docker-py` works via socket config
- Podman rootless runs as non-root user (preferred for fed/enterprise)
- Podman native SELinux support via `:Z` volume label (automatic in rootless)
- Podman supports FIPS mode when host kernel has FIPS enabled

**Air-gap image management**:
```bash
# Online machine: save approved image
docker save python:3.11-slim -o python-3.11-slim.tar
# Transfer to air-gapped environment
docker load -i python-3.11-slim.tar
# Podman equivalent
podman save/load same syntax
```

**Warm pool pattern** (optional optimization for repeated executions):
```python
# Create container once, reuse for multiple executions
container = client.containers.create(image=image, **constraints)
container.start()
# For each execution: put_archive + exec_run
# On shutdown: container.stop() + container.remove()
```
Note: Warm pool trades isolation (shared container) for performance. Default should be ephemeral (new container per execution).

#### D5/D6 — Hash Chain: Production-Ready Implementation

**Event immutability** — Current Event is a mutable `@dataclass`. Must change to `frozen=True` and wrap data dict:

```python
from types import MappingProxyType

@dataclass(frozen=True)
class Event:
    type: str
    timestamp: float
    run_id: str
    data: MappingProxyType               # Immutable view of data dict
    sequence: int = 0
    prev_hash: str = ""
    event_hash: str = ""
```

**MappingProxyType** prevents observer mutation of data dict. Construction:
```python
event = Event(
    type=event_type,
    timestamp=time.time(),
    run_id=self._run_id,
    data=MappingProxyType(dict(data)),    # Deep copy + freeze
    sequence=seq,
    prev_hash=prev,
    event_hash=computed_hash,
)
```

**Canonical bytes** for hash computation — deterministic serialization:
```python
def _canonical_bytes(event_type: str, timestamp: float, run_id: str,
                     data: Mapping, sequence: int) -> bytes:
    payload = json.dumps(
        {"type": event_type, "timestamp": timestamp, "run_id": run_id,
         "data": dict(data), "sequence": sequence},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return payload
```

**Hash computation**:
```python
GENESIS_PREV_HASH = "0" * 64  # 64 hex chars = 256 bits

def _compute_event_hash(prev_hash: str, canonical: bytes) -> str:
    return hashlib.sha256(
        prev_hash.encode("ascii") + canonical
    ).hexdigest()
```

**Thread safety** — EventBus.emit() must be thread-safe for observer callbacks:
```python
import threading

class EventBus:
    def __init__(self, run_id: str, ...):
        self._lock = threading.Lock()
        self._events: list[Event] = []

    def emit(self, event_type: str, data: dict | None = None) -> Event:
        with self._lock:
            seq = len(self._events)
            prev = self._events[-1].event_hash if self._events else GENESIS_PREV_HASH
            canonical = _canonical_bytes(event_type, time.time(), self._run_id,
                                        data or {}, seq)
            event_hash = _compute_event_hash(prev, canonical)
            event = Event(type=event_type, timestamp=..., run_id=self._run_id,
                         data=MappingProxyType(dict(data or {})),
                         sequence=seq, prev_hash=prev, event_hash=event_hash)
            self._events.append(event)
        # Observer callback OUTSIDE lock to prevent deadlock
        if self._on_event is not None:
            try: self._on_event(event)
            except Exception: pass
        return event
```

**Chain verification** — Three invariants:
```python
@dataclass
class ChainVerificationResult:
    valid: bool
    event_count: int
    first_broken_index: int | None = None
    error: str | None = None

def verify_chain(events: list[Event]) -> ChainVerificationResult:
    for i, event in enumerate(events):
        # 1. Self-hash: recompute and compare
        canonical = _canonical_bytes(event.type, event.timestamp, event.run_id,
                                    event.data, event.sequence)
        expected = _compute_event_hash(event.prev_hash, canonical)
        if expected != event.event_hash:
            return ChainVerificationResult(False, len(events), i, "self-hash mismatch")

        # 2. Chain linkage: prev_hash matches previous event's hash
        expected_prev = events[i-1].event_hash if i > 0 else GENESIS_PREV_HASH
        if event.prev_hash != expected_prev:
            return ChainVerificationResult(False, len(events), i, "chain break")

        # 3. Sequence order: must be monotonically increasing
        if event.sequence != i:
            return ChainVerificationResult(False, len(events), i, "sequence gap")

    return ChainVerificationResult(True, len(events))
```

**LoopResult.verify_integrity()** — Thin wrapper:
```python
def verify_integrity(self) -> ChainVerificationResult:
    return verify_chain(self.events)
```

**NIST compliance mapping**: AU-9 (Protection of Audit Information) — hash chain detects tampering. AU-10 (Non-repudiation) — event hashes prove chronological ordering.

#### D7/D8 — Adversarial Testing: 8 Attack Categories with Findings

**Real bugs discovered during research**:

1. **EventBus.emit() doesn't copy data dict** — Observer callbacks receive the same dict object. A malicious observer could mutate data, affecting subsequent observers and corrupting the event record. **Fix**: `MappingProxyType(dict(data))` in hash chain implementation resolves this.

2. **execute.py has no seccomp/chroot** — Current `make_execute_tool()` runs model code as a subprocess on the host with full filesystem access. **Fix**: `make_contained_execute_tool()` resolves this for opt-in users.

3. **No OS-level PID limits for fork bomb protection** — Current subprocess execution has no PID limits. A malicious `os.fork()` loop in model-generated code could exhaust host PIDs. **Fix**: Container `pids_limit=64` resolves this for contained execution.

**8 test categories with example payloads**:

| Category | File | Key Test Cases |
|----------|------|----------------|
| Prompt injection | `test_prompt_injection.py` | System prompt extraction, tool call injection via task text, instruction override in spawned child |
| Path traversal | `test_path_traversal.py` | `../../etc/passwd` in code execution, symlink escape from tmpdir, container mount escape |
| Resource exhaustion | `test_resource_exhaustion.py` | Fork bomb (`while True: os.fork()`), memory bomb (`"A" * 10**10`), infinite loop, disk fill via /tmp |
| Event chain tampering | `test_event_tampering.py` | Modify event data after emit, insert/delete events, reorder events, replay events from different run |
| Tool parameter injection | `test_tool_injection.py` | SQL injection via tool params, command injection in execute args, oversized parameters |
| Spawn depth bomb | `test_spawn_depth_bomb.py` | Recursive spawn to max_depth, parallel spawn flood, spawn with manipulated depth field |
| Steering injection | `test_steering_injection.py` | Inject instructions via tool result, manipulate system prompt through child spawn, context poisoning |
| Timing attacks | `test_timing_attacks.py` | Concurrent spawn race conditions, emit during verification, spawn+cancel race, spawn+steer race |

**OWASP coverage matrix**:

| OWASP ID | Threat | Test Category |
|----------|--------|---------------|
| LLM01 | Prompt Injection | prompt_injection, steering_injection |
| ASI02 | Tool Misuse | tool_injection, path_traversal |
| ASI05 | Unexpected Code Execution | resource_exhaustion, path_traversal |
| AU-9 | Audit Tampering | event_tampering |
| ASI08 | Cascading Failures | spawn_depth_bomb, timing_attacks |

#### D9 — Concurrent Spawn Stress Tests

**Key stress test scenarios**:

1. **10+ parallel spawns** — `asyncio.gather(*[run(...) for _ in range(10)])` with mock models that have controlled delays. Verify: no event interleaving between runs, all run_ids unique, all events have correct parent_run_id.

2. **Nested parallel spawns** — Parent spawns 3 children, each child spawns 2 grandchildren. Verify: event bubbling correct at all levels, `child.{run_id}.` prefix nesting is accurate.

3. **Spawn + cancel race** — Start a spawn, then cancel the parent mid-execution. Verify: child tasks are properly cancelled, no zombie asyncio tasks, events up to cancellation point are valid.

4. **Spawn + steer race** — Concurrent `run()` calls where one changes strategy mid-loop. Verify: strategy changes don't affect sibling runs.

**Test pattern** (from existing test suite): MockModel with predetermined responses:
```python
class MockModel:
    def __init__(self, responses: list[LLMResponse]):
        self._responses = iter(responses)
    async def invoke(self, messages, tools=None, **kwargs):
        return next(self._responses)
```

#### D10/D11 — NIST 800-53: Control Mapping Summary

**38 controls mapped across 8 families**:

| Family | Controls | Fully Impl. | Partial | Planned |
|--------|----------|-------------|---------|---------|
| AC (Access Control) | AC-3, AC-4, AC-6, AC-17, AC-25 | 3 | 1 | 1 |
| AU (Audit) | AU-2, AU-3, AU-6, AU-8, AU-9, AU-10, AU-12 | 4 | 2 | 1 |
| CM (Config Mgmt) | CM-2, CM-3, CM-5, CM-7, CM-8 | 3 | 2 | 0 |
| IA (Identification) | IA-2, IA-3, IA-4, IA-5 | 2 | 2 | 0 |
| SC (System/Comms) | SC-2, SC-3, SC-4, SC-7, SC-8, SC-13, SC-28, SC-39 | 4 | 3 | 1 |
| SI (System Integrity) | SI-2, SI-3, SI-4, SI-7, SI-10 | 3 | 2 | 0 |
| SA (System Acq.) | SA-4, SA-8, SA-10, SA-11 | 1 | 3 | 0 |
| CA (Assessment) | CA-2, CA-7, CA-8 | 1 | 1 | 1 |
| **Total** | **38** | **21** | **14** | **3** |

**Key controls enabled by Phase 4**:

| Control | Title | Phase 4 Feature |
|---------|-------|-----------------|
| AU-9 | Protection of Audit Information | SHA-256 hash chain (tamper-evident events) |
| AU-10 | Non-repudiation | Hash chain proves chronological ordering |
| SC-4 | Information in Shared Resources | Container isolation prevents data leakage between executions |
| SC-7 | Boundary Protection | Container network disabled, read-only FS |
| SC-39 | Process Isolation | Container per-execution with PID/mem/CPU limits |
| AC-25 | Reference Monitor | Sandbox check in executor.py (TOCTOU-safe in asyncio) |
| SA-8 | Security Engineering Principles | Least privilege defaults, fail-secure |
| CA-8 | Penetration Testing | 8-category adversarial test suite |

**Priority gaps for ATO**:
- AU-6 (Audit Review): Need automated analysis/alerting on events (partially addressed by observer pattern)
- SA-11 (Developer Testing): Need formal test plan document cross-referencing NIST controls
- CA-2 (Control Assessments): Need periodic assessment procedure documentation

#### Codebase Integration: Critical Implementation Details

**Event dataclass migration** (`events.py`):
- Current Event is `@dataclass` (mutable). Changing to `frozen=True` is a **breaking change** for any code that mutates events after creation.
- EventBus.emit() returns Event — callers may be storing references and mutating `.data`. Search for: `event.data["key"] = value` patterns.
- `data: dict[str, Any]` → `data: MappingProxyType` changes type signature. Callers doing `isinstance(event.data, dict)` will break. `MappingProxyType` supports `Mapping` protocol but not `MutableMapping`.

**EventBus thread safety** (`events.py`):
- Current `self._events.append(event)` is NOT thread-safe if observers run in threads.
- `threading.Lock` around emit body prevents concurrent sequence number conflicts.
- Observer callback MUST run outside the lock to prevent deadlock if observer calls emit.

**LoopResult.events type** (`types.py`):
- Currently `events: list[Any]`. Should become `events: list[Event]` for type safety.
- `_build_result()` in `react.py:188-204` constructs LoopResult. Events come from `state.event_bus.events` (shallow copy via `list()`). Hash chain integrity is preserved because Event is now frozen.

**Public API exports** (`__init__.py`):
- Currently 12 exports. Add: `make_contained_execute_tool`, `verify_chain`, `ChainVerificationResult`, `GENESIS_PREV_HASH`.
- `SandboxUnavailableError`, `SandboxTimeoutError`, `SandboxOOMError`, `SandboxRuntimeError` for error handling.

**Optional dependency** (`pyproject.toml`):
```toml
[project.optional-dependencies]
container = ["docker>=7.0"]
```
No optional deps section currently exists — must create it.

**Lazy import pattern** for docker:
```python
def make_contained_execute_tool(...) -> Tool:
    try:
        import docker
    except ImportError:
        raise ImportError(
            "Container support requires docker SDK. "
            "Install with: pip install arcrun[container]"
        )
```

### New Risks Discovered

1. **Event mutation by observers** — Current EventBus passes raw mutable data dict to observers. A malicious or buggy observer could corrupt the event record. Fixed by `MappingProxyType` + `frozen=True`.

2. **No thread safety on EventBus** — `list.append()` is thread-safe in CPython due to GIL, but sequence number computation (`len(self._events)`) and hash chain linkage are not atomic. Fixed by `threading.Lock`.

3. **Type signature change** — `Event.data: dict → MappingProxyType` is a breaking change. Must audit all callers.

4. **Container runtime availability** — `make_contained_execute_tool()` silently fails if Docker/Podman not installed. Must provide clear error with install instructions.

5. **Seccomp profile portability** — Custom seccomp profiles may not work on all kernel versions. Need fallback to default Docker seccomp profile.

---

## ArcLLM Budget Control & Compliance-Aware Routing — Build Decisions (2026-02-21)

**Phase**: build | **Status**: complete | **Total decisions**: 14

### Summary

Budget tracking extends TelemetryModule (not a separate module). Per-agent scope with calendar periods (monthly + daily + per-call). Enforcement configurable: warn (enterprise default) or block (federal). Routing replaces the adapter at the innermost stack position, maps data classifications to specific provider+model pairs. Both features follow existing OTel + structured logging patterns for durable audit.

### Architecture

#### D-188: Budget-Telemetry Integration Pattern
**Decision**: Extend TelemetryModule with budget tracking
**Alternatives**: Sibling module with shared state (rejected: shared mutable state complexity); Budget wraps Telemetry (rejected: two modules for one concern)
**Rationale**: Simplest approach — one module, one cost concern area. Budget can't be bypassed without bypassing telemetry. Stack order unchanged: `Otel > Telemetry(+Budget) > Audit > Security > Retry > Fallback > RateLimit > Adapter`. Cost IS telemetry.

#### D-189: Budget Scope Isolation
**Decision**: Per-agent ID scope
**Alternatives**: Hierarchical agent+tenant (rejected: shared state across agents); Per-provider (rejected: no per-agent attribution, contention); Configurable scope key (rejected: no structure enforcement)
**Rationale**: Flat lookup, shared-nothing. Maps 1:1 to ArcAgent DID identity. Per-agent spend attribution for NIST AU-3. `budget_scope="agent:agent-007"` passed at `load_model()` time — mandatory, no default.

#### D-190: Routing Module Stack Position
**Decision**: Router replaces adapter at innermost position
**Alternatives**: Outermost/before all modules (rejected: duplicates entire module stack per route, fragments budget/telemetry); Inside Security/outside Retry (rejected: tighter coupling)
**Rationale**: Router IS the provider. One module stack, multiple backends. All security/observability modules apply uniformly regardless of which provider handles the request. Clean abstraction.

#### D-191: Router Adapter Lifecycle
**Decision**: Eager — load all adapters at init
**Alternatives**: Lazy/load on first use (rejected: deferred config errors, lazy init complexity)
**Rationale**: Validates all configs upfront (fail fast). Predictable cold start and memory footprint. All connections established and auditable at startup.

#### D-192: Budget-Routing Interaction
**Decision**: Budget tracks total agent spend only — not per-provider
**Alternatives**: Budget tracks per-provider too (rejected: more accumulators, couples budget to routing)
**Rationale**: OTel spans already contain per-call provider info (`gen_ai.system`, `gen_ai.request.model`). Grafana can aggregate spend per provider via OTel queries. Budget accumulator stays simple — one counter per agent per period.

### Data Model

#### D-193: Budget Storage Backend
**Decision**: In-memory accumulator for enforcement + OTel spans/structured logs for durable persistence
**Alternatives**: SQLite local file (rejected: breaks pattern — no other module persists locally); External Redis/NATS KV (rejected: adds infrastructure dependency)
**Rationale**: Follows existing telemetry pattern exactly. TelemetryModule, AuditModule, and OtelModule all persist via OTel spans and structured logging to external collectors. Budget data flows through the same pipeline. External observability stack (Grafana, Jaeger, etc.) is the query/audit layer. In-memory accumulator resets on restart — acceptable because OTel has the durable record.

#### D-194: Budget Period & Reset
**Decision**: Calendar periods — monthly + daily + per-call max
**Alternatives**: Rolling window (rejected: requires storing all transactions, higher memory, harder to audit); Monthly only (rejected: no daily protection against runaway agents)
**Rationale**: Federal compliance (Anti-Deficiency Act 31 U.S.C. 1341). Monthly aligns with procurement/billing/fiscal reporting cycles. Daily acts as circuit breaker — prevents a runaway agent from burning an entire monthly allocation in hours. Per-call max prevents single expensive calls. Three enforcement layers. NIST 800-53 SA-2 (resource allocation) and OWASP LLM10 (unbounded consumption).

#### D-195: Per-Call Max Estimation
**Decision**: Pre-flight estimate via `max_tokens * cost_output_per_1m / 1_000_000`
**Alternatives**: Post-call enforcement only (rejected: reactive — money already spent, Anti-Deficiency violation already occurred)
**Rationale**: Conservative upper bound. One multiplication, zero overhead, no tokenizer dependency. Prevents obviously excessive calls before they happen. Underestimates (ignores input cost) but that's acceptable — it's a safety net, not a billing system.

### API Design

#### D-196: Budget API Surface in load_model()
**Decision**: Extend telemetry kwarg + mandatory `budget_scope` top-level kwarg
**Alternatives**: Separate budget kwarg (rejected: contradicts D-188, budget is part of telemetry)
**Rationale**: Budget config fields live in the telemetry dict (monthly_limit_usd, daily_limit_usd, per_call_max_usd, alert_threshold_pct, enforcement). `budget_scope` is a required top-level kwarg — forces caller to identify the agent. Config.toml sets org-wide defaults, `load_model()` overrides per-agent.

```python
load_model(
    "anthropic",
    telemetry={
        "monthly_limit_usd": 500.00,
        "daily_limit_usd": 50.00,
        "per_call_max_usd": 5.00,
        "alert_threshold_pct": 80,
        "enforcement": "block",
    },
    budget_scope="agent:agent-007",
)
```

#### D-197: Routing Classification Source
**Decision**: Caller-declared via `classification` kwarg at invoke() time
**Alternatives**: Content scanner detects (rejected: adds latency, false positives, duplicates Step 18); Both caller + verify (rejected: more complex, overlaps Content Scanner)
**Rationale**: Classification is a policy decision, not a detection problem. Caller (ArcAgent) knows its data context. Zero overhead — no content scanning. Auditable. Content Scanner (Step 18) can verify separately if needed later.

#### D-198: Routing Rules Structure
**Decision**: Classification -> provider + model mapping
**Alternatives**: Simple classification -> provider only (rejected: FedRAMP authorizes specific models, not just providers); Classification -> priority list (rejected: duplicates FallbackModule)
**Rationale**: FedRAMP authorizes specific models. Routing rules specify both provider and model. Cost optimization per classification tier (e.g., CUI on Sonnet, unclassified on GPT-4o-mini). Auditors can verify exact model authorized per classification.

```toml
[modules.routing.rules.cui]
provider = "anthropic"
model = "claude-sonnet-4-6"

[modules.routing.rules.unclassified]
provider = "openai"
model = "gpt-4o-mini"
```

### Security

#### D-199: Enforcement Behavior
**Decision**: Configurable — warn or block, default block
**Alternatives**: Hard block only (rejected: too rigid for dev/staging); Three-tier warn/soft/hard (rejected: three thresholds, more complex)
**Rationale**: Secure by default (block). `enforcement = "block"` raises `BudgetExceededError`. `enforcement = "warn"` logs + emits OTel event but allows the call, sets `response.metadata["budget_warning"] = True`. Alert threshold (default 80%) warns before the hard stop. Warn mode exists for dev/staging and non-federal deployments.

#### D-200: Unknown Classification Behavior
**Decision**: Follows enforcement config — enterprise (warn) routes to default, federal (block) raises error
**Alternatives**: Always fail closed (rejected: too strict for enterprise); Always fall back (rejected: security risk for federal)
**Rationale**: Same enforcement toggle governs both budget and routing behavior. `enforcement = "warn"` (enterprise default): unknown classification logs a warning and routes to `default_classification`. `enforcement = "block"` (federal): unknown classification raises `ArcLLMConfigError`, fail closed. No data sent to wrong provider in federal mode.

### Testing

#### D-201: Testing Strategy
**Decision**: Standard TDD + security-specific tests
**Alternatives**: Standard TDD only (rejected: budget/routing are security-critical modules)
**Rationale**: Standard TDD (unit + integration, >=80% coverage) plus dedicated security tests. Budget security: bypass attempts, scope isolation, negative cost injection, overflow/underflow, config injection via scope string. Routing security: classification downgrade attempts, provider config injection, adapter isolation, audit trail completeness.

```
tests/
  unit/
    test_budget.py           # Accumulator, limits, periods, enforcement
    test_routing.py          # Selection, classification, adapter lifecycle
  integration/
    test_budget_telemetry.py # Budget inside TelemetryModule end-to-end
    test_routing_stack.py    # Router with full module stack
  security/
    test_budget_security.py  # Bypass, isolation, injection, overflow
    test_routing_security.py # Downgrade, injection, isolation, audit
```

### Open Questions

None — all decisions resolved through interactive build session.

### Key Design Principles

- **Budget IS telemetry** — one module, one cost concern. Can't bypass budget without bypassing telemetry.
- **Router IS the provider** — replaces adapter at innermost position. All observability wraps uniformly.
- **Enforcement is configurable** — enterprise (warn, open) vs federal (block, closed). One toggle governs both.
- **OTel is the durable store** — in-memory accumulators for enforcement, external collectors for audit/queries.
- **Per-agent isolation** — shared-nothing budget tracking. Maps to DID identity.
- **Fail closed in federal mode** — unknown classification, exceeded budget, missing scope all raise errors.

### Components to Build

**Budget (extends TelemetryModule):**
- `modules/telemetry.py` — extend with BudgetAccumulator, period tracking, enforcement logic (~80 LOC added)
- `types.py` — add `BudgetExceededError` exception
- `config.toml` — add budget fields to `[modules.telemetry]`
- `config.py` — validate new budget config keys

**Routing (new module):**
- `modules/routing.py` — Router class implementing LLMProvider, holds multiple adapters (~150 LOC)
- `config.toml` — add `[modules.routing.rules.*]` sections
- `registry.py` — add routing kwarg to `load_model()`, wire Router as innermost provider
- `config.py` — add routing config validation

### Architecture Diagram

```
load_model("anthropic", telemetry={...budget...}, budget_scope="agent:007")
    |
    v
Otel (root span, GenAI attributes)
  |
  v
Telemetry + Budget (cost calc, spend tracking, limit enforcement)
  |  - Pre-check: cumulative >= limit? -> block/warn
  |  - Pre-check: estimated cost > per_call_max? -> block
  |  - Post-call: deduct actual cost_usd
  |  - Emit OTel budget attributes + structured logs
  |
  v
Audit (PII-safe metadata logging)
  |
  v
Security (PII redaction, request signing)
  |
  v
Retry (exponential backoff + jitter)
  |
  v
Fallback (provider chain)
  |
  v
RateLimit (token bucket per provider)
  |
  v
Adapter (single provider)
  -- OR --
Router (classification -> provider+model)
  |- AnthropicAdapter (CUI)
  |- OpenAIAdapter (unclassified)
  |- OllamaAdapter (air-gapped)
```

### Research Insights (via /deepen)

**Enriched**: 2026-02-21 | **Sources**: Codebase analysis (telemetry.py, registry.py, rate_limit.py, config.py, exceptions.py, base.py, adapters/base.py), OTel GenAI semantic conventions, NIST 800-53 controls, scheduler hardening solution, LiteLLM patterns

#### D-188 — Budget-Telemetry Integration: Codebase Contract

TelemetryModule is 106 LOC (`modules/telemetry.py`). Adding budget tracking requires extending several precise contracts:

1. **`_VALID_CONFIG_KEYS` (line 14)**: Must add budget keys — `monthly_limit_usd`, `daily_limit_usd`, `per_call_max_usd`, `alert_threshold_pct`, `enforcement`, `budget_scope`. Without this, `validate_config_keys()` rejects them at construction.

2. **Constructor validation (line 35-54)**: Budget limits must be validated `>= 0` using same pattern as cost fields. `enforcement` must be validated against `{"warn", "block"}`. `budget_scope` format must be validated (see D-189 insights).

3. **`invoke()` flow (line 69-105)**: Budget checks inject at two points:
   - **Pre-call** (before `self._inner.invoke()`): Check cumulative + estimate against limits. This is new — current invoke() has no pre-call logic.
   - **Post-call** (after response): Deduct actual `cost_usd` from accumulator. This hooks after `_calculate_cost()`.

4. **OTel span attributes (line 84-85)**: Currently sets `arcllm.telemetry.duration_ms` and `arcllm.telemetry.cost_usd`. Budget adds: `arcllm.budget.scope`, `arcllm.budget.cumulative_usd`, `arcllm.budget.daily_usd`, `arcllm.budget.monthly_limit_usd`, `arcllm.budget.daily_limit_usd`, `arcllm.budget.enforcement`, `arcllm.budget.action` (allowed/warned/blocked).

5. **OTel GenAI semantic conventions**: The official `gen_ai.*` namespace (set by OtelModule at lines 212-220) does NOT include cost or budget attributes. Our `arcllm.budget.*` namespace is correct — custom vendor attributes under our own prefix.

**Key pattern**: TelemetryModule returns `response.model_copy(update={"cost_usd": cost})` (line 87). Budget warning metadata should use the same pattern: `response.model_copy(update={"cost_usd": cost, "metadata": {"budget_warning": True}})` when in warn mode.

#### D-189 — Budget Scope: Validation from Solutions Archive

From the scheduler hardening solution (Fix 1: Unicode NFKC normalization), scope strings need the same defense:

```python
import re
import unicodedata

_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_:.\-]{0,127}$")

def _validate_budget_scope(scope: str) -> None:
    normalized = unicodedata.normalize("NFKC", scope)
    if normalized != scope:
        raise ArcLLMConfigError(
            f"budget_scope contains non-ASCII characters: {scope!r}"
        )
    if not _SCOPE_RE.match(scope):
        raise ArcLLMConfigError(
            f"Invalid budget_scope '{scope}'. Must be lowercase alphanumeric "
            "with colons, dots, hyphens. Max 128 chars. Example: 'agent:agent-007'"
        )
```

**Why**: Prevents scope string injection (`agent:007; DROP TABLE`), path traversal (`../../../etc`), and Unicode homoglyph attacks. Mirrors `_validate_provider_name()` in `config.py:126-144` which uses `_PROVIDER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")`.

**Accumulator key**: Use validated scope string directly as dict key. No need for hashing — the regex ensures it's safe for use as a dict key, OTel attribute value, and log field.

#### D-193 — Budget Storage: In-Memory Accumulator Pattern

The `_bucket_registry` pattern in `rate_limit.py:63` is the exact model:

```python
# Module-level shared state (same pattern as rate_limit.py)
_budget_registry: dict[str, "BudgetAccumulator"] = {}

def _get_or_create_accumulator(scope: str) -> "BudgetAccumulator":
    if scope not in _budget_registry:
        _budget_registry[scope] = BudgetAccumulator()
    return _budget_registry[scope]

def clear_budgets() -> None:
    """For test isolation — must be called in registry.clear_cache()."""
    _budget_registry.clear()
```

**Critical**: Must add `clear_budgets()` call to `registry.py:clear_cache()` (line 21-35) alongside existing `clear_buckets()` and `reset_sdk()`. Without this, tests will share budget state.

**Floating-point precision**: Current `_calculate_cost()` uses Python `float` (IEEE 754 double). For budget tracking, accumulated costs over thousands of calls could drift. However:
- At $0.001 per call, 10,000 calls = $10. Float64 has ~15 significant digits. Drift at this scale is ~1e-12 USD — irrelevant.
- Using `Decimal` would break compatibility with existing `cost_usd: float` on LLMResponse and all OTel attributes (which are float64).
- **Decision**: Keep `float`. The accumulator is for enforcement, not billing. OTel spans have the per-call exact amounts for billing reconciliation.

**Race condition analysis**: Python's GIL protects dict operations in CPython. For asyncio (single-threaded), there's no true concurrency risk on `_budget_registry[scope] += cost`. However, if the pre-check and post-deduct are separated by an `await`:
```python
# SAFE pattern: check + deduct in same synchronous block after await
response = await self._inner.invoke(...)  # yields to event loop
cost = self._calculate_cost(response.usage)
accumulator.deduct(cost)  # synchronous — no yield between check and write
```
The scheduler hardening solution (Fix 3: stale circuit breaker) warns about reading state before an operation and assuming it's still valid after. Our accumulator must read-check-deduct atomically (no `await` between check and deduct).

#### D-194 — Budget Periods: Reset Edge Cases

**Period boundary edge case**: A call starts at 23:59:59.999 on Jan 31, completes at 00:00:00.500 on Feb 1.

- **Which period is charged?** The period when the cost is *deducted* (post-call). Since deduction happens after `await self._inner.invoke()`, the call is charged to February.
- **Is this correct?** Yes — it's the standard "cash basis" accounting model. The cost isn't incurred until the response arrives. This aligns with Anti-Deficiency Act obligation timing.

**Daily reset logic**:
```python
from datetime import date, datetime, timezone

class BudgetAccumulator:
    def __init__(self) -> None:
        self._monthly_spend: float = 0.0
        self._daily_spend: float = 0.0
        self._current_month: int = 0  # YYYYMM
        self._current_day: int = 0    # YYYYMMDD

    def _maybe_reset(self) -> None:
        now = datetime.now(timezone.utc)
        month_key = now.year * 100 + now.month
        day_key = month_key * 100 + now.day
        if month_key != self._current_month:
            self._monthly_spend = 0.0
            self._daily_spend = 0.0
            self._current_month = month_key
            self._current_day = day_key
        elif day_key != self._current_day:
            self._daily_spend = 0.0
            self._current_day = day_key
```

**Why integer keys (YYYYMM, YYYYMMDD) instead of date objects**: Cheaper comparison, no timezone localization complexity, no DST edge cases (we use UTC).

**NIST SA-2 compliance**: The accumulator acts as a "sub-allotment tracker." NIST SA-2 requires "the organization determines, documents, and allocates resources required to adequately protect the information system." Our per-agent budget scope maps directly to per-agent resource allocation.

#### D-199 — Enforcement: Error vs Warning Patterns

**BudgetExceededError** must fit the existing exception hierarchy (`exceptions.py`):

```python
class ArcLLMBudgetError(ArcLLMError):
    """Raised when a budget limit would be exceeded."""

    def __init__(
        self,
        scope: str,
        limit_type: str,  # "monthly", "daily", "per_call"
        limit_usd: float,
        current_usd: float,
        estimated_usd: float | None = None,
    ) -> None:
        self.scope = scope
        self.limit_type = limit_type
        self.limit_usd = limit_usd
        self.current_usd = current_usd
        self.estimated_usd = estimated_usd
        super().__init__(
            f"Budget exceeded for {scope}: {limit_type} limit ${limit_usd:.2f}, "
            f"current ${current_usd:.2f}"
            + (f", estimated ${estimated_usd:.2f}" if estimated_usd else "")
        )
```

**Why `ArcLLMBudgetError` not `BudgetExceededError`**: Follows existing naming convention (`ArcLLM` prefix on all exceptions). Extends `ArcLLMError` (not `ArcLLMConfigError`) because this is a runtime error, not a configuration error.

**Warn mode metadata**: `response.metadata` is `dict[str, Any] | None` on LLMResponse. When warning:
```python
metadata = response.metadata or {}
metadata["budget_warning"] = True
metadata["budget_scope"] = self._scope
metadata["budget_cumulative_usd"] = accumulator.monthly_spend
response = response.model_copy(update={"metadata": metadata})
```

**Alert threshold**: At 80% (default), emit a structured log + OTel event but don't block/warn. This is an early notification before the hard limit:
```python
if accumulator.monthly_spend / monthly_limit >= alert_threshold:
    tel_span.add_event("budget_alert", {
        "arcllm.budget.scope": self._scope,
        "arcllm.budget.monthly_spend_usd": accumulator.monthly_spend,
        "arcllm.budget.monthly_limit_usd": monthly_limit,
        "arcllm.budget.threshold_pct": alert_threshold * 100,
    })
```

#### D-190 — Router Stack Position: Implementation Path

Router replaces the adapter at the innermost position. In `registry.py`, the current flow is:

```python
# Line 205: adapter created
result: LLMProvider = adapter_class(config, model_name, resolved_api_key=resolved_api_key)
# Lines 208-253: modules wrap in order
```

Router changes this to:

```python
# If routing enabled, Router replaces the single adapter
routing_config = _resolve_module_config("routing", routing)
if routing_config is not None:
    from arcllm.modules.routing import RoutingModule
    result = RoutingModule(routing_config, ...)  # holds multiple adapters internally
else:
    result = adapter_class(config, model_name, resolved_api_key=resolved_api_key)
# Then normal module wrapping continues
```

**Critical**: Router must implement `LLMProvider` protocol (has `name`, `model_name`, `invoke()`, `validate_config()`, `close()`). It selects which internal adapter to delegate to based on `classification` kwarg in `invoke()`.

**Router.close()**: Must close ALL internal adapters:
```python
async def close(self) -> None:
    for adapter in self._adapters.values():
        await adapter.close()
```

**Router.name/model_name**: Return the default route's provider name (for logging/span context when no classification is provided).

#### D-197/D-198 — Classification Routing: kwargs Flow

Classification passes through the entire module stack via `**kwargs`:

```python
await model.invoke(messages, tools, classification="cui")
```

Every module's `invoke()` already passes `**kwargs` to `self._inner.invoke()`:
- `TelemetryModule.invoke()` line 77: `await self._inner.invoke(messages, tools, **kwargs)`
- `AuditModule.invoke()` line 48: `await self._inner.invoke(messages, tools, **kwargs)`
- `BaseModule.invoke()` line 87: `await self._inner.invoke(messages, tools, **kwargs)`

So `classification` kwarg naturally flows down to the Router without any intermediate module changes.

**Router selection logic**:
```python
async def invoke(self, messages, tools=None, **kwargs):
    classification = kwargs.pop("classification", self._default_classification)
    # ... validate classification, select adapter ...
    return await selected_adapter.invoke(messages, tools, **kwargs)
```

**Important**: `kwargs.pop()` not `kwargs.get()` — remove classification before passing to the actual adapter, since adapters don't know about classification.

#### D-191 — Eager Loading: Multi-Adapter Init

Each route in config maps to a separate adapter instance. At Router init:

```python
def __init__(self, routing_config: dict[str, Any]) -> None:
    self._adapters: dict[str, LLMProvider] = {}
    for classification, rule in routing_config["rules"].items():
        provider_name = rule["provider"]
        model_name = rule["model"]
        # Load adapter using existing registry machinery
        adapter_class = _get_adapter_class(provider_name)
        provider_config = load_provider_config(provider_name)
        adapter = adapter_class(provider_config, model_name)
        self._adapters[classification] = adapter
```

**Gotcha from codebase**: `_get_adapter_class()` and provider config are cached at module level (`_adapter_class_cache`, `_provider_config_cache`). Router can reuse these caches. But each adapter instance needs its own `httpx.AsyncClient` — do NOT share clients across adapters.

**Vault key resolution**: Each adapter may need a different vault path. Router must resolve API keys per-provider, same as `load_model()` does at lines 187-199.

#### D-195 — Pre-Flight Estimate: max_tokens Resolution

`BaseAdapter._resolve_defaults()` (adapters/base.py:74-81) resolves max_tokens from kwargs > model meta > default (4096). The pre-flight estimate needs the same resolution:

```python
# In TelemetryModule.invoke(), before inner call:
max_tokens = kwargs.get("max_tokens")
if max_tokens is None and hasattr(self._inner, "_model_meta"):
    max_tokens = self._inner._model_meta.max_output_tokens if self._inner._model_meta else 4096
else:
    max_tokens = max_tokens or 4096

estimated_cost = max_tokens * self._cost_output / 1_000_000
```

**But TelemetryModule can't access inner adapter's _model_meta** — it's wrapped behind potentially multiple modules. Simpler approach: accept `max_tokens` from kwargs, fall back to `defaults.max_tokens` from global config (4096). The estimate is intentionally conservative (upper bound).

#### D-200 — Unknown Classification: Config-Driven Behavior

From user's input: "enterprise mode (warn, route to default) vs federal mode (block, fail closed)."

Router config needs `default_classification` and respects the same `enforcement` toggle:

```toml
[modules.routing]
enabled = true
enforcement = "warn"           # "warn" = enterprise, "block" = federal
default_classification = "unclassified"

[modules.routing.rules.cui]
provider = "anthropic"
model = "claude-sonnet-4-6"

[modules.routing.rules.unclassified]
provider = "openai"
model = "gpt-4o-mini"
```

When `classification` kwarg doesn't match any rule:
- `enforcement = "warn"`: Log warning, route to `default_classification`
- `enforcement = "block"`: Raise `ArcLLMConfigError("Unknown classification '{classification}' and enforcement is 'block'")`

#### D-201 — Testing: Exact Test File Structure

Following existing test patterns (test_telemetry.py is 385 lines, test_rate_limit.py):

```
tests/
  test_budget.py               # Budget accumulator, limits, periods, enforcement
    TestBudgetAccumulator      # Reset logic, period boundaries, float precision
    TestBudgetEnforcement      # Block mode, warn mode, alert threshold
    TestBudgetPreFlight        # Per-call max estimation
    TestBudgetValidation       # Config validation, scope validation
    TestBudgetOtelAttributes   # Span attributes for budget events

  test_routing.py              # Classification routing
    TestRoutingSelection       # Classification -> adapter mapping
    TestRoutingUnknown         # Unknown classification behavior
    TestRoutingAdapterLifecycle # Eager loading, close(), health
    TestRoutingValidation      # Config validation, rules structure
    TestRoutingKwargsFlow      # classification kwarg pop + passthrough

  test_budget_telemetry.py     # Integration: budget inside telemetry stack
    TestBudgetTelemetryIntegration  # Full module stack with budget + audit + otel

  test_routing_stack.py        # Integration: router with full module stack
    TestRoutingStackIntegration # Router as innermost with full module wrapping

  security/
    test_budget_security.py    # Budget bypass, scope injection, negative costs
    test_routing_security.py   # Classification downgrade, adapter isolation
```

**Test helper pattern** from test_telemetry.py: `_make_inner()` creates a MagicMock with `spec=LLMProvider`. Budget tests need an extended version that also returns configurable `cost_usd` values.

### Security Edge Cases Discovered

1. **Negative token count injection**: If a compromised adapter returns `Usage(output_tokens=-1000)`, `_calculate_cost()` returns negative cost, which would increase budget headroom. **Mitigation**: Clamp cost to `max(0.0, cost)` before deducting from accumulator.

2. **Config override via telemetry kwarg**: `load_model(telemetry={"enforcement": "warn", "monthly_limit_usd": 999999})` could override strict config.toml limits. **Mitigation**: `_resolve_module_config()` merges kwarg over config.toml defaults (line 115: `{**config_settings, **kwarg_value}`). This is by design — caller kwargs override config.toml. But if org-wide limits must be enforced, add a `budget_max_override_usd` ceiling in config.toml that can't be exceeded by kwargs.

3. **Scope string in logs**: Budget scope appears in structured logs. From `_logging.py`, `_sanitize()` already escapes `\n`, `\r`, `\t`. Combined with regex validation on the scope string, log injection is mitigated.

4. **Race condition window**: Between pre-check and post-deduct, another concurrent invoke() on the same scope could slip through. In asyncio single-thread, this only matters if there's an `await` between check and deduct — which there is (`await self._inner.invoke()`). **Mitigation**: Pre-check is a conservative estimate. The worst case is two concurrent calls both pass pre-check but together exceed the limit by one call's cost. This is acceptable — budget is an estimate, not a billing system. OTel has the exact record.

5. **Adapter isolation in Router**: Each adapter has its own `httpx.AsyncClient`. No shared state between adapters. A compromised adapter cannot access another adapter's client, keys, or state. This is inherent in the current `BaseAdapter` design.

### New Risks Discovered

1. **Clock manipulation on budget periods** — If system clock jumps backward (NTP correction, VM snapshot restore), monthly/daily accumulators could reset prematurely. Mitigation: Use monotonic clock for within-period tracking, wall clock only for period boundary detection. Log clock jumps as audit events.

2. **Budget exhaustion as DoS** — A malicious caller could deliberately burn budget to deny service to a legitimate agent sharing the scope. Mitigation: D-189's per-agent scope isolation prevents this. Each agent has its own accumulator.

3. **Router config hot-reload** — If config.toml is modified while Router is running, stale routing rules persist until restart. This is consistent with all other modules (none support hot-reload). Document as known limitation.

4. **Multi-provider cost inconsistency** — Router routes to different providers with different pricing. TelemetryModule's cost rates are set at init from a single provider's metadata. With Router, the cost rate must match the actually-selected provider. **Critical**: TelemetryModule must get pricing from the response's actual provider, not the init-time config. This requires Router to inject pricing metadata or TelemetryModule to look up pricing dynamically.

### Implementation Priority

Based on codebase analysis, the implementation order should be:

1. **BudgetAccumulator + scope validation** (standalone, no module changes)
2. **Extend TelemetryModule with budget** (pre/post hooks around existing invoke)
3. **Extend exceptions.py** with `ArcLLMBudgetError`
4. **Update config.toml** (budget fields under `[modules.telemetry]`)
5. **RoutingModule** (new module implementing LLMProvider)
6. **Update registry.py** (add routing/budget_scope kwargs to `load_model()`)
7. **Tests** (unit → integration → security)

Estimated total new LOC: ~230 (budget ~80, routing ~150)

---

## Bio-Memory (ArcAgent)

**Date**: 2026-02-21
**Feature**: Biologically-inspired memory module for arcagent
**Source**: ARC-Memory-System-v2.1-Final.md (Section 6: arc-memory / Agent)
**Status**: BUILD COMPLETE — ready for `/deepen` or `/specify`

### Context

Replaces the existing markdown-memory module as the default memory system. Implements biologically-inspired memory: working memory (scratchpad), identity (how-i-work.md), episodes (significant moments), retrieval (graph traversal), and consolidation (LLM-driven knowledge integration). Team memory is a separate build.

### Federal Auto-Applied Mandates

| # | Decision | Mandate | Tag |
|---|----------|---------|-----|
| D-202 | All memory writes emit audit events | NIST 800-53 AU-2 | `auto-applied: federal-mandate` |
| D-203 | Audit logs tamper-evident (append-only JSONL + OTel) | NIST 800-53 AU-9 | `auto-applied: federal-mandate` |
| D-204 | Memory content encrypted at rest | FIPS 140-2/3 | `auto-applied: federal-mandate` |
| D-205 | Memory content validated on read (integrity check) | OWASP ASI-06 | `auto-applied: federal-mandate` |
| D-206 | PII/CUI filtered before storage | NIST 800-53 SI-12 | `auto-applied: federal-mandate` |
| D-207 | Per-agent memory isolation | NIST 800-53 AC-3 | `auto-applied: federal-mandate` |
| D-208 | Entity file classification tracking in frontmatter | NIST 800-53 AC-16 | `auto-applied: federal-mandate` |

### Architecture Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|
| D-209 | Architecture | Scope of this build | Bio-memory is default module; markdown-memory becomes simpler alternative | New default, existing kept as opt-in alternative |
| D-210 | Architecture | Module mutual exclusivity | Mutually exclusive via config. `[modules.memory]` = bio-memory, `[modules.markdown-memory]` = alternative. Both enabled = ConfigError. | Priority: simplicity. Clear, no ambiguity. |
| D-211 | Architecture | Codebase location | Bio-memory replaces `modules/memory/`. Existing markdown-memory moves to `modules/markdown_memory/`. | Priority: simplicity. Bio-memory IS the default memory. |
| D-212 | Architecture | Agent-team relationship | Agent memory fully standalone. Team is optional overlay discovered via Module Bus events. No hard dependency. | Priority: simplicity. Works solo or with team. |
| D-213 | Architecture | Internal structure | Facade (BioMemoryModule) + internal helpers: WorkingMemory, IdentityManager, EpisodeStore, Retriever, Consolidator. | Priority: simplicity. Matches existing pattern. |
| D-214 | Architecture | Disk layout | `{workspace}/memory/` containing working.md, how-i-work.md, episodes/. | Priority: simplicity. Under existing workspace convention. |
| D-215 | Architecture | LLM access | Use existing eval model pattern from `model_helpers.py`. Same [eval] config. | Priority: simplicity. Zero new infrastructure. |
| D-216 | Architecture | Module Bus events | Map to existing events: assemble_prompt (inject), post_respond (working.md), shutdown (consolidate). No new core events. | Priority: simplicity. No core changes. |

### Data Model Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|
| D-217 | Data Model | working.md format | YAML frontmatter (topics, semantic tags, entity refs, importance, turn number, timestamp) + LLM-written markdown body | Frontmatter aids search/graph traversal. Body is turn state. |
| D-218 | Data Model | Episode file format | Rich YAML frontmatter (date, type, significance, participants, emotional_signal, entities_touched, source_agent, tags, links_to) + LLM narrative body | Follows PRD. Auditable metadata. |
| D-219 | Data Model | how-i-work.md format | Minimal frontmatter (last_updated, token_count, version) + LLM-written body. 500 token budget. | LLM decides structure. Budget is the constraint. |
| D-220 | Data Model | Episode naming | `YYYY-MM-DD-{llm-slug}.md` | Human-readable, date-sortable. |

### Tool Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|
| T-001 | API/Tools | Agent tools | Four tools: `memory_search`, `memory_note`, `memory_recall`, `memory_reflect`. Full agent control over memory. | Agent needs tools to create, update, use, manage memory. |

### Observability Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|
| D-221 | Observability | Telemetry | Follow existing OTel pattern via `AgentTelemetry.audit_event()`. 7 event types: retrieval, consolidation, note_created, working_updated, identity_updated, episode_created, reflect. | Priority: compliance (NIST AU-2) + simplicity. |

### Security Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|
| D-222 | Security | Memory poisoning defense | Sanitize on write (NFKC, strip zero-width/control chars, length limits) + boundary markers on retrieval. Reuses existing patterns. | Priority: security (ASI-06) + simplicity. |
| D-223 | Security | File access protection | Bash veto pattern for memory paths. Episodes append-only, how-i-work.md consolidation-only. **Sanitizer in `arcagent/utils/`** (shared, not in either memory module). | Priority: security. Self-contained at package level. |

### Integration Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|
| D-224 | Integration | Retrieval trigger | Inject how-i-work.md + working.md via assemble_prompt. Agent uses memory tools for on-demand retrieval. No automatic retrieval decision logic. | "LLM is the intelligence layer." |
| D-225 | Integration | Light consolidation | On agent:shutdown via spawn_background. Non-blocking. Failure-tolerant. | Priority: simplicity + scalability. |
| D-226 | Integration | Deep consolidation | Scheduler module + CLI trigger. No internal timer. | Separation of concerns. |

### Performance Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|
| D-227 | Performance | Token budget enforcement | Use existing `CHARS_PER_TOKEN` from `arcagent/utils/io.py`. Character-based estimation. | Priority: simplicity. No new dependencies. |
| D-228 | Performance | Working.md I/O | Synchronous write in post_respond. Sub-ms for ~2KB. | Priority: simplicity. No benefit to async. |
| D-229 | Performance | Retrieval search | Grep-based with wiki-link following (one hop). No database, no index. | Follows design doc primary path. Priority: simplicity. |

### Extensibility Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|
| D-230 | Extensibility | Config structure | Follow design doc. Paths, budgets, retrieval, consolidation settings. All with sensible defaults. Zero-config works. | Priority: simplicity. |
| D-231 | Extensibility | Team discovery | Module Bus event (`team:memory_available`). Zero coupling to team module. Retrieval scope expands when team is present. | Priority: simplicity + scalability. |

### Testing Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|
| TS-001 | Testing | Strategy | 70/20/10 split. Unit per helper class (mock eval model), integration through bus, e2e with real model. | Follows project test pyramid. |

### Deployment Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|
| DP-001 | Deployment | Migration | Clean start. No migration. Bio-memory creates fresh state at `memory/`. Existing markdown-memory data untouched. | Priority: simplicity. Agent learns organically. |

### CLI Decisions

| # | Category | Question | Decision | Rationale |
|---|----------|----------|----------|-----------|
| U-001 | CLI | Commands | Full design doc CLI: status, identity, episodes, working, search, consolidate (light/deep/dry-run). | Priority: simplicity. Follows PRD. |

### Tier Variations Summary

| Component | Federal | Enterprise | Personal |
|-----------|---------|------------|----------|
| Audit events | Required (block without) | Default on (warn if disabled) | Off (opt-in) |
| Encryption at rest | AES-256 mandatory | Default on | Off |
| PII filtering | Block storage | Warn | Skip |
| Classification frontmatter | Required | Optional | Skip |
| Memory isolation | Always on | Always on | Always on |
| Content sanitization | Always on | Always on | Always on |

### Build Order (from Design Doc)

**Phase 1 — Core (v0.1)**: working.md lifecycle, how-i-work.md, grep-based retrieval, token budgets, memory tools, light consolidation
**Phase 2 — Intelligence (v0.2)**: episode recording, deep consolidation (entity + graph pass), promotion gate hooks
**Phase 3 — Scale (v0.3)**: vector search fallback, NATS memory events, adaptive consolidation
**Phase 4 — Harden (v0.4)**: classification access control, encryption at rest, memory provenance, FedRAMP audit

---

### Research Insights (from `/deepen`)

**Date**: 2026-02-21
**Agents**: 3 parallel web research agents (memory poisoning, grep-based retrieval, LLM consolidation patterns)
**Sources**: 50+ papers, OWASP guides, production systems (Mem0, Zep/Graphiti, SimpleMem, LangMem, Hindsight, A-MEM, MemGPT, MAIF)

---

#### Architecture Research Insights

**Biological memory mapping is well-validated.** Complementary Learning Systems (CLS) theory maps directly: fast hippocampal system = episodes (specific, recent) + slow neocortical system = entity files (generalized, stable). Hindsight (NeurIPS 2025) explicitly implements this. Sleep-like replay reduces catastrophic forgetting (Nature Communications 2022), validating sleep cycle consolidation.

**Reconsolidation trigger.** Neuroscience: memories destabilize when prediction error occurs (PNAS 2022). For bio-memory: trigger entity rewrite when new episodes diverge from existing entity profiles (cosine distance), not on fixed schedule. Low divergence = skip consolidation = save tokens.

**Entity files should be derived artifacts.** Hindsight's key pattern: profiles synthesized fresh from source facts, not continuously rewritten. Episode store is append-only. Lossy consolidation becomes recoverable. This changes the failure model.

**Tiered fidelity for retrieval.** Adaptive Focus Memory (AFM): Full/Compressed/Placeholder per retrieved item based on relevance and decay. Preserves more information than uniform truncation under fixed token budgets.

---

#### Data Model Research Insights

**Frontmatter-first search is a production pattern.** Two-pass: (1) grep frontmatter block for tag/entity matches, (2) full-text on matched subset. Same effect as inverted index without database.

**Dendron schema reference.** Fields: `id` (UUID), `title`, `desc` (search abstract), `tags`, `created`/`updated`, `parent`/`children`. The `desc` field for search display is worth adopting.

**`links_to` best practices:**
- Store explicit outbound links as YAML list (O(1) lookup)
- Include `entity_type` (person, concept, decision, event)
- Include `last_accessed` for temporal decay
- Store search-optimized summary in frontmatter

**Bidirectional links.** Compute backlinks at read-time via reverse index (frontmatter grep on startup), not written into files. On delete: scan for `[[deleted-slug]]`, replace with tombstone. On rename: ripgrep-replace old slug across all files.

---

#### Security Research Insights (Memory Poisoning)

**Memory poisoning != prompt injection.** Prompt injection affects one response. Memory poisoning reshapes all future behavior permanently.

**Confirmed attacks (2025):** Google Gemini memory attack (hidden document prompts), Gemini calendar invite poisoning (73% High-Critical), MINJA (95-100% injection success via normal queries, bypasses all moderation).

**Wiki-link injection vectors:**
- Dangling link injection: `[[AttackerEntity]]` auto-creates on traversal
- Homoglyph: `[[Jоhn Smith]]` (Cyrillic о) creates shadow node. NFKC at write time is only defense.
- Link flood: expands traversal surface and storage
- Path traversal: peripheral entity links to core entity, consolidation blends content
- Link-as-instruction: `[[SYSTEM: ignore...]]` if links treated as commands

**Defense: Entity registry.** Only registered entity names followed. Unknown links flagged, not auto-created. Rate-limit entity creation per session.

**Boundary markers are mitigation only.** 480 scenarios: "very little difference between Markdown and XML." Best practice: randomize marker strings per-session (UUID-embedded tags).

**Promptware Kill Chain maps to bio-memory:** Initial Access → Persistence in episode → Privilege Escalation via consolidation → Lateral Movement via wiki-links → Command & Control.

**MINJA is largely undefended.** Plausible reasoning chains bypass all published moderation. Semantic outlier detection is the only partial defense — open research problem.

**MAIF pattern.** Ed25519 signatures per write + hash chain between versions + agent-ID provenance. Verification <0.1ms.

**Layered defense model:**
- L0: Input ingestion (strip Unicode, normalize, reject marker strings)
- L1: Write validation (Pydantic schema, field limits, entity allowlist, source trust tags)
- L2: Cryptographic integrity (Ed25519 + SHA-256 hash chain)
- L3: Consolidation security (provenance-weighted, immutable fields, contradiction detection, semantic diff)
- L4: Graph traversal (entity registry, depth limit, cycle detection, TTL)
- L5: Monitoring (behavioral baseline, trend tracking, human review for identity changes)

---

#### Integration Research Insights (Consolidation)

**Don't rely on 1-10 scoring.** Absolute scoring most vulnerable to adversarial manipulation (46-68% attack success). Two-gate approach: (1) deterministic pre-filter (entropy, semantic divergence, novel entity count), (2) LLM judgment only for passing content. SimpleMem: `H(W_t) = α·|E_new|/|W_t| + (1-α)·(1-cos(E(W_t), E(H_prev)))`, discard below τ=0.35.

**Entity rewrite safety.** Prompt pattern: "for each fact you drop, explicitly state why. Do not drop unless directly superseded." Forces LLM to justify lossy compression.

**Contradiction handling strategies:**
1. Bi-temporal invalidation (Zep/Graphiti): old fact gets `t_invalid`, both persist. Best for entity files.
2. CRUD resolution (Mem0): ADD/UPDATE/DELETE/NOOP. Simpler, loses history.
3. Confidence decay (Hindsight): penalty rather than replacement. Best for `how-i-work.md`.

**Link discovery grounding.** Graphiti uses 5 separate specialized prompts (not one combined). Separation reduces hallucinated relationships. Rule: never find connections without actual episode text. Names alone produce hallucinations.

**Crash safety.** Single-file: `os.replace()` + `os.fsync()`. Multi-file: write-ahead manifest (pending → write each file → complete). On restart: re-run pending.

**Idempotency.** Content-hash gating: hash all input episodes, skip if hash matches stored hash. Use `temperature=0.0` for consolidation.

---

#### Performance Research Insights (Retrieval & Scale)

**Grep wins at small scale.** LlamaIndex 2026: filesystem/grep correctness 8.4 vs RAG 6.4, relevance 9.6 vs 8.0. Crossover ~100 docs. GrepRAG: ripgrep 38.61% exact match vs GraphCoder 19.44% for named entities. 14x faster latency.

**Grep failure modes:**
- Vocabulary mismatch ("athletic footwear" misses "shoes"). BM25: ~0.72 recall. Hybrid: ~0.91.
- Keyword ambiguity: high-frequency tokens produce noise
- Context fragmentation: overlapping matches without deduplication

**Scale ceiling.** ~500-2K files for <100ms without caching. With BM25 pre-indexing: 10K+ files at microsecond queries.

**Token budget overflow strategy:**
1. Entity names + scores only (near-zero tokens)
2. Full content for top-N by relevance
3. Tiered compression (full → summary → pointer)
4. Truncate lowest-degree nodes in traversal subgraph

**Skip-least-connected caveat.** Safe only if low relevance AND low betweenness centrality. Degree-1 node may be sole bridge between clusters.

**Cost optimization (ranked):**
1. Skip unchanged entities (content-hash) — 80-90% reduction
2. Entropy pre-filter before LLM calls
3. Adaptive retrieval depth (k=3 simple, k=20 cross-entity)
4. Model tiering (cheap for significance/dedup, expensive for contradictions)
5. Batch consolidation at cluster similarity >0.85
6. All consolidation offline/async

---

#### Key References

| Paper/System | Relevance | URL |
|---|---|---|
| MINJA (2025) | Memory injection via queries, 95%+ success | arxiv.org/html/2503.03704v4 |
| Promptware Kill Chain (2026) | 7-stage attack framework for persistent agents | arxiv.org/pdf/2601.09625 |
| OWASP AI Agent Security | SecureAgentMemory reference class | cheatsheetseries.owasp.org |
| MAIF | Cryptographic memory format (Ed25519 + hash chain) | github.com/mbhatt1/maif |
| Unit42 Memory Persistence | Indirect prompt injection to long-term memory | unit42.paloaltonetworks.com |
| GrepRAG (2026) | grep vs vector empirical benchmark | arxiv.org/html/2601.23254v1 |
| REMINDRAG (2025) | 58.8% token reduction via guided traversal | arxiv.org/pdf/2510.13193 |
| AFM (2025) | Tiered fidelity (Full/Compressed/Placeholder) | arxiv.org/html/2511.12712 |
| SimpleMem (2026) | 30x token reduction, entropy filtering | arxiv.org/html/2601.02553v1 |
| Hindsight (NeurIPS 2025) | Belief confidence, background synthesis | arxiv.org/html/2512.12818v1 |
| Zep/Graphiti (2025) | Bi-temporal contradiction handling, 5 prompts | arxiv.org/html/2501.13956v1 |
| A-MEM (NeurIPS 2025) | Zettelkasten-style dynamic linking | arxiv.org/abs/2502.12110 |
| CLS + Sleep Replay (2022) | Biological basis for sleep consolidation | Nature Communications |
| Reconsolidation/PE (2022) | Prediction error triggers memory update | PNAS |

---

#### Research Gaps

1. **MINJA is undefended** — plausible malicious reasoning bypasses all moderation. Open research problem.
2. **No wiki-link-specific graph injection research** — extrapolated from text-level GIAs (NeurIPS 2024). Need purpose-built red teaming.
3. **Consolidation LLM injection defenses are immature** — multi-model committee (3-7x cost) is most effective but expensive. Sleep-cycle amortizes cost.
4. **No empirical grep recall for natural-language notes** — GrepRAG covers code. Vocabulary mismatch rate for prose likely higher than 28%.
5. **Multi-file atomic transactions** — no turnkey solution. All systems use single-file writes or eventual consistency with manifests.
6. **Concurrent writes** — all reviewed systems are single-writer. Race conditions on backlinks under multi-agent scenarios are untested.

---

## Azure OpenAI Provider (SPEC-010)

**Date**: 2026-02-25
**Spec**: SPEC-010
**Feature**: Azure OpenAI Service adapter for ArcLLM (Azure AI Foundry / GCC)

### Architecture

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-232 | Inheritance strategy | Subclass `OpenaiAdapter` — override `name`, `_build_headers()`, `invoke()` only | Simplicity | All tiers: same adapter, no tier-specific behavior |
| D-233 | Class name | `Azure_openaiAdapter` with `# noqa: N801` (follows `Huggingface_TgiAdapter` precedent) | Simplicity | auto-applied: pattern-following |
| D-234 | Provider name | `azure_openai` (TOML filename + adapter module path convention) | Simplicity | auto-applied: pattern-following |

### Data Model

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-235 | TOML config structure | Standard `[provider]` + `[models.*]` sections. `api_key_env = "AZURE_OPENAI_API_KEY"` | Simplicity | auto-applied: pattern-following |
| D-236 | Model/deployment name mapping | Deployment name as model — user passes deployment name to `load_model()`. TOML model metadata is reference-only for pricing/capabilities | Simplicity | All tiers: deployment name is the model identifier |

### API Design

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-237 | URL construction | `{base_url}/openai/v1/chat/completions` — no query params. v1 API hard-fails (400) on `?api-version=` | Simplicity | auto-applied: deepened research |
| D-238 | Auth header | `api-key: {key}` (lowercase, case-sensitive). NOT `Authorization: Bearer` | Simplicity | auto-applied: deepened research |
| D-239 | URL normalization | `base_url.rstrip('/')` before URL construction — defensive one-liner | Simplicity | All tiers: prevents user config mistakes |
| D-240 | Content filter handling | Inherit base behavior — `_parse_response()` already handles `null` content via `.get('content')` and maps `content_filter` stop reason | Simplicity | All tiers: no additional code |

### Observability

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-241 | Telemetry | Inherit all OTel tracing/audit from module stack. `provider` = `"azure_openai"` in spans | Simplicity | auto-applied: pattern-following |

### Audit & Compliance

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-242 | Audit trail | Inherited from AuditModule in stack. Every invoke() logged. API key excluded | Security | auto-applied: federal-mandate (NIST 800-53 AU-2) |

### Security

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-243 | Token handling | Env var only, vault-backed in production. Never filesystem, never logs | Security | auto-applied: federal-mandate (NIST 800-53 IA-5) |
| D-244 | HTTPS enforcement | Existing `_validate_https` validator. Both `.azure.com` and `.azure.us` are HTTPS | Security | auto-applied: existing validator |
| D-245 | Startup validation | None — no key prefix validation, no URL domain validation. Let Azure API return auth errors | Simplicity | All tiers: avoid false positives from over-validation |

### Integration

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-246 | Error mapping | Raw body passthrough via `ArcLLMAPIError`. No Azure-specific error code parsing | Simplicity | All tiers: caller sees full Azure error JSON |

### Performance

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-247 | Connection pooling | Inherit `httpx.AsyncClient` pool from `BaseAdapter`. One pool per adapter instance | Simplicity | auto-applied: pattern-following |

### Extensibility

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-248 | Future auth extensibility | API-key only, no preparation for Managed Identity. `_build_headers()` is the natural extension point | Simplicity (YAGNI) | Future: subclass + override `_build_headers()` for Entra ID |

### Testing

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-249 | Test strategy | Add to parametrized `CLOUD_PROVIDERS` tests + Azure-specific test file | Simplicity | auto-applied: pattern-following |
| D-250 | URL regression test | Explicit URL assertion with `'?' not in url` guard against api-version regression | Security | Guards most likely regression path |

### Deployment

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-251 | Dependencies | Zero new dependencies. Uses existing httpx, pydantic, arcllm internals | Simplicity | auto-applied: pattern-following |

### Summary

**20 decisions total**: 13 auto-applied (patterns + mandates), 7 user-decided.
**Key insight**: This is a thin adapter (~30 LOC) because Azure OpenAI uses OpenAI-compatible format. All meaningful differentiation is in URL construction and auth header.
**Risk**: The only dangerous regression is someone adding `?api-version=` to the URL — guarded by explicit test.

---

## Slack Messaging Module (SPEC-011)

**Date**: 2026-02-25
**Spec**: SPEC-011
**Feature**: Bidirectional Slack DM messaging for ArcAgent via Socket Mode

### Architecture

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-252 | Module structure | Mirror Telegram: `__init__.py`, `bot.py`, `config.py`, `MODULE.yaml` | Simplicity | auto-applied: pattern-following |
| D-253 | Socket Mode lifecycle | `connect_async()` (non-blocking), `close_async()` (clean shutdown) | Simplicity | auto-applied: deepened research |
| D-254 | Event handler registration | `@app.event("message")` not `@app.message()` — catches all subtypes | Simplicity | auto-applied: deepened research |
| D-255 | Message processing | Inline with `asyncio.Lock` — no queue, no background task. Lock serializes overlapping messages | Simplicity | Simpler than Telegram's queue pattern |

### Data Model

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-256 | Session storage | Single-user, mirror Telegram: `{workspace}/slack/state.json` with `{user_id, session_id}`. 1 agent = 1 user = 1 session | Simplicity | Each agent has its own Slack connection |
| D-257 | Config fields | `enabled`, `allowed_user_ids: list[str]`, `max_message_length: int = 4000`, `bot_token_env_var`, `app_token_env_var` | Simplicity | auto-applied: pattern-following |

### API Design

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-258 | Response delivery | Regular DM reply (no threads). Flat conversation like texting. Threading unnecessary for 1:1 | Simplicity | Differs from original SDD (thread replies) |
| D-259 | Processing indicators | No emoji reactions. Just process and reply. Response appearing IS the feedback | Simplicity | Differs from original SDD (:thinking_face: reactions) |
| D-260 | Proactive DMs | `conversations.open(users=user_id)` → cache channel ID → `chat_postMessage` | Simplicity | auto-applied: deepened research |
| D-261 | Commands | Regular text commands ('start', 'new', 'status') — no slash commands. No manifest changes needed | Simplicity | Eliminates 3-second ack complexity |
| D-262 | Message splitting | Copy `split_message()` to slack/bot.py with `max_length=4000`. Module stays self-contained | Simplicity | DRY extraction at N=3, not N=2 |

### Observability

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-263 | Telemetry events | `slack:message_received/sent`, `slack:notification_sent`, `slack:auth_rejected`, `slack:connected/disconnected`, `slack:error` | Simplicity | auto-applied: pattern-following |

### Audit & Compliance

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-264 | Audit trail | Every message and auth rejection is a telemetry event. No tokens in events | Security | auto-applied: federal-mandate (NIST 800-53 AU-2) |

### Security

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-265 | Token handling | Two env vars: `ARCAGENT_SLACK_BOT_TOKEN`, `ARCAGENT_SLACK_APP_TOKEN`. Never filesystem, never logs | Security | auto-applied: federal-mandate (NIST 800-53 IA-5) |
| D-266 | Token prefix validation | Validate `xoxb-` and `xapp-` prefixes at startup. Log clear error if wrong | Simplicity | 4 lines, saves debugging time |
| D-267 | Authorization | Check `event.user` against `allowed_user_ids`. Empty = deny all (fail-closed). Silent ignore + audit | Security | auto-applied: pattern-following |
| D-268 | Bot loop prevention | Check `event.get("bot_id")` to skip bot messages (not `subtype == "bot_message"`) | Security | auto-applied: deepened research |

### Integration

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-269 | Import strategy | Lazy import in `start()`. If slack-bolt not installed, log warning and stay dormant | Simplicity | auto-applied: pattern-following |
| D-270 | DM channel caching | Cache channel ID after first `conversations.open` call. Reuse for all notifications | Simplicity + Performance | DM channel IDs don't change |

### Performance

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-271 | Connection model | One WebSocket per bot via Socket Mode. slack-bolt handles reconnection | Simplicity | auto-applied: per-SDK |

### Extensibility

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-272 | notify tool | Register `slack_notify_user` tool during startup. Agent decides when to send | Simplicity | auto-applied: pattern-following |
| D-273 | Tool name | `slack_notify_user` (prefixed to avoid collision with Telegram's `notify_user`) | Simplicity | Explicit channel identification |

### Testing

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-274 | Mock strategy | Mock `AsyncApp`, `WebClient`, `AsyncSocketModeHandler`. Test handler logic | Simplicity | auto-applied: pattern-following |
| D-275 | split_message | Duplicate in Slack (not shared utility). Module stays self-contained. Extract at N=3 | Simplicity | Module independence over DRY for N=2 |

### Deployment

| # | Decision | Choice | Priority | Tier Notes |
|---|----------|--------|----------|------------|
| D-276 | Dependencies | `slack-bolt >= 1.20.0` + `aiohttp` as optional: `pip install 'arcagent[slack]'` | Simplicity | auto-applied: pattern-following |

### Summary

**25 decisions total**: 15 auto-applied (patterns + mandates + research), 10 user-decided.
**Key simplifications vs original SDD**:
- Single-user model (1 agent = 1 user), not multi-user
- No thread replies — flat DM conversation
- No emoji reactions — response IS the feedback
- Text commands instead of slash commands — no manifest changes
- Inline Lock instead of Queue — simpler processing
**Risk**: Socket Mode messages can be lost during WebSocket disconnect (no durable queue). Acceptable for chat.

---

## ArcLLM Call Queue — Build Decisions (2026-02-27)

**Phase**: build | **Status**: complete | **Total decisions**: 10 (7 user, 3 auto-applied)
**Priority framework**: simplicity → security → scalability → compliance
**Brainstorm**: `.claude/brainstorms/2026-02-27-arcllm-call-queue.md`

### Summary

An internal QueueModule for arcllm that manages LLM call concurrency with per-call timeouts that start at **send time**, not enqueue time. Sits between OtelModule and TelemetryModule in the wrapping stack. Uses asyncio.Semaphore for concurrency control, bounded waiters for backpressure, and piggybacks on Otel spans for observability. Configured via the standard `load_model()` kwarg pattern.

### Auto-Applied (Federal Mandates)

| # | Decision | Mandated Answer | Citation |
|---|----------|----------------|----------|
| D-277 | Audit trail on queue operations | All state changes (enqueue, dequeue, send, timeout, reject) logged | NIST 800-53 AU-2 |
| D-278 | Tamper-evident queue event log | Append-only audit events | NIST 800-53 AU-9 |
| D-279 | Queue metrics export | OTLP-compatible metrics | FedRAMP CA-7 |

### Architecture

#### D-280: Stack Position
**Decision**: QueueModule sits just inside OtelModule (between Otel and Telemetry)
**Priority**: Simplicity — Otel captures full picture including queue wait; telemetry only counts actual call
**Alternatives**: Outermost (rejected — Otel wouldn't see queue wait), Between Retry and RateLimit (rejected — more complex interaction, retries don't re-enqueue)
**Rationale**: Otel span wraps queue wait + call, giving complete timing. Telemetry/Audit/Security/Retry all operate within the queue slot.
**Stack**: `Otel → QueueModule → Telemetry → Audit → Security → Retry → Fallback → RateLimit → Adapter`
**Tiers**: Federal: queue enabled by default, audit events mandatory | Enterprise: queue enabled by default | Personal: queue enabled by default (simplicity benefit is universal)

#### D-281: Queue Scope
**Decision**: Per adapter instance (each `load_model()` gets its own queue)
**Priority**: Simplicity — zero shared state, matches arcllm's existing stateless pattern
**Alternatives**: Shared per provider endpoint (rejected — introduces global mutable state, thread-safety complexity, breaks arcllm's pattern)
**Rationale**: Agents already have separate model instances for main vs eval. Different models have different concurrency needs. No shared state means no coordination bugs.
**Tiers**: Same across all tiers

#### D-282: Concurrency Primitive
**Decision**: `asyncio.Semaphore` — stdlib, FIFO, 5 lines of core logic
**Priority**: Simplicity — proven pattern, zero deps, already used in arcagent's spawn_background
**Alternatives**: PriorityQueue + Semaphore (rejected — worker loop lifecycle, Future-based indirection, ~30 lines vs ~5)
**Rationale**: FIFO is sufficient. If priority becomes a real need, the module interface stays the same — only internals change.
**Tiers**: Same across all tiers

#### D-283: Backpressure
**Decision**: Max waiters limit — track waiter count, reject with `QueueFullError` when exceeded
**Priority**: Security — bounded resource usage prevents unbounded consumption (LLM10, ASI-08)
**Alternatives**: No limit / rely on caller timeouts (rejected — unbounded accumulation flagged as medium-severity in scheduler hardening review), Oldest-out eviction (rejected — canceling in-progress work is surprising)
**Rationale**: Clear failure mode. Caller decides how to handle rejection. Consistent with scheduler hardening findings.
**Tiers**: Federal: max_queued enforced, rejection audited | Enterprise: max_queued enforced | Personal: max_queued enforced (simplicity benefit)

#### D-284: Timeout Semantics
**Decision**: Send-time timeout only — timeout starts when semaphore is acquired (call actually fires), not when enqueued
**Priority**: Simplicity — this IS the core fix. Timeouts mean what they say.
**Alternatives**: Dual timeout / queue wait + send (rejected — two timeout configs to manage, more complex)
**Rationale**: Root cause of the bio_memory bug was timeouts that included invisible queue wait. Queue wait is bounded by backpressure (max_queued). Call timeout measures actual LLM response time.
**Tiers**: Same across all tiers

#### D-285: Configuration
**Decision**: Standard `load_model()` kwarg pattern — `queue=True|False|dict`, config.toml under `[modules.queue]`
**Priority**: Simplicity — identical to every other arcllm module, zero new patterns
**Alternatives**: Always-on with no config (rejected — some callers may need to disable or customize)
**Rationale**: Consistency with existing retry, rate_limit, telemetry modules. Config.toml provides defaults, per-model overrides via kwarg.
**Tiers**: Same across all tiers

#### D-286: Exception Hierarchy
**Decision**: Two exceptions under `ArcLLMError` — `QueueFullError` (backpressure rejection) and `QueueTimeoutError` (call exceeded send-time timeout)
**Priority**: Simplicity — minimal, precise, existing `except ArcLLMError` blocks catch both
**Alternatives**: Single QueueError with reason enum (rejected — less precise except blocks)
**Rationale**: Callers can handle each failure mode differently. Bio_memory can skip on QueueFullError, retry on QueueTimeoutError.
**Tiers**: Same across all tiers

### Observability & Telemetry

#### D-287: Queue Telemetry
**Decision**: Otel span attributes on existing span — `queue.wait_ms`, `queue.depth_at_entry`, `queue.rejected`, `queue.call_timeout`
**Priority**: Simplicity — zero new spans, piggyback on OtelModule's `llm.invoke` span
**Alternatives**: Dedicated metrics counters/histograms (rejected — more code, less essential), Both spans + metrics (rejected — over-engineering for v1)
**Rationale**: Immediately answers "was it slow because of queue or provider?" in any OTLP backend. Audit events (auto-applied) cover the compliance mandate independently.
**Tiers**: Federal: audit events always emitted regardless of Otel | Enterprise/Personal: Otel attributes when enabled

### Performance

#### D-288: Default Concurrency
**Decision**: `max_concurrent=2` per model instance
**Priority**: Simplicity — matches existing `eval_config.max_concurrent` default, safe on all Azure deployments including GCC
**Alternatives**: 1 (too restrictive — bio_memory + policy can't eval simultaneously), 5 (may hit rate limits on smaller deployments)
**Rationale**: Conservative default. Override in config for specific needs.
**Tiers**: Same across all tiers

#### D-289: Default Call Timeout
**Decision**: `call_timeout=60.0` seconds (from send time)
**Priority**: Simplicity — covers all non-reasoning models. Reasoning models override in config.
**Alternatives**: 120s (too long for fast models — stuck call holds slot for 2 min), No default (friction)
**Rationale**: gpt-4-1-mini responds in 5-15s, gpt-4-1 in 10-30s. Reasoning models (o4-mini, o1) need explicit override to 120-180s.
**Tiers**: Same across all tiers

### Testing

#### D-290: Test Strategy
**Decision**: Unit tests with mock inner adapter — 6 core test cases covering concurrency limiting, backpressure, send-time timeout, queue wait excluded from timeout, Otel attributes, and config loading
**Priority**: Simplicity — pure async tests, no real LLM calls, fast CI
**Alternatives**: Unit + integration with real provider (rejected — unnecessary for a concurrency primitive, integration tests exist for the adapter layer)
**Rationale**: The queue is a pure asyncio wrapper. Its behavior is fully testable with mocks.
**Tiers**: Same across all tiers

### Research Insights (via /deepen — 2026-02-27)

**Enhancement summary**: 4 parallel research agents investigated asyncio.Semaphore edge cases, LLM SDK queue patterns, Otel span attribute conventions, and arcllm module wrapping patterns. Key findings below. No decision changes required — all findings reinforce or refine existing decisions.

#### D-282 (Concurrency Primitive) — asyncio.Semaphore Deep Dive

- **Use `BoundedSemaphore` over `Semaphore`**: `BoundedSemaphore` raises `ValueError` if released more times than acquired. Prevents over-release bugs from unbalanced acquire/release in error paths. Negligible overhead.
- **FIFO guarantee**: Python 3.11+ guarantees FIFO ordering on `asyncio.Semaphore` (CPython implementation uses `collections.deque`). Earlier versions had edge cases but were functionally FIFO. Our min Python target (3.11+) is safe.
- **Cancellation safety**: `async with semaphore:` is safe — `__aexit__` always calls `release()`, even on `asyncio.CancelledError`. No manual try/finally needed when using context manager pattern.
- **Implementation note**: Track `_waiters` count with a simple `int` counter (increment on enter wait, decrement on acquire or reject). Don't introspect semaphore internals.

#### D-283 (Backpressure) — Bounded Queue Patterns

- **No major LLM framework implements priority queuing** — LiteLLM, LangChain, OpenAI SDK all use simple FIFO with concurrency limits. This validates our decision to start with FIFO and skip priority levels.
- **LiteLLM pattern**: Uses `asyncio.Semaphore(max_parallel_requests)` with a simple `num_retries` fallback. No explicit backpressure — callers just wait. Our `max_queued` adds the missing safety valve.
- **Reject-fast is correct**: Industry pattern is to fail fast with clear error when queue is full, rather than silent eviction or unbounded waiting. `QueueFullError` matches this.

#### D-287 (Queue Telemetry) — Otel Span Attribute Patterns

- **No standard OTel semantic convention for queue wait time** — there's no `rpc.queue.wait` or `messaging.queue.depth` attribute in the OTel semantic conventions spec. Custom attributes are the correct approach.
- **Naming convention**: Use `arc.queue.` prefix for all custom attributes (e.g., `arc.queue.wait_ms`, `arc.queue.depth`, `arc.queue.rejected`). This follows OTel's [attribute naming guidelines](https://opentelemetry.io/docs/specs/semconv/general/attribute-naming/) for vendor-specific attributes.
- **Access pattern**: Use `trace.get_current_span()` from within QueueModule to add attributes to the outer OtelModule's active span. This avoids creating a new span and keeps the trace structure clean.
- **Attribute types**: `wait_ms` as `int`, `depth` as `int`, `rejected` as `bool`, `call_timeout_ms` as `int`. OTel prefers integer milliseconds over float seconds for span attributes.

#### Module Implementation — arcllm Wrapping Pattern

- **BaseModule interface confirmed**: Extend `BaseModule(config, inner)`, override `async def invoke(self, messages, **kwargs)`, delegate via `await self._inner.invoke(messages, **kwargs)`.
- **`_span()` context manager**: Available from BaseModule for creating sub-spans. Not needed here since we piggyback on OtelModule's span, but available if we add queue-specific spans later.
- **`load_model()` integration**: Add `queue` to `_MODULE_DEFAULTS` dict in `registry.py`. The `_resolve_module_config()` function handles `True|False|dict|None` automatically. Module instantiation order in `_wrap_with_modules()` determines stack position.
- **Config**: Add `[modules.queue]` section to provider TOML files with `max_concurrent`, `call_timeout`, `max_queued` defaults. Config class inherits from `BaseModuleConfig`.

**Implementation checklist** (from codebase analysis):
1. `modules/queue.py` — QueueModule class extending BaseModule
2. `modules/config.py` — add QueueConfig (max_concurrent, call_timeout, max_queued)
3. `exceptions.py` — add QueueFullError, QueueTimeoutError under ArcLLMError
4. `registry.py` — add `queue` to `_MODULE_DEFAULTS`, wire into `_wrap_with_modules()` at correct stack position
5. Provider TOML files — add `[modules.queue]` default config
6. Tests — 6 test cases per D-290

### Categories Skipped (Not Applicable)

- **Data Model**: No persistence — pure in-memory asyncio state
- **API Design**: No endpoints — internal module only
- **Security**: Beyond auto-applied mandates, no additional security decisions — queue doesn't handle secrets, credentials, or user data
- **Integration**: No external services — queue is between caller and adapter
- **Extensibility**: Standard module pattern, no additional extension points needed
- **Deployment**: No migration — new module, additive change
- **UI/UX**: No user interface

### Open Questions

None — all resolved during build and deepen.

## Feature: arc-core-hardening
**Date:** 2026-04-17
**Scope:** Stability, integration gaps, tool policy pipeline, execution engine upgrade, heartbeat/cron system
**Status:** Deepened (research-enriched)

---

### Architecture

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|
| D-291 | Parallel tool execution | Validate sequentially, execute in parallel via `asyncio.gather()` | simplicity + performance | None |
| D-292 | Self-modification level | Skills + tools + extensions (full self-modification) | extensibility | Federal: extensions DISABLED. Enterprise: require approval. Personal: all enabled |
| D-293 | Hot-reload mechanism | Immediate reload on create (no watcher) | simplicity | None |
| D-294 | Tool policy pipeline | 5-layer execution-time (Global->Provider->Agent->Team->Sandbox) | security + compliance | Federal: all 5. Enterprise: 4 (no team). Personal: global only |
| D-295 | Heartbeat / proactive execution | Unified ProactiveEngine (merge pulse + scheduler) | simplicity | Federal: can be disabled. Enterprise/Personal: default enabled |
| D-296 | Session history model | Keep linear JSONL (no tree branching) | simplicity | None |
| D-297 | Loop termination signal | Structured `task_complete` tool with status/summary/artifacts | observability + integration | None |
| D-298 | Turn/step limits | Configurable defaults (max_turns=100, max_cost=5.00) | security + simplicity | Federal: hard caps. Enterprise: auto-approve 2x. Personal: always approve |
| D-299 | ArcLLM bridge wiring | Wire `on_event` through `load_eval_model()` helper | simplicity | None |
| D-300 | httpx client lifecycle | Explicit `model.close()` in `ArcAgent.shutdown()` | simplicity | None |

#### Research Insights: Decision 1 — Parallel Tool Execution

**Failure handling:** Use `asyncio.gather(return_exceptions=True)` — partial success is meaningful for tool batches. A failed `read_file` shouldn't abort a concurrent `web_search`. Inspect results post-gather, emit structured error events per failed tool. Do NOT use `TaskGroup` (fail-fast semantics wrong for tool batches; also had deadlock edge cases in Python 3.11).

**Concurrency limit:** Add `asyncio.Semaphore(max_parallel_tools)` with configurable default of 10. Unbounded `gather()` risks memory exhaustion (N tools x avg output size) and fd exhaustion (default Linux ulimit 1024). Semaphore releases slots as tasks complete, maintaining continuous throughput — better than chunked gather.

**Read-write classification:** Classify tools as read-only or state-modifying at registration time. Run batch in parallel ONLY if ALL tools are read-only. Any write in the batch forces the entire batch sequential. This is the Anthropic/Claude Code pattern and eliminates filesystem races without per-file locking.

**Implicit dependency detection:** Parameter-based heuristic: if tool call A produces a file path that appears as an argument in tool call B in the same batch, treat as dependent and execute sequentially. Catches the write-then-read pattern cheaply without full DAG planning (LLMCompiler).

**Audit ordering:** Assign monotonically incrementing sequence number at dispatch time (before gather). Record wall-clock at dispatch AND completion separately. Events get `{seq, tool_id, dispatch_ts, complete_ts, status}`. Sequence number establishes submission order; timestamps provide duration.

**FIPS/air-gapped:** Connection pooling via httpx client reuse. Semaphore on HTTPS-touching tools (2-4 concurrent on FIPS-constrained 2-vCPU VMs). FIPS TLS handshakes can reach 5s under contention.

**Scalability ceiling:** ~2KB per coroutine. Practical limit is rate limits on downstream services, not asyncio. Cap at 10-20 concurrent tools per turn via semaphore.

---

#### Research Insights: Decision 4 — Tool Policy Pipeline

**Pattern confirmed:** First-DENY-wins is correct — every major production system (AWS IAM, K8s RBAC, Istio, Envoy, OPA) uses this. First-ALLOW-wins is catastrophically wrong for security pipelines.

**Fail-closed is non-negotiable:** AuthZed's analysis: "a fail-open state can inadvertently grant access to unauthorized users during unexpected failures." Any exception during evaluation = DENY. Never propagate exceptions as allow.

**Performance target:** Sub-1ms per evaluation. OPA benchmarks: 40-50us with rule indexing. Short-circuit on first DENY. For 5 layers sequentially: budget ~5ms total, but most requests DENY in first 1-2 layers.

**Optimizations:**
- Short-circuit evaluation (return immediately on first DENY)
- Index rules by tool name for O(1) lookup, not O(n) iteration
- Decision caching: LRU cache keyed on `(agent_did, tool_name, classification)` with 30-60s TTL
- Load all reference data into memory at startup (never external calls during evaluation)

**Structured deny reasons MUST answer 3 questions:**
1. Which layer denied?
2. Which rule matched?
3. What input values triggered it?

Example: `"Tool requires SECRET clearance; agent has FOUO"` — not `"Access denied"`.

**Classification filtering (Bell-LaPadula):**
- No read up: agent can't call tools returning data above its clearance
- No write down: agent with classified context can't call tools that write to unclassified systems
- Classification must propagate through call chains (Agent A -> Agent B -> tool)

**Dynamic tool registration = privileged action:** Must be governed by Global layer. Newly registered tools classified before callable. Scoped to creating session (not globally visible).

**Dry-run/shadow mode:** Deploy new policies in audit-only mode before enforcing. Log denials without blocking. Essential for policy hot-reload safety.

**Air-gapped:** Signed policy bundles distributed like software releases. Local bundle fallback with max-age check. If bundle too old and server unreachable, enter restricted mode (deny all except hardcoded safe set).

---

#### Research Insights: Decision 5 — ProactiveEngine

**Timer architecture:** Single loop with min-heap priority queue (Celery Beat pattern). One asyncio task, priority queue sorted by `next_run`, sleep until earliest entry. Polling granularity: 1-5 seconds.

**Drift prevention:** Compute `next_run = last_actual_run + interval`, NEVER `next_run = now + interval`. The latter accumulates drift. Apply small negative adjustment (-0.010s) per cycle for scheduling overhead (Celery pattern). Add jitter parameter for multi-instance herd prevention.

**Clock source:** Use `time.monotonic()` (maps to `CLOCK_BOOTTIME` on Linux since Python 3.x) for interval calculations. Wall clock ONLY for "is it within active hours?" checks. `CLOCK_MONOTONIC` stops advancing during VM suspend — `CLOCK_BOOTTIME` includes suspend time. Add clock warp detection: compare `time.time()` delta against `time.monotonic()` delta per tick; warn if divergence > 5s.

**Heartbeat isolation (CRITICAL):** Heartbeat outputs MUST NOT enter the agent's main context window. Run heartbeat as a stateless side-channel call with its own minimal context. Different model outputs in main context cause behavioral inconsistencies.

**Cheap model for heartbeat:** Restrict decision boundary to `{idle, not_idle}`. Never `{idle, act_on_X, act_on_Y}`. Anything other than clear idle signal escalates to full model.

**Circuit breaker state machine (Resilience4j pattern):**
- CLOSED: normal, failures tracked in sliding window
- OPEN: disabled, rejects immediately, waits `waitDuration`
- HALF_OPEN: N probe executions allowed, success -> CLOSED, failure -> OPEN
- Exponential backoff on wait: `min(60 * 2^open_count, 1800)` seconds
- `auto_recovery_enabled = true` (moves OPEN -> HALF_OPEN automatically)
- Always expose `force_close()` and `force_open()` for manual override

**Concurrency policy:** If previous execution hasn't completed, skip new invocation and record miss. Prevents unbounded parallel agent runs. This is Kubernetes CronJob `concurrencyPolicy: Forbid`.

**Wake events:** Idempotent — compare `event.timestamp` against `last_wake_handled`. Discard stale wakes.

**Active hours / timezone:**
- Store UTC internally, IANA timezone for user-facing config (e.g., `America/Chicago`)
- Convert at schedule creation, never at evaluation time
- Overnight windows (22:00-06:00): detect `end < start`, handle as `now >= start OR now < end`
- DST: skip non-existent spring-forward times, don't double-execute on fall-back

**Multi-instance (10-20 agents):** Leader election via Kubernetes Lease or Redis. Only leader runs ProactiveEngine. If leader dies, lease expires and new instance acquires. Simpler and safer than per-schedule distributed locking. All scheduled actions MUST be idempotent (at-least-once semantics).

---

### Data Model

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|
| D-301 | Tool policy types location | New `arcagent/core/tool_policy.py` | simplicity | None |
| D-302 | Proactive schedule definition | TOML config + runtime API (tools) | simplicity + extensibility | None |
| D-303 | Agent-created tool format | Single `.py` file with `@tool` decorator | simplicity | None |
| D-304 | Agent-created extension format | Python file + MODULE.yaml (match convention) | simplicity | None |

#### Research Insights: Decision 13 — Dynamic Tool Format

**Decorator pattern:** Return original function unchanged (transparent). Store metadata separately from callable. Lock down registration after startup to prevent late/concurrent mutation. Use outermost decorator position for `@tool`.

**Schema from type hints, not manual:** Derive JSON Schema from Python type hints via Pydantic `validate_call`. AI writes typed functions; framework generates schema. This is the FastMCP, LangChain, and OpenAI Agents SDK pattern. Never let AI write schema manually alongside untyped functions.

**importlib isolation:** Do NOT register in `sys.modules`. Use unique module names: `f"_agent_tools.{name}_{hash(path)}"`. Use `Path.resolve()` before `spec_from_file_location`. Create fresh module object each time (no `reload()`).

**Name collision policy:** Namespace prefix agent-created tools: `agent.{session_id}.{name}`. Reserve `builtin.*` namespace. On collision: `"warn"` mode replaces with warning (FastMCP pattern). Configurable: `"error"`, `"replace"`, `"warn"`, `"ignore"`.

**Version conflict during execution:** Copy tool reference at dispatch time. Registry update takes effect for next call, not in-flight calls. Use asyncio.Lock for concurrent registration.

**Signature validation:** AST pre-validation checks function params match schema keys before loading. Pydantic `validate_call` wrapping at registration for runtime type coercion. Fail at registration, not at call time.

**Error handling:** Tool errors surfaced with source file + traceback (Python `linecache` auto-caches for real .py files). Timeout via `asyncio.wait_for(coro, timeout)` for async tools. For sync tools that might hang, use subprocess (killable) not threads (not killable).

---

### API Design

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|
| D-305 | Self-modification tool surface | 6 focused tools: create_skill, improve_skill, create_tool, create_extension, list_artifacts, reload_artifacts | simplicity | create_extension: Federal DENIED, Enterprise approval |
| D-306 | Schedule management tools | 5 tools: create/list/pause/resume/delete + bus-based wake events | simplicity + extensibility | None |
| D-307 | `task_complete` schema | Minimal: status + summary (required), artifacts + next_steps + error (optional) | simplicity | None |

---

### Observability

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|
| D-308 | Telemetry for new components | Bus events + OTel spans (both internal and external) | observability | None |

#### Research Insights: Decision 18 — Telemetry

**Policy evaluation telemetry must include:**
- `request_id` (correlate with agent trace)
- `session_id` (correlate with full session)
- `layer` (which policy layer evaluated)
- `policy_version` (which bundle was active)
- `decision` (ALLOW/DENY/ERROR)
- `matched_rule` (specific rule ID)
- `evaluation_time_us` (microseconds)
- `input_hash` (for cache validation)

**Metrics to instrument:**
- Denial rate by layer (spike = policy regression)
- Evaluation latency by layer (p50/p95/p99)
- Exception rate (any non-zero = policy bug)
- Cache hit rate
- Circuit breaker state per schedule
- Heartbeat tick rate and silent suppression count

---

### Security

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|
| D-309 | Dynamic tool sandboxing | Policy pipeline + restricted imports (AST validation, blocked imports, sandboxed ToolContext) | security | Federal: agent-created tools DENIED. Enterprise: restricted imports enforced. Personal: restricted + warning |
| D-310 | Config architecture | Contained TOML per package (each package owns its config, no cross-cutting) | simplicity | Tier set independently per package config |

#### Research Insights: Decision 19 — CRITICAL SECURITY GAPS IDENTIFIED

**AST scanning is necessary but insufficient.** RestrictedPython has had 3 CVEs (2023-37271, 2025-22153, 2024-47532) demonstrating bypass via generator frame traversal, try/except* confusion, and AttributeError.obj leakage.

**Immediate additions to blocked list (beyond os, subprocess, socket, etc.):**

| Must Block | Why |
|---|---|
| `ctypes` (CDLL, cdll, windll) | CVE-2025-68668: `CDLL(None).system("cmd")` bypasses ALL Python-level restrictions via libc FFI |
| `sys` (sys.modules) | Pre-loaded modules (including `os`) accessible without import via `sys.modules['os']` |
| `.gi_frame`, `.f_back`, `.f_builtins`, `.f_globals` | CVE-2023-37271: generator frame traversal reaches unrestricted `__import__` |
| `compile()` + `eval()` combination | Bypasses eval's single-expression restriction |
| `pickle` / `__reduce__` | Arbitrary code execution on unpickling |
| `__class__.__base__.__subclasses__()` | Class hierarchy traversal reaches importers |
| `getattr(__builtins__, ...)` | Dynamic attribute access to blocked builtins |
| `string.Formatter` | Format string attribute traversal leaks globals |
| Source encoding declarations | `# -*- coding: utf-7 -*-` codec attacks occur BEFORE AST parsing |

**NETWORK EGRESS IS UNADDRESSED (NEW REQUIREMENT):**
Even a perfectly process-isolated tool can exfiltrate data via outbound HTTP. Every tier needs explicit egress allowlist (deny-by-default outbound, approve specific endpoints). This is the `ToolContext.http` proxy — it must be the ONLY network path, and it must be logged.

**Recommended layered defense for tools:**
1. AST validation (blocks unsophisticated attempts)
2. Restricted builtins (scrubbed `__builtins__` dict)
3. Blocked attribute access (gi_frame, f_back, etc.)
4. Network egress proxy (deny-by-default, logged)
5. Policy pipeline (5-layer, execution-time)
6. Optional: seccomp-bpf syscall allowlist via subprocess (blocks everything AST misses)

**Non-compositional safety (arXiv:2603.15973):** Two individually-safe tools can compose to enable forbidden capabilities (e.g., "read file" + "send HTTP" = exfiltration). Consider capability inventory at deployment: what syscalls, network endpoints, and file paths does each tool require? Do combinations create dangerous conjunctions?

**Federal posture is defensible under NIST 800-53:**
- SI-7(15): Dynamic code fails cryptographic pre-installation authentication
- CM-5: Agent-generated code bypasses change control
- CM-8: Dynamic tools create untracked system components
- Frame as compliance requirement, not product limitation

**OWASP Agentic Top 10 (2026) relevant items:**
- ASI01: Created tool becomes vector for goal hijack
- ASI02: Agents chain self-created tools in unexpected sequences
- ASI04: Dynamically loaded extensions with compromised MODULE.yaml
- ASI05: Agent-generated code treated as trusted
- ASI08: Extensions subscribing to events trigger cascading failures

---

### Testing

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|
| D-311 | Test coverage target | 90% + adversarial security suite (~20 new test files) | security | None |

#### Research Insights: Decision 21 — Adversarial Test Cases

Based on research, the adversarial suite MUST include:

**Import bypass tests:**
- `__import__('os')` via builtins
- `sys.modules['os']` access
- Class hierarchy traversal (`__subclasses__()`)
- Generator frame traversal (gi_frame.f_back)
- ctypes FFI (`CDLL(None).system()`)
- Codec attack (utf-7 encoding)
- Format string globals leakage
- compile() + eval() combination
- pickle __reduce__ exploitation

**Capability composition tests:**
- read_file + http_request = data exfiltration
- create_tool + reload = privilege escalation
- bash + write_file = persistent backdoor

**Policy pipeline tests:**
- Concurrent policy evaluation under load
- Exception in middle layer (verify fail-closed)
- Dynamic tool registration during policy evaluation
- Classification downgrade attempt (write-down)
- Stale policy bundle detection

---

### CLI

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|
| D-312 | CLI surface for new features | Full mirror -- all features get CLI commands for scriptability/CI/CD | extensibility | None |

---

### Auto-Applied Federal Mandates

| Mandate | Citation | Applied To |
|---------|----------|------------|
| Every policy evaluation audit-logged | NIST 800-53 AU-2 | Tool policy pipeline |
| Completion events audit-logged | NIST 800-53 AU-2 | task_complete tool |
| Budget tracking mandatory (federal) | OMB A-123, FITARA | Turn/cost limits |
| Self-modification actions audit-logged | NIST 800-53 AU-2 | create_skill, create_tool, create_extension |
| Proactive execution audit-logged | NIST 800-53 AU-2 | ProactiveEngine (trigger type tagged) |
| Audit log retention minimum 1 year | NIST 800-53 AU-11 | All audit logs |
| Agent-created code cannot bypass audit | NIST 800-53 AU-9 | Dynamic tool sandbox |
| Schedule changes audit-logged | NIST 800-53 AU-2 | ProactiveEngine |
| Dynamic code DENIED in federal tier | NIST 800-53 SI-7(15), CM-5, CM-8 | Tools + extensions |
| Network egress deny-by-default | NIST 800-53 SC-7 | All tiers |

---

### Known Bug Fixes (Not Decisions -- Will Be Fixed)

1. **ArcLLM bridge not wired:** `create_arcllm_bridge()` defined but `on_event` never passed to `load_model()`
2. **ui_reporter missing MODULE.yaml:** Cannot be loaded by convention-based ModuleLoader
3. **REPL commands non-functional:** `/sandbox` and `/strategy` in `arc agent chat` are display-only
4. **httpx client leak:** `ArcAgent.shutdown()` doesn't call `model.close()`
5. **messaging byte_pos=0:** `svc.ack()` always passes `byte_pos=0`, breaking seek optimization
6. **Hardcoded constants:** `_CHECK_CIRCUIT_BREAKER_THRESHOLD`, `_MAX_STEERING_MESSAGE_LEN`, WebSocket URL, OTEL endpoint -- move to config

---

### New Files Summary

| File | Package | Purpose |
|------|---------|---------|
| `core/tool_policy.py` | arcagent | Policy pipeline types, layers, pipeline class |
| `modules/proactive/engine.py` | arcagent | Unified ProactiveEngine (replaces pulse + scheduler) |
| `modules/proactive/MODULE.yaml` | arcagent | Module metadata |
| `tools/skill_tools.py` | arcagent | create_skill, improve_skill |
| `tools/tool_tools.py` | arcagent | create_tool, list_artifacts, reload_artifacts |
| `tools/extension_tools.py` | arcagent | create_extension |
| `tools/schedule_tools.py` | arcagent | create/list/pause/resume/delete schedule |
| `tools/completion.py` | arcagent | task_complete tool |
| `tools/_decorator.py` | arcagent | @tool decorator for dynamic tools (Pydantic schema inference) |
| `tools/_dynamic_loader.py` | arcagent | Dynamic tool loading with AST validation + restricted builtins |
| `builtins/task_complete.py` | arcrun | task_complete built-in |

### Modified Files Summary

| File | Change |
|------|--------|
| `arcagent/core/agent.py` | Wire LLM bridge, register new tools, model.close() in shutdown |
| `arcagent/core/tool_registry.py` | Replace `_check_policy()` with pipeline, move to execution-time, add read-write classification |
| `arcagent/utils/__init__.py` | Add `on_event` parameter to `load_eval_model()` |
| `arcagent/modules/ui_reporter/MODULE.yaml` | NEW -- enable convention loading |
| `arcagent/modules/messaging/tools.py` | Fix byte_pos in ack() |
| `arcagent/modules/pulse/` | Replaced by proactive/ |
| `arcagent/modules/scheduler/` | Replaced by proactive/ |
| `arccli/agent.py` | Fix /sandbox and /strategy REPL, add new CLI commands |
| `arcrun/strategies/react.py` | Parallel tool execution with semaphore + read-write classification |
| `arcrun/loop.py` | task_complete handling, max_turns enforcement |
| Various configs | Move hardcoded constants to TOML |

---

### Research Sources

**Parallel Execution:** asyncio.gather patterns (SuperFastPython), LLMCompiler ICML 2024 (arXiv:2312.04511), Lamport timestamps, TLS handshake latency under FIPS
**Policy Pipeline:** AWS IAM evaluation logic, OPA performance docs, Goldman Sachs OPA at scale, AuthZed fail-open analysis, NIST 800-53 AC-3, Bell-LaPadula model, Istio dry-run mode
**Self-Modification Security:** RestrictedPython CVEs (2023-37271, 2025-22153, 2024-47532), n8n Pyodide escape (CVE-2025-68668), OWASP Agentic Top 10 2026, Safety non-compositionality (arXiv:2603.15973), secimport eBPF, NIST SI-7/CM-5/CM-8
**ProactiveEngine:** Celery Beat architecture, Resilience4j circuit breaker, ROS2 watchdog patterns, TigerBeetle clock research, EventBridge scheduler, Kubernetes leader election
**Dynamic Tools:** FastMCP tool registration, Pydantic validate_call, importlib isolation patterns, pluggy namespace isolation

---

## Hermes-Parity Roadmap — Build Decisions (2026-04-18)

**Phase**: build (cross-cutting roadmap level) | **Status**: complete | **Total decisions**: 26 (18 user, 8 auto-applied)
**Priority framework**: simplicity > modularity > security > scalability
**Source**: Inline conversation 2026-04-18 — Hermes (NousResearch/hermes-agent) feature comparison and Arc-style absorption plan
**Scope**: M1-M4 cross-cutting decisions only. Per-feature decisions deferred to follow-up `/build` runs.

### Summary

Arc absorbs the Hermes capabilities that fit Arc's stance — gateway daemon, session search, NL cron with delivery, skill auto-creation nudges, pluggable terminal backends, subagent delegation, finished TUI, optional skills hub, voice/web/browser modules — without violating Arc's `<3,500 LOC core` rule, federal-first defaults, or arcllm/arcrun/arcagent separation. **Single new sibling package**: `arcgateway`. Everything else is a module on existing packages or a CLI surface change. **Explicitly out of scope** (cut by user): MCP client, migration tooling, ACP/IDE adapter. Sessions become **per-(user, agent)** with shared agent memory and per-user profile — the agent is "a person" with multiple users, multiple sessions, one self. Federal/enterprise/personal tier lockdown drives all gating.

### Auto-Applied (Federal Mandates)

| # | Decision | Mandated Answer | Citation |
|---|----------|----------------|----------|
| D-313 | Inter-package transport encryption | TLS 1.2+ minimum; mTLS at federal | NIST 800-52r2, SC-8 |
| D-314 | Audit scope (gateway msgs, cron runs, skill installs) | Every state-changing op audited | NIST AU-2 |
| D-315 | Audit log retention | ≥1 year (3 years recommended) | NIST AU-11 |
| D-316 | Platform credential storage at federal | Vault-backed only; never on disk | NIST IA-5 |
| D-317 | Audit log integrity | Tamper-evident, append-only, hash-chained | NIST AU-9 |
| D-318 | New package extras release artifacts | SBOM required | EO 14028 |
| D-319 | Session/state files at rest (federal) | AES-256 / FIPS 140-3 | NIST SC-28 |
| D-320 | Voice transcript handling | PII; bidirectional redaction at federal/enterprise | NIST 800-53 SI-12 |

### Architecture

#### D-321: Package boundaries for new Hermes-parity capabilities (FINAL)
**Decision**: Exactly **ONE** new sibling package: `arcgateway` (long-running platform daemon). Voice/web/browser as `arcagent.modules.*`. Skills Hub extends `arcskill` (TOML+CLI gated, see D-322). **Out of scope** (explicitly cut by user): `arcmcp` (no MCP client), `arcmigrate` (no migration tooling), `arcacp` (no IDE adapter). Centralized command registry lives in `arccli` (D-323). TUI completion in `arctui` (D-324).
**Priority**: simplicity (one new package vs many); modularity (arcllm/arcrun/arcagent boundaries preserved); security (smallest possible new attack surface)
**Alternatives**: Original proposal had 5 new packages (arcgateway, arcmcp, arcskillhub, arcacp, arcmigrate); progressively cut by user across the walk.
**Rationale**: User chose to absorb only the Hermes capabilities that fit Arc's stance. MCP, IDE protocol, and migration tooling can ship later as community packages or follow-up roadmap items if demand emerges.
**Tiers**: Same package layout across tiers; tier behavior differs in policy layer.

#### D-325: arcgateway process model
**Decision**: Long-running separate daemon, one ArcAgent instance per chat (not bundled with agent process)
**Priority**: simplicity (single ops unit, restart independently); scalability (horizontal scale via N gateways)
**Alternatives**: Same-process multi-tenant pool; per-chat OS subprocess; per-chat asyncio task in shared daemon
**Rationale**: Crash isolation; horizontal scale; matches existing arcui pattern.
**Tiers**: Same model; tier flips agent-spawn isolation (D-326).

#### D-326: Agent dispatch model inside arcgateway
**Decision**: In-process asyncio task per active chat; tier flips executor — federal adds subprocess isolation per chat (own DID, own tool sandbox, own audit boundary). Same code path; policy layer chooses executor.
**Priority**: simplicity (asyncio default); security (federal pays subprocess overhead for safety)
**Alternatives**: NATS-routed (forces NATS dep on personal); subprocess-always (cold start kills UX); single shared agent (breaks DID model)
**Rationale**: Lets Arc match Hermes' "$5 VPS" ergonomics at personal while honoring SCIF requirements at federal.
**Tiers**: Federal: subprocess per chat. Enterprise: asyncio with strict resource limits + per-chat audit. Personal: asyncio.

#### D-327: Session storage engine
**Decision**: JSONL primary (append-only, audit-friendly, human-readable) + SQLite FTS5 derived index (background-built). Search reads SQLite; truth lives in JSONL. Crash-safe rebuild from JSONL.
**Priority**: simplicity (no breaking change); modularity (search can be swapped without touching primary store); security (JSONL audit posture preserved)
**Alternatives**: Full SQLite migration; pluggable session store; non-SQLite full-text (whoosh/tantivy)
**Rationale**: Adds Hermes' search capability without losing Arc's existing audit posture. Index lag acceptable for search use case.
**Tiers**: Same engine all tiers. Federal: SQLite file at AES-256 (D-319); JSONL append + checksum chain.

#### D-328: Session API ownership
**Decision**: Sessions live in `arcagent.modules.session`; arcgateway depends on arcagent for the API.
**Priority**: simplicity (no new package); modularity (arcgateway = "the daemon that runs ArcAgents," dep is honest)
**Alternatives**: New `arcsession` package; NATS RPC; split-brain per-package
**Rationale**: arcgateway will never run without arcagent; an extracted session package would be ceremony without payoff.
**Tiers**: Same.

#### D-329: Session identity model (revised)
**Decision**: Session = `(user, agent)` pair. Same user across multiple platforms (Slack + Telegram + …) = same session. Different user = new session. Agent owns multiple sessions and shares its own memory across them; can read across its own sessions for context per the ACL model in D-330.
**Priority**: simplicity (matches "agent = person" mental model); security (per-user isolation by default)
**Alternatives**: Per-platform isolation (Hermes default); always-unified by user (info-flow violation at federal); per-chat isolation (loses memory accumulation)
**Rationale**: User explicitly framed agent as a person who may speak with many users across many channels — sessions belong to the agent but are bounded by the user they're with.
**Tiers**: Same model; tier governs cross-session reads (D-330) and cross-user data flow.

#### D-331: Multi-message concurrency
**Decision**: One session = one in-flight turn. Different sessions (different users) run concurrently. No interrupt; no parallel turns on the same session. Per-user-session FIFO is the natural consequence of D-329.
**Priority**: simplicity; security (no race conditions on session memory); modularity
**Alternatives**: Interrupt-and-redirect; parallel turns; gateway-level block
**Rationale**: Direct consequence of D-329.
**Tiers**: Same.

### Extensibility & Lockdown Configuration

#### D-322: Skills Hub gating
**Decision**: TOML toggle (`[skills.hub] enabled = false` default) **plus** CLI-only install path (`arc skill hub install <name>`). No agent-driven auto-install at federal.
**Priority**: security (defense-in-depth: must enable AND must run CLI); simplicity (one toggle for the off case)
**Alternatives**: Optional pip extra; toggle-only; signature-required-always
**Rationale**: Two-step gate prevents accidental enablement leading to silent skill installs. User explicitly requested both layers.
**Tiers**: Federal: hub blocked OR allowlisted source list only; signed skills only; install requires admin role. Enterprise: hub on, signature verify required, admin approval per install. Personal: hub off by default; once enabled, agent can request install with user confirmation.

#### D-330: Cross-session context reads (per-session ACL)
**Decision**: Each session carries an ACL: `private` | `shared-with-agent` | `shared-with-other-users-via-agent`. Tier sets defaults.
**Priority**: security (information flow control); modularity (ACL surface is one field, enforcement is one gate)
**Alternatives**: Always shared (info-flow violation); always isolated (loses value); memory-shared/history-isolated
**Rationale**: Enables D-329's "agent can read across own sessions for context" while preserving multi-tenant safety.
**Tiers**: Federal: default `private`; cross-session reads blocked unless user marks shared. Enterprise: default `shared-with-agent` within team; warn on cross-org. Personal: default `shared-with-agent`.

#### D-332: Pluggable terminal backends in arcrun
**Decision**: arcrun exposes `ExecutorBackend` protocol; ships `local` and `docker` in core. `ssh`/`modal`/`daytona`/`singularity` ship as separate `arcrun-backend-{name}` packages or extras.
**Priority**: modularity (arcrun = execution); simplicity (core stays tiny); security (tier can allowlist which backends are even loadable)
**Alternatives**: Backends in arcagent (violates CLAUDE.md split); all in core (deps explosion); local-only
**Rationale**: Honors Arc's package boundaries; matches existing sandbox.py location.
**Tiers**: Federal: backend allowlist required; remote backends require approved network reachability. Enterprise: warns on unsigned backend plugins. Personal: any backend installed is usable.

#### D-333: Platform credential storage
**Decision**: Federal/enterprise must resolve via vault backend (extends existing `arcagent.modules.vault_azure` pattern; pluggable for HashiCorp/AWS/etc.). Personal uses `~/.arc/gateway.toml` with 0600 perms.
**Priority**: security (credentials never on disk at federal — IA-5); simplicity (personal stays one-file)
**Alternatives**: Vault always (personal friction); env+file (federal violation); OS keyring (no FIPS)
**Rationale**: Builds on existing Arc secret hygiene.
**Tiers**: Federal: vault required, hard error otherwise. Enterprise: vault preferred, env fallback warns. Personal: file or env, file 0600.

#### D-323: Centralized slash command registry
**Decision**: Single `arccli.commands.registry` (CommandDef list) consumed by arccli, arcgateway, arctui, telegram/slack/discord platforms. One source of truth for dispatch + help + autocomplete + platform menus.
**Priority**: simplicity (one file change to add/alias/category); modularity (each surface picks how to render but shares the catalog)
**Alternatives**: Per-surface registries (drift); registry in arcagent (UX in agent layer)
**Rationale**: Hermes' biggest single maintenance leverage; worth the cross-package read dep.
**Tiers**: Same. Federal: command catalog can be filtered by config (hide commands not allowed by tier).

### Data Model

#### D-334: Memory architecture under per-(user, agent) sessions
**Decision**: Two-tier. `bio_memory.identity` and `bio_memory.episodic` stay agent-wide (the agent's own self/history). NEW `user_profile/{user_id}.md` per user (preferences, communication style, history with this agent). Read order on turn: agent_memory + relevant user_profile.
**Priority**: modularity (two clean surfaces); simplicity (one new file type); security (per-user reads gated by D-330 ACL)
**Alternatives**: Single shared agent memory (multi-tenant leakage); per-user only (loses agent compounding); three-tier (complexity)
**Rationale**: Mirrors Hermes' MEMORY/USER split adapted to Arc's per-user session model and existing bio_memory.
**Tiers**: Federal: cross-user profile reads blocked per D-330. Enterprise: warn on cross-user. Personal: free read.

### Integration

#### D-335: Natural-language cron parser
**Decision**: Deterministic parser first (regex/grammar for cron exprs, ISO timestamps, `every Nh`, `9am daily`, `30m`); arcllm fallback only on parse failure with strict JSON-schema response.
**Priority**: simplicity (90% free); security (deterministic = federal auditable); scalability (no per-schedule LLM cost in common case)
**Alternatives**: Always-LLM (cost + non-determinism); deterministic-only (loses NL UX); two-stage user-confirm (friction)
**Rationale**: Best of both — predictable for the common case, NL flexibility on the long tail.
**Tiers**: Federal: LLM fallback disabled by default; deterministic-only mode. Enterprise: fallback enabled with audit event per LLM-resolved schedule. Personal: full fallback.

#### D-336: Subagent delegation primitive
**Decision**: arcrun gains a `spawn()` primitive (child execution context with own tool budget, identity, sandbox). The agent-facing tool lives in arcagent (`arcagent.tools.delegate`) and calls `arcrun.spawn(...)`. NOT routed through arcteam (which is fleet-level).
**Priority**: modularity (arcagent decides WHAT, arcrun does HOW — matches CLAUDE.md split); simplicity (one new primitive, no NATS dep)
**Alternatives**: New tool only in arcagent (loses arcrun integration); arcteam routing (overkill for ephemeral); both tools (UX confusion); skip
**Rationale**: User explicitly said "arc run should handle the delegation through its spawn in the execution run." Spawning is execution, not coordination.
**Tiers**: Federal: spawn requires explicit allowlist + own DID per child + delegation audit chain. Enterprise: warn on deep recursion. Personal: depth limit only.

### Self-Improvement

#### D-337: Skill auto-creation nudge location
**Decision**: New `arcagent.modules.skill_improver.nudge` submodule. Subscribes to module bus events (`tool.success`, `tool.error`, `user.correction`); counts per turn; injects a system message at threshold, calling existing skill_improver create/patch path.
**Priority**: simplicity (uses existing event bus + skill_improver substrate); modularity (nudge is a thin trigger over existing reflector/evaluator/Pareto)
**Alternatives**: New `compounding` module (yet another module); arcrun loop hook (violates CLAUDE.md); manual-only (loses self-improvement)
**Rationale**: skill_improver already has the substrate (reflector, evaluator, Pareto, candidate_store); only the trigger logic was missing.
**Tiers**: Same trigger; auto-created skills audited and human-confirmable at federal/enterprise (D-314 applies).

### UI/UX

#### D-324: TUI tech stack
**Decision**: Textual (Python-only). No Node/Ink dependency.
**Priority**: simplicity (one runtime); modularity (no polyglot bridge); security (no Node toolchain in air-gapped/SCIF)
**Alternatives**: Hermes Ink + Python RPC; prompt_toolkit only; web TUI via xterm.js
**Rationale**: Arc is Python-only. Air-gapped deployments shouldn't need Node. Textual is mature enough.
**Tiers**: Same. Federal: tier filter applied to slash command catalog (D-323).

### Open Questions (deferred to per-feature `/build` runs)

- **arcgateway**: NATS vs in-process queue for inbound message routing across N gateway instances (only matters at >1 gateway — defer to `/build arcgateway`)
- **session-search**: FTS5 indexer crash recovery + rebuild strategy (defer to `/build session-search`)
- **skill hub**: source allowlist format and signature scheme at federal tier (defer to `/build skill-hub`)
- **Voice/web/browser modules**: provider lists, headless mode defaults, air-gap providers (defer to per-module `/build`)
- **Cron self-scheduling prevention**: copy Hermes pattern (cron sessions cannot create cron jobs)? Confirm in `/build cron-nl`
- **Concurrent arcgateway instances**: single instance default, NATS routing for >1 — defer
- **arcrun ExecutorBackend protocol**: exact interface contract, how backend plugins register, how tier allowlist is read (defer to `/build executor-backends`)

### Explicitly Out of Scope

- **MCP client** — not building an arcagent MCP module. Reconsider only if user explicitly asks.
- **Migration tooling** (`arc migrate hermes` / `arc migrate claw`) — not building. Users adopt Arc fresh.
- **ACP / IDE adapter** (`arcacp`) — not building. May ship later as community package if demand emerges.

### Related Solutions

- `.claude/builds/multi-agent-ui-architecture/` — UIReporter, agent-to-UI WebSocket pattern (informs D-325/D-326)
- `.claude/builds/arcllm-call-queue/` — call queueing, delegation patterns (informs D-336)
- `.claude/builds/scheduling-heartbeat/` — scheduler internals (informs D-335 cron pipeline)
- `.claude/builds/slack-messaging/` and `telegram-messaging/` — existing platform adapter patterns (informs D-325)

### Handoff

User chose `/deepen` for parallel research before `/specify`. Recommended deepen targets per D-* mapping in this section.

---

## Hermes-Parity Roadmap — Deepening Insights (2026-04-18)

**Deepened on:** 2026-04-18 | **Research agents spawned:** 8 (parallel) | **Solutions referenced:** prior Arc builds (arcllm-call-queue, scheduling-heartbeat, slack-messaging, telegram-messaging, multi-agent-ui-architecture)

### Top Findings That Changed the Roadmap Confidence

1. **arcrun spawn.py stub already exists** at `packages/arcrun/src/arcrun/builtins/spawn.py` with the right defaults (300s timeout, 5 concurrent, 25 turns/child). D-336's "arcrun gains a spawn primitive" is partially implemented; deepen-finding upgrades the gap from "design + implement" to "harden + integrate." Existing event bubbling at `child.<run_id>.<event_type>`.
2. **Hermes explicitly REJECTED the JSONL-primary + FTS5-mirror design** (D-327) and consolidated to SQLite-primary with JSONL as export only — "Provides persistent session storage with FTS5 full-text search, replacing the per-session JSONL file approach." Their reason was performance/contention. **Arc's federal audit-trail requirement keeps JSONL primary**, but the polling-with-checkpoint indexer (NOT file watcher, NOT in-process queue) is the only crash-safe pattern that satisfies both constraints.
3. **Arc's existing `skill_improver` module already collects every signal needed** for D-337's nudge (trace_collector.py has tool counts, error counts, task_outcome classification, coverage_pct, fingerprints, cooldown logic, exempt tags, MutationEvent audit). Nudge is a thin consumer at module-bus priority 150 — NOT a reimplementation.
4. **Hermes' session key is `f"{agent_id}:{platform}:{chat_type}:{user_id}"`** — exactly the (user, agent) shape from D-329. Identity graph (`user_identity_id → [telegram:123, slack:U456, ...]`) is resolved BEFORE session-key construction so the same human collapses across platforms. Validates D-329 mid-walk pivot.
5. **Use `cronsim` not `croniter`** for D-335 — croniter has DST evaluation bugs; cronsim is actively maintained by Healthchecks.io (production), pure Python, DST-correct via zoneinfo.
6. **Hermes' self-scheduling prevention removes the cronjob tool from the registry** for the duration of cron sessions (`disabled_toolsets=["cronjob", "messaging", "clarify"]`). Tool-registry-layer enforcement, NOT a policy flag — survives prompt injection. This is the right pattern for Arc's open-question item.
7. **Sigstore keyless (cosign) + Fulcio + Rekor** is the answer for D-322 federal-tier signing — PyPI shipped this exact pattern in Nov 2024 (GA 2025); proven; OIDC-based; no key management UX brick wall. **SLSA Build Level 3** at federal, Level 2 at standard.
8. **NDSS 2025 KV-cache sharing paper** documents cross-tenant side channels — D-330's federal-tier "private session" default is justified by recent crypto research, not paranoia.

### New Risks Discovered (Not in Original Decisions)

- **Race condition: pre-await session-active check** (Hermes PR #4926) — must set `_active_sessions[key]` SYNCHRONOUSLY before any `await`, or fast-message bursts/Slack-Socket-Mode replays spawn duplicate agents per session. Affects D-325/D-326/D-331.
- **Telegram polling-conflict cascade** — exactly one process can long-poll a bot token; multi-instance arcgateway needs sticky webhook routing or NATS JetStream per-session subjects to avoid silent crash-loops. Affects D-325 horizontal scale.
- **LLM stream flood-control on rate-limited platforms** — Telegram/Slack edit-rate-limit + linear retry stalls the gateway dispatch loop. Hermes uses 3-strikes → final-send-only fallback. Must be in arcgateway core.
- **Hermes' implicit token-pool bug** — children debit no shared root budget; a parent spawning 3 children at 50 iters each spends 4× a non-delegating run with no warning. D-336 must implement root-pooled token budget.
- **Hermes' heartbeat-thread leak** for child execution — daemon threads keep parent's inactivity timer warm but leak on hang. Use `asyncio.TaskGroup` (3.11+) with structured concurrency.
- **Hermes' tool-name global mutation** — `model_tools._last_resolved_tool_names` is a process-global; race condition under true parallelism. Arc's per-RunState `ToolRegistry` already correct; do not regress.
- **ClawHavoc (Jan-Feb 2026): 1,184 malicious ClawHub skills** by single actor `hightower6eu` (677 alone) using typosquat + ClickFix (Prerequisites tells user to `curl … | bash` a remote payload). Static scan can't catch remote payload. Defense: critical-severity auto-block on `curl_pipe_shell` and `remote_fetch` patterns; federal blocks any skill that fetches remote at install OR runtime.
- **Snyk Feb 2026 audit: 13.4% of 3,984 skills had critical-severity issues** in the broader ecosystem. Arc's skills-hub default-off + CLI-only-install (D-322) is correctly conservative.
- **Recent CVEs:** CVE-2025-6514 (mcp-remote RCE), CVE-2025-59536/CVE-2026-21852 (Claude Code hooks RCE), CVE-2023-41039 + CVE-2024-49755 (RestrictedPython escapes — DON'T use it; use Firecracker microVM dry-run instead).

---

### Architecture — Research Insights

#### D-321: Package boundaries — Research Insights
No external research changes; reaffirms the boundary discipline. Prior Arc builds (`arcllm-call-queue`) confirm the "module-not-package" decision pattern works well — the QueueModule for arcllm sits in the existing wrapping stack at a defined position rather than becoming a sibling package. Same logic applies to all new arcagent modules in the roadmap.

#### D-325 & D-326: arcgateway process model + dispatch — Research Insights

**Reference architecture:** Hermes `gateway/run.py` (`GatewayRunner`, ~485KB) is the canonical implementation. Key patterns:
- **One adapter = one coroutine + one `asyncio.TaskGroup` (3.11+)** — adapter crash doesn't kill siblings.
- **Single reconnect watcher** walks a `_failed_platforms: {Platform: {config, attempts, next_retry}}` dict. Backoff `min(30 * 2**(n-1), 300)` capped at 5min, 20 attempts.
- **Tier dispatch via `Executor` Protocol**: `AsyncioExecutor` / `SubprocessExecutor` / `NATSExecutor` all satisfy `Executor.run(event) -> AsyncIterator[Delta]`. Streaming consumer is transport-agnostic.

**Concrete footprints (production observed):**
| Executor | RSS | Cold start |
|---|---|---|
| asyncio task | 2-8 MB | 10-50 ms |
| subprocess (Python) | 35-60 MB | 150-400 ms |
| Firecracker microVM | 128 MB | 125 ms |
| NATS-routed remote agent | — | 10-30 ms RTT overhead |

**Pitfalls:**
- The "set-active-before-await" race (Hermes PR #4926) is the #1 gateway bug — set `_active_sessions[session_key] = asyncio.Event()` SYNCHRONOUSLY before `asyncio.create_task(...)`. Use `_AGENT_PENDING_SENTINEL` placeholder in agent cache before first `await` to close same-class race in agent resolution.
- **Cross-session prompt contamination via shared caches** is the federal-tier killer — Hermes shares `session_store`, `channel_directory`, `_voice_mode`, `process_registry`, and the global `httpx.AsyncClient` pool across tenants. For Arc federal: subprocess-per-session minimum + classification-partitioned caches + no shared connection pool (DNS reuse is a covert channel).
- **DM pairing**: 8-char from 32-char unambiguous alphabet `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (no 0/O/1/I), 1h TTL, 3 pending max per platform, 1/10min rate limit, 5 fails → 1h lockout, atomic temp-file + `os.replace()` + `chmod 0600`. Federal: bind code to approver's DID signature.

**Files cited:** Hermes `gateway/run.py`, `gateway/platforms/base.py`, `gateway/session.py`, `gateway/pairing.py`, `gateway/stream_consumer.py`.

**Recommended arcgateway layout:** `runner.py` + `adapters/base.py` + `adapters/{platform}.py` + `session.py` + `pairing.py` + `executor.py` + `delivery.py` + `stream_bridge.py`. Core (runner+base+session+executor) ≈ 1,200 LOC; adapters live outside the 3,500 LOC core budget per ADR-004.

#### D-327: JSONL primary + SQLite FTS5 derived index — Research Insights

**Conflict with Hermes (acknowledged, not adopted):** Hermes EXPLICITLY rejected this architecture and consolidated to SQLite-primary (`hermes_state.py` line ~1, "replacing the per-session JSONL file approach"). They use external-content FTS5 with synchronous triggers; their hard-won knobs are WAL + `BEGIN IMMEDIATE` + 1s busy timeout + 20-150ms jittered app-level retries + PASSIVE checkpoint every 50 writes. Worth knowing — but Arc's federal audit-trail requirement (AU-9 tamper-evident, append-only) keeps JSONL as primary.

**For JSONL-primary, polling-with-checkpoint is the only crash-safe pattern:**
- File watchers (inotify/FSEvents/watchdog) miss events during indexer downtime, race on rotate, silently drop on macOS FSEvents.
- In-process queues lose entries on crash.
- Polling with byte-offset + inode in `sync_state` table is idempotent on replay.

**FTS5 specifics:**
- Use **external-content** tables (`content=messages, content_rowid=id`), NOT contentless — keeps `snippet()` and `highlight()` for UX.
- Tokenizer: `porter unicode61 remove_diacritics 2`; add `trigram` for substring/fuzzy.
- `columnsize=0` saves ~10% disk; always set.
- WAL mode = readers don't block writers; only writer-vs-writer blocks (we have one indexer instance — non-issue).

**Rebuild costs from JSONL** (real numbers):
| JSONL size | Full rebuild |
|---|---|
| 100 MB | 1-2 min |
| 1 GB | 10-20 min |
| 10 GB | 2-3 hr (use `PRAGMA synchronous=OFF`, re-enable after) |

**Indexer cadence:** 30s polling is invisible — agents rarely search just-written messages.

**Federal at-rest encryption:** SQLite SEE ($2k commercial) or SQLCipher (BSD); polling indexer is oblivious. JSONL writes to encrypted FS or age-encrypted at rotation.

**Concrete code sketch provided** (60-line `SessionIndex` class with `_poll_loop`, `_scan_once`, `search`). Wire to existing `packages/arcagent/src/arcagent/core/session_manager.py` — purely additive.

#### D-328: Session API ownership — Research Insights

Confirmed by gateway research: Hermes' `GatewayRunner` calls into shared `session_store` for read/write and uses agent-cache `OrderedDict` (cap=128, 1h idle TTL). Same pattern works for Arc — sessions stay in arcagent, gateway imports them. No external research surfaces a reason to extract.

#### D-329 & D-331: Per-(user, agent) session model — Research Insights

**Strongly validated by Hermes' own session-key shape:** `f"{agent_id}:{platform}:{chat_type}:{user_id}"`. The (user, agent) pivot is the right model.

**Identity graph pattern:** `user_identity_id → [telegram:123, slack:U456, signal:+1...]` resolved BEFORE session-key construction collapses the same human to one session across platforms. Store in SQLite keyed on stable cryptographic DIDs from Arc's `identity.py`. **Linking telegram:123 ↔ slack:U456 is itself a disclosure event at federal tier — audit it.**

**Concurrency:** D-331's per-session-FIFO needs the same set-active-before-await guard as D-325/D-326. Without it, photo-album bursts and Socket Mode replays spawn duplicates.

#### D-322: Skills Hub gating — Research Insights

**Signing scheme: Sigstore keyless (cosign + Fulcio + Rekor).** GPG is dead for community authors (key management UX brick wall). Ed25519 detached still requires key distribution. PyPI shipped Sigstore attestations in Nov 2024 (GA 2025) — proven pattern. OIDC identity (e.g., GitHub Actions `id-token: write`) means no keys to manage. Rekor transparency log aligns with NIST AU-10 + FedRAMP Rev5 supply-chain controls.

**SLSA provenance levels:** Arc requires **Build Level 3** at federal (hermetic builds, non-falsifiable provenance) + Level 2 at standard. SBOM via `cyclonedx-py`.

**Security scanning — extend Hermes' 80+ regex patterns with semgrep + bandit + GuardDog:**
| Category | Patterns | Reuse |
|---|---|---|
| Exfiltration | `curl/wget/httpx` w/ `KEY/TOKEN/SECRET/PASSWORD`; `~/.ssh`/`.aws`/`.kube`/`.gnupg`/`.netrc` reads; DNS exfil; `${}` in markdown links | Hermes patterns |
| Prompt injection | role hijack, "ignore previous", DAN/dev-mode, hidden HTML, `display:none` | Hermes patterns |
| Destructive | `rm -rf /`, `mkfs`, `dd of=/dev/`, `>/etc/`, `shutil.rmtree` on absolute | Hermes |
| Persistence | `crontab`, `.bashrc`, `authorized_keys`, **`AGENTS.md/CLAUDE.md/SOUL.md` writes** (covert cross-session instruction plants — ASI06 critical) | Hermes |
| Network/reverse | `nc -lp`, `/dev/tcp/`, `ngrok`, `cloudflared`, `webhook.site`, `requestbin` | Hermes |
| Obfuscation | `curl …| bash`, `base64 -d |`, `eval()`, `__import__("os")`, `chr()+chr()` chains | Hermes |
| Credential leak | `ghp_`, `github_pat_`, `sk-`, `sk-ant-`, `AKIA`, `-----BEGIN … PRIVATE KEY-----` | Hermes |
| Structural | files ≤50, total ≤1MB, single file ≤256KB, no ELF/Mach-O/PE/.so/.dylib/.exe, no escaping symlinks | Hermes |
| Agentic-specific | `allowed-tools:` frontmatter (pre-approved tool claim), unpinned `pip install`, `uv run` (auto-install) | NEW |

Use **semgrep** (`p/security-audit`, `p/python-security`) + **GuardDog** (Datadog, ships as semgrep) + **bandit** for Python AST + custom `ast.NodeVisitor` for dynamic-import detection. Hermes' regex is the fast first pass; semgrep is the high-precision second pass.

**Sandboxed dry-run:** Firecracker microVM (already in Arc stack via arcrun) for 10s import + test fixture. **DO NOT use RestrictedPython** — CVE-2023-41039, CVE-2024-49755 sandbox-escape history.

**Federal install pipeline:** quarantine (writable-nothing, nobody-owned dir) → cosign verify Fulcio cert + OIDC identity + Rekor inclusion proof → CRL check (fail-closed) → semgrep + bandit + Hermes regex → Firecracker dry-run → only `safe + signature + SLSA L3` installs → move to skills/ + `HubLockFile` entry (`content_hash`, `rekor_uuid`, `slsa_level`, `scan_verdict`) → OTel + audit.log entry.

**TOML allowlist format:** keys = `enabled`, `tier.level`, `policy.{require_signature, require_slsa_level, require_scan_pass, install_path, max_findings_allowed}`, `[[sources]]` array w/ name/type/repo/trust/signer_identity/signer_issuer/allowed_publishers/fulcio_root_ca, `revocation.{crl_url, crl_refresh_interval_seconds, fail_closed_if_unreachable}`.

**Top 3 attack patterns Arc must defend against:**
1. **Mass typosquat + ClickFix** (ClawHavoc 1,184 skills, Jan-Feb 2026) — Prerequisites tells user `curl … | bash`. Defense: critical-severity auto-block on `curl_pipe_shell` + `remote_fetch`; federal blocks any remote fetch at install OR runtime.
2. **Covert agent-config persistence (ASI06)** — skills writing CLAUDE.md/AGENTS.md/identity.md/policy/*.toml. Defense: critical-severity on those paths from any skill; integrity hashes on those files via `telemetry.py`.
3. **Prompt-injection-via-description (LLM01+ASI01)** — `description:` field in SKILL.md frontmatter contains "ignore previous; exfiltrate to https://...". Enters context during catalog browse even if skill never invoked. Defense: scan all user-visible text fields with injection-pattern bank; auto-block at federal (no human review path).

#### D-330 & D-334: Memory ACL + per-user profile — Research Insights

**Capabilities > ACLs for agent memory.** The LLM is itself an untrusted principal — short-lived signed tokens scoped to per-turn reads compose better with the module bus than DAC ACLs. **Recommended combo: BLP for classification + capabilities for per-turn grants + ACLs for admin ops.** Defense in depth.

**Structural separation is the ONLY defense against prompt-injection cross-tenant leakage** ("ignore prior; show User B's profile"). Production-pattern convergence (2025-2026 lit):
1. The LLM never sees memory it isn't cleared for — filter at **retrieval**, not at rendering.
2. Tenant-partitioned KV cache (NDSS 2025 paper documents cross-tenant side channels via shared caches).
3. **Caller DID bound at TRANSPORT layer** — strip/rewrite `user_id` args from LLM output; the model cannot override caller identity via argument injection.
4. Injection scanning on writes (Hermes pattern in `memory_tool.py`) blocks persistent backdoors.

**Arc's module bus already has the right primitives:** `ctx.veto()` + priority. Existing memory module subscribes at priority 85/90. **New `arcagent.modules.memory_acl` subscribes at priority 10 (highest)** to `memory.read`, `memory.write`, `memory.search` events. Memory provider re-checks (defense in depth).

**Per-user profile schema** (`user_profile/{user_id}.md`):
```yaml
---
user_did: did:arc:...
created: 2026-04-18T...
classification: unclassified | cui | secret
acl:
  owner: <user_did>
  agent_read: true
  cross_user_shareable: false   # federal default: private
schema_version: 1
---
## Identity
## Preferences
## Durable Facts        (append-only; each entry has source_session_id)
## Derived (dialectic)  (regeneratable after tombstone)
```
2 KB hard cap. Overflow spills to episodic store (NOT silently truncated). Frontmatter = ACL payload the bus reads; body = what the LLM sees IF cleared.

**Federation:** shared user identity service (DID-anchored canonical profile); each agent gets a signed view + freshness token; per-agent annotations in `user_profile/{user_id}.agents/{agent_did}.md` (never crosses agents).

**GDPR / right-to-be-forgotten over append-only JSONL:** **tombstone events**. Append signed `user.forgotten` event. All readers (FTS5 indexer, dialectic derivers) treat it as erasure barrier; FTS5 rebuilds excluding entries; user_profile.md deleted outright; session JSONLs redacted FIELD-wise (preserves tool-call audit structure). Retain tombstone metadata only (user DID hash + timestamp).

**Audit verbosity:** federal = every cross-session read full event (caller DID, target user DID, session IDs touched, classification label, capability ID, content-returned bool). Same-session reads sample 1-in-100 with full-fidelity on policy violations.

**Reference systems analyzed:** Honcho (peers/workspaces/sessions, dialectic derivers — referenced by Hermes README), Mem0 (scope filters, no per-record ACL), LangGraph Store (tuple namespaces, weak default), Letta/MemGPT (single-principal, MemFS).

#### D-335: NL cron parser hybrid — Research Insights

**Library recommendation: `cronsim` + `dateparser`.**
- `cronsim` — actively maintained by Healthchecks.io (production), pure Python, zero-dep, **DST-correct via zoneinfo**. Fast next-run iteration. Fails loud on `L/W/#` it can't evaluate (vs. croniter failing quiet — known DST bugs).
- `dateparser` — Scrapinghub-maintained; handles "in 30m / tomorrow 9am / next Tuesday 3pm" + TZ cleanly. No recurrence; perfect for the one-shot/relative branch.
- **Hermes accepts only 4 formats** (`30m`/`2h`/`1d`, `every Nm`, 5-field cron, ISO timestamp) — pushes NL up to LLM. Right design.

**Schema-constrained LLM fallback:** Anthropic tool-use with explicit `normalize_schedule` schema:
```json
{"name":"normalize_schedule","input_schema":{
  "type":"object","required":["kind","cron_or_iso","tz"],
  "properties":{
    "kind":{"enum":["cron","interval","once"]},
    "cron_or_iso":{"type":"string"},
    "interval_minutes":{"type":"integer","minimum":1,"maximum":525600},
    "tz":{"type":"string","pattern":"^[A-Za-z_]+/[A-Za-z_]+$"},
    "rationale":{"type":"string","maxLength":200}
  }
}}
```
OpenAI equivalent: `response_format={"type":"json_schema","strict":true}`. Air-gap llama-3-8b: use `llguidance`/`outlines` w/ same schema (small models 60% valid-JSON without grammar; ~99% with).

**Self-scheduling prevention (Hermes pattern, the ONLY injection-proof approach):** REMOVE the cronjob tool from the registry for the duration of cron sessions:
```python
disabled_toolsets=["cronjob", "messaging", "clarify"]
quiet_mode=True; skip_context_files=True; skip_memory=True
```
Tool-registry-layer enforcement, NOT a policy flag — survives prompt injection. The model cannot emit a `cronjob.create` call because the tool doesn't exist in that session's manifest.

**Sanity check after parse:** compute next 5 fire times. Reject if min-interval < 60s (every-second accidents) or max gap > 366 days (dead schedule) or any iteration raises.

**Top 3 edge cases:**
1. DST "spring forward" skip — `0 2 * * *` on transition day never fires. cronsim correctly skips to next valid instant; croniter historically fires at 3am wall-clock (wrong semantics). Always test with fixed `zoneinfo` tz, not system TZ.
2. `L/W/#` croniter parses without raising but returns wrong dates ~30% of months. Validate with next-12-fires + reference calendar; reject any expr you can't verify.
3. Self-scheduling loop amplification — geometric growth. Tool-registry removal is the only enforcement that survives prompt injection.

**Time zone:** user profile TZ wins; agent deployment TZ fallback; UTC for audit/storage only. Always store `run_at` as aware ISO 8601 with offset.

#### D-336: Subagent delegation primitive — Research Insights

**arcrun.spawn() stub already exists** at `packages/arcrun/src/arcrun/builtins/spawn.py` with right defaults (`_DEFAULT_SPAWN_TIMEOUT_SECONDS=300`, `_DEFAULT_MAX_CONCURRENT_SPAWNS=5`, `_DEFAULT_MAX_CHILD_TURNS=25`, event bubbling at `child.<run_id>.<event_type>`). Existing scaffolding aligns with research — gap is "harden + integrate," not "design + implement."

**Hermes patterns to copy from `tools/delegate_tool.py` (1,200 LOC):**
- Child receives ONLY `goal` + optional `context` + `workspace_path` — no parent history, no parent messages. Fresh conversation per child. (`_build_child_system_prompt`, lines 90-122)
- Tool allowlist **intersected with parent's** (`child ⊆ parent`, line 313). Children never gain tools the parent lacks.
- `DELEGATE_BLOCKED_TOOLS` frozenset strips `delegate_task`, `memory`, `send_message`, `execute_code`, `clarify` from every child (line 32).
- Depth cap: `MAX_DEPTH = 2` via `child._delegate_depth = parent._delegate_depth + 1` (line 408). Federal: configurable, default 2.
- Per-parent active-children semaphore (Hermes: 3 default).
- Structured `SpawnResult` with `status ∈ {completed, max_iterations, timeout, interrupted, error, budget_exhausted}`.
- Each child gets own `session_id` linked to `parent_session_id` for audit joinability.

**Top 3 traps to avoid (Hermes bugs):**
1. **Implicit token pooling.** Hermes caps `max_iterations` per child but does NOT pool tokens at the root — parent spawning 3 children at 50 iters spends 4× a non-delegating run with no warning. Arc MUST track root-level `token_budget_remaining` that every descendant debits atomically; in-flight children get `budget_exhausted` status when exhausted.
2. **Heartbeat thread leakage.** Hermes spawns daemon thread per child (lines 466-497) to keep parent's inactivity timer warm; leaks if child hangs. Arc is async-first — use `asyncio.TaskGroup` (3.11+) with structured concurrency; no daemon threads.
3. **Tool-name global mutation.** Hermes mutates `model_tools._last_resolved_tool_names` (process-global) during child construction, then restores in `finally` (lines 759-786). Race condition under true parallelism. Arc's `ToolRegistry` is per-`RunState` by design — keep it that way.

**Federal additions:**
- Per-child DID via `HKDF(parent_sk, nonce=spawn_id, info="arc-delegate-v1")`. Short-lived (TTL = spawn_timeout). Never share parent's Ed25519 keypair.
- OTel `trace_id` inherits via Context propagation; child span is `child_of` parent w/ attribute `arc.delegation.depth`.
- Hash-chained audit: child's first entry includes parent's last entry hash as `parent_chain_tip`; chain merges back deterministically.
- `spawn.start` event w/ parent DID + child DID + task hash; `spawn.complete` w/ exit reason + token usage.

**Recommended API:**
```python
async def spawn(
    *, parent_state: RunState, task: str, context: str | None = None,
    tools: list[Tool],                 # MUST be subset of parent's tools
    system_prompt: str,                # built by arcagent, not arcrun
    identity: ChildIdentity,           # DID + ephemeral keypair (from arcagent)
    sandbox: SandboxConfig | None = None,
    max_turns: int = 25,
    token_budget: int | None = None,   # drawn from parent's root pool
    wallclock_timeout_s: int = 300,
    model: LLMClient | None = None,    # inherits parent if None
    on_event: Callable[[Event], None] | None = None,
) -> SpawnResult: ...

async def spawn_many(specs: list[SpawnSpec], *, max_concurrent: int = 3, fail_fast: bool = False) -> list[SpawnResult]: ...
```
Agent-facing `delegate` tool in `arcagent/modules/delegate/` is a thin wrapper.

#### D-337: Skill auto-create nudge — Research Insights

**Hermes' "trigger" is PROSE in the tool schema description**, not a code hook. `SKILL_MANAGE_SCHEMA.description` tells the model: "complex task succeeded (5+ calls), errors overcome, user-corrected approach worked, non-trivial workflow discovered, or user asks you to remember a procedure. Skip for simple one-offs. Confirm with user before creating/deleting." Hermes leans on LLM judgment + per-create scan via `skills_guard.py`. False-positive control: the confirmation gate.

**Arc's existing `skill_improver` has every signal needed** (gap analysis):
| Signal | File | Use |
|---|---|---|
| Tool-call counts per span (`ToolCallRecord` w/ `result_status ∈ {ok, error, vetoed}`, `duration_ms`) | `trace_collector.py:150-177` | "5+ calls" + "errors overcome" |
| `task_outcome ∈ {success, partial, failure}` from error counts | `trace_collector.py:197-205` | Voyager-style binary gate |
| `usage_counts`, `turn_number` | `trace_collector.py:86-88, 148` | Cooldown |
| Coverage pct (expected-vs-actual tool coverage) | `trace_collector.py:188-195` | "Novel workflow" proxy (low coverage = novel) |
| SHA-256 fingerprint | `models.py:151-153, 170-172` | Dedup |
| Append-only audit (`MutationEvent`) | `candidate_store.py:136-141`, `models.py:227-276` | NIST AU-3 |
| Cooldown + exempt tags (`cooloff_turns=200`, `exempt_tags=[security-critical, compliance, auth]`) | `guardrails.py:99-121`, `config.py:45-48` | Reuse verbatim |

**Trigger conjunction (all true, AND):**
```
turn.tool_calls_ok >= 5
AND turn.task_outcome == "success"
AND (turn.error_count >= 1 OR turn.user_correction_detected
     OR turn.max_existing_skill_coverage < 0.3)
AND NOT in_cooldown(session_id)
AND NOT any(skill.tags & exempt_tags)
```

**Dedup (pre-commit):** name collision check via `candidate_store._validate_skill_name`; fingerprint match against `Candidate.fingerprint`; semantic similarity ≥ 0.85 (mirror existing `config.trace_similarity_threshold=0.85`).

**Cooldown:** max 1 nudge per 50 turns (align w/ `trace_buffer_turns=50`); per-skill-shape suppress 200 turns (reuse `cooloff_turns=200`); hard ceiling 3 nudges per session.

**Wiring:** `NudgeEmitter` subscribes to `agent:post_plan` at priority 150 (after trace_collector at 200 so span closed and `task_outcome` set). Reads collector's just-closed span via small ring buffer; evaluates conjunction; consults per-session deque for cooldown; publishes `system_message_nudge` event the context_manager injects on next turn.

**Nudge prose (ASI-09 + LLM06 — agent asks, doesn't act):** "The last turn used N tools successfully, recovered from an error, and doesn't match any existing skill (top coverage X%). If this workflow is likely to recur, consider calling `skill_manage(action='create', ...)`. Skip if one-off. Confirm with user before committing."

**Federal audit:** every nudge → `TelemetryEvent("skill_improver.nudge_emitted", {turn_id, session_id, signal_vector, tool_sequence_hash, outcome_source})`. Every auto-created skill → `MutationEvent(stop_reason="auto_nudge", trace_ids=[turn's trace_id])` via existing append.

#### D-332: ExecutorBackend protocol — Research Insights

**Hermes' `BaseEnvironment` (in `tools/environments/base.py` 26KB) has ONLY 1 abstract method** (`_run_bash` returning `ProcessHandle` Protocol w/ `poll/kill/wait/stdout/returncode`) plus `cleanup()`. Everything else (session snapshot, CWD tracking, stdout drain, interrupt poll, SIGTERM→SIGKILL escalation, heartbeat callbacks) lives in the base class. **New backends are ~200 LOC instead of 2,000.** This is the single most important pattern.

**`_ThreadedProcessHandle` is the SDK-adapter trick:** SDK-only backends (Modal, Daytona) have no real subprocess; Hermes wraps `(exec_fn, cancel_fn)` behind `os.pipe` — a worker thread writes to the pipe so the unified poll/drain loop still works. Critical for plugging wildly different backends behind one Protocol.

**Backends fall into 2 capability classes:**
- **Bind-mount** (`local`, `docker`, `singularity`) — host FS visible, no file sync.
- **Remote/SDK** (`ssh`, `modal`, `daytona`) — need `FileSyncManager` (Hermes `tools/environments/file_sync.py`) tracking mtime+size, batch-uploading via backend-supplied `UploadFn/BulkUploadFn/BulkDownloadFn/DeleteFn` callbacks.

Modal/Daytona use `_stdin_mode = "heredoc"` because SDK exec doesn't accept piped stdin.

**Capability discovery beats LCD interface.** Don't force `copy_file` on every backend. Use `BackendCapabilities` Pydantic model:
```python
class BackendCapabilities(BaseModel):
    supports_file_copy: bool
    supports_persistent_workspace: bool   # daytona=T, serverless modal=F
    supports_port_forward: bool
    supports_bind_mount: bool             # local/docker/singularity
    cold_start_budget_ms: int             # local≈10, docker≈800, ssh≈300, singularity≈2000, daytona≈4000, modal cold≈6000 / warm≈400
    max_stdout_bytes: int
    isolation: Literal["none","container","vm","remote"]
```

**Minimum Protocol:**
```python
@runtime_checkable
class ExecutorBackend(Protocol):
    name: str; capabilities: BackendCapabilities
    async def run(self, command: str, *, cwd=None, env=None, timeout=120.0, stdin=None) -> ExecHandle: ...
    async def stream(self, handle: ExecHandle) -> AsyncIterator[bytes]: ...
    async def cancel(self, handle: ExecHandle, *, grace=5.0) -> None: ...   # SIGTERM, then SIGKILL
    async def close(self) -> None: ...
```
Optional methods guarded by capabilities: `copy_to`, `copy_from`, `workspace_id`, `port_forward`.

**Three-tier discovery (federal-aware):**
1. **Built-ins** — `local` and `docker` ship in `arcrun.backends`, imported directly. Always trusted.
2. **Explicit config (primary)** — `arcrun.toml` names backend by dotted import path: `backend = "arc_backends_ssh:SSHBackend"`. Loader imports + verifies `isinstance(obj, ExecutorBackend)`.
3. **Entry points (opt-in, dev-tier ONLY)** — setuptools group `arcrun.executor_backends`. **DISABLED in federal tier** because entry_points execute arbitrary code from any installed wheel. Federal: requires backend in signed `allowed_backends` manifest, Ed25519 signature verify against Arc signing cert before import. (LLM03/ASI04 mitigation per CLAUDE.md.)

**Streaming/cancellation:**
- Async iterator of bytes (NOT lines — ANSI/binary safe). Backpressure via `asyncio.Queue(maxsize=N)`. Hard truncation at `capabilities.max_stdout_bytes` w/ marker frame. ANSI stripped at display layer (Hermes `tools/ansi_strip.py`), never at capture — audit logs keep raw bytes.
- Cancel: SIGTERM, wait `grace`, SIGKILL. Local backend MUST `os.setsid` + `killpg` to avoid orphaned process groups (Hermes hit this in production at `local.py::_kill_process`). Remote backends: SDK cancel (Modal `sandbox.terminate()`, Daytona `sandbox.stop()`) w/ network timeout half the grace budget.

**Existing Arc files to refactor behind ExecutorBackend:** `packages/arcrun/src/arcrun/executor.py` (84 LOC) + `arcrun/sandbox.py` (50 LOC, permission layer pairs w/ backend) + `arcrun/builtins/execute.py` + `arcrun/builtins/contained_execute.py`.

#### D-333: Platform credential storage — Research Insights

No external research overrides. Existing `arcagent.modules.vault_azure` pattern is sound; pluggable via Protocol. DM-pairing security model from gateway research applies here too — pairing codes stored on local disk (Hermes `~/.hermes/pairing/`) won't survive multi-instance gateway; move to Postgres with pessimistic lock on approve at federal/enterprise.

#### D-324: TUI tech stack — Research Insights

No external research changes. Textual remains correct: Python-only, no Node toolchain in air-gapped/SCIF, mature framework. Sketch arctui with one event loop integrated with arcagent's asyncio loop (don't spawn separate process — that's Hermes' Ink/Node split, justified only by their React ecosystem dependency).

#### D-323: Centralized command registry — Research Insights

Confirmed by gateway research: Hermes' `hermes_cli/commands.py` `COMMAND_REGISTRY` is the single source of truth — 6 downstream consumers (CLI dispatch, gateway dispatch, gateway help, Telegram BotCommand menu, Slack subcommand routing, autocomplete). Adding a slash command = ONE file change. This is Hermes' largest single maintenance leverage; worth the cross-package read dep from arcgateway/arctui to arccli.

**Adopt CommandDef shape:** `name`, `description`, `category` (`Session|Configuration|Tools & Skills|Info|Exit`), `aliases` tuple, `args_hint`, `cli_only`, `gateway_only`, `gateway_config_gate` (config dotpath for conditional availability). Federal tier: command catalog filtered at render by tier-permission; `GATEWAY_KNOWN_COMMANDS` always includes config-gated commands so gateway can dispatch them; help/menus only show when gate is open.

---

### Citations (Authoritative External Sources)

**Reference architectures (read directly):**
- Hermes Agent (NousResearch) — `gateway/run.py`, `gateway/platforms/{base,telegram,slack}.py`, `gateway/session.py`, `gateway/pairing.py`, `gateway/stream_consumer.py`, `cron/scheduler.py`, `cron/jobs.py`, `tools/delegate_tool.py`, `tools/skills_hub.py`, `tools/skills_guard.py`, `tools/skill_manager_tool.py`, `tools/memory_tool.py`, `tools/environments/{base,local,docker,modal,daytona,file_sync}.py`, `hermes_state.py`, `hermes_cli/commands.py`
- Honcho (plastic-labs/honcho) — peer/workspace/session model, dialectic derivers
- Mem0 (mem0ai/mem0), LangGraph Store (tuple namespaces), Letta/MemGPT (MemFS)

**Standards & specs:**
- agentskills.io, MCP servers ecosystem, Sigstore (cosign + Fulcio + Rekor), PEP 740 attestations, SLSA v1.1, in-toto, NIST 800-53 (AU-2/AU-9/AU-10/AC-3/SI-12/IA-3/SC-8/SC-28), FedRAMP Rev5

**Papers:**
- ACE: Agentic Context Engineering (arXiv:2510.04618)
- Voyager (NeurIPS 2023, arXiv:2305.16291) — skill library induction
- Prompt Leakage via KV-Cache Sharing in Multi-Tenant LLM Serving (NDSS 2025)

**Security advisories (2025-2026):**
- ClawHavoc — 1,184 malicious skills on ClawHub (Jan-Feb 2026)
- Snyk audit — 13.4% of 3,984 skills had critical issues (Feb 2026)
- CVE-2025-6514 (mcp-remote RCE), CVE-2025-59536 / CVE-2026-21852 (Claude Code hooks RCE), CVE-2023-41039 + CVE-2024-49755 (RestrictedPython escapes)

**Tools:**
- cronsim (Healthchecks.io), dateparser (Scrapinghub), semgrep (`p/security-audit`, `p/python-security`), GuardDog (Datadog), bandit, cyclonedx-py, llguidance/outlines, Firecracker

---

**Deepening complete.** All 17 user decisions have research-backed insights. Roadmap is ready for `/specify` per milestone.

---

# Build Decisions: nlit-demo-local-build

**Date:** 2026-04-27
**Brainstorm:** [.claude/brainstorms/2026-04-27-nlit-demo-local-build.md](brainstorms/2026-04-27-nlit-demo-local-build.md)
**State:** [.claude/builds/nlit-demo-local-build/state.json](builds/nlit-demo-local-build/state.json)
**PRD:** [.claude/NLIT2026-Demo-PRD.md](NLIT2026-Demo-PRD.md)
**Tier target:** personal (federal-ready as product seed)
**Top principle:** Showcase real Arc capability, no fakes. Local first.

## Design at a glance

Two **independent** Arc agents in `team/`, each demonstrating one mode of "agent as coworker."

| | Agent 1 — `nlit_soc_agent` | Agent 2 — `nlit_ccri_agent` |
|---|---|---|
| Mode | Conversational, live on stage | Autonomous, scheduled overnight |
| Trigger | I chat (STIG/security topics) | `[modules.scheduler]` cron 23:00 |
| Workspace | `entities/{type}/{name}.md` from templates | gap report + run history |
| Skill set | `write_entity` (template-driven) | `read_document`, `stig_cross_reference`, `poam_validator`, `draft_gap_report` |
| Stage UX | arcui dashboard + Obsidian split-screen | arcui scheduler history + the Markdown report |

Shared between them: **nothing**. No vault, no skills, no process, no DID. Two clean Arc agents.

## Decisions

| # | Decision | Answer | Principle |
|---|----------|--------|-----------|
| 1 | Project location | Two independent `team/{nlit_soc_agent,nlit_ccri_agent}/` projects. No shared anything. | Modularity |
| 2 | Execution pattern | Both served (long-lived). Agent 1 chat-driven; Agent 2 + scheduler module fires overnight. | Simplicity (matches existing arc serve pattern) |
| 3 | Skill packaging | Each agent's own `tools/`. No `[extensions] paths` sharing. | Simplicity (no shared code, no shared bugs) |
| 4 | Agent 1 vault structure | `workspace/entities/{type}/` + **template-driven `write_entity`**. 9 entity templates: System, Event, STIG-Reference, Incident, Finding, Person, Vendor, Process, Project. Each has rich frontmatter the agent fills in. Agent writes `[[wikilinks]]` inline; Obsidian renders graph natively. NO `scan_and_connect` engine. | Simplicity + demo value (templates make structure visible — that's the wow) |
| 5 | Agent 2 cron | Built-in `[modules.scheduler]` (croniter, circuit breaker, timeout, telemetry). Stays inside Arc — schedule history is part of the demo. | Real Arc capability (vs external unix cron) |
| 6 | Stage visibility UX | **arcui** (web, `localhost:8420`, via existing `[modules.ui_reporter]`) + **Obsidian** split-screen on projector. | Real Arc capability (ui_reporter is already wired) |
| 7 | Determinism guards | Low temp (0.2) + strong `identity.md` per PRD + rehearsal. NO eval pass, NO Pydantic strict-mode demo-only validation, NO pre-recorded fallback. | No fakes (demo behavior must match production) |
| 8 | Test data | Hand-author Agent 2's 4 PRD files (system_inventory.md, stig_checklist.csv with V-220812 planted, poam_log.csv with date mismatch, org_chart.json). Agent 1: 5-7 STIG/security anchor prompts I'll riff between. | Simplicity + control (exact planted findings) |

## Explicitly out of scope (deferred)

- Pass 4 (proximity scan) — not needed at all (no `scan_and_connect` engine)
- Backup screen recordings + offline/Nemotron fallback
- Cloud deployment (Azure)
- Custom hosted brain viewer (Obsidian IS the viewer)
- Cross-agent connection ("money moment" from PRD) — replaced by two distinct demonstrations of agent capability
- `_pending.md`, `_context.md`, `connected_to[]` PRD vault metadata — unused without the inference engine

## What changed from the PRD

The PRD assumed two analyst agents writing into a shared Atlas vault, with `scan_and_connect` discovering cross-agent links automatically. Build session reframed as **two independent agents demonstrating two different modes** (conversational entity capture + autonomous scheduled processing). This:

- Cut ~10 of the 14 PRD skills (no `scan_and_connect`, no `write_relationship`, no `_pending.md` machinery, no `memory_recall` separate from arc's bio_memory)
- Eliminated cross-agent coordination complexity
- Made each agent independently shippable as an example
- Preserved the 9-entity-type richness via templates instead of via 4-pass inference

## Next step

Ready for `/specify nlit-demo-local-build` — decisions are tightly scoped and unambiguous.

---

## Research Insights — nlit-demo-local-build (2026-04-27 /deepen pass)

### 1. Scheduler — confirmed solid, with two gotchas

- `[modules.scheduler]` is real and usable. Schedule entries created via the `schedule_create` tool with cron expression (croniter syntax). Engine fires `agent_run_fn(entry.prompt, tool_choice={"type": "any"})` — **schedules fire a natural-language prompt, not a tool call directly.** The agent receives the prompt and decides what tools to use.
- **Cron is UTC by default** (`scheduler.py:159`). For 11pm EST → use `0 4 * * *` (4am UTC). Verify timezone before stage.
- **No queue persistence across restart**: in-flight entries are lost on agent crash; metadata persists.
- Hardening already applied per `solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md`: Unicode normalization, update field allowlist, circuit-breaker re-reads from store.
- Bus events emitted: `schedule:completed`, `schedule:failed` — these flow to arcui and audit.
- Recommended config for CCRI overnight: `timeout_seconds=600`, `circuit_breaker_threshold=3`.
- Files: `packages/arcagent/src/arcagent/modules/scheduler/{__init__.py:20-126, scheduler.py:357-401, models.py:119-189, config.py:13-28, store.py:71-88, tools.py:44-82}`

### 2. arcui — telemetry ready, scheduler view NOT built

- arcui dashboard (`packages/arcui/src/arcui/static/index.html`) currently shows: LLM calls, tokens, latency, cost — **all live-updating** with sub-100ms event flush.
- Event taxonomy works: `agent:init/ready/pre_tool/post_tool/error`, `llm:call_complete`, scheduler bus events.
- WebSocket auth via `~/.arcagent/ui-token` (TOCTOU-safe), auto-connect on agent startup, 1000-event buffered local deque, decorrelated jitter reconnect.
- 300 unit tests, recent commits (clean refactor 3 weeks ago) — production-quality plumbing.
- ⚠️ **Gap:** arcui has NO frontend rendering for: scheduler fire history, next-run prediction, tool-call timeline, agent state machine. The events flow over the wire but no card displays them.
- **Implication for Act 2:** "Audience sees cron history in arcui" needs either (a) ~2-3 hours of frontend work to add a schedule card OR (b) show schedule history via the filesystem (`schedules.json`) or audit log instead, OR (c) demo Act 2 visibility entirely through Obsidian (the report file appears + audit events fire). Option (c) is most honest and zero new code.

### 3. Existing skill patterns — copy-don't-invent

- **Template to copy:** `packages/arcagent/src/arcagent/tools/write.py` — uses factory pattern `create_tool(workspace, *, allowed_paths=None) -> RegisteredTool`.
- Tool fields: `name, description, input_schema (hand-written JSON Schema dict, not Pydantic), transport=ToolTransport.NATIVE, execute, source, classification ("state_modifying"|"read_only"), capability_tags`.
- Auto-discovered from `workspace/tools/*.py` or `workspace/extensions/*.py` — no toml registration needed.
- identity.md is **plain markdown, not YAML** — sections like Core Truths / Values / Personality / Decision Framework / Lessons / Boundaries / Available Tools / Behavior. Used as agent context, not auto-parsed.
- **No Jinja2 in arc** — use `string.Template` (stdlib) or f-strings for templates. Keep deps minimal.
- `entities/` is already a workspace convention.
- Workspace must use `resolve_workspace_path()` for path traversal protection.
- Tool execute() returns `str` (not raises) — prefix errors with `"Error: "`.

### 4. Obsidian behavior — two real gotchas, both fixable

- **YAML wikilinks need quoting**: `system: "[[host-alpha]]"`. Bare `[[...]]` in lists can be parsed as YAML sequences and break the graph. Wikilinks in frontmatter DO create graph edges (since Obsidian 1.4).
- **Property types Obsidian recognizes**: text, list, number, checkbox, date, datetime. No native "link" type — wikilinks-as-strings just work.
- **Performance**: smooth at <500 notes, fine at 500-1000. Stage demo (50-200 notes) is well within sweet spot.
- ⚠️ **Graph view does NOT auto-refresh on external file writes.** Notes appear in file explorer but graph requires manual click-refresh or the [Refresh Any View](https://github.com/mnaoumov/obsidian-refresh-any-view) plugin. **Action item:** install Refresh Any View plugin OR plan to click-refresh between stage prompts.
- ⚠️ **Vault cache truncation bug**: when external process writes a file LARGER than previous version, Obsidian's cache transiently overwrites disk with stale content for 1-2s. Mitigation: always write NEW files (never edit-in-place) during the demo.
- Recommended plugins: **Extended Graph** (47k+ downloads, node colors/shapes by metadata, scales by attributes, SVG export — strongest stage option), Folders-to-Graph, 3D Graph.
- Graph color-by-tag works natively; configure groups for each `type:` value (System=blue, Event=red, STIG=yellow, etc.).
- YAML pitfalls to avoid: unquoted colons (`title: STIG V-220812: SSH...` breaks), tab indentation, numeric-looking IDs (`id: 2024-001` parses as date — quote `"FIND-2024-001"`).
- File-write pattern that works: write COMPLETE files with full frontmatter every time, never edit in place.

### 5. STIG/CCRI schema — real field names verified

- **CCRI was renamed to CORA (Cyber Operational Readiness Assessment) on 2024-03-01** by JFHQ-DODIN. Federal NLIT 2026 audience knows it as CORA. **Action item:** update naming throughout (PRD, identity files, demo dialogue) to CORA, or add a "formerly CCRI" parenthetical.
- **STIG XCCDF field names verified**: `vuln_id` (V-XXXXXX), `rule_id` (SV-XXXXXXrXXXXXXX_rule), `stig_id` (e.g., RHEL-09-211010), `severity` (high|medium|low — NOT CAT I/II/III in YAML), `weight`, `cci` (list of CCI-XXXXXX), `vuln_discussion`, `check_content`, `fix_text`. Real example documented (V-257777 RHEL 9).
- **CCI format**: `CCI-XXXXXX` (six zero-padded digits). Many-to-many with NIST 800-53.
- **NIST 800-53 Rev 5 control families** (20 total, two-letter codes): AC, AT, AU, CA, CM, CP, IA, IR, MA, MP, PE, PL, PM, PS, **PT** (new), RA, SA, SC, SI, **SR** (new). Use these exact codes in `control_family` field.
- **Severity dual-naming**: XCCDF XML uses `high|medium|low`. STIG Viewer UI shows CAT I/II/III. Frontmatter should carry both: `severity_xccdf: high` + `severity_cat: "CAT I"` for clarity.
- **POA&M required fields**: poam_id, weakness_description, weakness_detector_source (STIG|ACAS|pen test|self-assessment), severity (Critical|High|Moderate|Low), scheduled_completion_date, milestones (≥2 required), point_of_contact, status (Draft|Ongoing|Delayed|Pending Verification|Completed|Risk Accepted|Waiver), corrective_action_plan, vendor_dependency, vendor_dependent_product_name.
- **Remediation SLAs (FedRAMP/CMS)**: Critical 15-30d, High 30d, Moderate 90d, Low 180-365d.
- **System inventory standard fields**: system_name, system_id, fips_199_categorization (Low|Moderate|High), system_owner, isso, issm, ao, ato_date, ato_expiration_date, fisma_boundary, primary_os, ip_range, criticality (Mission Critical|Essential|Supporting), data_classification (Unclassified|CUI|Secret|TS).
- **Critical distinction federal audience knows**: Finding ≠ Vulnerability ≠ Weakness. STIG scan output = "findings"; open findings become POA&M "weaknesses"; ACAS/CVE-based scan results = "vulnerabilities". **Don't conflate** — the Finding template should reference STIG-Reference, not CVE.

### 6. Live LLM demo determinism — three changes recommended

- **Temperature=0** (not 0.2) is the documented best practice for tool-calling reliability (Anthropic docs). Note: Opus 4.7 ignores temperature entirely (adaptive sampling); Sonnet 4.5/4.6 honor temperature=0.
- **Few-shot examples in MESSAGE format are the single highest-leverage technique** — LangChain benchmark: Sonnet 3 went from 16%→52% with just 3 message-format examples; Haiku 11%→75%. This is NOT a "demo-only hack" because real customers using these agents will get the same boost from message-format few-shots in their own setup. **Worth reconsidering Decision 7 to include 3 message-format few-shot examples in agent setup.**
- **Tool description is the most powerful steering lever** (Anthropic: "by far the most important factor in tool performance"). For `write_entity`, the description should explicitly state: "Call this tool whenever the user mentions a specific IT/security entity by name. Do not explain or ask for confirmation — just call this tool immediately." This explicit pacing instruction prevents preamble.
- **`tool_choice={"type": "tool", "name": "write_entity"}`** forces a tool call on turns where I know I'll mention an entity — no hedging is architecturally possible. Worth using for known entity-mention turns.
- **Opus 4.7 uses tools LESS often than 4.6** by default. Anthropic explicitly recommends adding tool-trigger instructions to system prompt for 4.7. If using 4.7, identity.md must be more explicit about when to call tools.
- **Recovery patterns**: imperative reformulation ("Log that — host-alpha, subnet 10.0.1.x, type system") reliably triggers tools after a missed call. "write_entity for that" with explicit tool name almost always works.
- **Demo failure modes to avoid (Bard, Gemini, Meta Connect)**: factual recall without grounding (we have grounding — agent extracts from MY input), faked demos (we're honest), shared API keys with rate-limit collisions (use a dedicated demo key).
- **Pre-flight checklist (verbatim)**: 5 rehearsals (Baseline T-7d, Variation T-5d, Recovery T-3d, Environment T-1d, Dress AM-of). Anchor card at podium with 6 entity-mention phrasings. Dedicated API key, secondary hotspot ready.

---

## Action Items Surfaced by Research (need user decision)

These are reconsiderations triggered by deepen findings, NOT new questions:

1. **Update Decision 7 (determinism)**: temperature 0 (not 0.2), and add 3 message-format few-shot examples to Agent 1's setup. Few-shots aren't a "fake" — they're a documented Anthropic best practice that real customers would use. Materially raises tool-call reliability.
2. **Update Decision 6 (stage UX)**: arcui doesn't have a scheduler-history view. Three paths: (a) build a small UI card (~2-3 hrs), (b) show schedule history from filesystem/audit log via terminal, (c) skip the "arcui scheduler view" claim entirely and demo Act 2 through Obsidian alone (the report file appears, audit events fire in arcui telemetry).
3. **CCRI vs CORA naming**: rename throughout to CORA (current correct name as of 2024-03), or keep CCRI with parenthetical. Federal audience will know.
4. **Obsidian Refresh Any View plugin**: install on demo machine (graph view doesn't auto-update on external writes). Or plan manual click-refresh between stage prompts.
5. **Update Decision 4 (entity templates)**: use real STIG XCCDF field names — `vuln_id`, `rule_id`, `stig_id`, `cci`, `severity`+`severity_cat`, `vuln_discussion`, `check_content`, `fix_text`. NIST 800-53 Rev 5 control family codes (the real 20). POA&M with proper status taxonomy. Distinguish Finding/Vulnerability/Weakness.


## Resolutions from /deepen (2026-04-27)

| # | Original decision | Updated to |
|---|------------------|------------|
| Naming | `nlit_ccri_agent` | **`nlit_cora_agent`** — full rename to CORA throughout (agent dir, identity, demo dialogue, anchor prompts). PRD's "CCRI" stays as historical reference but live demo uses CORA. |
| 4 (templates) | Generic field names | **Use real STIG XCCDF + NIST 800-53 Rev 5 + POA&M field names from research.** STIG-Reference template uses `vuln_id`, `rule_id`, `stig_id`, `severity` (high\|medium\|low), `severity_cat` (CAT I\|II\|III), `cci` (list), `vuln_discussion`, `check_content`, `fix_text`. Finding template uses POA&M-style fields with proper status taxonomy. System template uses real federal inventory fields (fips_199_categorization, isso, issm, ao, ato_date, fisma_boundary, primary_os, ip_range, criticality, data_classification). Distinguishes Finding vs Vulnerability vs Weakness explicitly. |
| 6 (stage UX) | arcui + Obsidian split-screen | **Same plus build a small arcui scheduler card (~2-3 hrs frontend work)** to render schedule history (last 5 fires + next fire). Custom event type + simple HTML card. Becomes part of arcui going forward — not demo-only. |
| 7 (determinism) | temp 0.2, no few-shots | **temp=0 + 3 message-format few-shot examples loaded as pre-conversation turns.** Documented Anthropic best practice (Sonnet: 16%→52% tool-calling reliability with 3 few-shots; LangChain benchmark). Real customer pattern, not a demo hack. Also: explicit tool-trigger instructions in identity.md + tool description, `tool_choice={"type": "tool", "name": "write_entity"}` on known entity-mention turns. |
| Obsidian | Vanilla setup | **Install Refresh Any View plugin** on demo machine (graph view doesn't auto-refresh on external writes). Verify in rehearsal. |

**Items 1, 2, 3, 5, 8 unchanged.** Project location, execution pattern, skill packaging, scheduler mechanism, test data — all stand as originally decided.


---

## Unified Capability System — Build Decisions (2026-04-28)

**Phase**: build | **Status**: complete | **Total decisions**: 31 (12 ratified, 6 auto-applied, 13 user-decided)
**ID range**: D-338 to D-368
**Brainstorm**: `.claude/brainstorms/2026-04-28-unified-capability-system.md`
**Priority framework**: simplicity → modularity → security → scalability

### Summary

Replace four parallel registration paths (built-in tool list, `[tools.native]` config, `extension(api)` factory, `MODULE.yaml` modules) with one unified `CapabilityLoader`. Two file types only: `.py` with decorator (`@tool`, `@hook`, `@background_task`, or `@capability` class for lifecycle), and `.md` skill folders with structured frontmatter and seven required title-case sections. Four scan roots in precedence order (builtins / global / agent / agent-authored), last-wins with audit. Modules become an optional packaging concept managed by `arc module` CLI — runtime never reads `MODULE.yaml`. Skills/tools manifest injected into system prompt at session start (extending D-073/D-074 pattern), bodies pulled lazily via `read`, one-shot semantics. `reload()` is the sole self-mod surface. Tier-specific TOFU governs agent-authored Python execution: personal auto-runs, enterprise prompts on first sight then persists approval, federal denies entirely without Sigstore signature. Big-bang migration in a single spec — old code deleted in same edit per CLAUDE.md mandate.

### Brainstorm Ratifications

These were decided in the brainstorm and are captured here for the global decision IDs.

#### D-338: Modules are optional shipped bundles, not a runtime concept
**Decision**: A module is a folder of capabilities (`.py` + skill folders). Runtime sees only capabilities; `arc module enable/disable/install/uninstall` manages bundles. `MODULE.yaml` is read by the CLI for marketplace metadata, never by the runtime.
**Priority**: simplicity (one runtime concept), modularity (CLI vs runtime separation)
**Tiers**: Federal — install requires Sigstore signature. Enterprise — warns on unsigned. Personal — accepts unsigned with info log.

#### D-339: Tools may declare an optional `requires_skill` field
**Decision**: Tool frontmatter has `requires_skill: Optional[str]`. When set, runtime auto-attaches the named skill body when the tool is called. Most tools omit this field.
**Priority**: simplicity (tight tool↔skill couplings made explicit)

#### D-340: Workspace boundary stays — agent writes only inside workspace
**Decision**: Agent-authored capabilities land in `<agent_root>/workspace/.capabilities/` (the only path the agent can write to). User-curated capabilities live in `<agent_root>/capabilities/` (read-only to agent).
**Priority**: security (agent can't escalate by writing outside workspace)

#### D-341: Last-wins on name collisions with audit
**Decision**: Scan order builtins → global → agent → workspace; later-loaded capability replaces earlier with same name. Audit emitted on every shadow. No special protection for builtins.
**Priority**: simplicity (consistent rule), modularity (extensibility — user can override builtins)

#### D-342: Skills are folders, one tier — no quick-vs-full distinction
**Decision**: Every skill is a folder with `SKILL.md` + optional `references/`, `scripts/`, `templates/`, `assets/`. Required frontmatter and required sections enforced uniformly. Short sections OK; missing sections rejected; filler ("N/A", "none", empty body) flagged.
**Priority**: simplicity (one mental model), modularity (consistent shape eases discovery)

#### D-343: `triggers` is a semantic hint, not a runtime matcher
**Decision**: Frontmatter `triggers: [list of phrases]` is concrete examples for the LLM to recognize when a skill applies. Never matched literally at runtime. Routing remains LLM-driven via description + triggers in the manifest.
**Priority**: simplicity (no regex/match maintenance), scalability (LLM scales semantically; regex doesn't)

#### D-344: Title-case section headings standardized
**Decision**: SKILL.md sections are exactly `## Resources`, `## Contract`, `## Knowledge`, `## Steps`, `## Anti Patterns`, `## Examples`, `## Validation`. Validator hardcodes these.
**Priority**: simplicity (one canonical form, no drift)

#### D-345: `update_*` is a separate skill+tool from `create_*`
**Decision**: `create_tool` / `create_skill` start fresh at version `1.0.0`. `update_tool` / `update_skill` modify existing capabilities and bump version. Bump direction (major/minor/patch) is decided by the LLM as a step in the update skill — see D-357.
**Priority**: simplicity (single responsibility per tool), modularity (clean tool boundaries)

#### D-346: Skills/tools manifest in system prompt at session start; bodies lazy via `read`
**Decision**: Manifest XML (extending D-073/D-074) injected into system prompt at session start. LLM picks a skill from the manifest and uses `read` on the listed SKILL.md path. Body is one-shot in conversation memory (not pinned). References pulled lazily by `read` if cited.
**Priority**: simplicity (existing bus injection pattern), scalability (no per-turn rebuild)

#### D-347: Skill-usage instruction injected via bus, not hardcoded in prompt builder
**Decision**: A short (~3-line) instruction ("scan available-skills, pick the most specific, read its SKILL.md, never read more than one up front") is injected at priority 91 alongside the skill manifest. Lives as a constant in the bus subscriber.
**Priority**: modularity (identity.md stays user-owned)

#### D-348: Policy is authoritative; skill `tools` field is descriptive
**Decision**: A skill's `tools: [...]` declares intent (which tools the skill expects to use). Runtime policy decides what actually executes. Mismatch (skill lists denied tool) registers the skill, emits `skill.tool_dependency_policy_denied` audit, surfaces at call-time refusal.
**Priority**: security (no policy override via metadata)

#### D-349: Two file types only — `.py` (decorated) and `.md` (skill folder)
**Decision**: Capability surface is exactly: Python files with `@tool` / `@hook` / `@background_task` / `@capability`-class decorators; or skill folders containing SKILL.md. Nothing else (no JSON manifests, no YAML configs at runtime). `MODULE.yaml` exists only for `arc module` CLI metadata.
**Priority**: simplicity (minimum viable type set), modularity (clean kind boundary)

### Auto-Applied (Federal Mandates)

| ID | Category | Decision | Mandated Answer | Citation |
|----|----------|----------|-----------------|----------|
| D-350 | Audit | Capability lifecycle audit | Emit on register / unregister / replace / registration_failed | NIST 800-53 AU-2 |
| D-351 | Audit | Audit log integrity | Tamper-evident, append-only for capability lifecycle | NIST 800-53 AU-9 |
| D-352 | Security | Marketplace install signing | Sigstore signature verification required for federal install | EO 14028 §4 (SBOM) |
| D-353 | Security | AST validator on agent-authored Python | Required — blocked imports, blocked attrs, blocked calls; restricted-builtins compile | NIST 800-53 SI-7(15), CM-5 |
| D-354 | Security | Federal tier blocks agent-authored `.py` reload | Reload only registers signature-verified capabilities at federal tier | NIST 800-53 CM-5, CM-7 |
| D-355 | Audit | Capability source in audit content | Track scan root and source classification (builtin / module / agent / global) on every capability event | NIST 800-53 AU-3 |

### Architecture

#### D-356: Lifecycle abstraction — three decorators + one class form
**Decision**: Four primitives:
- `@tool(name, description, when_to_use, classification, capability_tags, requires_skill?, version)` — LLM-callable function
- `@hook(event, priority)` — bus subscriber
- `@background_task(name, interval)` — periodic runtime-callable
- `@capability` class with optional `setup()` / `teardown()` — heavy-resource case (browser, memory pool, websocket)

Tools-on-class are written as `@tool` on bound methods. Each primitive maps 1:1 to a distinct runtime behavior.
**Priority**: simplicity (each decorator does one thing) > modularity (clean axis: LLM-call vs runtime-call vs lifecycle)
**Alternatives**: One unified `@capability(kind=...)` decorator (rejected — fields-by-kind is implicit schema, type-checker can't help). Two decorators `@tool` + `@reactive` (rejected — `@reactive` becomes a kitchen sink).

### Data Model

#### D-357: Versioning format — semver, LLM picks bump
**Decision**: All capabilities carry semver (`1.0.0`) in frontmatter. Create starts at `1.0.0`. LLM decides major/minor/patch as a step in the `update-skill` / `update-tool` procedure. No hardcoded auto-bump rule.
**Priority**: simplicity (don't over-engineer the bump rule; let the skill teach it)
**Alternatives**: simple int (rejected — marketplace will want semver later); always-bump-patch rule (rejected per user — the update skill is the right place to teach judgment).
**Tiers**: Federal — `version` required, audit logged on every bump. Enterprise — required, warns on missing. Personal — required (cheap to populate).

### API Design

#### D-358: `reload()` returns a plain string diff
**Decision**: `reload()` returns a single human-readable string. One line nominal:
```
reload: +3 added (create-tool, format-date, write-blog), ~2 replaced (read 1.0.0→1.0.1, write 1.0.0→1.0.1), -1 removed (legacy-grep), 0 errors
```
Multi-line only when errors exist (each error appended on its own line with skill/tool name + reason).
**Priority**: simplicity (LLM reads it like a sentence; no JSON parsing)
**Alternatives**: structured newline format (rejected — more tokens, no real benefit); JSON (rejected — parse overhead with no gain for the LLM consumer).
**Tiers**: Federal additionally writes the diff to audit log per D-350.

### Security

#### D-359: Validation script trust model — tier-specific TOFU
**Decision**: Same trust model applies to all self-executing agent-authored code (validators, `@tool`, `@hook`, `@background_task` files). Three tiers:
- **Personal**: auto-run any agent-or-user-authored script. Toml toggle (`[security] auto_run_agent_code = true`) lets user disable.
- **Enterprise**: default-allow trusted (builtins + signed modules); default-deny new agent-authored code. First sight prompts user approval. Approved scripts persisted to toml policy with hash + timestamp + approver. Subsequent loads of unchanged scripts auto-approved by hash match.
- **Federal**: default-deny everything. Only Sigstore-verified bundles run. Agent-authored code never executes (denied at AST-load time per D-354). Approval flow goes through external compliance tooling, not in-band prompt.
**Priority**: security (TOFU is industry-standard for self-executing code) > modularity (one trust model across all agent-code paths)
**Alternatives**: same AST gate as `@tool` files (rejected — auto-run-on-reload has different risk profile from LLM-invoked tool); separate stricter sandbox for validators (rejected — second sandbox flavor adds complexity for marginal gain).

### Deployment

#### D-360: Migration approach — big bang, single spec
**Decision**: New `CapabilityLoader` replaces all four old paths in one PR/spec. Every existing module (memory, browser, scheduler, voice, telegram, slack, etc., ~15 modules) rewritten to the new decorator form in the same edit. `ExtensionLoader`, `_load_modules_by_convention`, `register_native_tools`, `MODULE.yaml` runtime parsing, `[tools.native]` config block all deleted in the same edit.
**Priority**: simplicity (one mental model after merge, no parallel paths) > codebase mandate (CLAUDE.md "no legacy/backward-compat" — local-only repo, no users to break)
**Alternatives**: phased migration with parallel paths (rejected — directly contradicts CLAUDE.md, parallel paths tend to become permanent); brand-new only with old modules untouched (rejected — worst-of-both, permanent dual-path).
**Spec impact**: produces one large spec with ~15 module-migration tasks plus the loader/registry rewrite.

### Pinned (no real tradeoff)

These were settled in the build session without a question because the answer was forced by earlier decisions or had no genuine alternative.

#### D-361: Conflict UX
Last-wins everywhere with audit. No special shielding for core builtins. User explicitly wanted extensibility (better-implementation override).

#### D-362: Hot reload trigger
Explicit `reload()` call only. No file watcher, no auto-reload at session boundaries. Predictable + auditable.

#### D-363: Denied capabilities not in manifest
Hidden from prompt manifest. LLM doesn't see what it can't use; no temptation to suggest unavailable actions to the user.

#### D-364: Malformed capability — skip + audit, agent keeps starting
Frontmatter validation failure or AST rejection causes the single capability to be skipped with audit; agent continues startup. Errors surface in next `reload()` diff so the LLM can fix.

#### D-365: Skill-usage instruction text location
Hardcoded constant in the bus subscriber that injects the skills manifest. ~3 lines. Operator override deferred (would be a small TOML field if needed later).

#### D-366: state.json is not framework-enshrined
Framework writes runtime metadata (version history, last validated, last reload) to `.skill-meta.json` (hidden, framework-owned). Skills create their own state files with descriptive names if they need persistence. No reserved-keys-in-shared-file race.

#### D-367: Bus event names for capability lifecycle
`capability:added`, `capability:removed`, `capability:replaced`, `capability:registration_failed`. Parallels existing `agent:*` taxonomy.

#### D-368: `arc module install` source scope (v1)
Local file (`.tgz`, `.zip`, directory) only. Git URL and marketplace registry deferred to a future spec. Sigstore verification on install at federal tier per D-352.

### Frontmatter Schemas (pinned, captured under D-356/D-342)

**Tool decorator** (`@tool`):
- Required: `name`, `description`, `when_to_use`, `classification` (`read_only` | `state_modifying`), `capability_tags` (list, may be empty), `version`
- Optional: `requires_skill`, `examples`, `model_hint`

**Skill SKILL.md frontmatter**:
- Required: `name`, `description`, `triggers` (list), `tools` (list), `version`
- Optional: `model_hint`

**Skill `tools` field validation**: at register, check listed tools exist. Federal blocks if any missing; enterprise warns; personal info-only.

**Mandatory SKILL.md sections** (title-case, validator-enforced): `## Resources` (auto-generated by loader from folder contents — author never edits), `## Contract`, `## Knowledge`, `## Steps`, `## Anti Patterns`, `## Examples`, `## Validation`.

### Tier Variations Summary

| Concern | Federal | Enterprise | Personal |
|---------|---------|------------|----------|
| Agent-authored .py reload (D-354) | Blocked — Sigstore required | TOFU prompt → policy | Auto-run (toml toggle) |
| Module install (D-352, D-368) | Refuse unsigned | Warn on unsigned, allow | Accept unsigned, info log |
| Validator scripts (D-359) | Signed only | TOFU on first sight | Auto-run (toml toggle) |
| Capability-lifecycle audit (D-350) | Required, hard error on failure | Required, warn on failure | Optional, info-level |
| Audit log integrity (D-351) | Tamper-evident required | Append-only required | Best-effort |
| Skill `tools` field mismatch (D-348) | Blocks registration | Warns | Info-level |

### What Gets Ripped Out

- `arcagent/core/extensions.py` (`ExtensionLoader`)
- `_load_modules_by_convention` in `core/agent.py`
- `register_native_tools` in `core/tool_registry.py`
- `MODULE.yaml` runtime parsing
- `[tools.native]` config block (TOML schema)
- `make_create_tool_tool` (the in-memory-only wrapper)
- Hardcoded list in `arcagent/tools/__init__.py:create_builtin_tools`
- Four parallel registration paths in `core/agent.py` startup (lines ~376-444)

Replaced by a single `CapabilityLoader.scan_and_register()` + one `CapabilityRegistry`.

### Open Questions / Deferred to Future Specs

- MCP integration (loader designed extensible; new transport added in a future spec)
- Marketplace registry / Git-URL install sources
- Full `arc module` CLI UX (subcommand grammar, output formats)
- `CapabilityLoader` interface sketch — handled in `/specify` SDD
- Concrete test fixture strategy for capability folders — handled in `/specify` test plan

### Research Insights (Deepen — 2026-04-28)

Research conducted across five parallel streams: plugin discovery patterns (VSCode, Pulumi, Backstage, MCP, pluggy, FastAPI), AST-validated dynamic loading security (CVE catalog, Sigstore/Rekor, NIST SI-7, OWASP Agentic Top 10), skill structure best practices (Anthropic Agent Skills, OpenAI Codex Skills, Claude Code, LangChain Deep Agents, Cursor), hot-reload + lifecycle patterns (Django/Uvicorn/Streamlit/Jupyter, Pulumi/Terraform, NestJS, K8s, asyncio), and codebase migration inventory (every file/line affected by the big-bang).

#### Decisions Confirmed by Research

| Decision | Confirmation |
|----------|--------------|
| D-342 (skills are folders, one tier) | Matches Anthropic Agent Skills exactly — single SKILL.md + folder structure, no quick/full split |
| D-346 (manifest in prompt, body lazy via read) | Claude Code's exact pattern. Confirmed via reverse-engineered system prompts — `<available_skills>` XML block injected at session start, body fetched via dedicated Skill tool that strips frontmatter before injecting |
| D-347 (skill-usage instruction injected via bus) | Mellanon GitHub gist quantifies impact: routing accuracy goes from ~20% (no instruction) → ~50% (description-only) → 84% (explicit "scan/select-one/read" instruction) → ~90% (instruction + examples). The instruction wording matters as much as the manifest itself |
| D-360 (big-bang migration) | Local-only repo + CLAUDE.md "no legacy" mandate aligns with the "fresh start, no parallel paths" pattern; phased migrations historically become permanent (Pulumi #2389) |
| D-362 (explicit reload only, no file watcher) | Validated by KrakenD docs (10–30% throughput hit + stability issues), Uvicorn `--reload`+`--workers` graceful-shutdown bug, Pulumi's subprocess-per-config model. Production systems consistently avoid file watchers |
| D-352 (Sigstore signing for federal install) | PyPI PEP 740 (GA Nov 2024) is the reference. Bundle = `<artifact>.tar.gz` + `<artifact>.tar.gz.sigstore`. `sigstore verify identity --bundle --cert-identity --cert-oidc-issuer` is the verify call. Air-gapped federal requires self-hosted `rekor-server` |
| D-353 (AST validator core mechanism) | Confirmed pattern: AST walk → restricted-builtins compile → execute. Already in `arcagent/tools/_dynamic_loader.py` |
| D-358 (full-rescan diff on reload) | VSCode and MCP both use full-list re-enumeration on `list_changed` notification, not incremental patches. Incremental requires persisted state and isn't worth it for explicit-only reload |
| D-364 (skip + audit on malformed) | Middle ground between pluggy (hard error on duplicate) and VSCode (silent overwrite). VSCode users have an open issue #77979 asking for the audit trail we'll have |

#### Decisions Surfaced for Reconsideration

These are genuine tensions the research uncovered. Each is a candidate for adjustment before `/specify`.

1. **D-343 (`triggers` as separate field) — STRONG signal to fold into description**
   - **Finding**: Zero major skill standards (Anthropic Agent Skills, OpenAI Codex Skills, LangChain Deep Agents) use a separate `triggers` array. All converged on 2-field frontmatter (`name` + `description`) with trigger semantics embedded as "Use when [X]" clauses inside the description. LangChain explicitly tried algorithmic keyword matching against trigger arrays in early router implementations and abandoned it because it degrades on paraphrase variations.
   - **Implication**: A separate `triggers` field surfaced to the LLM may *confuse* the model about which field to weight, and provides no benefit over a well-written description that includes "Use when..." phrasing.
   - **Reconsider option**: Keep `triggers` as authoring-only frontmatter (human guidance for skill writers), do NOT surface it separately in the manifest XML. Or remove entirely.
   - Sources: [Anthropic Agent Skills Best Practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices), [Mellanon Routing Accuracy Data](https://gist.github.com/mellanon/50816550ecb5f3b239aa77eef7b8ed8d)

2. **D-341 (last-wins everywhere) — REFINE for hooks**
   - **Finding**: Stevedore's three-pattern taxonomy (DriverManager / HookManager / ExtensionManager) maps cleanly to our three decorators. Tools and skills are DriverManager-style (one name → one impl, last-wins is right because override is the user intent). But **hooks should be fan-out** — pluggy's pattern. Two `@hook(event="agent:ready")` registrations are both legitimate subscribers, not a collision. Backstage's hard-ownership model also distinguishes this.
   - **Implication**: D-341 as written is correct for tools/skills but wrong (or at least under-specified) for hooks and background tasks.
   - **Reconsider option**: Refine D-341 — "tools/skills: last-wins with audit; hooks: fan-out (all called in priority order, pluggy LIFO with `tryfirst`/`trylast` overrides); background tasks: last-wins (cancel + replace per below)."
   - Sources: [pluggy docs](https://pluggy.readthedocs.io/), [stevedore patterns](https://docs.openstack.org/stevedore/latest/user/patterns_loading.html)

3. **D-353 (AST validator) — known bypass categories must be explicitly closed**
   - **Finding**: Current validator blocks `ctypes/subprocess/socket/os/sys/pickle/marshal/shelve` plus `gi_frame/f_back/__class__/__bases__/__subclasses__/__reduce__/__mro__/__dict__/modules` plus `compile/eval/exec/__import__`. Research surfaced additional shipped bypass categories: `gi_code`/`gi_yieldfrom` (CVE-2023-37271), `AttributeError.obj`/`.name` (CVE-2026-0863, Python 3.10+), format-string `__format__` invocations (`f'{0.attr}'.format(obj)` triggers attr access without `getattr`), `__init_subclass__` (fires at class-creation, before validator inspects), metaclass `__getitem__` via subscript, descriptor protocol side-channels (`__pos__`/`__neg__`/`__get__`/`__set__`).
   - **CRITICAL**: AST-only validation cannot block `ctypes` FFI escapes — Cyera's CVE-2025-68668 writeup: "function-level blocking is not capability-level isolation." A correct fix requires OS-level sandboxing (seccomp on Linux, sandbox-exec on macOS).
   - **Reconsider option**: Two separate enhancements — (a) extend `_BLOCKED_ATTRIBUTES` and `_BLOCKED_CALLS` lists with the new categories above; (b) add an OS-level sandbox layer for the loader process at enterprise+ tier (Federal already requires Sigstore so agent-authored .py never executes there).
   - Sources: [CVE-2026-0863 JFrog](https://research.jfrog.com/post/achieving-remote-code-execution-on-n8n-via-sandbox-escape/), [CVE-2025-68668 Cyera](https://www.cyera.com/research/n8scape-pyodide-sandbox-escape-9-9-critical-post-auth-rce-in-n8n-cve-2025-68668), [HackMD Python jailbreak](https://hackmd.io/@rqdaA/S145ZYeYT)

4. **D-359 (TOFU model) — TOML must be outside agent's writable workspace**
   - **Finding**: npm lockfile-poisoning analog applies. If the trust TOML lives where the agent can write, a compromised agent can pre-approve its own malicious skills by writing entries to the file. VSCode workspace trust persists outside workspace settings for the same reason.
   - **Implication**: D-359's enterprise-tier persistence path needs to be a path the agent has *no* write access to (e.g., `~/.arc/security/trust.toml` rather than `<agent_root>/workspace/...`).
   - **Reconsider option**: Pin location: `~/.arc/security/trust.toml` (read-only from agent's perspective; only the human user or `arc` CLI can mutate). Add an entry-poisoning audit event if hash mismatch is detected on subsequent loads.
   - Sources: [npm lockfile poisoning](https://medium.com/node-js-cybersecurity/lockfile-poisoning-and-how-hashes-verify-integrity-in-node-js-lockfiles-0f105a6a18cd), [VSCode Workspace Trust](https://code.visualstudio.com/docs/editing/workspaces/workspace-trust)

5. **D-367 (bus event names) — add `capability:setup_failed`**
   - **Finding**: A capability can pass static validation and register successfully but fail at `setup()` time (DB connection refused, browser process won't start). Consumers (UI, audit, retry logic) need to distinguish "never loaded" from "loaded but broke at init." Current event list (`added`/`removed`/`replaced`/`registration_failed`) doesn't cover this.
   - **Reconsider option**: Add `capability:setup_failed` event. Total: 5 capability lifecycle events.
   - Sources: [NestJS lifecycle hooks issue #14773](https://github.com/nestjs/nest/issues/14773), [K8s preStop pattern](https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/)

#### Implementation Patterns for `/specify`

These are concrete implementation choices research surfaced. Not new decisions — the build decisions don't constrain these. Captured here so the SDD inherits them.

1. **Hybrid decorator pattern** (FastAPI / dontusethiscode). Decorator stamps `func._arc_meta = ToolMetadata(...)` AND registers in a side-effect dict. Decouples import-time collection from runtime use. Already partly in `tools/_decorator.py`; extend with new fields per D-356.

2. **AST-cache before import** (ModelScope pattern). Hash file by `MD5(source) + mtime`; cache scan results. Skip re-validation on unchanged files at every reload. Significant perf win on large `~/.arc/capabilities/` trees.

3. **Asyncio RWLock around registry** (CRITICAL). CPython issue #126548 documents `importlib.reload()` is thread-unsafe. Tool calls must hold a reader lock; `reload()` holds a writer lock. Library: `aiorwlock` (PyPI). Without this, concurrent tool calls during reload can race against `sys.modules` mutation.

4. **`setup()` called every reload, not just first**. Pulumi's `Configure` RPC pattern. New capability instance is fresh each time. Enables in-place version upgrades cleanly.

5. **Teardown order: reverse-topological**. NestJS `onModuleDestroy` pattern (issue #14773). Setup in topo order, teardown in reverse-topo. Build the dependency map at scan time from `requires_skill` and explicit `depends_on` (if added) fields.

6. **Background task on reload: drain-then-replace**. `old_task.cancel()` → `await old_task` (catching `CancelledError`) → `new_task = asyncio.create_task(new_fn())`. Never overlap. Use `aiojobs.shield` for tasks that must finish their current iteration before cancellation (e.g., mid-write `memory_flush`).

7. **Sigstore bundle format** (PyPI PEP 740). For federal-tier module install: `module-name.tar.gz` + `module-name.tar.gz.sigstore`. Verify with `sigstore verify identity --bundle <bundle> --cert-identity <expected> --cert-oidc-issuer <issuer> <artifact>`. Air-gapped: self-host `rekor-server` (open-source binary from Sigstore project).

8. **Three-phase teardown** (K8s preStop). Stop accepting new tool calls → drain in-flight calls (await them) → release resources. Prevents tool calls from hitting a half-torn-down capability.

#### Module Migration Mapping (Ready for /specify Task Breakdown)

Codebase analyzer surfaced concrete decorator-form for each existing module:

| Module | New form |
|--------|----------|
| memory | `@hook` × 6 (assemble_prompt, pre/post_tool, post_respond, pre_compaction, shutdown) + `@tool`s for memory operations |
| scheduler | `@capability` class with `setup`/`teardown` (engine lifecycle) + `@tool` × 4 (CRUD) + `@hook("agent:ready")` |
| browser | `@capability` class (Chrome process lifecycle) + `@tool`s |
| voice | `@tool` × 2 (transcribe, synthesize) + `@hook` × 2 |
| telegram | `@background_task` (poll loop) + `@tool` (notify) + `@hook` × 3 |
| slack | `@capability` (WebSocket connect/disconnect) + `@tool` + `@hook` |
| policy | `@hook` × 3 only (pure subscriber) |
| ui_reporter | `@hook` per subscribed event (~17 events) — or `@capability` class with internal dispatch |

#### Codebase Inventory (Migration Surface)

Files and LOC the migration touches (per codebase-analyzer):

**Delete entirely**:
- `arcagent/core/extensions.py` (611 LOC)
- `arcagent/core/module_loader.py` (257 LOC, runtime path — moves to CLI for `arc module` parsing)
- `arcagent/tools/__init__.py` hardcoded `create_builtin_tools` list (45 LOC)
- `arcagent/tools/tool_tools.py` (replaced by built-in `reload()` capability)
- All `arcagent/modules/*/MODULE.yaml` files (replaced by decorator-form Python files; YAML moves to `arc module` packaging metadata only)
- `[tools.native]` and `[extensions]` TOML config blocks; `_ENV_DENYLIST_PREFIXES` entries `tools__native`, `tools__process`

**Trim significantly**:
- `arcagent/core/agent.py` (1003 LOC → ~150-200 LOC removed across startup branches lines 376-444, reload at 822-857, prompt-injection setup at 912-981, `_load_modules_by_convention` at 964-981)
- `arcagent/core/tool_registry.py` (686 LOC → ~30-50 LOC removed: `register_native_tools` lines 491-515 + module-path validation)

**Extend in place**:
- `arcagent/tools/_decorator.py` (159 LOC → ~+50 LOC for `when_to_use`, `requires_skill`, `version`, `examples`, `model_hint` fields)
- `arcagent/tools/_dynamic_loader.py` (extend `_BLOCKED_ATTRIBUTES`/`_BLOCKED_CALLS`/etc. per insight #3 above)
- `arcagent/core/skill_registry.py` (186 LOC → likely replaced as `CapabilityRegistry` skill kind, +100 LOC for required-section validator + `triggers`/`tools`/`version` fields)

**New files**:
- `arcagent/core/capability_loader.py` (~250-350 LOC estimated)
- `arcagent/core/capability_registry.py` (~150-250 LOC estimated)

**Net core LOC change**: Brainstorm estimated -1,500 to -2,000. Codebase analyzer's bottom-up estimate suggests closer to -1,200 to -1,800 net. Either way brings `core/` under the 3,500 LOC quality gate.

**Bus events**: existing `agent:extensions_loaded` and `agent:skills_loaded` become redundant under unified system — replace with `capability:added/removed/replaced/registration_failed/setup_failed` per insight #5.

**Test infrastructure to reuse**:
- `ModuleContext` factory pattern (current module unit tests)
- AST validator test suite under `tests/unit/tools/_dynamic_loader/`
- Extension scan-precedence tests directly map to capability scan tests
- Policy denial audit assertion pattern (`tool.policy_denied` event)

**Existing assets to keep / extend (not rewrite)**:
- `@tool` decorator + `_schema_from_signature()` in `tools/_decorator.py` already infers JSON schema from typed signatures — extend, don't rewrite
- `_arc_tool_meta` stamp pattern already follows hybrid stamp+register from FastAPI — keep
- `arctrust.policy.PolicyPipeline` already integrates `TierConfig` — TOFU plugs in as a new policy layer (not a parallel system)
- `AgentTelemetry.audit_event()` is the single emission point already used for `extension.loaded`, `tool.executed`, `tool.policy_denied` — capability lifecycle events fire from same call surface

#### Key Sources

**Standards & specifications**:
- [Anthropic Agent Skills — Overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
- [Anthropic Agent Skills — Best Practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- [Anthropic Engineering: Equipping Agents for the Real World](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)
- [OpenAI Codex Skills](https://developers.openai.com/codex/skills)
- [LangChain Deep Agents Skills](https://docs.langchain.com/oss/javascript/deepagents/skills)
- [MCP Lifecycle 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)
- [PEP 740 — PyPI Attestations](https://blog.trailofbits.com/2024/11/14/attestations-a-new-generation-of-signatures-on-pypi/)

**Plugin systems / patterns**:
- [VSCode Activation Events](https://code.visualstudio.com/api/references/activation-events) and [Contribution Points](https://code.visualstudio.com/api/references/contribution-points)
- [Pulumi Provider Architecture](https://www.pulumi.com/docs/iac/guides/building-extending/providers/provider-architecture/)
- [Pulumi Issue #2389 — version pinning regret](https://github.com/pulumi/pulumi/issues/2389)
- [Backstage Software Catalog — Life of an Entity](https://backstage.io/docs/features/software-catalog/life-of-an-entity/)
- [pluggy](https://pluggy.readthedocs.io/) and [stevedore patterns](https://docs.openstack.org/stevedore/latest/user/patterns_loading.html)
- [FastAPI OpenAPI Schema Generation](https://deepwiki.com/fastapi/fastapi/2.5-openapi-schema-generation)

**Hot reload + lifecycle**:
- [importlib.reload thread-unsafety — CPython #126548](https://github.com/python/cpython/issues/126548)
- [Uvicorn server behavior](https://www.uvicorn.org/server-behavior/)
- [KrakenD Hot Reload (cautionary)](https://www.krakend.io/docs/developer/hot-reload/)
- [NestJS Lifecycle Events](https://docs.nestjs.com/fundamentals/lifecycle-events) and [Issue #14773 (topo ordering)](https://github.com/nestjs/nest/issues/14773)
- [aiorwlock](https://pypi.org/project/aiorwlock/), [aiojobs](https://aiojobs.readthedocs.io/)
- [K8s Container Lifecycle Hooks](https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/)

**Security**:
- [CVE-2023-37271 — RestrictedPython generators](https://github.com/advisories/GHSA-wqc8-x2pr-7jqh)
- [CVE-2026-0863 — AttributeError.obj bypass](https://research.jfrog.com/post/achieving-remote-code-execution-on-n8n-via-sandbox-escape/)
- [CVE-2025-68668 — n8n Pyodide ctypes escape](https://www.cyera.com/research/n8scape-pyodide-sandbox-escape-9-9-critical-post-auth-rce-in-n8n-cve-2025-68668)
- [HackMD Python jailbreak cheatsheet](https://hackmd.io/@rqdaA/S145ZYeYT)
- [VSCode Workspace Trust](https://code.visualstudio.com/docs/editing/workspaces/workspace-trust)
- [npm lockfile poisoning](https://medium.com/node-js-cybersecurity/lockfile-poisoning-and-how-hashes-verify-integrity-in-node-js-lockfiles-0f105a6a18cd)
- [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
- [NIST 800-53 SI-7](https://csf.tools/reference/nist-sp-800-53/r5/si/si-7/) and [SI-7(15)](https://csf.tools/reference/nist-sp-800-53/r5/si/si-7/si-7-1/)
- [AgentSpec ICSE 2026](https://cposkitt.github.io/files/publications/agentspec_llm_enforcement_icse26.pdf)

#### Reconsiderations Surfaced — Action Items for User Decision

These are NOT new questions; they are research-driven flags to revisit before `/specify`. Each maps to one or more existing D-IDs.

| # | D-ID(s) | Action |
|---|---------|--------|
| 1 | D-343 | Fold `triggers` into description as "Use when [X]" clauses (zero-precedent for separate field, accuracy data favors well-written description) — OR keep as authoring-only frontmatter not surfaced to manifest |
| 2 | D-341 | Refine: tools/skills last-wins (override intent); hooks fan-out (multiple subscribers valid); background tasks last-wins (drain-then-replace) |
| 3 | D-353 | Add bypasses to `_BLOCKED_ATTRIBUTES`/`_BLOCKED_CALLS`: `gi_code`, `gi_yieldfrom`, `tb_frame`, `AttributeError.obj`/`.name`, `__format__`, `__init_subclass__`, metaclass `__getitem__`, descriptor protocol; AND add OS-level sandbox layer (seccomp/sandbox-exec) for enterprise tier |
| 4 | D-359 | Pin trust TOML location to `~/.arc/security/trust.toml` (outside agent's writable workspace) to prevent agent-self-approval attack |
| 5 | D-367 | Add 5th bus event `capability:setup_failed` (distinct from `registration_failed`) |

If any of these are agreed, they get logged as deepening updates with the same D-ID by reference (per the SKILL.md ID convention — never re-allocate). If declined, the rationale gets logged as a "## Resolutions from /deepen" block following the precedent at the end of the nlit-cora-agent demo section.

#### Resolutions from /deepen (2026-04-28)

User-confirmed updates from the five surfaced reconsiderations. References existing D-IDs by reference (no new IDs allocated, per ID convention).

| # | D-ID | Original | Updated to |
|---|------|----------|------------|
| 1 | D-343 | `triggers` as semantic-hint field surfaced separately in manifest | **Unchanged.** Keep as separate field. Rationale: research data is on small skill sets and naive routers; LLMs are better at integrating multiple labeled signals than the LangChain-era keyword matchers were. Manifest stays with `triggers` as concrete examples next to `description`. Reconsider only if production accuracy suffers. |
| 2 | D-341 | Last-wins on name collisions everywhere | **Refined.** Tools and skills: last-wins with audit (override is the user intent). Hooks: fan-out — all matching subscribers run in priority order (pluggy LIFO with `tryfirst`/`trylast` overrides on `@hook`). Background tasks: last-wins via drain-then-replace (cancel old → await → start new). Conflict UX rule split by capability kind in the loader. |
| 3 | D-353 | AST validator with current blocked imports/attrs/calls list | **Enhanced.** Two additions: (a) extend `_BLOCKED_ATTRIBUTES` with `gi_code`, `gi_yieldfrom`, `tb_frame`; extend `_BLOCKED_CALLS` with format-string `__format__` invocation pattern; add `__init_subclass__`, metaclass `__getitem__`, descriptor protocol side-channels (`__pos__`/`__neg__`/`__get__`/`__set__`); reject `AttributeError.obj`/`.name` access (Python 3.10+). (b) Add an OS-level sandbox layer for self-executing agent code at enterprise tier (seccomp on Linux, `sandbox-exec` on macOS) — AST gate alone is insufficient against `ctypes` FFI escapes (CVE-2025-68668). Federal already blocks all unsigned agent .py via D-354. |
| 4 | D-359 | TOFU trust persistence "in toml" — location unspecified | **Pinned.** Trust persistence lives in `arcagent.toml` itself (under `[security.validators] approved = [...]`), at agent root, outside `<agent_root>/workspace/`. Agent has no write access to agent-root files. Approval entries are written by the human user via the TOFU prompt (CLI / UI), not by the agent. Seeded from template at `arc agent build` time alongside the rest of `arcagent.toml`. Same model for global trust at `~/.arc/security/trust.toml` if cross-agent approvals are needed later. |
| 5 | D-367 | 4 capability lifecycle bus events | **Extended to 5.** Add `capability:setup_failed` distinct from `capability:registration_failed`. Final list: `capability:added`, `capability:removed`, `capability:replaced`, `capability:registration_failed` (parse/AST/frontmatter rejected), `capability:setup_failed` (registered successfully but `@capability` class `setup()` raised). Consumers can distinguish "never loaded" from "loaded but broke at init." |

**Frontmatter for D-343 update**: `triggers` stays in required frontmatter list, surfaced in manifest XML alongside `description`. No change to skill format.

**Implementation impact for D-341 update**:
- Conflict resolution branches by decorator kind in `CapabilityLoader.register()`
- Hooks use pluggy-style fan-out semantics; tool/skill use last-wins; background_task uses drain-then-replace
- Audit event includes the kind: `tool.shadowed_by` vs `hook.subscribed_alongside` vs `background_task.replaced_via_drain`

**Implementation impact for D-353 update**:
- New blocked-attribute and blocked-call entries added to `arcagent/tools/_dynamic_loader.py`
- New OS-sandbox wrapper module — likely `arcagent/core/os_sandbox.py` — invoked from the loader before executing agent-authored code at enterprise tier
- Optional dependency for federal/enterprise `pip install arcagent[federal]` / `[enterprise]` extras

**Implementation impact for D-359 update**:
- New TOML schema fields under existing `arcagent.toml` `[security]` block:
  - `[security.validators] auto_run_agent_code = bool` (personal toggle)
  - `[[security.validators.approved]]` entries with `name`, `hash`, `approver`, `timestamp`
- `arc agent build` template includes the empty `[security.validators]` block so users see it on day one
- Approval-write path: human-only via CLI/UI (`arc trust approve <hash>`); agent has no `write` permission on agent-root files (confirmed by D-340 workspace boundary)

**Implementation impact for D-367 update**:
- `CapabilityLoader.register()` emits `capability:registration_failed` on AST/frontmatter rejection
- `CapabilityLoader._call_setup()` emits `capability:setup_failed` with the exception captured
- ui_reporter module subscribes to all 5 to surface in arcui dashboard

These resolutions complete `/deepen` for unified-capability-system. Ready for `/specify`.

---

## NLIT SCAP Demo — Build Decisions (2026-05-04)

**Phase**: build | **Status**: complete | **Total decisions**: 15 (3 user, 12 auto-applied)
**Priority framework**: simplicity > modularity > security > scalability
**Source**: `/Users/joshschultz/Desktop/arc-openscap-nlit-demo.md` (doubles as brainstorm — already covers WHY, audience, use cases, outcomes, principles, build plan)
**Feature scope**: SCAP extension at `~/.arc/capabilities/scap/` + skill at `~/.arc/skills/scap/`. 6 read-only tools. Real federal STIG scan data, hostnames rebranded. 9-min, 5-act NLIT pitch demo. ~13 hr build budget.

### Summary

SCAP extension wraps OpenSCAP / SCC / STIG Viewer outputs into a queryable model the agent reasons over for ATO evidence assembly, baseline gap analysis, drift detection, and MITRE ATT&CK threat correlation. All 6 tools are `read_only` for the demo (remediation deferred). Dev-mode install (files dropped directly into `~/.arc/capabilities/scap/`) — bundle/signing/marketplace deferred to post-NLIT per source doc §11. Real data from 4 hosts (Palo NDM, NX-OS NDM, RHEL workstation, Windows Server 2019), sanitized at ingest with persistent reviewable mapping. WeasyPrint renders ATO control narratives so they look like real federal documents. Drift artifact for Act 4 produced by a programmatic generator script — reproducible, auditable, runs once.

### User Decisions

| # | Decision | Choice | Priority | Rationale | Tier Notes |
|---|----------|--------|----------|-----------|------------|
| D-369 | PDF rendering library for ATO narrative + POA&M | WeasyPrint (HTML/CSS → PDF) | simplicity (visual) > modularity | Output must look like a real federal ATO document for the pitch to land. macOS + brew handles cairo/pango deps; backup laptop pre-installed for risk mitigation. ReportLab rejected for visual gap. | Same all tiers (rendering is offline). |
| D-370 | Hostname/IP/MAC sanitization mapping persistence | Persisted TOML at `~/.arc/capabilities/scap/data/sanitize_map.toml` | security > simplicity | Federal-grade answer to "how do we trust the rebranding" (source doc §9): file on disk + audit-event emission on first ingest. Reproducible across runs. Auditor can eyeball mapping. | Federal: mapping write also signed via `SignedChainSink`. Enterprise/Personal: JSONL audit only. |
| D-371 | T-30 drift artifact production for Act 4 | Programmatic drift generator script | modularity > simplicity | Reproducible, auditable, lives in repo. Source doc §4 framing ("real data + small fake delta") preserved — script flips ~5–10 sshd-hardening rules pass→fail and adjusts timestamps deterministically. Hand-edit rejected for re-run reproducibility. | Same all tiers (build-time artifact, not runtime). |

### Auto-Applied (Convention / Federal Mandate / Source Doc)

| # | Decision | Choice | Source |
|---|----------|--------|--------|
| D-372 | Extension architecture & install location | `~/.arc/capabilities/scap/` (global discovery root, precedence #2) | Source doc §2; arcagent capability precedence list |
| D-373 | Tool declaration | `@tool` decorator from `arcagent.tools._decorator` with frozen `ToolMetadata` | Convention — every built-in uses this; AI-authored tools never write JSON Schema by hand |
| D-374 | Tool return shape | Plain strings (or JSON-serializable strings) — no Pydantic models exposed to LLM | Convention — `builtins/read.py`, `find.py` return `str`; errors as `"Error: ..."` strings |
| D-375 | Tool classification | All 6 tools `read_only`; remediation/tailoring/live-scan deferred | Source doc §3, deliberate scope cut |
| D-376 | Audit emission per tool call | Framework `audit_event()` on every invocation, sensitive-key redaction on | `auto-applied: federal-mandate` (NIST 800-53 AU-2, AU-9) + Arc four-pillars (CLAUDE.md ADR-019) |
| D-377 | Sanitization determinism | Code-driven, deterministic — `sanitize.py` runs once at ingest, mapping reviewable | Source doc §4, §9 honesty-gap answer |
| D-378 | In-memory ingest cache lifetime | Module-level dict in `capabilities/scap/`, agent-runtime scoped (lost on restart, fine for 9-min demo) | Convention — matches `_runtime` pattern in builtins |
| D-379 | Packaging for demo | Skip — dev-mode install (files copied directly), no bundle, no signing, no `arc ext install` | Source doc §10, §11 (post-NLIT roadmap) |
| D-380 | ATT&CK → 800-53 mapping source | CTID (Center for Threat-Informed Defense) published JSON, bundled in `data/attack_to_800_53.json` | Source doc §5; NIST IR 8473 references CTID as canonical implementation |
| D-381 | POA&M output format | CSV with columns: control, finding, milestones, owner, remediation language, due date | Source doc §3, §6 Act 3 |
| D-382 | 800-53 catalog source | NIST OSCAL JSON Rev 5 bundled in `data/nist_800_53_rev5.json` | Source doc §5 |
| D-383 | FedRAMP baseline membership source | Official FedRAMP Low/Mod/High definitions bundled in `data/fedramp_baselines.json` | Source doc §5 |

### Implementation Impact

- **`~/.arc/capabilities/scap/sanitize.py`** owns deterministic redaction; emits `sanitize.mapping_written` audit event on first ingest; writes `data/sanitize_map.toml` with per-host `original → demo.local` mapping.
- **WeasyPrint dependency** added to extension's import surface — `pip install weasyprint` in dev environment; brew prereqs documented in extension `README` (post-demo bundle stage).
- **`scripts/synth_drift.py`** (one-off, repo-resident) reads `stig-wkstn01.ipa.local.xml`, flips configured sshd rules, adjusts XCCDF `<test-result>` start/end times back ~30 days, writes `linux-ws-01.t-30.xml`. Deterministic — same input → same output.
- **Ingest cache** is a module-level `_INGESTS: dict[str, IngestResult]` keyed by `host_alias`. `scap_query`, `scap_crosswalk`, `scap_baseline_compare`, `scap_attack_correlate`, `scap_evidence_pack` all read from it.
- **Tier policy** for demo: dev-mode install bypasses bundle verification entirely (per D-379). Production deployment (post-NLIT) inherits D-372 + signing pipeline from §11.

### Out-of-Scope (Confirmed Deferred)

- Live `scap_run` (invoking openscap-scanner / scc binaries on the host)
- `scap_remediate` (state-modifying — needs sandbox + policy gate + approval flow)
- `scap_tailor` (XCCDF profile authoring)
- Bundle distribution: `arc ext install scap-1.0.0.tgz`, Sigstore sidecar, TOFU approval (post-NLIT, source doc §11)
- "Act 0" install flow in the pitch (source doc §11.5 — only after distribution lands)

Ready for `/specify`.

---

## tool_choice passthrough — review follow-ups (2026-06-18)

Source: `/review` of `feat/restore-tool-choice-passthrough` (merged to main `2dbaba8`, develop `b3250aa`). The passthrough itself landed clean (PASS); the items below are **inherited** debt the review surfaced in arcrun/arcllm. Deliberately **not** fixed in the arcagent PR — the fixes cross package boundaries (arcllm owns the concept), and folding them in would violate "don't mix modules/packages." Tracked here for a dedicated arcllm-owned change.

| # | Decision | Choice | Priority | Rationale | Tier Notes |
|---|----------|--------|----------|-----------|------------|
| D-384 | Canonical `tool_choice` type ownership | Export `ToolChoice = Literal["auto","none","required"] \| dict[str, Any] \| None` from `arcllm/types.py`; promote to a typed keyword-only param on `LLMProvider.invoke` (parity with `ResponseFormat`); arcrun (`state.py`, `streams.py`, `loop.py`) and arcagent (`core/agent.py`, `core/agent_dispatch.py`) `import` it instead of redefining `dict[str, Any]` per layer | modularity > simplicity | arcllm owns the concept (defines values, translates `"required"`→`"any"` for Mistral) but exports no type, so 6 sites hand-redefined it too narrowly — excludes the string forms OpenAI/Mistral accept and arcllm's own tests exercise (`test_mistral.py:131-162`); `mypy --strict` rejects a legitimate string caller at the agent surface. Architect recommends an ADR. | Same all tiers (type contract, not posture). |
| D-385 | `tool_choice` input validation at the loop boundary | Validate once where it enters the arcrun loop: reject empty `{}` and malformed shapes with a clear `ValueError` + audit warn before the provider call; normalize the no-tools case to one behavior across adapters (recommend: drop + warn, matching Anthropic) | security > simplicity | Today empty `{}` passes the `is not None` guard and 400s at the provider; Anthropic silently drops on no-tools while OpenAI sends unconditionally → inconsistent. Conflicts with CLAUDE.md "validate all inputs" / LLM05. Validate at the single owning boundary, not per-layer. | Federal: validation failure is an audit event. |
| D-386 | Audit the turn-0 force-pin | Add `forced_tool_choice` to the `turn.start` payload in `arcrun/strategies/react.py` when `turn_count == 0 and state.tool_choice is not None` (~2 lines) | security (audit) > simplicity | A forced first-turn tool call is an authority-shaping decision invisible in the current audit trail — NIST AU gap; post-hoc review can't distinguish model-elected vs orchestrator-forced first calls. Closes at the single emission point. | Federal: also captured in `SignedChainSink`. |

### Ownership

All three are **arcllm/arcrun-owned** (D-384 originates in arcllm; D-385/D-386 in arcrun). The arcagent surface (`run`/`run_collected`/`dispatch_stream`) only forwards the value and is correct as-is. See memory `project_arcllm_owns_wire_types`.

---

## Provider Prompt Caching (arcllm/arcrun/arcagent) — Build Decisions (2026-07-01)

**Phase**: build/deepen | **Status**: IMPLEMENTED (SPEC-029, all gates green) | **Total decisions**: 11
**Priority framework**: simplicity > modularity > security > scalability (principled-coder)
**Source**: caching audit + 5-agent deepen research. Enriched doc: `.claude/specs/llm-prompt-caching/DEEPENED.md`

### Summary

arcrun's loop is cache-correct by construction (append-only, stable system prompt, deterministic
tool + parallel-result ordering), but arcllm emits no cache directive so Anthropic caching is OFF
and OpenAI/Gemini/Ollama caching is implicit + unmeasured. Fixes split cleanly by package:
arcllm sets breakpoints + reads telemetry; arcrun guarantees an immutable ordered tool set;
arcagent owns compaction. A live per-turn cache-buster was found in arcagent's `ContextManager`
(sliding-window prune every turn >70%).

### Decisions

| # | Decision | Choice | Priority | Rationale / Tier Notes |
|---|----------|--------|----------|------------------------|
| D-387 | Anthropic cache_control placement | Auto-place ≤3 breakpoints (last tool, system-as-block, rolling tail message) inside `AnthropicAdapter._build_request_body`, behind one config flag `enable_prompt_caching` (default on). NO type-system change; system string → 1-block list only when caching on. | modularity > simplicity | Keeps `cache_control` (Anthropic wire specific) inside the adapter; arcrun/arcagent stay provider-agnostic. Cascade + 4-breakpoint cap → ≤3 fixed is correct. Read side already wired (`_parse_usage` 156-165). |
| D-388 | Cache TTL default | 5-minute ephemeral default; 1-hour opt-in via config. | security > scalability | Smaller exfil window (LLM07) + half the write cost (1.25× vs 2×). Ceiling: agents whose turn cadence >5min re-pay writes — expose ttl for long-lived agents. |
| D-389 | OpenAI/Gemini cache telemetry read-back | Map `usage.prompt_tokens_details.cached_tokens` → `Usage.cache_read_tokens` in `OpenaiAdapter._parse_usage`; mirror in SSE parser. Keep `google.py` inheriting `openai.py` (Gemini compat returns identical field). Leave `cache_write_tokens=None`. | simplicity > modularity | ~3 lines/site; billing (`telemetry_cost.py` 28-31) already consumes it. Forking Gemini parsing = boundary violation, zero payoff. |
| D-390 | prompt_cache_key / cachedContent | Do NOT adopt (YAGNI). | scalability | A shared cache key overflows OpenAI's ~15 req/min-per-prefix ceiling at fleet scale and *reduces* hit rate; `cachedContent` is a heavyweight managed resource. Defer until a measured miss problem. |
| D-391 | Tool-set stability owner | arcrun owns an **immutable, deterministically-ordered per-run tool set**, NOT caching. Freeze `ToolRegistry` after construction (reject add/remove on the per-run object); memoize `list_schemas()`. | modularity > security | Resolves "don't mix concerns": cache hit is emergent at arcllm boundary. Freezing also closes ASI04/LLM06 mid-run tool-injection surface. |
| D-392 | Dynamic capability mid-run | Surface new tools/skills via deferred menu meta-tool (name only, body into message tail) OR subagent (own registry + cache). Do NOT append to the live tools block as primary mechanism. | scalability > security | `use_skill` already does this. Deferred loading is O(1) in the tools block — the only pattern with no ceiling. Append-only grows the block unboundedly + forces a write. |
| D-393 | Cache breakpoint location | Breakpoint is set in arcllm (`_build_request_body`), never in arcrun. arcrun's contract to arcllm is "stable ordered list," nothing more. | modularity | No caching concept leaks into the loop nucleus. |
| D-394 | transform_context contract (arcrun side) | Document append-only contract on the seam (loop.py/streams.py/react.py/state.py docstrings); optional debug-flag-gated assertion returned-prefix==input-prefix. Never in the default hot path. | modularity > simplicity | Invariant's owner is the caller (arcagent). arcrun documents + optionally asserts; does not enforce caching semantics in the loop (<500ms cold start, no per-turn O(context) diff). |
| D-395 | Ollama model residency | Warm KV cache via server env `OLLAMA_KEEP_ALIVE` (finite 30m–24h on shared boxes; `-1` only dedicated single-model hosts). Zero arcllm code. | simplicity > scalability | `keep_alive` is silently ignored on the OpenAI-compat `/v1` path (Ollama #11458); no body passthrough exists. `-1` disables idle eviction → OOM/503 risk on multi-model boxes. Native `/api/chat` override only if per-request control becomes a hard requirement. |
| D-396 | Compaction model (arcagent) | Replace per-turn sliding-window prune with **discrete, persisted, debounced checkpoint compaction**: append-only between boundaries; on threshold cross, compact ONCE, write back a new baseline (`[system][tools][summary][protected tail]`), compact deep (~50%) for hysteresis; emergency truncation stays as rare valve. Trigger off reported tokens where available. **Compaction METHOD (truncate vs summary turn vs structured extraction vs hybrid) is OPEN — under web research.** | scalability > simplicity | Current `ContextManager.transform_context` (context.py 209-258) rewrites a sliding prefix every turn >70% → busts cache every turn on long-running agents (O(new)→O(full) re-encode). Discrete + persisted + debounced = one cache miss per boundary, then re-warm. |
| D-397 | Doc correction | Fix CLAUDE.md structure diagram: `core/context_manager.py` → `core/session_internal/context.py` (class `ContextManager`; logger still `arcagent.context_manager`). | simplicity | File named in the standards doc does not exist; stale pointer. |

### Open question (blocking D-396 method choice)

What IS compaction — truncation, an LLM summary turn, structured detail-extraction to bullet
fields, or a hybrid? Under research (Anthropic guidance / production teams / academic methods).
Decision to be recorded once research synthesizes.

### `/specify` scoping

- **SPEC A — arcllm caching**: D-387..D-390, D-393, D-395-doc. Self-contained, highest ROI.
- **SPEC B — arcrun tool immutability**: D-391, D-392, D-394 (arcrun half).
- **SPEC C — arcagent compaction**: D-396, D-397 (+ arcrun D-394 contract). Blocked on method research.

Dependency: SPEC A Anthropic *tool-block* cache hits need SPEC B stable ordering (system/message hits land regardless).

### D-396 Resolution — Compaction Method (2026-07-01)

**Research**: 3 web agents (Anthropic guidance / production teams / academic + benchmarks). Convergent.
**Key evidence**: JetBrains "Complexity Trap" (arXiv:2508.21433) — masking ≈/> LLM summarization at
~half cost; summarization causes 15% trajectory elongation by smoothing over "stuck" signals.
Factory.ai (36,611 prod msgs) — structured schema-anchored extraction 3.70 > generic Anthropic 3.44 /
OpenAI 3.35; "structure forces preservation." "Less Context, Better Agents" (arXiv:2606.10209) —
pruning+structured running summary **91.6%** vs pruning-only 79% vs full-context 71%, at flat token
cost; recency window plateaus ~N=5 tool calls, summary window >3 adds nothing. Manus — append-only +
file offload ("restorable compression"); KV-cache hit rate is THE production metric (100:1 in:out).
Recursive summarization = highest drift risk (arXiv:2602.09789 knowledge-overwriting/semantic-drift) —
avoid as default.

| # | Decision | Choice | Priority | Rationale |
|---|----------|--------|----------|-----------|
| D-398 | Compaction method | **Hybrid: observation-masking + structured running summary, applied TOGETHER at discrete debounced boundaries, persisted (write-back), append-only between boundaries.** Not continuous, not freeform prose, not recursive. | scalability > simplicity | Masking does cheap bulk token reduction; structured summary supplies cumulative task-awareness masking alone loses (premature-termination failure in the D365 study). Doing both at a boundary keeps the loop fully append-only between boundaries → one cache miss per boundary, not per turn. |
| D-399 | Summary structure | Schema-anchored fields, single-shot regenerate (or delta-merge), NOT prose: `goal` (verbatim, never overwritten), `constraints` (security/user, verbatim), `progress` (quantified), `key_facts[]` (w/ provenance), `files_modified[]`, `decisions[]` (w/ rationale), `rejected_approaches[]`, `open_questions[]`, `next_step`. | simplicity > scalability | "Structure forces preservation" (Factory). The two most-omitted-yet-critical fields per the research: `rejected_approaches` (what masking/prose loses → repeated dead ends) and quantified `progress` (fixes pruning's premature termination). Verbatim goal + constraints = Claude Code's hard-preserve rule (LLM07/ASI01). |
| D-400 | Observation masking policy | Keep the existing `prune_observations` mechanism but fire it **at the compaction boundary and persist the masked result** (write-back), NOT as a per-turn sliding view. Mask tool outputs older than a fixed recency window (~N=5–10 tool calls); keep tool-call name/args visible. | scalability | This is the direct fix to the current per-turn cache-buster (context.py 209-258). Persisting + batching at a boundary makes the masked prefix stable and cacheable; keeping call metadata preserves provenance (JetBrains finding). |
| D-401 | Large/durable content offload | Large tool outputs and any cross-session state go to workspace files / memory (restorable compression); keep only pointers (path/id) in live context. Route "must survive across resets" through the memory/file layer, never through in-context summarization. | simplicity > scalability | Manus restorable-vs-irreversible compression; Anthropic's own long-running-agent study: compaction alone is insufficient, durable file artifacts carry correctness. Cache-neutral (append pointers, never mutate prefix). |
| D-402 | Emergency truncation | Keep `_emergency_truncate` as a rare last-resort floor only (hard ceiling breach), also a discrete persisted boundary event. Never the primary mechanism. | security (availability) > simplicity | Truncation is semantically blind; acceptable only to prevent a crash after masking+summary+offload have run. |

**Trigger/hysteresis (confirms D-396 shape):** trigger off provider-reported tokens where available
(`token_ratio()`), estimate as pre-call guard. Compact **deep** (~50%) so many append-only turns follow
before the next boundary — avoids threshold hover/thrash. Single-shot summary per boundary (no recursion);
if incremental, merge only the newly-evicted delta into the persisted summary (MemGPT/Factory pattern),
never regenerate-from-scratch every round.

**Layering (matches Anthropic's stack):** append-only (default) → mask+offload at boundary (cheap,
restorable) → structured summary at boundary (lossy but schema-guarded) → emergency truncate (floor).
Retrieval/memory offload is orthogonal (cross-session), not an in-loop compaction lever.

D-396 status: **RESOLVED** → see D-398..D-402. Method no longer blocking SPEC C.

### SPEC-029 /review follow-ups (2026-07-01) — applied

Swarm review (security/clean-code/architecture/QA) found 2 BLOCKING correctness bugs — both
pre-existing latent, both exposed/worsened by SPEC-029 making transform_context identity. Fixed:

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D-404 | Compaction summary must reassemble | `compaction_summary` entry now carries `role="user"` + rendered `content` (type/counts are ignored-extra keys). | `agent_dispatch` builds history via `Message(**record)`; the old entry had no role/content → pydantic ValidationError on the first dispatch after ANY compaction. Was never hit only because the trigger was dead (D-405). |
| D-405 | Compaction trigger = estimate, not reported tokens | `maybe_compact` uses `session.context_ratio()` = `ContextManager.message_fill_ratio(live messages)`. | `update_reported_usage` has zero production callers → `token_ratio()` always 0.0 → compaction never fired. The reported accumulator is also cumulative-and-never-reset (would thrash). The estimate over current messages is the honest signal and drops after a boundary (natural debounce). `token_ratio`/`update_reported_usage` kept as a telemetry surface. |
| D-406 | In-band summary sanitized | `summary_text` routed through `_sanitize_context_output` before it re-enters the baseline (symmetry with the context.md flush). | ASI-06/LLM-01: prevents injection laundering into the persisted session baseline. |
| D-407 | Emergency valve keeps newest | `_emergency_truncate` always keeps >=1 (the newest) message. | Prior loop returned `[]` when a single newest message exceeded budget → zero messages to provider. |

Also: fail-open flush now logs `exc_info=True`; compaction audit emitted inside the lock; dead
`usage_ratio` removed; stale docstrings (context.py, manager.py) corrected; `context_manager`
typed `ContextManager | None` (removed `Any`-laundering). Regression tests added:
reassembly-after-compaction, estimate-trigger, emergency-keeps-newest. All gates green.

---

## Ongoing Daily Notes (arcagent memory) — Build Decisions (2026-07-02)

**Phase**: build | **Status**: pending (SPEC-030) | **Priority**: simplicity > modularity > security > scalability
**Source**: SPEC-029 review found `agent:pre_compaction` orphaned; 2-agent web research on memory cadence.
Spec: `.claude/specs/SPEC-030-ongoing-daily-notes/`

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D-408 | Notes trigger | Decouple from compaction entirely; remove `memory_pre_compaction`. | MemGPT coupled memory to context pressure; Letta sleep-time compute exists to undo exactly that. Memory quality must not be hostage to token pressure. |
| D-409 | Capture cadence | Per-turn RAW append, no LLM, on `post_respond` (before background extract). | "ASSUME INTERRUPTION" (Anthropic memory tool) — sessions don't end cleanly; cheap append is the crash-safety floor. LLM-per-turn is the rejected anti-pattern (LangMem hot-path). |
| D-410 | Enrichment | Keep existing `entity_extraction_loop` (interval background). | Off-critical-path LLM work = Letta sleep-time latency isolation; already present. |
| D-411 | Session consolidation | One eval-model dedupe/tidy pass on `agent:shutdown`, fail-open. | The guaranteed floor for one-shot runs (Reflexion per-episode; Anthropic harness update-at-session-end). |
| D-412 | Day rollup | LAZY on new-day file creation → `spawn_background(rollup(prev))`, `.rolled` marker for idempotency. | Dual-mode (daemon crossing midnight OR next-day one-shot) with no live scheduler — the gap the research flagged as unsolved. Generative Agents daily reflection + practitioner nightly rollup. |
| D-413 | Deferred | No importance-scoring, no wall-clock heartbeat (v1). | YAGNI: session-end + new-day boundaries suffice; 1.0s loop bounds intra-turn loss. |

All tiers config-toggleable under `[memory]`; consolidation output sanitized (ASI-06); module-only (no arcrun/arcllm).

### SPEC-030 /review follow-ups (2026-07-02) — applied

4-reviewer swarm (security/clean-code/architecture/QA). Security PASS, architecture PASS on the
invariant, clean-code FAIL (DRY + clocks). No critical/high; a consensus correctness+hardening
cluster fixed before merge.

| # | Decision | Choice |
|---|----------|--------|
| D-414 | Sanitizer: one shared, hardened impl | Converge the 4th inline copy onto `utils/sanitizer.py:sanitize_text`; harden IT to strip Unicode Tag chars (U+E0000–E007F), variation selectors, soft hyphen, U+180E, DEL — fixes invisible-instruction smuggling (M2) across every caller incl. SPEC-029's compaction flush. Add `truncation_suffix`. |
| D-415 | Drop `rollup_compacts_source` | Never rewrite the raw source day. Removes the crash-retry summary-of-summary ordering hazard AND the identical yesterday double-count AND a YAGNI flag in one deletion. |
| D-416 | Long-term recall correctness | `_longterm.md` truncation keeps the NEWEST sections (was `text[:max]` = oldest); exclude today/yesterday sections from Long-term injection (already shown as ### Today/### Yesterday). 3-way-consensus bug. |
| D-417 | Drain the full backlog | `_unrolled_prior_days` rolls ALL un-rolled prior days (oldest-first), not just the newest — no starvation. |
| D-418 | Harden + audit | Sanitize Tier-1 raw excerpts + fail-open; neutralize markdown headings in rollup body (anti section-spoofing); UTC clock throughout; audit events on lossy consolidation/rollup rewrites. |

Doc corrections (code was more correct than spec): config lives in `modules/memory/config.py`;
dropped `hook_active` guard (no-op for direct writes). Final: ruff 0, mypy --strict 0, arcagent 3312 passed.

## ArcLLM Gateway Hardening — SPEC-015/016/017 (2026-07-05)

Three opt-in arcllm transport modules (informed by the gavio gateway comparison), built federal-first (NIST 800-53 + OWASP LLM/ASI). Full rationale in each spec's `packages/arcllm/.claude/specs/<id>/README.md`. Shipped to `main`: ruff 0, mypy --strict 0, 1289 tests, 100% coverage on new modules.

### SPEC-015 Content Guardrails (D-419–D-434)

| ID | Decision | Rationale |
|----|----------|-----------|
| D-419 | InjectionModule ships OFF by default, opt-in per call | Heuristic scanning can false-positive; flagging user intent is a policy concern the agent owns; keeps default path zero-cost. |
| D-420 | Pattern-corpus tier is the zero-dep default; semantic tier is `arcllm[injection-semantic]` | Regex/substring corpus catches common attacks dep-free; embedding-cosine detection belongs behind an extra. |
| D-421 | InjectionModule flags/blocks only — never interprets, rewrites, or executes content | Respects the transport boundary; semantic "is this an attack" judgement lives in arcagent/arcrun. |
| D-422 | Scans INBOUND user + tool-result content pre-provider, pre-redaction | Tool results = ASI06 vector, user turns = LLM01; both untrusted-adjacent, scanned while text is original. |
| D-423 | Secret scanner folded in as a togglable `SECRETS` category → `[SECRET:TYPE]` | Secrets are a leak class, not a subsystem; reuses the detect→redact path (D-094 tag format). |
| D-424 | Checksum validators (Luhn / mod-97 / ABA) gate entity matches | Raw regexes false-positive on order numbers/IDs; cheap arithmetic validator cuts noise dep-free. |
| D-425 | Add gov/CUI entities: US_PASSPORT, US_DRIVERS_LICENSE, DOD_ID/EDIPI, CAC, BANK_ACCOUNT, DOB, MRN, IPV6 | Federal deployments handle CUI beyond SSN/CC/email (SI-10 / SC-28). |
| D-426 | Entity categories individually toggleable via `pii_entities` allow/deny | Feature toggles via config, not code branches. |
| D-427 | Implement the allowlisted `pii_detector_class` loader spec-012 D-093/FR-13 specced but never built, mirroring `vault.py` | Removes the `_VALID_DETECTORS` hard-reject; makes bring-your-own spaCy/Presidio real with the same ASI04 allowlist guard. |
| D-428 | GuardrailsModule validates the RESPONSE per call via a `guardrails={...}` kwarg | Structural output validation is a transport concern once bytes are in hand; per-call kwarg mirrors routing's `classification`. |
| D-429 | Semantic guardrails (grounding, correctness, toxicity) OUT OF SCOPE | Require model reasoning + agent context → arcagent/arcrun. arcllm validates structure, not meaning. |
| D-430 | Stack: Injection above Security (sees original text); Guardrails just inside Audit (validates final resolved response) | Injection needs pre-redaction text; Guardrails must run post-Retry/Fallback and be recorded by Audit. |
| D-431 | New `ArcLLMInjectionError`, `ArcLLMGuardrailError` (subclass `ArcLLMError`) | Distinct types let callers catch block-mode failures precisely without string-matching. |
| D-432 | Config: new `[modules.injection]`, `[modules.guardrails]`; extend `[modules.security]` with `pii_entities` + `pii_detector_class` | Each module owns its TOML section; security enrichment stays in the security section. |
| D-433 | New optional extra `arcllm[injection-semantic]` | Semantic deps must never load when injection is pattern-only or disabled — zero-dep-when-disabled. |
| D-434 | Both modules use `enforcement="block"\|"warn"` (warn = flag + continue, block = raise) | Same vocabulary as RoutingModule; warn enables observe-only rollout before enforcing. |

### SPEC-016 Full Trace Capture & Replay (D-435–D-448)

| ID | Decision | Rationale |
|----|----------|-----------|
| D-435 | Flip default: `store_raw_bodies=True` — capture full request + response | Metadata-only can't answer "what was actually sent/received"; full forensic replay is a hard requirement (ASI10, LLM05/09). Compensating controls keep it federal-safe. |
| D-436 | Reuse existing `request_body`/`response_body` fields; no parallel `*_raw`, no linked record | Fields already exist; adding parallel ones duplicates; inline keeps one record = one atomically-hashed unit. |
| D-437 | Hash chain covers raw bodies + encryption envelope, no new integrity machinery | `compute_hash()` already digests every field but `record_hash`; raw capture is tamper-evident for free (AU-10). |
| D-438 | Federal tier: AES-256-GCM envelope, DEK wrapped by vault/KMS key; plaintext never on disk when on | Raw prompts/responses are an LLM02/LLM07 exfil target; envelope encryption (SC-28, AU-9) is the primary compensating control. |
| D-439 | Add per-record `classification` tag (default from config floor) | Classification-aware handling (SI-12); drives retention/access filtering; aligns with routing classification. |
| D-440 | Retention: `max_age_days` + `max_bytes` drive rotation + whole-file purge | Raw-by-default grows unbounded; AU-11/SI-12 require defined lifecycle; purge never rewrites live chain lines. |
| D-441 | Right-to-erasure DROPPED (no crypto-shred) | GDPR/CCPA feature that conflicts with federal audit immutability/retention (AU-9/10/11, Federal Records Act); not an enterprise need; was the sole source of the impossible per-record shred. Retention (D-440) covers lifecycle, encryption (D-438) confidentiality. |
| D-442 | Replay READ path (`load_for_replay`) in arcllm; EXECUTION in arcrun | arcllm reconstructs the request object only; re-invoking a model would put loop logic in the wrong package. |
| D-443 | Lineage token persisted VERBATIM, never constructed by arcllm | Lineage (template/RAG provenance) is built by arcrun/arcagent; arcllm has no visibility and must not fabricate it. |
| D-444 | Turning raw capture off emits an audited `config_change` record | Capture is ON by default; disabling it is a logged security-relevant action (AU-2) — no silent blinding. |
| D-445 | New optional extra `arcllm[trace-encryption]` (cryptography), helper in `_trace_crypto.py` | Zero crypto deps in core; only federal encryption pulls it. Mirrors `arcllm[signing]`. |
| D-446 | Tier behavior: personal plaintext chmod-locked; enterprise same (encryption recommended); federal encryption+classification+retention required | Stringency is metadata not a gate (ADR-019); every tier captures, hash-chains, chmod-locks, audits. |
| D-447 | Encryption-key resolution reuses `vault.py` VaultResolver (allowlisted, TTL cache, KMS-wrapped) | DRY; wrapping key fetched exactly like an API key; credentials never touch the filesystem. |
| D-448 | GCM AAD binds ciphertext to `trace_id` + `timestamp` | Anti-transplant: decryption fails if the record identity was altered; strengthens tamper-evidence beyond the SHA-256 chain. |

### SPEC-017 Load Balancing (D-449–D-458)

| ID | Decision | Rationale |
|----|----------|-----------|
| D-449 | Scope is intra-provider: spread across N endpoints/keys of the *same* provider | Distinct from FallbackModule (inter-provider) and RoutingModule (classification); raises aggregate throughput, never changes which provider/model answers. |
| D-450 | Default strategy: weighted round-robin | Deterministic, stateless-per-call, zero deps; weights bias larger replicas / higher-quota keys. |
| D-451 | Health-aware strategy skips endpoints via a per-endpoint circuit mechanism owned by the pool | Reuses circuit_breaker failure-count/cooldown per endpoint; a tripped replica is skipped until cooldown. |
| D-452 | Sticky routing (session/agent key) optional, off by default | Pins a caller for prompt-cache locality; trades even distribution for cache warmth — explicit operator choice. |
| D-453 | Pool topology `[[endpoints]]` in provider TOML; strategy in `[modules.load_balance]` | Endpoints are provider connection settings; behavior is module config. Mirrors D-098. |
| D-454 | Cursor + per-endpoint health in a shared per-pool registry guarded by `asyncio.Lock` | Mirrors rate_limit `_bucket_registry`; thousands of agents share one cursor/health view — no singleton bottleneck, no per-agent drift. |
| D-455 | Cross-AGENT scheduling / fairness / global prioritization OUT OF SCOPE | Loop/runtime concern → arcrun. arcllm balances one caller's invoke across endpoints, never between agents. |
| D-456 | Per-call opt-in via `load_model(..., load_balance=True\|{dict})` | Consistent with every optional module; single-endpoint behavior unchanged when absent. |
| D-457 | Each endpoint's key resolves through the same vault/env path as the base provider | No plaintext keys in TOML/pool state; health data holds counters/timestamps only, never message content. |
| D-458 | LB sits at the innermost stack position, holding a pool of endpoint adapters (Router-like) | Selection happens closest to the wire; RateLimit/Retry/CircuitBreaker wrap the pool; replaces the single adapter like RoutingModule. |

Post-review fix pass (20 findings, 2 HIGH / 5 MED / 13 LOW): spool bodies sealed once when encryption on (no plaintext CUI leak); full-text banned-content scan (closed length-cap bypass); added sk-ant-/sk-/AIza/xox secret patterns; trace-store hash+write offloaded to `asyncio.to_thread` (event-loop unblock at scale); tool_call args redacted; shared `resolve_enforcement` helper; honest `verify_chain` docs. OPEN: head-truncation/rollback tamper-evidence requires the arctrust `SignedChainSink` external anchor (cross-package, not arcllm's to implement).

---

---

---
## Deepening Summary

**Deepened on:** 2026-07-21
**Sections enhanced:** 16
**Solutions referenced:** 2
**Skills matched:** coding-workflow:langchain-patterns, coding-workflow:agent-delegation, coding-workflow:compound-docs, coding-workflow:auto-documentation, architecture-adr-generator, coding-workflow:python-patterns

### Key Findings
- CORRECTION to D-467/D-468 rationale: arcui's operator gate is ROLE-based on a shared bearer token (auth.py:189-239), not DID-based. There is no per-user DID on the arcui write path. 'arcui signs with the operator key' requires the server process to hold the operator private key — an unresolved design question, not a free reuse.
- CORRECTION to D-470 rationale: the skill list-to-drawer-to-diff-to-rollback pattern exists only on the BACKEND. skill_versions.py's diff/rollback endpoints are entirely unconsumed by the frontend, and no diff renderer exists anywhere in web/src. The Prompts tab builds the first one.
- Packaging trap: .md files will be silently dropped from the wheel unless each touched pyproject.toml declares artifacts = ["src/<pkg>/**/*.md"] — the exact lesson from the SPEC-047 blueprint TOML migration (arcagent/pyproject.toml:136).
- Byte-identity trap named: prompt.py:12 uses a backslash-continued opening quote to suppress a leading newline, and read_text() returns a .md file's trailing newline verbatim. This is precisely the failure D-469 exists to catch.
- CONTEXT_MAINTAINER_SYSTEM_PROMPT (the largest prompt at ~4.9 KB) has ZERO test coverage today. Tests must be written against the constant BEFORE it moves.
- Three open questions resolved: capability_registry.format_for_prompt() needs no slot mechanism (it emits pure XML with no authored preamble; the prose wrapper _SKILL_USAGE_INSTRUCTION is already a separate constant on a separate bus subscriber); all four arcskill improver prompts are cleanly externalizable (runtime data is pre-rendered to flat strings before the f-string); _DEFAULT_PREFIX is confirmed dead with only two hits in all of src/, both its own definition.
- Every commercial prompt-management system surveyed (Langfuse, LangSmith, Humanloop, Agenta) auto-derives version identity rather than trusting a hand-typed field — independent convergence on D-462's reasoning.
- tier = "personal" is hardcoded in EIGHT places in the arc agent create scaffold, so D-467's sign-at-every-tier lands signing friction on every scaffolded agent by default.

### New Risks Discovered
- ~~Overlay staleness inverted: D-465 avoids staleness for un-overridden prompts, but an OVERRIDDEN prompt silently misses every upstream improvement with no 3-way merge available. This is the documented dpkg conffiles limitation (dpkg stores only a stock checksum, which is why ucf exists as a separate layer).~~ **ACCEPTED — see D-480.** An override is intended to be permanent; upstream prompt changes deliberately do not reach it. No stock-hash tracking, no merge machinery, no staleness UI.
- arcrun mounts identity.md/context.md read-only in the docker backend — writes may be invisible to a live agent until restart. Must be verified against D-461's pin-at-run-start assumption.
- No agent/session fixture exists that assembles a full system prompt end-to-end. arcagent/tests/conftest.py has exactly one fixture. The assembled-prompt harness must be built, not reused.
- Coverage gates (line >= 80%, branch >= 75%, fail_under=80) mean a thin new arcprompt loader with few tests can drag a package under threshold.
- A new leaf package needs its own hand-written tests/architecture/test_no_arcprompt_imports_*.py guard — leaf-ness is not generically enforced; all 11 architecture tests are hand-written AST scans with no central DAG table.
- decomposer.py:38 _PROTECTED_NAMES omits context.md where _validation.py:23 includes it — a latent inconsistency independent of this work, worth fixing under the repo's leave-it-correct standard.


## Editable System Prompts (arcprompt) — Build Decisions (2026-07-21)

**Phase**: build | **Status**: complete | **Total decisions**: 23 (20 user, 3 auto-applied)
**ID range**: D-459 to D-481
**Priority framework**: simplicity → modularity → security → scalability

### Summary
Externalize every system prompt across arc packages into per-package context/ markdown, loaded through arcprompt (a leaf package), overridable per agent via signed operator-written overlays, and viewable/editable in arcui. v1 is externalize + view/edit; evals and GEPA-style self-tuning are v2 on this spine.

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- _(none)_

**Edge Cases:**
- _(none)_

**Performance:**
- _(none)_

**References:**
- _(none)_

### Auto-Applied (Compliance Mandates)
| ID | Category | Decision | Mandated Answer | Citation |
|---|---|---|---|---|
| D-472 | Security | No secrets in prompt text | Prompt writes run the existing arcui secret scanner (_find_secret) before persisting; a detected credential rejects the write. Prompts are treated as exfiltrable. | OWASP LLM07 System Prompt Leakage; OWASP baseline (no declared compliance regime) |
| D-473 | Security | Input validation at trust boundary | Frontmatter parses through a Pydantic model at the arcprompt load boundary; unparseable frontmatter raises rather than degrading (see D-463). | OWASP baseline; CLAUDE.md "Pydantic models for all data boundaries" |
| D-474 | Security | Loaded artifacts verified before use | Overlay signature verification is mandatory at every tier, with no tier-conditional bypass flag. | CLAUDE.md Four Pillars — Sign (ADR-019); implemented by the overlay-signing decision in this section |

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- D-472's secret scanner is confirmed present and reusable: _find_secret (files_write.py:60-67) iterates arcllm._secrets.SECRET_PATTERNS then falls back to a keyword-anchored generic token regex, returning a secret_type label that becomes the audit detail.

**Edge Cases:**
- _(none)_

**Performance:**
- _(none)_

**References:**
- packages/arcui/src/arcui/routes/agent_detail/files_write.py:49-67

### Architecture

#### D-459: Prompt load model
**Decision**: arcprompt is a leaf package exposing load(pkg, name). Each package ships stock prompts as markdown at src/<pkg>/context/*.md. Resolution walks overlay then stock.
**Priority**: simplicity
**Alternatives**: central registry with import-time registration; arcstore DB-backed prompts
**Rationale**: Keeps prompts in git, so version control is inherited rather than built. Mirrors how SKILL.md already loads from disk. Leaf position is forced by the dependency DAG: arcrun, arcskill, arcagent and arcmemory all consume prompts, so arcprompt must sit at arctrust's level and never import upward. No DB dependency on a core prompt path.

#### D-460: Overlay scope
**Decision**: Two layers only: packaged stock, then a per-agent overlay. No fleet-wide layer.
**Priority**: simplicity
**Alternatives**: stock → fleet → agent (three layers); fleet-wide overlay only
**Rationale**: Shared-nothing per agent, consistent with the architecture's isolation model and with the cross-agent memory-bleed lesson. Resolution stays a single conditional. Cost accepted: a fleet-wide prompt change is N edits; revisit only if that friction proves real.

#### D-461: Reload semantics
**Decision**: Resolve and snapshot the prompt set at run start; the snapshot is fixed for every turn of that run. Edits take effect on the next run.
**Priority**: security
**Alternatives**: re-read per call like identity.md; explicit operator-triggered reload only
**Rationale**: Per-call re-reads let a prompt shift between turns of one run, so no single version is attributable as the cause of a behavior. Pinning makes every run attributable to exact prompt bytes, which is what makes the audit record meaningful. Mirrors the existing frozen-tool-registry-per-run precedent. Hot-reload is preserved from the operator's seat — no redeploy, just next run.

#### D-463: Resolution failure mode
**Decision**: Absent overlay resolves to stock silently (the normal path). Overlay present but unparseable, empty, or unsigned raises. Missing stock prompt raises as a packaging bug.
**Priority**: security
**Alternatives**: always fall back to stock with a warning; fail closed on any resolution gap including absent overlay
**Rationale**: Mirrors the configured-gate policy rule: unconfigured is a no-op, configured-but-broken is a hard failure. Silent fallback is the embedder silent-degrade shape — a warning nobody reads while the fleet runs the wrong prompts. An operator who wrote an overlay intended to change behavior and must learn immediately that they did not.

### Research Insights

**From Solutions Archive:**
- security-issues/2026-04-18-tier-must-flow-through-construction.md (SPEC-017) — DIRECT HIT on D-471. Audit trails recorded tier=personal in federal deployments because the enforcement path saw the real tier while the annotating context saw a hardcoded fallback. Enforcement was correct; the audit lied. arcprompt must take tier/posture at CONSTRUCTION, never resolve it per-call.
- security-issues/2026-04-18-ast-validator-is-not-enough-defense-in-depth.md — a single gate is not a control; corroborates pairing D-466 (placement) with D-467 (verification) rather than relying on either alone.

**Best Practices:**
- Version identity should be auto-derived, with a separate mutable POINTER expressing deployment intent. Langfuse uses auto-increment + protected labels; LangSmith uses commit hash + tags; both warn against pinning ':latest' in production.
- arcprompt's own README already reserves the package for exactly this purpose ('prompt content lives outside runtime code, versioned independently and swappable without recompiling') and names arcrun.prompts.get_strategy_prompts as the migration source — D-459 follows an existing intent rather than inventing one.
- No context/ directory exists anywhere in the repo, so D-459's layout collides with nothing.
- Follow blueprints/loader.py:51's Path(__file__).parent convention for locating packaged files — the repo's established pattern, not importlib.resources.

**Edge Cases:**
- Two-layer stock/overlay chains cannot 3-way-merge upstream changes into a locally-edited overlay — the documented dpkg conffiles limitation. Recording the stock hash an overlay forked from would let arcui surface 'stock changed since you overrode this'. RECOMMENDATION, not a decision change to D-465.
- Kustomize's strategic-merge-patch cannot delete array items and breaks silently when list items lack merge keys. D-460's whole-file replace semantics dodge this class entirely — worth stating explicitly so a future 'just patch a section' proposal is refused on purpose.
- Rails-style multi-file config cascades produce load-order-dependent precedence bugs; a further argument for D-460's two layers over three.

**Performance:**
- Moving prompt text out of arcrun/src reduces NCLOC (docstrings and triple-quoted constants count per check_loc_budgets.py:22), so the arcrun 5,400 foundation budget gets slack rather than pressure.
- A new arcprompt package carries no LOC ceiling (check_loc_budgets.py:62-64) — consider adding one for consistency with the foundation tier.

**References:**
- https://langfuse.com/docs/prompt-management/features/prompt-version-control
- https://raphaelhertzog.com/2010/09/21/debian-conffile-configuration-file-managed-by-dpkg/
- https://github.com/kubernetes-sigs/kustomize/issues/4596
- packages/arcagent/src/arcagent/blueprints/loader.py:51

### Data Model

#### D-462: Frontmatter schema
**Decision**: Authored fields: name, description, tunable. Version identity is derived — content sha256 computed at load, change history from git. No hand-maintained version field.
**Priority**: simplicity
**Alternatives**: name + description only; explicit version plus owner/tier/eval_suite
**Rationale**: A hand-bumped version drifts from the body the first time someone edits and forgets, and then the audit trail lies — the same failure as spec-status drift. A content hash cannot drift by construction. `tunable` is the one forward-looking field kept, because the v2 optimizer needs a declared way to mark a prompt off-limits; tier and eval_suite are deferred as YAGNI with no consumer.

#### D-465: No seed-on-install
**Decision**: Agent context/ starts empty. Stock is never copied into agents at install or agent-create time; an overlay file exists only where someone deliberately overrode a prompt.
**Priority**: modularity
**Alternatives**: arccli seeds all stock prompts into each agent at install; seed with recorded stock hash and auto-update unmodified copies
**Rationale**: Seeded copies make a stale copy indistinguishable from a deliberate override, so upstream prompt improvements never reach deployed agents and every upgrade becomes an N-agent manual patch — the drift already seen when the arcmemory distiller fix landed in the scaffold but not in deployed tomls. Overlay-only keeps stock authoritative and upgradeable, and makes diff-vs-stock pure signal.

#### D-480: An override is permanent — upgrade divergence accepted
**Decision**: Once a prompt is overridden, upstream changes to that prompt never reach that agent, by design. Arc ships no stock-hash tracking, no three-way merge, and no "stock has changed" staleness indicator. The overlay is the answer until a human deletes it (reset-to-stock, D-464).
**Priority**: simplicity
**Alternatives**: record the stock hash an overlay forked from and surface divergence in arcui; three-way merge on upgrade (the dpkg/ucf model)
**Rationale**: Operator ruling, accepting the risk the deepening pass raised. An override exists precisely because stock was wrong for this deployment; silently pulling upstream wording back in would defeat it, and a staleness banner on every upgrade is noise for a signal the operator has already decided to ignore. This bounds the blast radius of D-465: un-overridden prompts (the overwhelming majority) still upgrade automatically, and the few deliberate overrides are frozen on purpose. Revisit only if an upstream prompt change is ever security-relevant, in which case the fix is a release note, not merge machinery.

### Research Insights

**From Solutions Archive:**
- _(none — no archive entry covers prompt or config schema design)_

**Best Practices:**
- Independent convergence on D-462: Langfuse auto-increments an integer per edit, LangSmith assigns a commit hash, Humanloop and Agenta both hash parameters to derive a version ID. None trusts a hand-typed version field, and the cited reason is exactly ours — humans edit content and forget to bump.
- Markdown + YAML frontmatter is the converging portable-prompt format: Microsoft Prompty (.prompty) and GitHub Copilot (.prompt.md / .chatmode.md). Frontmatter fields recurring across ecosystems: name, description, version/tags, model+provider+params, inputs/outputs schema, tools allowlist. D-462's name/description/tunable is a strict subset — defensible as YAGNI, and the surveyed supersets show where growth would go.
- _FRONTMATTER_RE already exists at arcui/routes/agent_detail/_common.py:55 and the frontend has a frontmatter.tsx MarkdownFile renderer — the frontmatter path is already paved on both ends.

**Edge Cases:**
- Content hash alone gives no human-readable 'what changed' signal; sources recommend a hybrid. D-462's answer is git history — sound in-repo, but note an OVERLAY lives in team/<agent>/context/ which is gitignored in this repo, so overlays have NO history mechanism at all. This is a genuine gap in D-462's 'history from git' rationale for the overlay half.
- GEPA's candidate object requires per-module prompt text, full parent lineage, and per-instance scores against a maintained Pareto set; SkillOpt requires retaining REJECTED candidates so the optimizer avoids repeating failed edits. Both are materially richer than D-462's schema — fine for v1, but the v2 optimizer will need a store, not just frontmatter.
- Two agents pinned to different versions of the same logical prompt diverge silently ('template drift') — a known config-versioning failure mode that D-460's per-agent overlays make possible by design.

**Performance:**
- _(none)_

**References:**
- https://prompty.ai/core-concepts/file-format/
- https://arxiv.org/abs/2507.19457 (GEPA)
- https://arxiv.org/abs/2605.23904 (SkillOpt)
- https://humanloop.com/docs/explanation/environments

### API Design

#### D-464: Dedicated prompt endpoints
**Decision**: GET /api/agents/{id}/prompts (all prompts, all packages, with overridden state); GET .../{pkg}/{name} (stock + effective + diff); PUT (write overlay); DELETE (reset to stock). Stock is read server-side from the installed package; the client never supplies a filesystem path.
**Priority**: security
**Alternatives**: extend the generic files API with a stock read root and ?stock=true; both a curated prompts API and raw file access
**Rationale**: Prompts are (package, name)-shaped with a stock/overlay duality; the files API is path-shaped and assumes one root per request. Teaching it a read-from-package/write-to-workspace redirect would require building enumerate-all, diff, and reset anyway. Reading stock server-side with no client-supplied path is tighter than widening file-API confinement into installed package code, and keeps the files API's single-root invariant intact. Handlers reuse the existing confinement and secret-scan helpers rather than reimplementing them.

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- D-466's placement is API-compatible for free: _VALID_ROOTS = {'workspace','agent'} (_common.py:52) and _compute_write_target (_common.py:111-128) already resolve anything under the agent root, so team/<agent>/context/ needs NO new root registration.
- Follow skill_versions.py's error convention precisely: _error() wrapping ErrorResponse, and _store_unreadable() -> 503 with the exception verbatim, so a 200-with-empty-list is distinguishable from a 503-unreadable. Directly applicable to 'this agent has no overlays' vs 'the package dir could not be read'.
- Route registration is a flat manual manifest (routes/agent_detail/__init__.py:72-112): new module with __all__, alphabetized import block, explicit Route entries per method, plus a docstring bullet. Response models go in arcui/schemas.py, hooks in web/src/lib/queries.ts, types in web/src/lib/types.ts.

**Edge Cases:**
- Partial duplication is real and should be acknowledged rather than denied: identity.md and context.md are ALREADY readable and writable through the generic file surface (GET/PUT /api/agents/{id}/files/read) and edited in the UI via file-tree.tsx FileViewer:141. What genuinely does not exist is stock-vs-effective resolution, prompt-specific listing, reset-to-stock, and any diff rendering — which is exactly D-464's justification, and it survives.
- Note the existing PUT reuses the GET's path (/files/read) rather than a distinct write path — a convention worth deliberately NOT copying for /prompts, where PUT and DELETE have clean REST semantics.

**Performance:**
- skill_versions.py:95-105 memoizes diff generation with @lru_cache(maxsize=256) keyed on (hash_a, hash_b, body_a, body_b) — directly reusable shape for stock-vs-overlay diffs, which are far more cacheable since stock never changes at runtime.

**References:**
- packages/arcui/src/arcui/routes/agent_detail/_common.py:52,111-128
- packages/arcui/src/arcui/routes/agent_detail/skill_versions.py:60-105

### Security

#### D-466: Overlays outside agent tool reach
**Decision**: Overlays live in the agent's config root (team/<agent>/context/), structurally outside the workspace subtree the agent's file tools are confined to. Written only by arcui and arccli acting as operator.
**Priority**: security
**Alternatives**: extend DEFAULT_PROTECTED_NAMES to denylist the context/ tree; allow agent self-writes with signed audit events
**Rationale**: An agent able to rewrite its own system prompt is ASI01 goal-hijack and ASI06 context-poisoning in one, and would bypass the operator gate entirely. Placement puts the file out of the tool's addressable range with no denylist to maintain and no path-normalization slip to exploit — secure by default rather than by configuration.

#### D-467: Overlays are signed and verified before injection
**Decision**: Every overlay carries an Ed25519 operator signature, verified by arcprompt before the text is injected. Verification failure refuses the load. Same rule at every tier, no bypass flag. The run-start audit event records source (stock|overlay) and sha256 per prompt.
**Priority**: security
**Alternatives**: hash and audit only, following the identity.md precedent; hash now, signing deferred until prompts become distributable
**Rationale**: Hash-and-audit only records tampering after the model has consumed the text; verification refuses to load it. Against Arc's assumed threat model — a potentially hostile host — that difference is decisive. The key infrastructure is already ambient (DIDs, arctrust.keypair, an operator DID on the arcui write path), so the key-management cost is largely imaginary. CLAUDE.md's Sign pillar admits no carve-out for locally authored artifacts. That identity.md is unsigned is a gap to flag, not a precedent to extend.
**Deployment**: federal: identical code path; FIPS-validated crypto per tier requirements | enterprise: identical code path | personal: identical code path; self-signed operator key acceptable

#### D-468: Sign-on-write editing flow
**Decision**: arcui signs with the operator key as part of the save. On the box, `arc prompt edit <pkg>/<name>` opens $EDITOR and signs on close. A raw editor save produces an unsigned file that fails to load with a message naming the fix (`arc prompt sign ...`).
**Priority**: simplicity
**Alternatives**: detached .md.sig sidecar; signature stored in frontmatter
**Rationale**: Signing is only sustainable if the ergonomics match what people already do; friction is what drives operators to disable verification. Signing as a side effect of the normal write path means no separate step to remember, and the failure message converts a raw vim save from a mystery into a one-command fix. Where the signature bytes physically live (sidecar vs frontmatter) is left open — see Open Questions.

#### D-481: Signing authority resolved per-request, never hardcoded
**Decision**: The write path never names a key. It calls a `SigningAuthority` seam that resolves the signing identity from the authenticated principal on the request. Today that resolves to the single deployment operator key at `~/.arc/` for any operator-role caller; when arcui moves to per-user login, the same seam resolves to the logged-in user's own key with no call-site change. The audit event records the **resolved** signer DID, never a constant.
**Priority**: modularity
**Alternatives**: arcui holds one process-level operator key referenced directly at the call site; arcui writes unsigned and the CLI signs separately; browser-side WebCrypto signing
**Rationale**: Per-user login is a stated near-term direction, so the variable to design around is *which principal signs*, not *which key the server holds*. Binding the call site to a single process key would have to be unpicked in every handler later; a resolver seam makes that migration a one-implementation change. The audit half is not cosmetic — SPEC-017 (`tier-must-flow-through-construction`) is the in-repo precedent where enforcement was correct while the audit event recorded a hardcoded fallback, so federal audit trails lied. Recording a constant `signer_did` while the real signer varies would reproduce that defect exactly. Accepted interim exposure: until per-user login lands, holding the shared operator token confers signing authority — which still strictly improves on today's baseline, where `identity.md` is writable through that same token and carries no signature at all.

### Research Insights

**From Solutions Archive:**
- security-issues/2026-04-18-ast-validator-is-not-enough-defense-in-depth.md — no single gate is a control. Supports D-466 + D-467 as layered rather than alternative.

**Best Practices:**
- If the signature goes in frontmatter, use git's gpgsig excluded-field pattern: sign every field EXCEPT the reserved signature key, then splice it in; verification strips it to regenerate the exact payload. JWS solves the same chicken-and-egg identically (RFC 7515) — it never signs its own serialized form.
- If the signature goes in a sidecar, it matches arc's existing .arcsig convention exactly (artifact.py, files_write.py:43, arcskill/improver/codepatch.py:31) and eliminates canonicalization risk entirely, since arc's verify_artifact already signs raw content bytes.
- Keep signing frictionless or people disable it — the gitsign pattern (keyless, OIDC, credential cache for headless CI) exists precisely because humans reach for --no-gpg-sign when signing blocks them. D-468's sign-on-write is the same instinct.

**Edge Cases:**
- CORRECTION to D-467/D-468's stated rationale: arcui's operator gate is ROLE-based via a shared bearer token (auth.py:189-239, hmac.compare_digest on operator_token), NOT DID-based. There is no per-user DID on the arcui write path. My claim that key infrastructure was 'already ambient on the arcui write path' was wrong — the operator KEYPAIR lives at ~/.arc/ and is used by the CLI (arc blueprint sign), so server-side signing needs the arcui process to hold the operator private key. OPEN DESIGN QUESTION, not a settled reuse.
- There is no arc skill sign command (arc skill has only list/create/validate/search/evals), so a hand-authored skill cannot be signed today and simply cannot load at enterprise/federal. D-468's arc prompt sign would be the second operator-signing CLI after arc blueprint sign — worth deciding whether skills get the same treatment.
- Canonicalization traps that silently break byte-exact verification: editors inserting a final newline (VS Code default), core.autocrlf rewriting LF to CRLF on checkout, YAML round-tripping by linters or yq reordering keys and changing quote style. All apply to a frontmatter-embedded signature; none apply to a raw-bytes sidecar.
- Sidecar loss is the counterweight: cp, archive extraction, sparse-checkout, and copy-paste routinely drop a companion .sig. Sigstore's .sigstore.json bundle exists specifically to reduce that N-file sprawl.
- identity.md has NO integrity check on the read path — ContextManager reads it with a plain read_text() (context.py:110-114). The only hash touching it is identity_goal_hash() in planning/_runtime.py:169-181, which detects goal drift for stale plans, not file integrity. This confirms the earlier judgment that identity.md is a gap, not a precedent.
- tier = 'personal' is hardcoded in eight sections of the arc agent create scaffold (_common.py:142,237,264,305,316,459,470,495), so D-467's every-tier signing applies to every scaffolded agent from creation. Personal is the de-facto default and this is where the friction lands.

**Performance:**
- _(none)_

**References:**
- https://git-scm.com/docs/signature-format
- https://datatracker.ietf.org/doc/html/rfc7515
- https://docs.sigstore.dev/about/bundle/
- https://docs.github.com/en/get-started/git-basics/configuring-git-to-handle-line-endings
- packages/arcui/src/arcui/auth.py:189-239

### Extensibility & Lockdown Configuration

#### D-471: Tier variance via policy content, not code
**Decision**: arcprompt has one code path at all tiers. Tier stringency is expressed as PolicyPipeline content: a federal deployment can DENY prompt:write to freeze prompts after authorization; personal declares no rule, so the gate is a no-op ALLOW.
**Priority**: modularity
**Alternatives**: no tier variance at all; federal requires a countersignature (two-person integrity)
**Rationale**: Consistent with ADR-019 — tier is stringency metadata, not a gate, and the pipeline runs identically everywhere. Reusing the existing policy layer gives federal a CM-3/CM-5 change-control freeze with zero tier branching in arcprompt. Two-person countersigning has no existing consumer; if it is wanted later, the mechanical operator-approval work (SPEC-035) is its natural home.

### Research Insights

**From Solutions Archive:**
- security-issues/2026-04-18-tier-must-flow-through-construction.md — the canonical warning for any tier-conditional behavior, including D-471's policy-content approach.

**Best Practices:**
- PromptLayer generalizes deployment labels into dynamic/percentage-split labels, so the overlay mechanism doubles as canary rollout. Out of scope for v1 but shows where D-471's policy-gated model could extend without redesign.

**Edge Cases:**
- Personal is the de-facto default: tier='personal' appears in eight sections of the agent-create scaffold (arccli/commands/agent/_common.py). Only the three shipped blueprints (personal-assistant, enterprise-ops, federal-analyst) set anything else, and they are opt-in presets. Whatever D-467 requires at personal tier IS the default experience.

**Performance:**
- _(none)_

**References:**
- packages/arccli/src/arccli/commands/agent/_common.py:142,305
- https://docs.promptlayer.com/features/prompt-registry/release-labels

### Testing

#### D-469: Byte-identical migration assertion
**Decision**: Before any Python prompt constant is deleted, a test asserts the loaded markdown equals the original string byte-for-byte. Migrate one package at a time; the temporary test and the constant are deleted together once it passes.
**Priority**: security
**Alternatives**: golden-file snapshot of the fully assembled system prompt; both per-prompt bytes and an assembled snapshot
**Rationale**: Prompts break invisibly — a lost trailing newline or collapsed blank line changes model behavior without failing anything. Byte equality against the exact string being replaced is the tightest possible proof of a faithful move, and this repo's history of shipping correct predicates with dead wiring makes a real-path assertion non-optional. Deleting the scaffold with the constant honors the no-vestigial-code standard.

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- The exact-equality idiom D-469 needs already exists in-repo: arcrun/tests/test_prompts.py:52 asserts result['code_exec_guidance'] == CODE_EXEC_GUIDANCE, and :57 the same for CONTAINED_EXEC_GUIDANCE. Follow that shape rather than inventing one.
- arcui integration-test doctrine matches D-469's spirit — real CandidateStore through arcskill's own write path, real AgentIdentity, real tmp_path dirs, only the audit logger disabled. Explicit header docstring says do not fake the store.
- Add a SHA-256 digest assertion alongside string equality so a diff failure reports a short hash rather than dumping 4.9 KB of prompt into the test output.

**Edge Cases:**
- THE trap D-469 exists to catch, now named concretely: workpad/prompt.py:12 opens with a backslash-continued triple quote to suppress the leading newline, and Path.read_text() returns a .md file's trailing newline verbatim. Byte-identity will fail on exactly this unless the loader's newline handling is decided deliberately.
- CONTEXT_MAINTAINER_SYSTEM_PROMPT (~4.9 KB, the largest prompt in the repo) has ZERO test coverage — no test file references it. Tests must be authored against the constant BEFORE it moves, which inverts the usual order.
- Existing assertions are too loose to prove anything: test_prompts.py:112-120 asserts only isinstance and len(...) > 50 on prompt_guidance, so a migration could silently truncate the text and still pass. context_manager.py:78 asserts 'identity' in prompt.lower() with an or-fallback.
- No snapshot library is installed anywhere (no syrupy, pytest-regressions, approvaltests). This is hand-rolled — the arcskill 'golden' suites are machine-authored eval cases, not byte-comparison fixtures.
- No fixture assembles a full system prompt. arcagent/tests/conftest.py has exactly one fixture (_isolate_arcstore_data_dir). The closest harness is the local chain in test_context_manager.py:17-51, which yields a ContextManager but is not wired to arcrun.get_strategy_prompts().

**Performance:**
- Coverage gates (line >= 80%, branch >= 75%, arcagent fail_under=80 in pyproject) mean a thin arcprompt loader with sparse tests can drag a package below threshold — budget test coverage into the migration, not after it.

**References:**
- packages/arcrun/tests/test_prompts.py:52,57,112-120
- packages/arcagent/tests/unit/core/test_context_manager.py:17-51,78
- packages/arcui/tests/integration/test_skill_versions_routes.py:1-15

### UI/UX

#### D-470: Prompts tab with drawer editor
**Decision**: A Prompts tab in agent detail lists prompts grouped by package, each row chipped [stock] or [overridden]. Clicking opens a drawer with a stock / effective / diff toggle, edit, save, and reset-to-stock.
**Priority**: simplicity
**Alternatives**: a section inside the existing Files page; a fleet-wide prompts page plus a per-agent tab
**Rationale**: Reuses the established tools-skills.tsx plus skill-drawer.tsx pattern, which already implements list → drawer → diff → rollback, so the feature inherits the existing design language instead of inventing one. The file tree cannot express the stock/overlay duality. A fleet-wide view serves the auditor persona but is v2-shaped; the v1 driver is tuning one agent.

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- skill-drawer.tsx (158 lines) is the concrete template: Sheet side='right' sm:max-w-xl, sticky action bar, lazy useQuery enabled on selection, useOperatorMode() gating the Edit button, apiPut then queryClient.invalidateQueries, and the prevSkill compare-during-render trick to reset state on selection change.
- Reusable primitives already exist: sheet, badge, tabs, textarea, scroll-area, separator, tooltip, skeleton, data-table.tsx, states.tsx, page-header.tsx, stat-card.tsx, status-badge.tsx, markdown.tsx, frontmatter.tsx (MarkdownFile), code-block.tsx, plus apiGet/apiPut helpers in lib/api.ts:35-65.
- Copy the three-banner convention from skill-drawer.tsx:123-137 — destructive for error, status-warning for signature staleness, status-online for success. A stale-signature banner is directly relevant given D-467.

**Edge Cases:**
- CORRECTION to D-470's stated rationale: there is NO diff renderer and NO version-timeline UI anywhere in web/src. skill_versions.py's backend diff/rollback endpoints (__init__.py:81-97) are entirely unconsumed by the frontend today. The Prompts tab builds the first diff view in the product — the pattern to reuse is the drawer shell, not the diff.
- There is also no useAgentSkillVersions hook to copy; new hooks must be written fresh against queries.ts's useApiQuery/useQuery conventions.
- The generic file editor (file-tree.tsx FileViewer:141) already offers Edit/textarea/Save over identity.md with the same affordance — take care the Prompts tab reads as a distinct, curated surface rather than a second file editor.

**Performance:**
- Server-side unified diff with lru_cache (skill_versions.py:95-105) avoids shipping a diff library to the browser — the established choice, and stronger here since stock content is immutable at runtime.

**References:**
- packages/arcui/web/src/components/skill-drawer.tsx
- packages/arcui/web/src/components/file-tree.tsx:138-174
- packages/arcui/web/src/lib/queries.ts:407-414

### Observability & Telemetry

#### D-475: Prompt telemetry rides existing audit emission
**Decision**: No separate decision. The run-start snapshot (D-461) emits one audit event listing every prompt used with its source and sha256; prompt resolution failures raise and are audited (D-463).
**Priority**: simplicity
**Rationale**: Prompt telemetry rides the existing arctrust.audit.emit single emission point and the run span; no new export target, metric, or sampling decision is introduced.

### Research Insights

**From Solutions Archive:**
- security-issues/2026-04-18-tier-must-flow-through-construction.md — THE cautionary precedent for this section. Enforcement was correct while the audit event recorded a hardcoded tier=personal fallback, so federal audit trails lied. D-467's run-start event records prompt source and sha; if the loader resolves posture correctly but the audit context is built with defaults, this reproduces exactly. Pass tier and posture through construction.

**Best Practices:**
- Prompt telemetry rides the existing arctrust.audit.emit single emission point, so no new export target or sampling decision is introduced — but the event payload must be populated from the same constructed posture the loader used, not re-derived.

**Edge Cases:**
- A per-run event listing ~25 prompts with source and sha is materially larger than a typical audit record; confirm sink payload limits and whether the JsonlSink and SignedChainSink handle it without truncation.

**Performance:**
- _(none)_

**References:**
- .claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md

### Audit & Compliance

#### D-476: Audit inherited from arctrust sinks
**Decision**: No separate decision. Audit schema, storage, and retention are inherited from the existing arctrust audit sinks; the prompt-specific content is the per-run source+sha record.
**Priority**: simplicity
**Rationale**: No declared compliance regime in .claude/steering/compliance-mandates.json, so no retention or classification mandate auto-applies. Change-management tracking for federal is handled by D-471's policy freeze.

### Research Insights

**From Solutions Archive:**
- _(none beyond the tier-flow entry cited under Observability)_

**Best Practices:**
- _(none)_

**Edge Cases:**
- D-462 records history as 'from git', but overlays live under team/<agent>/ which is gitignored in this repo — so the overlay half of the system has no history mechanism at all. Either overlays get a store-backed history (as skill candidates do via arcstore) or the audit trail is the only record of what changed. This materially affects the auditor persona.

**Performance:**
- _(none)_

**References:**
- packages/arcstore/src/arcstore/query.py:45-77

### Integration

#### D-477: Not applicable
**Decision**: Not applicable. arcprompt is a leaf library with no external service connections, no message-bus participation, no protocol choice, and no inter-agent communication. Prompt resolution is local filesystem reads.
**Priority**: simplicity
**Rationale**: Recorded explicitly rather than skipped silently. If prompts ever become distributable (a hub, or optimizer-proposed variants arriving off-box), this category reopens — and so does the signing-scope question.

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- _(none — category confirmed not applicable; arcprompt performs local filesystem reads only)_

**Edge Cases:**
- The not-applicable status is conditional on prompts never travelling. GEPA and SkillOpt both assume optimizer-proposed candidates, and SkillOpt requires retaining rejected candidates — the moment v2 lands, this category and the signing-trigger question reopen together.

**Performance:**
- _(none)_

**References:**
- https://arxiv.org/abs/2605.23904

### Performance

#### D-478: Prompt I/O bounded by run-start pinning
**Decision**: No separate decision. Run-start pinning (D-461) is the caching strategy: prompts are read and hashed once per run, not per call, so per-turn prompt I/O is zero.
**Priority**: simplicity
**Rationale**: Reading roughly 25 small markdown files once per run is negligible against a single LLM call and preserves the sub-500ms cold-start budget. No connection pooling, backpressure, or batching concerns arise for local file reads.

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- Cache stock-vs-overlay diffs with the lru_cache shape already used at skill_versions.py:95-105; stock bodies are immutable at runtime so the hit rate is far better than the skill case.

**Edge Cases:**
- _(none)_

**Performance:**
- Reading ~25 small markdown files once per run is negligible beside a single LLM call, and externalizing reduces NCLOC against the arcrun 5,400 budget.

**References:**
- scripts/check_loc_budgets.py:57-61

### Deployment

#### D-479: Package-by-package rollout, no feature flag
**Decision**: No separate rollout decision beyond D-469. Migrate package by package (arcrun, then arcagent, then arcmemory, then arcskill), each package's constants deleted only once its byte-identical tests pass. No feature flag: behavior is provably unchanged at each step, so there is nothing to toggle between.
**Priority**: simplicity
**Alternatives**: big-bang migration of all packages; feature-flagged dual-path loading
**Rationale**: A flag implies two live code paths and the option of running the old one, which is exactly the backward-compat shim the repo standard forbids. Byte-identical proof per package makes each step a no-op in behavior, so rollback is plain git revert.

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- MANDATORY packaging step, learned from the SPEC-047 blueprint migration: every touched pyproject.toml needs artifacts = ['src/<pkg>/**/*.md'] or hatch silently drops the prompt files from the wheel. arcagent/pyproject.toml:136 carries exactly this line with the comment that packaged presets are loaded at runtime and not optional data.
- The blueprint migration (config constants -> packaged TOML) is the direct in-repo precedent for D-469's package-by-package sequencing, including the Path(__file__).parent loader convention at blueprints/loader.py:51.
- Recommended order: (1) author exact-equality + SHA-256 tests against the live constants, especially CONTEXT_MAINTAINER_SYSTEM_PROMPT which has none; (2) move text to .md beside the loader; (3) add the artifacts= line; (4) re-run the pinned hash tests.

**Edge Cases:**
- A new leaf package needs a hand-written tests/architecture/test_no_arcprompt_imports_*.py guard — leaf-ness is NOT generically enforced. All 11 architecture tests are individual AST import scans with no central dependency-DAG table.
- tests/architecture/test_workspace_install.py:47 _CANONICAL_PACKAGES = ['arcgateway','arccli','arcagent','arcllm','arcrun'] must gain arcprompt, and Makefile:52-60 (make install) alongside it.
- Blueprint verification was behavioral (asserting three packaged presets resolve), NOT byte-identical — so D-469 is a deliberate strengthening over the precedent, not a copy of it.
- arcrun mounts identity.md/context.md read-only in the docker backend. Confirm whether a live agent sees overlay writes at all before restart, since D-461 assumes next-run pickup.

**Performance:**
- Moving prompt text out of Python reduces measured NCLOC — the arcrun 5,400 foundation budget gains headroom rather than losing it.

**References:**
- packages/arcagent/pyproject.toml:136
- packages/arcagent/src/arcagent/blueprints/loader.py:51,102,164
- tests/architecture/test_workspace_install.py:47
- scripts/check_loc_budgets.py:49-65

### Open Questions
- Where do signature bytes live — detached <name>.md.sig sidecar (clean diffs, two files to keep together) or a frontmatter field (one artifact, churning crypto line in every diff)? D-468 settled the flow, not the storage.
- Does arcprompt absorb arcskill.improver's prompt builders (build_reflection_prompt, build_judge_prompt, suitegen._prompt)? They interpolate trace data per call — generated, not authored — so they may not be the same class of artifact at all.
- capability_registry.format_for_prompt() generates an XML capability manifest at runtime. Does the authored preamble around it become a prompt file with a slot, or stay entirely in code?
- arcrun/strategies/code.py:14 _DEFAULT_PREFIX duplicates CodeExecStrategy.prompt_guidance almost verbatim and is unreachable in src/. Confirm it dies during migration rather than becoming a second prompt file.
- identity.md is unsigned while prompt overlays will be signed (D-467). Is that gap worth closing separately, and does it belong to this spec or its own?
- Does `tunable: false` in frontmatter need any enforcement in v1, or is it inert metadata until the v2 optimizer exists?

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- RESOLVED — capability_registry.format_for_prompt() (capability_registry.py:360, _render_manifest_locked :371-411) emits pure ElementTree XML with NO authored preamble. The prose wrapper is _SKILL_USAGE_INSTRUCTION (agent_lifecycle.py:38), already a separate constant injected by a separate bus subscriber at priority 91 vs the manifest's 85. No slot mechanism is needed; the boundary is already clean.
- RESOLVED — all four arcskill improver prompts are cleanly externalizable. Runtime data is pre-rendered to flat strings BEFORE each f-string (mutate.py:40-46), so a markdown template taking pre-rendered values is a mechanical swap. evaluator.py:83 has the largest static share (~20 lines) and its module-level DIMENSIONS table is authored prose in Python — a second externalization candidate. Note mutate.py:37 accepts an intent_header parameter that is never interpolated (dead argument).
- RESOLVED — _DEFAULT_PREFIX is dead. system_prompt_prefix has exactly two hits across all packages/*/src, both its own definition (code.py:35-36). Six of its seven guideline bullets duplicate prompt_guidance with only cosmetic differences (hyphen vs em-dash, 'You will receive' vs 'You receive'). Delete during migration.

**Edge Cases:**
- STILL OPEN and now sharper — signature storage. Both options have in-repo/prior-art support: sidecar matches arc's existing .arcsig convention and eliminates canonicalization risk; frontmatter matches git's gpgsig excluded-field pattern and cannot desync from its file. There is NO established convention for signing markdown+frontmatter, so frontmatter means inventing one.
- NEWLY OPEN — who holds the operator private key for arcui-side signing, given the gate is role-based on a shared token with no DID (auth.py:189-239). D-468 assumed this was solved; it is not.
- STILL OPEN — identity.md signing gap, now confirmed as a real absence rather than a design choice (plain read_text at context.py:110-114, no verification anywhere).
- STILL OPEN — whether tunable: false needs v1 enforcement. GEPA and SkillOpt both need far richer per-candidate metadata than frontmatter carries, so v2 likely needs a store regardless; tunable may stay inert metadata.

**Performance:**
- _(none)_

**References:**
- packages/arcagent/src/arcagent/capabilities/capability_registry.py:360,371-411
- packages/arcagent/src/arcagent/core/agent_lifecycle.py:38,353,366-375
- packages/arcskill/src/arcskill/improver/mutate.py:33-72,163-183
- packages/arcrun/src/arcrun/strategies/code.py:14,35-36

### Related Solutions
_(none)_

### Research Insights

**From Solutions Archive:**
- security-issues/2026-04-18-tier-must-flow-through-construction.md (SPEC-017) — tier/posture must flow through construction, not per-call; audit events lied while enforcement was correct.
- security-issues/2026-04-18-ast-validator-is-not-enough-defense-in-depth.md — a single gate is not a control; layer placement with verification.

**Best Practices:**
- _(none)_

**Edge Cases:**
- _(none)_

**Performance:**
- _(none)_

**References:**
- _(none)_


---

## arctui — Terminal Agent Interface — Build Decisions (2026-07-24)

**Phase**: build | **Status**: complete | **Total decisions**: 7 (5 user, 2 auto-applied)
**ID range**: D-482 to D-488
**Priority framework**: simplicity → modularity → security → scalability

### Summary
arctui is a thin TERMINAL INTERFACE over arc's existing systems, not a new platform. It surfaces and drives what arc already provides (agents, capabilities/tools/skills/modules, memory, MCPs, arcrun loops, arcllm models, hooks); extensibility lives in arc and is rendered by the TUI (inspiration: pi's tui package, a rendering framework separate from its coding-agent). v1 = the reliable working agentic loop through arcrun, daily-usable like Claude Code on real folders. Because it is a thin interface, the categories not listed below INHERIT arc's existing infrastructure rather than defining new mechanisms: Data Model (no new store — uses arcagent.toml, the ~/.arc agent layout, and arc session/transcript stores; trusted folders persist in the agent toml allowed_paths), API (no new network API — drives arcagent/arcrun in-process), Observability (arc OTel + the arcrun event stream rendered in the activity pane), Audit (arctrust audit — tool calls, approvals, and folder-trust grants emit audit events), Integration (models via arcllm, MCPs via arc's MCP support — no new external services), Performance (in-process Textual; arc cold-start/loop budgets), Testing (arc pytest standard; existing arctui unit+smoke tests), Deployment (arc packaging; the existing `arc tui` entry point).

### Auto-Applied (Compliance Mandates)
| ID | Category | Decision | Mandated Answer | Citation |
|---|---|---|---|---|
| D-487 | Security | No secrets in code or plaintext on disk | arctui stores no credentials; model/provider keys are resolved through arcllm/vault, never written to arctui code or config. | OWASP baseline (no declared regime) |
| D-488 | Security | Input validation at trust boundaries | The folder path, selected agent id, and model id are validated before use; folder access is granted only through the explicit trust gate (D-484). | OWASP baseline (no declared regime) |

### Architecture

#### D-482: arctui is a thin terminal interface over existing arc
**Decision**: arctui adds NO new extension/agent system. It is a terminal interface that surfaces and drives arc's existing systems — agents, capabilities (tools/skills/modules), memory, MCPs, arcrun loops, arcllm models, and hooks. Extensibility lives in arc; the TUI renders it. Modeled on pi's tui package (a rendering/component framework) sitting separate from the agent.
**Priority**: Simplicity + Modularity
**Alternatives**: A new arctui-specific plugin layer wrapping arc packages (parallel system, weaker modularity); extending arc's CORE extension system for UI contributions across web+tui (larger blast radius, more scope now).
**Rationale**: The owner's framing: 'arctui is just an interface to what already exists, a new interface through terminal.' Reusing arc's package/hook machinery dogfoods the existing API and avoids a second extension system to maintain.

#### D-483: Coding capability = a tuned arcagent, not TUI logic
**Decision**: The coding ability is a normal arcagent (bash/tools already built in) whose system prompt + identity are tuned for coding. arctui is the control surface that runs whichever agent you point it at; the coding smarts live in the agent/preset, not the TUI. 'General, then tune for coding' = a preset.
**Priority**: Simplicity + Modularity
**Alternatives**: Bake file-edit/diff/shell/approval logic into arctui as core features (couples the interface to one use case, bypasses the preset/dogfood path); a hybrid where coding VIEWS are first-party but smarts stay in the preset (revisit post-v1).
**Rationale**: Owner: 'I create an arcagent, it comes with bash tools already, I tune the system prompt and identity to work on coding, and control it from arctui in various folders.' Keeps the interface generic and dogfoods the agent/preset path.

#### D-484: Folder scoping via an explicit trust gate that edits policy
**Decision**: Launching arctui in a folder prompts 'is this folder safe?'. On confirm, arctui persists the grant by adding that folder to the agent's [tools.policy] allowed_paths in arcagent.toml (wherever arc controls those policies). The folder becomes the agent's working + tool-confinement root for the session — the Claude Code trust model wired to arc's policy surface. Untrusted until confirmed; the grant should emit an audit event.
**Priority**: Security + Simplicity
**Alternatives**: Repo-local .arc agent per project (more modular per-project memory/identity, but two-tier discovery + more setup); a global agent whose allowed-paths is silently extended at launch (weakest isolation).
**Rationale**: Owner: 'tui should ask if this folder is safe; if it is, it modifies the toml to allow that area for reading/writing.' Preserves arc's workspace-confinement invariant while enabling Claude-Code-style multi-folder use; secure by default.

### UI/UX

#### D-485: v1 = the reliable working agentic loop through arcrun
**Decision**: v1's must-have is start-up → immediately use/code/talk agentically through arcrun, reliably, on a trusted folder — built on the existing transcript/activity/input panes plus tool-approval prompts (needed for safe agentic action). The richer view catalog (diff viewer, model/agent pickers, slash-command palette, @file autocomplete) is DEFERRED to post-v1.
**Priority**: Reliable loops over features
**Alternatives**: Lead with a polished view catalog (diff viewer, pickers, palette) before the loop is solid — rejected: contradicts the 'reliable loops over features' principle and the 'usable like Claude Code' bar.
**Rationale**: Owner: 'start up, and start agentically using/coding/talking through arcrun.' The v1 success test is that the agentic loop actually works end-to-end in the terminal, not a breadth of views.

### Extensibility

#### D-486: Extensions are arc units the TUI surfaces; one real extension proves the seam
**Decision**: An extension IS an arc package/capability/hook unit (tools, skills, modules, memory, MCPs — already contributed through arc). arctui surfaces them; the terminal-specific contribution points (custom views/components, slash-commands, autocomplete providers, themes — pi-style) are the minimal additions, wired in a later phase. v1 proves the end-to-end seam by installing ONE real external extension. Extensions remain signed/policy-gated/audited (secure by default) and built-ins use the same public API (dogfood).
**Priority**: Modularity (dogfood the extension API)
**Alternatives**: Build a full public registry/marketplace + sandboxed third-party views up front (deferred — that's the ecosystem phase, not v1).
**Rationale**: The wedge is the extensible ecosystem, but v1 is a vertical slice: prove the hook surface works with one real extension rather than shipping a marketplace.


### Open Questions
- Extension package/manifest FORMAT: reuse the arc package layout as-is, or a lighter 'arctui extension' manifest? How much of the hook surface already exists (capabilities/modules/ExtensionPoint/arcskill.hub) vs must be built (TUI-view hooks, slash-command hooks, install/discovery flow)?
- Trust/sandbox model for third-party TUI VIEWS specifically — a custom Textual widget is arbitrary in-process code; tools/skills have a trust path, views are new.
- Which open-source model backends first (Ollama / vLLM / llama.cpp) and the in-session model-switch UX.
- What 'reliable arcrun loops' concretely needs beyond arcrun today (checkpoint/resume UX, derail detection, cost/turn caps surfaced in the TUI).
- Repo-local .arc agent vs global ~/.arc agent — how agent selection spans both (D-483/D-484 chose folder-trust on a global agent; repo-local remains an option).
- Relationship to arcui (web): share components/telemetry or stay fully separate?

### Related Solutions
- Internal prior art: arc capability/module system; arcagent.extension.ExtensionPoint families (select-one/select-many); arcskill.hub (skill-marketplace connector); existing arctui package (~1700 LOC Textual — transcript/activity/input, `arc tui`, graceful no-agent mode).
- External references: pi tui + coding-agent (github.com/earendil-works/pi), pi.dev/packages, Claude Code.
- Brainstorm: .claude/brainstorms/2026-07-24-arctui-terminal-agent.md


### Research Insights (Deepened 2026-07-25)

**Deepening summary.** Six parallel Explore agents researched the open questions against the arc codebase + pi/external refs. Headline: **arctui v1 is largely INTEGRATION of existing arc primitives, not new subsystems.** Nearly everything the vertical slice needs already exists (arcrun reliability, arcllm OSS backends, capability/extension + signing/TOFU, agent roster, arcstore approvals, arctrust audit); several TUI pieces are even built-but-unwired. Key new risk surfaced: streaming is currently block-at-end (not token-live), and the built approval modals aren't connected to the real approval store.

**Extension surface (D-482 / D-486).** Arc already ships the full agent-side contribution system arctui only RENDERS: `@tool/@hook/@background_task/@capability` decorators, `CapabilityLoader` (scan-roots + signing/TOFU gate), the `agent:*` module-bus (arctui already subscribes, `app.py:127`), `arcskill.hub` (signed Sigstore/Rekor marketplace), MCPs (config-only). The ONLY genuinely-new seams are UI-side: (a) a view/widget registry (compose is hardcoded, `app.py:92`), (b) TUI-only slash-commands merged into `SlashCommandCompleter` (`command_completer.py:22`), (c) stackable autocomplete providers, (d) wiring the dead `[tui.theme]` override. **Recommendation (Simplicity→Modularity→Security): no new manifest** — ship TUI contributions as scan-many `@capability` units discovered through the existing `CapabilityLoader`, reusing one trust model; add a thin declarative view/theme registry (dataclasses + `[tui]` config keys); reuse `arcskill.hub` for anything installable. Refs: `extension/{point,families,select}.py`, `capabilities/capability_loader.py:84,190,323-356`, `tools/_decorator.py:134-270`, `arcskill/hub/config.py`.

**Third-party VIEW sandbox (Security).** A Textual widget is arbitrary in-process Python; arc already rejected in-process AST/RestrictedPython sandboxing (ADR-017C, 3 CVEs). **Realistic posture = provenance + operator-approval + audit, not runtime confinement**: a view rides the exact SPEC-047/033 signed-`.arcsig` + TOFU + tier-floor gate (`require_signature` at enterprise/federal, DID-key-pinned, BYO refused above personal). **v1: first-party views only** (builtins root is trusted); third-party views allowed only signed + operator-allowlisted at personal, blocked above by the unsigned-floor. Defer any untrusted-view marketplace + OS isolation (Firecracker) to a later spec. Edge: malicious widget reading/exfiltrating the transcript → deny view-code egress; hanging `render()` → mount with timeout, degrade to Null view.

**arcllm OSS backends + model switch (D-482, No-lock-in).** 17 providers ship; **Ollama, vLLM, HF-TGI already present, and any local OpenAI-compatible `base_url` works TODAY with zero new code** (`config.py:55` allows `http://localhost`). Recommend **Ollama first** (zero-config, no key), **vLLM second** (throughput + endpoint pool). In-session switch: no `set_model` today — arctui updates `config.llm.model`, closes the old model (`agent.py:897`), sets `self._model=None`, lets `_ensure_model` rebuild (close the old httpx pool). Edge: real incremental SSE only on the OpenAI-wire adapters (Ollama/vLLM inherit it; Anthropic/base yield one block); per-model `supports_tools` gate hard-fails tool use on some OSS models (e.g. `deepseek-r1`).

**arcrun loop reliability (D-485, reliable-loops principle).** arc ALREADY has: turn/token/cost breakers + runaway-loop + error-cascade detection (`react.py:50-96`), cancel/kill-switch (`loop.py:284-304`), turn-boundary checkpoint/resume (`checkpoint.py:99-123`), HITL approval pause (`react.py:164-179`), per-tool timeout (`executor.py:87-104`), steer injection, typed StreamEvents. **v1 = SURFACE these** in the terminal (render turn count, per-turn cost/token deltas the TUI derives, tool activity, approvals, halts with reason, cancel). Gaps (mostly defer): no LLM-level retry/backoff (belongs to arcllm; TUI shows no "retrying"), no semantic-derail detection, no mid-tool-write resume, cost breaker is best-effort/priced-only (label it). Edge: cancel is checked between turns/pre-dispatch only — an in-flight write completes (tools must poll `ToolContext.cancelled`).

**Agent discovery / folder-trust (D-483 / D-484).** An agent = any dir with `arcagent.toml`; `team_roster.list_team()` globs `team_root/*/arcagent.toml`; `~/.arc` is the config root; **no repo-local `.arc` agent concept exists** today. arctui `entry.py:64-90` currently does a CWD-only single-agent load — replace with a roster picker over `arc_home()/team` (or `--team-root`). Folder-trust (D-484) coexists cleanly with the fixed workspace: `resolve_workspace_path` checks the workspace first, then `allowed_paths` as additional roots (`_validation.py:274-291`), and `protected_paths` still overlay-deny. **Recommend a SESSION-SCOPED `allowed_paths` grant** (in-memory) over persisting to `arcagent.toml` (avoids stale grants); prune non-existent dirs on load. Repo-local later = just a second roster source (scan `cwd/.arc/*/arcagent.toml`), no engine change. Edge: empty roster → offer `arc agent create`; CWD == a protected_path → warn (protection wins).

**arctui gap vs v1 + arcui relationship.** HAVE: in-process agent + `arc tui` + no-agent mode, streaming transcript (StreamEvents), activity pane (module-bus), input composer + slash autocomplete, theming, slash dispatch. GAPS: approval modals are **built in `prompts.py` but never wired** to `app.py` or the real approval path (`arcstore.approvals.ApprovalStore` + `HumanGate`/`approval_provider`); no folder-trust launch gate; no agent/model picker (hardcoded `arcagent.toml`, session `tui:main`); `TurnEndEvent` totals + DENY/error outcomes ignored; streaming is block-at-end (`streams.py:379-392`), not token-live. **arctui↔arcui recommendation (Simplicity→Modularity): SHARE the render-agnostic event/telemetry/store layer** (arcrun StreamEvents, arcstore, arctrust audit — all exposed via duck-typed seams with no arcui import) but **keep render layers fully separate** (Textual vs React can't share widgets; coupling would drag in Starlette). Smallest v1 additions: (1) wire `ApprovalModal` → `ApprovalStore`/`approval_provider` (poll the async out-of-band store, handle timeout/deny); (2) folder-trust launch gate mirroring `arcui/routes/trust.py` over arctrust; (3) render `TurnEndEvent` (turns/cost/tokens) + outcomes; (4) agent/model picker modal. Refs: `arctui/{app.py:152-176,242-336,prompts.py:37-215,entry.py:64-107}`, `arcstore.approvals`, `arcagent/tools/approval_policy.py:54-130`, `arcui/routes/{approvals,trust}.py`.

**New risks for /specify.** (1) Block-at-end streaming undercuts the "live" coding feel — decide whether v1 needs token-live streaming (an arcrun/arcllm change) or accepts block-at-turn. (2) Async out-of-band approvals mean the TUI modal must POLL the store, not block a callback. (3) `supports_tools=false` OSS models silently can't drive the coding loop — the model picker must surface tool-capability.


## Coding-agent working directory — Build Decisions (2026-07-26)

Phase: build | Status: complete | Total decisions: 3 (D-489 to D-491) | See: ADR-029, blueprints/coding, SPEC-058

**Context.** A coding agent must work in the project directory you open it in (like Claude Code / OpenCode) while its own brain — memory, sessions, context.md, identity — stays in one stable home. Those two were the same `workspace` in code; conflating them forced a bad choice (pin to workspace → can't work in your project; point at cwd → the agent's memory scatters into every repo you open).

| # | Decision | Choice | Priority | Rationale |
|---|----------|--------|----------|-----------|
| D-489 | Home vs working dir | Split them: a `working_dir` = where the LLM's file/exec tools operate (bash cwd + relative-path root), distinct from the `workspace` = the agent's home. Defaults to workspace (every existing agent unchanged). | modularity > simplicity | Lets one agent work across many projects without moving its brain. The OpenCode model. (ADR-029) |
| D-490 | Opt-in + sandbox floor | `working_dir` = the launch cwd only when the agent opts in (`[tools] operate_in_launch_dir`, set by the coding blueprint) AND the dir is already inside `workspace + allowed_paths` (the folder-trust prompt puts it there). Boundary check unchanged. | security > simplicity | Secure by default (off for every other agent); working_dir moves the root, never the fence — it can never widen the sandbox. |
| D-491 | State-persistence invariant | Framework/module state (memory, sessions, context.md, identity, audit chain) persists via DIRECT workspace I/O, never by calling the LLM's file tools. | security > simplicity | This is what makes the split safe: because agent state is written straight to the workspace (not through the cwd-rooted tools), moving the tools to your project never drags the agent's memory into your repo. A skill writing PROJECT files via the tools is correct; one saving AGENT state must use a workspace path. (ADR-029, CLAUDE.md) |

---

## Deepening Summary

**Deepened on:** 2026-07-30
**Sections enhanced:** 7
**Solutions referenced:** 4
**Skills matched:** coding-workflow:plan-deepener, coding-workflow:python-patterns, coding-workflow:build-decisions, architecture-adr-generator, testing-coverage-gap-finder, academic-paper-breakdown

### Key Findings
- BLOCKER — arcmemory's own `sanitize()` injection filter deletes the match AND everything to end of line (`_INJECTION_RE`, security.py:33-43). Two alternates are ordinary English: `you are now ...` and `forget everything/all previous ...`. Verified destructive on realistic benchmark sentences. This silently vaporizes evidence turns and corrupts the measurement itself, invisibly.
- BLOCKER — D-499's per-session cadence is unreachable by config. `_CONSOLIDATE_POLL_INTERVAL = 300.0` is a module constant, not a config key (modules/memory/capabilities.py:35). The both-limits claim is confirmed correct and still insufficient. Fix without a framework change: the harness awaits the public `consolidate_poll_once()` directly — the same function the loop calls — which is also the ONLY reliable quiescence signal that exists today.
- BLOCKER — the default recall envelope admits exactly ONE 2000-char chunk. `top_k=5` but `budget=1024` tokens; a 2000-char event is ~525 tokens and `enforce_budget` (security.py:261-280) counts the boundary-marked block, so 525+525 > 1024. Unchanged, the run benchmarks `enforce_budget`, not memory.
- BLOCKER — 500 throwaway agents sharing an `agent.name` collide on an exclusive flock over a SHARED WORM audit chain (`~/.arc/…/worm/audit-chain-<slug>.jsonl`, core/agent.py:183-209 + arctrust/audit.py:238-250). The second concurrent agent raises RuntimeError at startup. Needs a unique name per question and a workspace-relative `security.policy_audit_log`.
- D-498's rationale is refuted on its own terms. The LongMemEval reference implementation keeps `haystack_dates` as a PARALLEL METADATA ARRAY and ships `temp_query_search_pruning.py` to filter candidates by an inferred time range; the authors measure +7-11% temporal recall from it. Zep (`reference_time=`) and mem0 (`timestamp=`) both take a first-class timestamp parameter. Inline-in-text is not what they do.
- The `Event.ts` consequence in D-498 is stated backwards. `datetime.now(UTC)` has microsecond resolution, so sessions are NOT uniform — they are perfectly ordered by INGEST ORDER. Recency is an unweighted 4th RRF list worth up to 25% of the max achievable score. On LongMemEval-S (sessions timestamp-sorted) this is accidentally correct; on ORACLE (explicitly unsorted) it hands 25% of max score to an arbitrary 5 sessions — and D-500 runs Oracle FIRST.
- `capture_respond` joins EVERY message in the payload — the user turn and the assistant turn (capabilities.py:220-221 + agent_dispatch.py:333-340). Sub-2000-char chunks are therefore stored TWICE; >=2000-char chunks truncate back to the `user` event byte-for-byte and the Deduper drops them, so the reply is never stored. D-496's open question about commentary noise is both worse and, at the cap, self-cancelling.
- `Consolidator.run()` uses an unbounded `TimeWindow()` (consolidate.py:185) — every pass re-distills the ENTIRE episodic stream from event 0. Per-session consolidation over 40 sessions is O(n^2) in LLM cost, and it guarantees the agentic engine breaches `max_tokens=20_000` after ~3 sessions, degrades, and pays for the pipeline anyway.
- Every arcmemory-internal audit event is discarded in a live agent. `_runtime.configure` calls `select_brain(...)` without `audit_sink=` (modules/memory/_runtime.py:127-136 vs brain/select.py:50), so `_audit` falls back to `NullSink`. `memory.dedup_skipped`, `memory.consolidation_degraded`, `memory.fact_updated` all vanish. Watch the `arcmemory.consolidate` Python logger instead.
- Ground truth for D-500's stratification, measured from the dataset: multi-session 133, temporal-reasoning 133, knowledge-update 78, single-session-user 70, single-session-assistant 56, single-session-preference 30 (with 30 `_abs` cross-tagged). Proportional 50q sampling puts single-session-preference at n=3. At n<10 a Wilson interval is not even stable; at n=13 and 77% observed the 95% CI is roughly [50%, 92%].
- The official judge pins the DATED string `gpt-4o-2024-08-06`, `temperature=0`, `max_tokens=10`, and `print_qa_metrics.py` hard-asserts that exact string (open issue #47). There is NO `seed` parameter in the real script despite secondary sources claiming one. The reference judge is also a single-threaded loop with no per-line flush.
- BLOCKER — D-492 is not enforced by anything today. `git check-ignore` confirms that `evaluations/**/data/*.json`, run workspaces, `traces/*.jsonl`, `.audit/*.worm` and results JSONL are ALL currently trackable. There is one `.gitignore` in the repo and it does not mention `evaluations/`. The patterns must land before the first run, and the preflight should hard-fail on `git check-ignore -q <run_dir>`.
- BLOCKER — `[modules.telemetry] store_raw_bodies = true` is the arcllm DEFAULT, encryption off. That writes full prompt and response bodies — every haystack chunk verbatim — as plaintext JSONL into `<agent_root>/traces/`, which for an `evaluations/`-rooted agent is inside the repo tree and currently not ignored. Several GB per full run.
- BLOCKER — `ArcAgent(cfg)` without `config_path` resolves `./workspace` against the PROCESS CWD, not the config. `agent.py:105` sets the attribute but line 109 branches on the PARAMETER, so the natural programmatic call puts the workspace, `traces/`, `.audit/` and the capability scan root at the arc repo root. Always pass `config_path=<abs>/arcagent.toml` and assert `agent._workspace` is under the run dir.
- D-493's premise holds for the WRITE path only. `sanitize()`/`privacy_filter()` clean the copy being STORED; the raw chunk still reaches the model as a user-role message with no boundary marking (agent_dispatch.py:112,132). The trust boundary is a memory-integrity boundary, not a model-input boundary. Recall read-back IS correctly boundary-marked and defanged — but `context.md` (workpad) and `policy.md` land in the SYSTEM PROMPT filtered only by `utils/sanitizer.py`, which has no injection-drop at all.
- At personal tier `bash` is an UNFENCED host shell, and the capability ledger tags `subprocess` as `untrusted_input` ONLY — justified by a comment asserting `--network=none`, which is false at personal tier. So `read` + `bash curl` never completes the trifecta and HumanGate never fires. A default-scaffolded agent ships ~35 LLM-callable tools with `allow = []` (allow-all) and 40 agentic turns per chunk. The ingest agent needs ZERO tools.
- The benchmark's scoring is not one number. Task-averaged accuracy is a MACRO mean of the six per-type accuracies; overall accuracy is the micro mean; abstention accuracy is reported separately. `single-session-preference`'s `answer` field is a RUBRIC, not a literal string, and gets its own judge prompt. `_abs` questions use a refusal-checking prompt and are EXCLUDED from retrieval scoring entirely.

### New Risks Discovered
- Measurement-corrupting input filter: `_INJECTION_RE` and `privacy_filter`'s `secret|password|token[:=]` pattern can delete gold evidence before it is ever stored, producing a real-looking low score for a crippled input path.
- Oracle-phase recency artifact: Oracle haystacks are unsorted, so the first phase of D-500 is the arm where the recency channel is actively misleading rather than merely uninformative.
- Cost blowout well past D-496's '~one LLM call per chunk': unbounded consolidation window (O(n^2)), a doomed 20k-token agentic attempt before every pipeline pass, full session history re-sent every turn if all chunks share one session key, plus unbounded `resolve_entity` disambiguation calls with no per-pass budget.
- Silent truncation of an oversized single turn is undetectable through the public path — `_capture` discards `capture()`'s return value (capabilities.py:231) — so D-495's 'never split mid-turn' has no enforcement and no alarm.
- Write-path temporal damage D-498 does not mention: all 40 sessions bucket into ONE `daily-log/<today>.md`, the day-summary prompt is handed ingest-clock HH:MM, and every `Fact.date` is stamped today — which degrades `knowledge-update`'s `| was:` contradiction trail, not just `temporal-reasoning`.
- Default-on scaffold modules (workpad context.md rewrite, policy eval, scheduler, skills sweep, messaging/tasks dialling NATS, shared arcstore) add unbudgeted LLM calls, latency, shared-state writes, and startup failures at 500x.
- Dataset version drift and known gold-label errors: the Sept-2025 'cleaned' revision is not numerically comparable to the original, and issues #37-41/#50 document relative-date and gold-answer mistakes still open.
- Comparability trap: published mem0 LongMemEval numbers come from marketing pages with undisclosed reader model and unconfirmed use of the official judge — they are not a valid target to beat.
- Self-modification tools (`create_tool`, `create_skill`, `update_tool`, `reload`) carry ZERO trifecta legs, and `~/.arc/capabilities` is a GLOBAL scan root shared by every agent — so a tool planted by one question is loaded by every subsequent question and persists on the host.
- 500 agents share `~/.arc/store` and one flocked WORM chain slugged from the agent NAME, so any parallelism raises `RuntimeError` and sequential runs still cross-contaminate the operational spool.
- The harness will be reused for email and Slack adapters (D-497), where the corpus genuinely IS adversarial. The tool-surface and boundary mitigations belong at the seam now, not retrofitted then — that retrofit is exactly the federal-preservation rule this project exists to avoid.
- No dataset integrity check is decided anywhere in D-492..D-500. A third-party download feeding a memory store is LLM04 territory; a SHA-256 in the run manifest, verified at preflight, closes it and makes runs reproducible at the same time.


## Memory Ingestion & LongMemEval Evaluation — Build Decisions (2026-07-30)

**Phase**: build | **Status**: complete | **Total decisions**: 9 (7 user, 2 auto-applied)
**ID range**: D-492 to D-500
**Priority framework**: simplicity → modularity → security → scalability

### Summary
An evaluations/ folder in the arc repo that bulk-ingests outside data through the existing, unmodified Arc agent + arcmemory stack, then measures memory quality against LongMemEval. A single ingest pathway with pluggable source adapters (longmemeval first; email and Slack to follow) feeds sessions to a real ArcAgent turn-by-chunk; arcmemory's own distill prompts and retrieval are what is under test. Zero framework changes.

### Research Insights

**From Solutions Archive:**
- MEMORY INDEX (project) — `arcmemory embedder silent-degrade`: a missing sentence-transformers install made dedup a silent no-op. Same failure class the D-500 preflight must catch; degrade LOUD.
- MEMORY INDEX (project) — `arcmemory distiller unwired`: `distill_provider` was unset in blueprints and scaffold, so consolidation was dead fleet-wide. This is the exact seam D-500's preflight exists for.
- MEMORY INDEX (project) — `Producers-unwired pattern`: Arc specs ship correct predicates with dead activating wiring. Demand an E2E-through-the-real-path assertion, never a self-report.

**Best Practices:**
- Pull `longmemeval_s_cleaned.json` / `longmemeval_oracle.json` explicitly and record the dataset sha256 + HF revision in every result row — the Sept-2025 'cleaned' revision changed session content to reduce cross-question interference and is NOT comparable to the original.
- Report three numbers the way the benchmark defines them: Task-averaged Accuracy (macro mean of the six per-type accuracies), Overall Accuracy (micro over all 500), and Abstention Accuracy (the 30 `_abs` questions, separately).
- State the reading strategy explicitly in results. The paper's Figure 6 shows a ~15-point QA-accuracy swing between 'NL + Direct' and 'JSON + Chain-of-Note' under ORACLE retrieval — same evidence, same reader. Prompt format alone moves the headline number more than most memory improvements will.

**Edge Cases:**
- `single-session-preference`'s `answer` field is a grading RUBRIC, not a literal answer string. Fuzzy-matching it silently misscores ~6% of the benchmark.
- Abstention (`_abs` id suffix, 30 questions) is not a 7th `question_type` — it is a cross-tag on the existing six. It uses a refusal-checking judge prompt, is folded into its base type for QA accuracy, and is EXCLUDED from retrieval scoring (`answer_session_ids`/`has_answer` are meaningless for it).
- Known open gold-label defects in the cleaned dataset: relative-date resolution off by a week and a Valentine's-Day session-date mismatch (issue #50), plus annotation-error reports #37-#41. Spot-check rather than assuming labels are clean.
- Session-count sources disagree: the repo README says ~40 history sessions for LongMemEval-S, the paper body says ~50. The ~115k-token figure is confirmed by both. A direct measurement in the first Oracle run settles it.

**Performance:**
- LongMemEval-S: ~115k tokens of history per question x 500 questions. LongMemEval-M: ~1.5M tokens per question, deliberately too large for direct long-context reading. Measured average over the first 50 S questions: ~49 sessions (range 41-57).
- The GPT-4o judge is a second cost layer on top of ingest and answering — 500 judge calls per phase, single-threaded in the reference script.

**References:**
- https://arxiv.org/abs/2410.10813 — LongMemEval (ICLR 2025)
- https://github.com/xiaowu0162/LongMemEval
- https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
- https://github.com/xiaowu0162/LongMemEval-V2 — agentic-context successor, closer to Arc's actual use case
- packages/arcmemory/README.md:278,374 — arcmemory's own 'no published benchmark yet' status

### Auto-Applied (Compliance Mandates)
| ID | Category | Decision | Mandated Answer | Citation |
|---|---|---|---|---|
| D-492 | Security | Dataset, workspaces, and credentials never committed or written in plaintext | LongMemEval JSON files, the 500 throwaway agent workspaces, and all run artifacts are gitignored. Judge and provider API keys are read from the environment only, never from a config file in the repo. | OWASP baseline (no compliance-mandates.json present; tech.md declares regime fedramp/nist but ships no mandate table) |
| D-493 | Security | Benchmark content is untrusted input at the memory boundary | No separate validation layer is added. Haystack text is untrusted third-party content and already crosses the existing trust boundary: FastCapture runs sanitize() then privacy_filter() then windowed dedup before anything is stored. Bypassing that path to ingest faster would both violate the boundary and invalidate the measurement. | OWASP baseline (input validation at trust boundaries); LLM01 prompt injection; arcmemory/capture.py |

### Research Insights

**From Solutions Archive:**
- .claude/solutions/security-issues/2026-04-18-ast-validator-is-not-enough-defense-in-depth.md — 'static validation is inherently incomplete.' `_INJECTION_RE` is seven literal English phrasings; it is exactly the single-layer static filter that solution warns against, and D-493 currently treats it as sufficient.
- .claude/solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md — injection-prevention and unicode-normalization patterns from the scheduler review apply verbatim to this ingest path.
- MEMORY INDEX (project) — `Trifecta is a context-resolved 3-leg model (SPEC-057)`: never bypass the gate. The finding below is that on a default agent the gate is not bypassed — it is simply never reached.

**Best Practices:**
- Give the ingest agent ZERO tools. Its job is 'read a chunk, say something short.' Set an explicit `[tools.policy] deny` list covering every builtin and module tool, `allowed_paths = []`, `egress_allowlist = []`, and belt-and-braces `[sandbox] allowed_tools = []` with `max_turns = 2` in `arcrun.toml`. Make the HumanGate unreachable by construction rather than tuning it.
- Disable `[modules.workpad]` and `[modules.policy]` for the eval. Both write LLM-derived text into the SYSTEM PROMPT every turn through a sanitizer with no injection filtering. This is a measurement decision as much as a security one: left on, the run measures arcmemory plus two other summarizers.
- Set `[tools.human_gate] timeout_seconds = 1.0` and `auto_approve = []`. A live approval channel is wired by default, so an unattended trifecta block POLLS for 300 seconds before denying. Never use `auto_approve` — the config permits listing the whole trifecta, which is precisely the wrong knob.
- Isolate the shared state: `ARCSTORE_DATA_DIR=<run_dir>/.arcstore` (env has highest precedence) or `[arcstore] enabled = false`; a unique `[agent] name` per question; an explicit workspace-relative `[security] policy_audit_log`.
- Set `[llm.modules.telemetry] store_raw_bodies = false` for the eval agent — it removes the largest plaintext artifact and gigabytes of disk.
- Run at `tier = "personal"`. Enterprise forces `custody = "vault_transit"`, adding a network round-trip per audit record; at personal tier signing is `in_process` (~50us x 40k ~= 2 seconds total). This is the one place where the lower tier is the right call — BECAUSE the zero-tool posture above removes what personal tier fails to fence.
- Keep `WormSink` (the default audit sink). Swapping to `NullSink` would be a framework change, and an eval that disables the audit chain no longer exercises the production surface it claims to measure. With no tools, the chain is nearly empty anyway.
- Checksum the dataset (SHA-256 in the run manifest, verified at preflight) and scrub every model-derived artifact field through `arcmemory.privacy_filter` before it reaches the results JSONL — including exception text, since provider error bodies echo request context.

**Edge Cases:**
- GITIGNORE — verified NOT IGNORED today: `evaluations/longmemeval/data/longmemeval_s.json`, `evaluations/runs/q1/workspace/memory/index.db`, `evaluations/runs/q1/traces/traces-*.jsonl`, `evaluations/results/run.jsonl`, `evaluations/runs/q1/.audit/trace-checkpoint.worm`, `evaluations/runs/q1/arcagent.toml`. Report only, not edited.
- GITIGNORE GOTCHA — git cannot re-include a file under an IGNORED DIRECTORY, so a broad `evaluations/**/runs/` permanently swallows anything beneath it and a `!` negation inside will not work. Keep harness code out of `runs/`. Also avoid a blanket `evaluations/**/*.toml` (it would swallow a checked-in template) — name `arcagent.toml`/`arcllm.toml`/`arcrun.toml` individually and keep `*.toml.example`. Note the existing `architecture/` and `**/architecture/` rules will silently swallow an `evaluations/architecture/` if one is ever created.
- ADD A REPO-ROOT GUARD to `.gitignore` (`/workspace/`, `/traces/`, `/.audit/`, `/capabilities/`) against the `ArcAgent(cfg)`-without-`config_path` leak, so the mistake is caught even if the harness makes it.
- `privacy_filter`'s six patterns cover OpenAI/GitHub/Slack/AWS/PEM/`key: value` but MISS `sk-ant-` (Anthropic), `AIza` (Google) and JWTs — patterns that exist in `arcllm/_secrets.py` but which arcmemory does not use. There is no entropy tier, so a bare unprefixed key is caught by neither.
- The secrets story is otherwise clean and D-492 is satisfied as shipped: `resolve_api_key()` goes vault -> env -> raise, the vault-fallback warning logs NAMES only, and the scaffold stores env-var names rather than values. One undocumented third source exists: `~/.arc/secrets/{name}` with 0600 enforced.
- Audit events are clean — `AuditEvent` carries `payload_hash` only, and `_emit_drop` explicitly hashes. But `_redact_sensitive` masks by KEY NAME only, so a secret sitting in a value under a benign key passes.
- `memory.captured` audit events are DISCARDED in the default wiring (`select_brain` is called without `audit_sink`), so `arcmemory/capture.py:10`'s claim that memory writes are audited does not hold in a real agent. A framework gap, not an eval problem — but it means the write path is unaudited during the run.
- An injected `schedule_create` would execute INSIDE the live run (min interval 60s, up to 50 schedules), and `messaging_send` to a third-party handle is one of the very few calls that WOULD trip the gate. Both vanish with the deny list.

**Performance:**
- Signed-chain cost is a non-issue at personal tier: one SHA-256 plus one Ed25519 sign per record, ~50us each, so 40k records is about 2 seconds. Rotation is fine (max_records 100_000, max_bytes 50MB). At enterprise `custody = "vault_transit"` the same 40k becomes 40k network round-trips.
- The real audit-adjacent cost is trace disk: 40k calls x (recall block + up to 2000-char chunk + response) with bodies on by default is conservatively several GB of plaintext JSONL.
- Telemetry volume: `memory.capture` and `memory.recall` emit ~2 log lines per chunk, so roughly 80k lines on a full-S run. Volume, not integrity.

**References:**
- packages/arcmemory/src/arcmemory/security.py:25-29, 33-43, 47-54, 58-82, 199-233, 236-250, 288-290
- packages/arcagent/src/arcagent/core/agent_dispatch.py:112-114, 132
- packages/arcagent/src/arcagent/utils/sanitizer.py:42-58 — no injection filtering on the system-prompt path
- packages/arcagent/src/arcagent/core/session_internal/context.py:45, 52, 57-59, 239-245
- packages/arcagent/src/arcagent/core/session_internal/capability_ledger.py:68-95, 113, 119
- packages/arcagent/src/arcagent/builtins/capabilities/bash.py:34-45; _runtime.py:533-538
- packages/arcagent/src/arcagent/core/agent.py:105-112, 198-209, 467
- packages/arcllm/src/arcllm/config.toml:29-33, 42-47; vault.py:110-157
- packages/arctrust/src/arctrust/audit.py:138-143, 159-170, 186-189, 220-221
- packages/arccli/src/arccli/commands/agent/_common.py:96-551 — the default tool and module inventory

### Architecture

#### D-494: Home: an evaluations/ folder, not a package
**Decision**: evaluations/ at the arc repo root, with evaluations/longmemeval/ beneath it. Plain scripts. No pyproject, no package, no install, no entry in the uv workspace. Nothing under packages/ imports it, so the dependency DAG is untouched and the framework never learns it exists.
**Priority**: Simplicity
**Alternatives**: A standalone sibling repo (rejected: re-solves installing 5+ fast-moving arc packages by hand for no benefit). A packages/arcbench/ package (rejected: puts a data-ingestion tool inside the framework's dependency DAG and buys quality gates the scripts do not need).
**Rationale**: The scripts are consumers of an already-built system. Living in-repo means they import arcmemory and arcagent from the workspace that is already installed, with zero install ceremony. 'The framework must not know about it' is satisfied by direction of dependency, not by physical distance.

#### D-495: Ingest granularity: session as one call, chunked on turn boundaries
**Decision**: One haystack session is fed as one logical unit. When the transcript exceeds MemoryConfig.max_event_chars (default 2000), it is split on turn boundaries into successive chunks and fed in order. Never split mid-turn.
**Priority**: Simplicity
**Alternatives**: Turn-by-turn replay (rejected: 5-10x the calls for granularity the session unit already preserves). Raising max_event_chars via backend.dynamics (rejected: chunking needs no config override at all). Leaving the 2000 cap with unchunked sessions (rejected: sanitize() silently truncates, so most evidence turns would be cut and the run would measure the cap, not the memory).
**Rationale**: Verified: FastCapture calls sanitize(text, max_length=cfg.max_event_chars) with a 2000-char default, so an unchunked session is silently truncated. Chunking on turn boundaries keeps every evidence turn intact with no framework or config change. Feeding the transcript as text also means the dataset's real assistant turns land in memory as content, so single-session-assistant evidence is preserved rather than replaced by anything the agent invents.

#### D-496: Ingest path: a real ArcAgent turn per chunk
**Decision**: Each chunk is fed via a real ArcAgent.run(). Full turn machinery: module bus, hook dispatch, memory recall at assemble_prompt, capture_user, the model call, and capture_respond. The agent's own reply is captured into the store as it would be in production. No framework change.
**Priority**: Modularity
**Alternatives**: Calling brain.capture() directly (rejected by the operator despite being byte-identical to the hook at capabilities.py:231, and free of both LLM cost and synthetic events). Adding a capture-only ingest entry to arcagent (withdrawn: framework change). Adding a capture_respond=false config flag (withdrawn: framework change).
**Rationale**: Operator's explicit call: the system must be exercised through the same surface a live agent uses, and the agent path is the product. Accepted costs, stated plainly: roughly one LLM call per chunk (~4,000 on the sampled S run, ~40,000 on full S), a memory recall pass per chunk, and one model-commentary event per chunk sitting in the store alongside real evidence where it can compete at recall time.

#### D-497: Source adapter seam: one read() contract, longmemeval first
**Decision**: Each source is one module exposing a read() that yields sessions of ordered turns with a source timestamp and a conversation id. The chunker, the agent driver, and the consolidation waiter are shared and source-agnostic. Only the longmemeval adapter is built now; email and Slack attach to the same seam later without touching the pipeline.
**Priority**: Modularity
**Alternatives**: Building email and Slack adapters now (rejected: YAGNI, and the seam is unproven until a second source exists). Emitting arcmemory Event objects directly (rejected: couples adapters to an internal type and skips the FastCapture security boundary).
**Rationale**: The operator's stated goal is one pathway into memory with many attachments. The pathway is the shared chunk-drive-consolidate loop; the attachment point is read(). Keeping the seam to a single method means a new source is one file and no pipeline edit.

### Research Insights

**From Solutions Archive:**
- .claude/solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md — injection-prevention, unicode-normalization and unbounded-resource-consumption patterns; the same `sanitize()` surface this harness feeds.
- .claude/solutions/security-issues/2026-02-21-arcrun-phase4-hardening-review-learnings.md — async/sync bridge, thread-safety and cleanup patterns for a long-running agent loop; directly applicable to per-question workspace teardown.
- MEMORY INDEX (project) — `contextvars sibling-task regression (build/bind split)`: build once at startup, `bind()` at the top of every turn-dispatch entry. Confirms the safe concurrency shape below.

**Best Practices:**
- Chunk at ~1700 chars, not 2000, and put a `\n` between every turn. The newline is a real defense: `_INJECTION_RE` destroys to end-of-line, so a newline confines the damage to one turn instead of the rest of the chunk. The margin absorbs NFKC expansion (normalization can LENGTHEN text) and the `[Session date: …]` prefix.
- Build a sanitize-fidelity gate in the adapter: import `sanitize` and `privacy_filter` from `arcmemory.security` (both are in `__all__`, no framework change), run every chunk through them locally before ingest, and assert output length ~= input length. Cross-check every shrink against that question's `answer_session_ids` / `has_answer` turns. If gold evidence was eaten, the question's result is VOID, not a memory failure. This is the single highest-value thing to build.
- Use one session key per chunk (`ingest:<session_idx>:<chunk_idx>`) and a separate key for the question turn. Memory scope is `agent_did`-only — `_capture` passes no `session_id` (capabilities.py:231) — so this costs nothing in recall and removes quadratic prompt growth and compaction entirely.
- Raise the recall envelope in the eval TOML (`top_k = 20`, `budget = 8000`) or the run measures `enforce_budget` rather than memory.
- Disable in the eval TOML: `[spawn]`, `[modules.workpad]`, `[modules.policy]`, `[modules.scheduler]`, `[modules.messaging]`, `[modules.tasks]`, `[modules.runcontrol]`, `[modules.skills]`, `[arcstore]`, telemetry trace export. Leave `[modules.memory]` and `[modules.memory_acl]` alone — those ARE the system under test.
- Give every question a unique `agent.name` (e.g. `lme-<question_id>`) AND an explicit workspace-relative `security.policy_audit_log`, or concurrent agents deadlock on the shared WORM chain.
- Concurrency shape: one `asyncio.Task` per question via `asyncio.gather` over `run_collected(...)` coroutines. NEVER manually interleave `agent.run()` async generators in a single task — async generators do not get isolated contexts in CPython, so `activate_runtime_bindings` from one clobbers the other mid-iteration.
- Bound the pool. Guard every provider call with an `asyncio.Semaphore(N)` sized against the actual tier's RPM, retry with capped exponential backoff plus jitter, honour `Retry-After`, and raise a distinct exception class for rate-limit vs hard failure. Unbounded `asyncio.gather` over 500 questions has no backpressure and turns one 429 into a cascade.
- Parallelize ACROSS questions, keep chunk ingestion SERIAL within a question — ordering matters to distillation and it keeps reproducibility simple.
- `ArcAgent.run()` is an async generator requiring a `SessionManager`, not a string. Use `run_collected(input_text, session_key=...)`; `startup()` must have run first.
- Cheapest correct throwaway construction is NOT `arc agent create` (it mints identities into `~/.arcagent/keys`, signs capabilities, and auto-registers over NATS). Replicate `_load_arcagent` + `_scaffold_workspace`: `render_agent_config(...)` -> scaffold -> `load_config` -> `ArcAgent(...)` -> `startup()`. Leave `identity.did = ""` so the key mints lazily.
- Recall does NOT require consolidation — `iter_source_chunks` indexes every raw episodic event 1:1 (index/source.py:42-75), so BM25/vector/graph recall works from turn one. Consolidation is what is under test, not what makes recall possible.

**Edge Cases:**
- `_INJECTION_RE` (arcmemory/security.py:33-43) deletes the match and `[^\n]*` after it. `"Congratulations! You are now a certified PM as of May 2023, and your ID is 88231."` becomes `"Congratulations!"`. `"I told my boss to forget everything about the old plan…"` becomes `"I told my boss to"`. Both verified against the live regex.
- `privacy_filter` (security.py:52) redacts `password|passwd|secret|api[_-]?key|token\s*[:=]\s*\S+`. Ordinary prose triggers it: `"My secret: I actually hate cilantro"` -> `"My [REDACTED] actually hate cilantro"`. It does NOT touch names, emails, phones or addresses — verified byte-identical passthrough — so most gold evidence survives.
- `sanitize()` hard-truncates at `max_length` with no ellipsis, no word boundary, no warning and no return signal (security.py:73). NFKC normalization runs FIRST and can expand length, so a 2000-char raw chunk can exceed 2000 post-normalize.
- `capture_respond` stores `chunk_text + "\n" + reply`, not the reply (capabilities.py:220-221, agent_dispatch.py:333-340). Under 2000 chars: every evidence chunk is stored twice and distillation sees it doubled. At/over 2000 chars: the joined text truncates back to the `user` event byte-for-byte, the Deduper drops it, and the reply is never stored at all — chunking exactly at the cap eliminates D-496's commentary-noise concern by accident.
- Dedup is exact SHA-256 equality over the last 128 captures (security.py:90-112), NOT similarity. Near-duplicate chunks from different sessions are never dropped. Non-risk for this workload.
- An oversized single turn is silently truncated and undetectable through the public path: `_capture` discards `capture()`'s return value (capabilities.py:231). Whether LongMemEval contains turns >2000 chars is UNMEASURED — the adapter must measure it before the Oracle run, because it decides whether D-495's 'never split mid-turn' is achievable at all.
- WORM flock: `_policy_audit_log_path` slugs `agent.name` into a SHARED `resolve_data_dir()/worm/audit-chain-<slug>.jsonl` and `WormSink` takes `flock(LOCK_EX|LOCK_NB)`, raising `RuntimeError: another writer holds …` (core/agent.py:183-209, arctrust/audit.py:238-250).
- `[arcstore] data_dir = ""` in the scaffold template points every agent at one shared `~/.arc/store` SQLite; `modules.messaging` and `modules.tasks` both dial `nats://127.0.0.1:4222` at startup. 500 agents = 500 connection attempts plus degrade timeouts.
- `Event` has no `source`, `role` or `origin` field. The ONLY discriminator between ingested benchmark text and the agent's own commentary is `kind`, and `curate_conversation_kinds` feeds BOTH `user` and `respond` to distillation — so nothing downstream can filter the commentary out.

**Performance:**
- Recall budget is the binding constraint: `top_k=5` but `budget=1024` tokens (modules/memory/config.py:41-42). A 2000-char event is ~525 tokens and `enforce_budget` counts the boundary-marked block, so exactly ONE 2000-char recall survives regardless of `top_k`.
- One session key for all 40 chunks re-sends the full history every turn (agent_dispatch.py:277) — quadratic prompt growth plus a compaction LLM call at the 0.85 threshold. Per-chunk session keys remove both.
- `[spawn] enabled = true` is the template default and costs a SECOND full `assemble_system_prompt` per turn.
- Two `brain.retrieve()` calls per turn by default — memory recall at `assemble_prompt`, plus the skills-improver `inject_insight` hook at `pre_respond`.
- Workpad's `context.md` rewrite fires `every_n_runs = 20`, so a 40-chunk loop triggers two extra background LLM calls per question before any of the eval's own cost.
- Benign shared state: arcllm's `_local_cache` loads the local embedding model once under a `threading.Lock` and encodes via `asyncio.to_thread` — parallel questions contend on CPU, not correctness.

**References:**
- packages/arcmemory/src/arcmemory/security.py:33-43, 47-54, 58-74, 90-112
- packages/arcmemory/src/arcmemory/capture.py:60, 64-71, 76, 80-89
- packages/arcagent/src/arcagent/modules/memory/capabilities.py:183-234
- packages/arcagent/src/arcagent/core/agent_dispatch.py:234-341
- packages/arcagent/src/arcagent/core/agent.py:183-209, 612-699
- packages/arcagent/src/arcagent/modules/memory/_runtime.py:96-99, 154-172
- packages/arctrust/src/arctrust/audit.py:238-250
- tests/architecture/test_no_module_global_agent_state.py — AST gate that fails the build on `global` reassignment in a `_runtime.py`

### Data Model

#### D-498: Source timestamps are carried in the chunk text, not the event ts
**Decision**: Each chunk is prefixed with its session date, e.g. '[Session date: 2023-05-14]', so the date enters memory as content. The stored Event.ts remains datetime.now(UTC). Logged as a known limitation.
**Priority**: Simplicity
**Alternatives**: Threading a ts parameter through Brain.capture and FastCapture.capture (rejected: framework change). Skipping the temporal-reasoning question type (rejected: discards a whole capability the benchmark exists to measure).
**Rationale**: Verified: FastCapture hard-codes Event ts to now(), and no public capture signature accepts a source timestamp. The text prefix makes the date reachable by both keyword and semantic recall with zero framework change, and matches what LongMemEval's own reference implementations do. Known limitation to carry into results: all 40 sessions in a haystack share a wall-clock ts, so recency ranking has no signal to work with, and any future email or Slack backfill inherits the same gap.

### Research Insights

**From Solutions Archive:**
- .claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md — a value that is constant for a run but consumed at many call sites must flow through construction. The source timestamp is exactly that value, and the fact that it cannot flow is what D-498 is working around.
- MEMORY INDEX (project) — `Trifecta is a context-resolved 3-leg model` and `arcmemory architecture`: structural/analogical retrieval is time-blind by design, which is why the recency channel carries disproportionate weight here.

**Best Practices:**
- Pin and RECORD the adapter's ingest order per run. The entire recency analysis depends on it, and LongMemEval-S is timestamp-sorted while Oracle is not — without the record, arms of any later ablation are not comparable.
- The LongMemEval reference keeps `haystack_dates` as a parallel metadata array and ships `src/index_expansion/temp_query_search_pruning.py`, which infers a time range from the query and FILTERS candidates. The authors measure +7-11% temporal recall from it. Zep's `add_episode(reference_time=)` and mem0's `client.add(timestamp=)` are both first-class parameters, explicitly motivated by data migration and consistent chronology.
- Report the temporal-reasoning number with the confound named in the same sentence, not in a footnote. Zep's advantage over mem0 on this exact sub-task is ~15 points and is attributed to timestamp modelling.

**Edge Cases:**
- CORRECTION to D-498's stated consequence: `datetime.now(UTC).isoformat()` has microsecond resolution, so the ~40 sessions share a wall-clock DAY, not a timestamp. Every event is strictly ordered — the recency channel carries a full, perfectly-ordered signal that happens to equal INGEST ORDER. 'Recency ranking has no signal' is false; the accurate statement is that recency ranks by ingest order.
- On LongMemEval-S/M, `haystack_session_ids` are timestamp-sorted, so ingest order = chronological and the recency channel is ACCIDENTALLY CORRECT — any temporal score partly rides an artifact you did not design. On Oracle, sessions are explicitly unsorted, so recency hands up to 25% of max score to an arbitrary handful of sessions. D-500 runs Oracle first.
- Write-path damage D-498 does not mention: `by_day[event.ts[:10]]` (consolidate.py:394) collapses all 40 sessions into ONE `memory/daily-log/<today>.md`; the day-summary prompt explicitly demands an HH:MM chronological timeline and is handed the ingest wall clock; `Fact.date` defaults to today (types.py:92), so the `| was:` contradiction trail loses date discrimination — that hits `knowledge-update`, not just `temporal-reasoning`.
- Decay is inert: all `last_hit` = today, so `elapsed_days ~= 0`, `exp(0) = 1`, and `edges_decayed` is always 0. The forgetting path is untested in every eval run and that metric is meaningless.
- BM25 tokenizer mismatch: `fts5` indexes `[Session date: 2023-05-14]` as `session/date/2023/05/14` (default unicode61), but the QUERY tokenizer strips non-alphanumerics WITHIN a word, so a query containing `2023-05-14` becomes the single term `20230514` — a token that does not exist in the index. And since every chunk carries a date prefix, `2023`/`05`/`14` have near-zero IDF anyway.
- Graph channel is effectively unreachable by a date: `_phrase_regex` rewrites `-` to a space, so a slug `2023-05-14` compiles to `\b2023\ 05\ 14\b` and will not match the hyphenated text. The structural/analogical channel has no ts or date handling at all.
- `rebuild_index()` deliberately writes `mtime = None` (index/rebuild.py:127-128,144), and `index_if_needed` only rewrites content-hash-changed chunks — so after any rebuild the recency list silently collapses to a lexicographic `chunk_id` ordering and stays that way.
- `Event` has no metadata dict, no tags and no note field. `refs: list[str]` is persisted and re-hydrated but never written and never read — a dead field, and not settable through `capture()` anyway. `kind` IS free-form and settable, but a custom kind is silently dropped from distillation unless `curate_conversation_kinds` is also overridden.

**Performance:**
- Recency is an unweighted 4th list in RRF: `[bm25, graph, recency]` plus `vec` when available, fused at `1/(60+rank)` with no weights (index/surface.py:178-182, fusion.py:13,29). Max single-list contribution 1/60 = 0.01667; max total 0.0667. The recency list alone can supply 25% of the maximum achievable score, and a rank-0-by-recency chunk gets exactly as much as the top BM25 hit.
- `_recency_order()` is an UNBOUNDED `SELECT … ORDER BY COALESCE(mtime,0) DESC` over every chunk in the scope. With ~500 chunks per haystack (250 event chunks, doubled by D-496's per-chunk commentary), contributions span 1/60 down to 1/559 — a ~9x spread, but concentrated: only the ~60 most recently ingested chunks (roughly the last 5 sessions) get a boost comparable to a strong lexical hit.
- The embedder embeds the WHOLE chunk as one vector, so a ~25-char date prefix inside a 2000-char chunk is ~1% of the token mass — and dense embeddings are weak at exact date discrimination regardless. Expect near-zero contribution from the vec channel.

**References:**
- packages/arcmemory/src/arcmemory/types.py:62-79, 92, 112, 212-216
- packages/arcmemory/src/arcmemory/index/surface.py:100-117, 159-164, 178-182, 240-247, 300-308
- packages/arcmemory/src/arcmemory/index/rebuild.py:127-128, 144
- packages/arcmemory/src/arcmemory/index/graph.py:83, 107, 185-218
- packages/arcmemory/src/arcmemory/consolidate.py:187, 394
- https://github.com/xiaowu0162/LongMemEval — `src/index_expansion/temp_query_search_pruning.py`
- https://arxiv.org/abs/2501.13956 — Zep temporal knowledge graph (temporal-reasoning 62.4% vs 45.1% full-context)
- https://docs.mem0.ai/platform/features/timestamp
- https://help.getzep.com/graphiti/core-concepts/adding-episodes

### Integration

#### D-499: Consolidation fires per session, and the harness waits for it
**Decision**: The eval agent's toml lowers the consolidation triggers so a pass fires at each session boundary and/or every 60 seconds. Critically, BOTH rate limits are lowered: the agent module's consolidate_event_threshold / consolidate_idle_seconds / consolidate_interval_seconds, AND arcmemory's own consolidate_interval_minutes (default 60) via backend.dynamics. The harness then waits for quiescence before asking the question rather than assuming the pass completed.
**Priority**: Security
**Alternatives**: Production cadence plus one final flush (rejected: operator wants per-session distillation). One consolidation at the end (rejected: a single window over 40 sessions is far outside the incremental regime the distill prompts were written for).
**Rationale**: Consolidation is where arcmemory's distill prompts run, so it is the thing under test; if it does not fire, the run measures raw episodic recall and silently reports it as memory quality. Verified failure mode: brain.consolidate() gates on consolidator.due(now, interval_minutes=cfg.consolidate_interval_minutes), so polling every 60 seconds while that inner limit sits at 60 minutes returns an empty result every time. Lowering one limit without the other is a silent no-op.

### Research Insights

**From Solutions Archive:**
- .claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md — the two-layer config split that makes lowering one limit a silent no-op is the same defect class: one reader saw the real value, another saw a fallback.
- MEMORY INDEX (project) — `Cadence counters must persist`: in-memory 'every N turns' gates are defeated by restarts. For a single long-lived eval process the polarity INVERTS — see edge cases.
- MEMORY INDEX (project) — `SHIPPED: agentic consolidation on arcrun`: the sleep pass is a bounded arcrun ReAct agent with a pipeline fallback. That fallback is where the cost lands here.

**Best Practices:**
- Drive consolidation directly. `consolidate_poll_once()` is public and exported in `capabilities.__all__`; awaiting it returns only after the whole pass completes, returns a bool telling you whether the gates opened, and propagates exceptions to the harness instead of the loop swallowing them into a WARNING. This is not a framework change — it is calling a public function, and it is the ONLY reliable quiescence signal that exists today.
- Set FIVE knobs, not two: `brain = "arcmemory"`, a non-empty `distill_provider`, `consolidate_event_threshold`, `consolidate_idle_seconds`, `consolidate_interval_seconds`, AND `[modules.memory.config.dynamics] consolidate_interval_minutes = 0.0`. `distill_provider` outranks all of them — unset, there is no consolidator at all and every other setting is theatre.
- Do NOT run both the background loop and manual calls. There is no lock anywhere in arcmemory (`grep -rn 'asyncio.Lock' packages/arcmemory/src` returns zero hits), so two passes can interleave on the same SQLite connection and the same `.consolidate-manifest.json`. Either leave the arcagent thresholds at production values and force passes yourself, or accept the loop — never both.
- Belt-and-braces quiescence: read `<workspace>/memory/.consolidate-last-run` before and after and assert it advanced, and assert `.consolidate-manifest.json` is absent afterwards.
- Attach a `logging` handler to `arcmemory.consolidate` at WARNING for the whole run — it is the ONLY channel on which `dedup_skipped` and the degrade warnings are visible, because the audit sink is null.
- Consider `consolidate_engine = "pipeline"` in `dynamics` for the eval, to avoid paying for a doomed 20k-token agentic attempt before every pipeline pass — and RECORD that choice, because it changes what is being measured.
- Extend the D-500 preflight to a live end-to-end assertion in a scratch workspace: `_runtime.state().brain` is not a `NullBrain`; `brain._embedder`, `brain._distiller` and `brain._model` are all non-None after startup; one forced `consolidate_poll_once()` returns True; `.consolidate-last-run` advanced; the returned `episode_summary` reports `window_events > 0`; and a `memory/daily-log/*.md` exists. `arc agent build --check` tests none of this.

**Edge Cases:**
- D-499's cadence is unreachable by config: `_CONSOLIDATE_POLL_INTERVAL = 300.0` is a module constant (modules/memory/capabilities.py:35, 261, 271). The TOML knobs only gate the predicate INSIDE `consolidate_poll_once`. Forty session boundaries at 300s each is 3.3 hours of wall-clock waiting per question.
- `hygiene_due` SHORT-CIRCUITS the interval gate: `run_hygiene` calls `self.run(now=now)` directly without consulting `due()`. In a fresh per-question workspace the FIRST `consolidate()` always does real work; only calls 2..N are gated by `consolidate_interval_minutes`.
- Counter persistence inverts for a long-lived eval process. The in-memory arcagent counters (`events_since_consolidate`, `last_activity`) are the RELIABLE half — they never reset mid-run. The PERSISTED half is the dangerous one: `.consolidate-last-run` survives and suppresses the next 59 minutes of passes. D-500's throwaway-workspace-per-question isolates this.
- `events_since_consolidate = 0` executes AFTER the await, so every capture landing during a multi-minute pass is zeroed and never counts toward the next threshold.
- Silent no-op paths that never raise: empty `distill_provider` (no consolidator, empty result, no log, no audit); un-lowered `consolidate_interval_minutes` (empty result, no log); an empty curated window still STAMPS `.consolidate-last-run`, blocking the next pass for the full interval; `_parse` returns `{}` on any non-JSON completion so zero facts are extracted with no error; unavailable embeddings make `merge_cues` a silent `[]`.
- Every arcmemory-internal audit event is discarded. `_runtime.configure` calls `select_brain(...)` WITHOUT `audit_sink=`, so `_audit` falls back to `NullSink` — `memory.consolidation_degraded`, `memory.dedup_skipped`, `memory.fact_updated` and `memory.entity_merged` all vanish in a live agent. This is a latent framework observability bug worth its own ticket.
- `merge_entities()` runs inside EVERY consolidation pass, not nightly (consolidate.py:201). It needs BOTH an embedder and a confirmer (the distiller); without either it emits `dedup_skipped` and returns `[]`. Only the deterministic alias merge, backlink repair and workspace dedup are hygiene-only (once per local day).
- A run crossing local midnight escalates to the heavier full-hygiene pass instead of a normal consolidation. Unquantified cost; a long full-S run will hit it.
- `.consolidate-last-run` is workspace-scoped while `Consolidator` instances are per-Scope — the first scope to run blocks the others for the interval. Irrelevant to D-500's one-workspace-one-question design; a landmine for anything that shares a workspace.
- Module configure failure is FAIL-OPEN: `configure_module_runtimes` catches and continues, so a `dynamics` value that fails pydantic validation degrades to `NullBrain` and the agent starts normally with memory entirely off.
- `docs/config-reference.md:59-63` documents `curate_keep_tools` / `curate_min_substantive_chars` / `curate_tool_requires_entity` / `curate_tool_keep_salience`, none of which still exist in `arcmemory/config.py`. Anyone tuning curation from the docs is silently ignored.

**Performance:**
- COST BOMB: `Consolidator.run()` uses `window = TimeWindow()` with `start=None, end=None`, so every pass re-reads the ENTIRE episodic stream (consolidate.py:185-187; `EpisodicStore.events` is an unbounded `SELECT … ORDER BY seq`). With per-session consolidation over 40 sessions this is O(n^2) in LLM cost. A `start = last_run()` bound would fix it.
- The agentic engine is bounded at `max_turns=16`, `max_tokens=20_000` (cumulative input+output), `timeout=180s`. Rendering the whole stream into one task string breaches 20k tokens by roughly session 3 — so it degrades and the FULL PIPELINE runs anyway. You pay one large wasted call before every pass.
- Pipeline calls per pass, C = chunks in window, D = distinct days, F = fact candidates: `extract_facts` 1xC, `mint_insights` 1xC, `extract_procedures` 1xC, `summarize_day` 1 per day (D ~= 1 given D-498), `confirm_entity_merges` 1 total, plus an embedding call per fact candidate — and `resolve_entity` disambiguation at up to 1 call PER FACT CANDIDATE, UNBOUNDED. There is no per-pass call-count or cost budget on the pipeline path.
- There is no lock and no serialization: the background loop is serial with itself, but a harness that also calls `consolidate_poll_once()` can interleave two passes against the same SQLite connection and the same manifest file.

**References:**
- packages/arcagent/src/arcagent/modules/memory/capabilities.py:35, 232, 261-271, 274-318
- packages/arcagent/src/arcagent/modules/memory/config.py:46-50, 67-68
- packages/arcagent/src/arcagent/modules/memory/_runtime.py:85-87, 127-136
- packages/arcmemory/src/arcmemory/brain.py:178-202, 269-287
- packages/arcmemory/src/arcmemory/consolidate.py:154-155, 171-179, 185-194, 201, 220-259, 272-296, 495-515, 636-679
- packages/arcmemory/src/arcmemory/config.py:102-104, 111-113, 158-162
- packages/arcmemory/src/arcmemory/provider.py:46-52, 57, 100-114
- packages/arcrun/src/arcrun/strategies/react.py:60-66, 435-441

### Testing

#### D-500: Run scale, isolation, and what is measured
**Decision**: One clean throwaway workspace per question, never shared. Phased: LongMemEval-Oracle across all 500 questions first, then a stratified sample of LongMemEval-S (~50 questions covering all six types), then full S only once the harness is clean. Both benchmark metrics are recorded per question: QA accuracy judged by GPT-4o via the benchmark's evaluate_qa.py, and turn-level plus session-level memory recall accuracy from the dataset's has_answer flags and answer_session_ids. Results are JSONL keyed by question_id, resumable so an interrupted run continues. A preflight hard-fails if the embedder or distiller seam is unwired.
**Priority**: Security
**Alternatives**: A single shared workspace across all 500 haystacks (rejected: each instance is a different persona, so merging them makes consolidation fuse 500 contradictory identities and directly poisons the knowledge-update question type). Straight to full S (rejected: ~40,000 ingest calls before the first answer, paid twice on any harness bug). QA accuracy alone or recall alone (rejected: the benchmark defines both, and either alone turns a failure into a single bit that cannot be attributed).
**Rationale**: Per-question isolation is what LongMemEval defines and what keeps a miss attributable. Oracle first buys a near-free end-to-end proof of ingest, consolidation, recall, answer, and judge before any real spend. Recording both metrics splits a failure into 'memory never surfaced it' versus 'memory surfaced it and the answer was still wrong', which are different bugs with different fixes. The preflight exists because both seams degrade silently today: embed_backend='none' drops recall to BM25 plus graph, and an empty distill_provider makes consolidation a no-op, either of which would produce a real-looking number for a crippled system.

### Research Insights

**From Solutions Archive:**
- .claude/solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md — unbounded resource consumption and circuit-breaker patterns; the spend ceiling below is the same control applied to token cost.
- .claude/solutions/security-issues/2026-02-21-arcrun-phase4-hardening-review-learnings.md — cleanup patterns; the `finally`-scoped workspace teardown below comes straight from it.
- MEMORY INDEX (project) — `Producers-unwired pattern`: demand E2E-through-the-real-path tests and never trust a worker's self-report. The live preflight is the concrete form of that rule here.

**Best Practices:**
- Make the QUESTION the atomic unit of resume, not the chunk. Write the result row only after ingest -> query -> judge all complete, with a terminal `"status":"complete"` field. A question is done iff a line for its `question_id` exists with that status; anything else — missing, partial, or unparseable — is rebuilt from scratch.
- Mark workspace readiness with an `.ingest_complete` marker written LAST. On resume, a workspace lacking it is garbage: delete and rebuild, never resume mid-ingest. Chunk-level LLM calls are order-sensitive and not naturally idempotent.
- Append-only, never read-modify-write. Build the done-set once at startup by reading the JSONL into a dict, keep the handle open in append mode, and never seek. Tolerate a truncated final line on load — that is the normal SIGKILL signature.
- `f.write(); f.flush()` per line is cheap; `os.fsync()` only every N completed questions or at clean shutdown. Note the reference `evaluate_qa.py` does neither and can lose its last buffered lines.
- Pin the judge by DATED model string (`gpt-4o-2024-08-06`, never the `gpt-4o` alias), `temperature=0`, `max_tokens=10`, and record the string in every result row. The alias silently repoints and breaks comparability between runs months apart. There is NO `seed` parameter in the real script — do not rely on one.
- Instantiate a SEPARATE client for the judge, with its own key variable and its own module. Never share a client, config dict or `messages` list between the system under test and the judge.
- Log judge disagreement as a first-class metric: double-judge a fixed sample or the borderline cases, log the agreement rate, and keep old labels in the row when the judge prompt or model changes so old and new can be diffed rather than silently overwritten.
- Ship a `--dry-run` / `--estimate` mode that walks the dataset through the REAL chunker (not `len(text)/4`), sums estimated tokens, and multiplies by a versioned pricing table kept in config. At ~40k calls a 20-30% token-count error compounds into a meaningfully wrong ceiling.
- Enforce a hard spend ceiling as a circuit breaker that ABORTS, not warns, at ~110% of the dry-run estimate — and persist the running total so it survives resume.
- Gate `--full` behind a `--smoke N` (3-5 questions across types) that runs ingest -> query -> judge end to end.
- Log `{tokens_in, tokens_out, cost_usd, n_llm_calls, wall_seconds}` per question. A median-cost diff against the prior run catches an ingest-cost regression before it burns the full-S budget.
- Every result row carries: `git_sha` (with dirty flag), `harness_version`, `config_hash` (sha256 of the canonicalized RESOLVED config, since env overrides matter), `dataset_sha256`, `agent_model_id`, `judge_model_id`, `embedder_model_id`, `distiller_model_id`, `run_timestamp_utc`, `question_id`. Write a `run_manifest.json` too, but keep the block redundantly in every row so a single row is self-describing after rows are concatenated across runs.
- Teardown runs in a `finally`/context manager, and the harness refuses to start a phase if leftover workspaces from an aborted run exceed a threshold — 'throwaway' workspaces that are never thrown away are the standard way these harnesses fill a disk.

**Edge Cases:**
- `print_qa_metrics.py` hard-asserts the judge string is `gpt-4o-2024-08-06` and CRASHES on any other judge (open issue #47). gpt-4o and gpt-4o-mini as judges agree only ~85.7%, with mini stricter — accuracy is not comparable across judge choices.
- `temperature=0` does not make the judge deterministic. Recent work shows 1-2 of 7 borderline items still flip under forced greedy decoding. Do not build an acceptance gate on a single judge call for borderline cases.
- `_abs` questions must be EXCLUDED from retrieval scoring (the reference filters `if '_abs' not in x['question_id']`) but INCLUDED in QA accuracy under a refusal-checking prompt. Running the standard 'does the response contain the answer' grader on them misscores all 30.
- Retrieval metrics are `recall_any@k` and `recall_all@k` — the default print script reports `recall_all@5`/`recall_all@10` at session level and adds `@50` at turn level. 'Recall' without the any/all qualifier is ambiguous and not comparable.
- 'Treating workspace-exists as question-complete' is the classic resume bug: a crash mid-ingest leaves a plausible-looking directory missing chunks, and it silently produces a worse answer on resume.
- SQLite WAL/SHM siblings are not removed by deleting the `.db`. Checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`) or close cleanly before deleting, and delete the whole workspace directory — a stray `-wal` from a crashed run can exceed the main db file.
- The three published-baseline traps: mem0's LongMemEval numbers come from marketing pages with undisclosed reader model and unconfirmed official-judge use; the benchmark's own issue tracker is full of unvetted self-reported 89-99% results; and none are comparable without matching dataset variant, judge model, reader LLM, and whether the official scripts were used verbatim.

**Performance:**
- Measured type distribution of LongMemEval-S (500q): multi-session 133, temporal-reasoning 133, knowledge-update 78, single-session-user 70, single-session-assistant 56, single-session-preference 30. Proportional 50q stratification gives roughly 13/13/8/7/6/3.
- Statistical floor for D-500's ~50q phase: at n<10 a Wilson interval is not stable; at n=13 with 10/13 correct the 95% CI is ~[50%, 92%]; at n=30 with 24/30 it is ~[62%, 91%]. You need n>=30 per stratum before the interval is narrow enough (~+/-15-17pp) to separate systems differing by less than ~20 points. At 50 total across 6 strata, only the POOLED accuracy supports a confident claim — per-type numbers are directional and must be printed with their CIs. Reserve per-type claims for full S (n=56-133 per stratum, ~+/-7-12pp).
- Disk: ~150-300KB raw session JSON + ~1.2-3MB of vectors (200-500 chunks x 1536-dim float32) + 1.5-2x sqlite/WAL/index overhead = roughly 3-8MB per question workspace, so ~1.5-4GB for 500 — fine on a laptop IF WAL files are checkpointed on teardown.
- Keep per question (a few KB each, cheap for all 500): the raw judge prompt and response, the final agent answer, the full result row, and a manifest of ingested chunk ids + hashes. Delete on success: the sqlite db, WAL/SHM and raw embedding vectors — they regenerate from the manifest. Keep them only for failed or judge-disagreement questions behind a `--keep-workspace-on-failure` flag.
- Paper baselines for orientation (GPT-4o judge): GPT-4o Oracle 0.870 (0.924 with Chain-of-Note) vs LongMemEval-S full-history 0.606 (0.640 with CoN) — a ~30% relative drop. Zep on S: 71.2% with gpt-4o vs 60.2% full-context. Commercial products in the paper's own pilot: ChatGPT+GPT-4o 0.5773, Coze+GPT-4o 0.3299.

**References:**
- https://raw.githubusercontent.com/xiaowu0162/LongMemEval/main/src/evaluation/evaluate_qa.py
- https://raw.githubusercontent.com/xiaowu0162/LongMemEval/main/src/evaluation/print_qa_metrics.py
- https://raw.githubusercontent.com/xiaowu0162/LongMemEval/main/src/retrieval/eval_utils.py
- https://github.com/xiaowu0162/LongMemEval/issues/47 — print_qa_metrics crash on non-gpt-4o judges
- https://github.com/xiaowu0162/LongMemEval/issues/50 — relative-date / gold-answer inconsistency
- https://arxiv.org/html/2606.26185v1 — temperature control is necessary but not sufficient for judge reproducibility
- https://sqlite.org/tempfiles.html — WAL/SHM lifecycle
- https://blog.getzep.com/state-of-the-art-agent-memory/
- https://mem0.ai/research — marketing-sourced LongMemEval numbers, methodology undisclosed

### API Design

_(no decisions in this category for this feature)_

### Observability

_(no decisions in this category for this feature)_

### Audit & Compliance

_(no decisions in this category for this feature)_

### Performance

_(no decisions in this category for this feature)_

### Extensibility

_(no decisions in this category for this feature)_

### Deployment

_(no decisions in this category for this feature)_

### UI/UX

_(no decisions in this category for this feature)_


### Open Questions
- Does the operator have an OpenAI API key available for the GPT-4o judge? The benchmark's evaluate_qa.py requires it, and comparability to published numbers depends on using the same judge. To be verified before the Oracle run.
- Model-commentary events from D-496 sit in the store alongside real evidence and may compete at recall time. Whether this measurably moves scores is unknown until the Oracle run; if it does, the ingest agent's reply handling is the first thing to revisit.
- Event.ts is uniform across a haystack (D-498), so recency ranking contributes nothing. If temporal-reasoning scores come back poor, this is the first confound to rule out before concluding memory is weak.
- Whether the sampled-S stratification should weight question types evenly or match the full set's natural distribution. Deferred until Oracle results show which types are weakest.

### Research Insights

**From Solutions Archive:**
- MEMORY INDEX (project) — `Embedder silent-degrade` and `arcmemory distiller unwired` both resolved as 'the seam was dead and nothing said so'. Every open question below that ends in 'we will find out at the Oracle run' should instead be an assertion in the preflight.

**Best Practices:**
- Answer the two cheapest open questions BEFORE the Oracle run rather than after: (a) measure the LongMemEval turn-length distribution to learn whether any single turn exceeds `max_event_chars` (decides whether D-495's 'never split mid-turn' is even achievable); (b) run every chunk through `sanitize` + `privacy_filter` locally and count how often gold evidence shrinks (decides whether any score is trustworthy at all).
- The D-496 commentary-noise question is partly self-answering: at chunk sizes >= `max_event_chars` the joined user+assistant `respond` event truncates back to the `user` event and is deduped away, so no commentary event is stored. Below that size, every evidence chunk is stored twice. Choose the chunk size deliberately and record which regime the run is in.

**Edge Cases:**
- NEW — does the answering prompt tell the model what 'today' is, and what date does it give? If the answerer believes it is today and every `Fact.date` is stamped today, `temporal-reasoning` is unanswerable regardless of retrieval quality. This may dominate every other temporal confound listed here.
- NEW — the D-498 recency confound is Oracle-specific in DIRECTION. Oracle haystacks are unsorted, so the first phase of the plan is the arm where ingest-order recency is actively misleading; on S it is accidentally helpful. Do not read the Oracle temporal number as a memory-quality signal.
- NEW — is `arcllm`'s SPEC-038 budget / circuit breaker capable of tripping mid-run and turning distillation into a silent no-op? Not traced. A long full-S run is exactly the workload that would trip it.
- NEW — does `_current_did` stay bound if the harness wraps `run()` in `asyncio.create_task`/`gather`? The binding lives in the child task, so a parent-task `consolidate_poll_once()` would raise `MemoryIsolationError`. Verify empirically or call `activate_runtime_bindings(agent)` immediately before.
- RESOLVED — 'does the operator have an OpenAI key for the GPT-4o judge'. The requirement is sharper than logged: it must be `gpt-4o-2024-08-06` specifically, because `print_qa_metrics.py` hard-asserts that exact string and any other judge both crashes the script and breaks comparability. Still open underneath it: is the judge called in-process through arcllm (so its prompts land in the trace store) or by shelling out to `evaluate_qa.py` (so they land wherever that script writes)? Different artifact surfaces, different redaction requirements.
- NEW — sequential or parallel across the 500 questions? D-500 does not say, and the answer changes the required config: parallel makes per-run `ARCSTORE_DATA_DIR` and unique agent names MANDATORY rather than advisory, because of the flocked WORM chain.
- NEW — where exactly do run dirs live? `evaluations/longmemeval/runs/<qid>/` is assumed throughout this research, and the gitignore patterns depend on it. Not stated in D-494 or D-500.
- NEW — which tier does the eval agent run at? `MemoryConfig.for_tier` changes `alpha` and the other write/decay dynamics, and federal 'writes slower'. Whatever is chosen, the LongMemEval number is TIER-SPECIFIC and must be reported as such. Not decided anywhere in D-492..D-500.
- NEW — disabling `workpad` and `policy` is a validity decision, not only a security one. Left on, the run measures arcmemory plus two other LLM summarizers writing into the system prompt; turned off, the setup deviates from 'a real production agent', which is D-496's whole premise. This needs an explicit call, recorded.

**Performance:**
- Two cheap diagnostics that split 'memory is weak' from 'the harness confounded it', both runnable without a framework change: (1) use the recall-vs-QA split D-500 already records — temporal recall normal but QA low means the WRITE path (collapsed daily-log, facts stamped today) is the confound, not ranking; (2) `await brain.rebuild_index()` NULLs `chunks.mtime`, collapsing the recency list to a constant ordering, so re-running only the query step with and without it ablates the recency channel on an already-ingested workspace at zero re-ingest cost.
- The stronger 2x2 ablation, harness-side only, on ~20 temporal questions: prefix on/off x dataset-order/shuffled, plus an oracle-ts arm that writes the true `haystack_date` into `episodic.ts` AND `chunks.mtime` with two SQL statements. That last arm is the ceiling a real timestamp would buy and is the number that decides whether to revisit D-498. Run the same arms on `single-session-user` as a control — if the deltas are the same size there, it is generic retrieval noise.

**References:**
- packages/arcmemory/src/arcmemory/index/rebuild.py:144 — `rebuild_index()` NULLs mtime, which is what makes the free recency ablation possible
- https://arxiv.org/abs/2410.10813 — paper section 5.4 on time-agnostic memory designs
- https://github.com/xiaowu0162/LongMemEval/issues/50

### Related Solutions
_(none)_

