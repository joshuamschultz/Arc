# arcui-llm-telemetry — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-037–D-071 (35 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## ArcUI LLM Telemetry — Build Decisions (2026-03-01)

**Phase**: build | **Status**: complete | **Total decisions**: 27 (21 user, 6 auto-applied)
**Priority framework**: simplicity > security > scalability > compliance
**Brainstorm**: inline conversation (no file — brainstorm covered architecture, persistence, control plane)

#### Summary

ArcUI is a new subpackage (`packages/arcui/`) providing browser-based monitoring and control for ArcLLM (and eventually the full ArcMAS stack). Architecture: Starlette + uvicorn serves static vanilla HTML/CSS/JS + WebSocket + REST endpoints. ArcLLM gains three new capabilities: TraceStore (JSONL default, SQLite optional, hash-chained for tamper evidence), `on_event` callback (matching ArcRun's pattern), and ConfigController (runtime get/set). arcUI attaches to live ArcLLM instances via `attach_llm()`, collects events, computes server-side rolling aggregates, and streams to browser over WebSocket. Two auth roles (viewer/operator). Default telemetry page shows live stream + stat cards + cost breakdowns.

#### Research Insights (from /deepen — 6 parallel agents)

##### Architecture & Integration Patterns (Codebase Analysis)

**Existing code to match/reuse:**
- `arcrun/events.py:EventBus` — Hash chain with `GENESIS_PREV_HASH = "0"*64`, `_canonical_bytes()` uses `json.dumps(sort_keys=True, separators=(",",":"))`, SHA-256 via `hashlib`. Thread-safe with `threading.Lock`. `on_event` callback fires OUTSIDE the lock. `verify_chain()` does three-part check: self-hash recompute, prev-hash linkage, sequence contiguity.
- `arcrun/loop.py` — `on_event: Callable[[Event], None]` wired at `_build_state()`. Use same signature for ArcLLM.
- `arcllm/modules/telemetry.py:TelemetryModule` — Computes duration_ms, cost_usd, all usage fields. `BudgetAccumulator` with `deduct()`, `check_limits()`. Shared registry keyed by `budget_scope`.
- `arcllm/modules/base.py:BaseModule` — `_inner` chain pattern, `_span()` for OTel spans. New TraceStore hooks should wrap similarly.
- `arcagent/core/module_bus.py:ModuleBus` — Async pub/sub, priority-grouped dispatch, `EventContext` with veto. Bridge maps arcrun events to agent events.
- `arcagent/core/agent.py:create_arcrun_bridge()` — Maps `tool.start→agent:pre_tool`, etc. Uses `loop.create_task()` with `_pending` set for GC. Same pattern for `create_arcllm_bridge()`.

**Key insight**: The on_event callback pattern is already proven in ArcRun. ArcLLM's implementation should mirror it exactly — optional param on `load_model()`, fires after state mutation, never blocks the caller.

##### Data Model — JSONL Hash Chain Best Practices

**Canonical JSON**: Use `jcs` library (RFC 8785) for deterministic JSON serialization instead of `json.dumps(sort_keys=True)`. RFC 8785 handles Unicode normalization, number serialization edge cases (e.g., `-0` → `0`, `1e2` → `100`), and key ordering more rigorously than stdlib.

**Performance**: `orjson` is 3-10x faster than stdlib `json` for serialization. `orjsonl` provides streaming JSONL append/read. Consider for high-throughput traces.

**Rotation continuity**: Use rotation tombstone records — last record in a day's file contains `{"type":"rotation","next_file":"traces-2026-03-02.jsonl","chain_hash":"<last_hash>"}`. Next file's first record references this. Unbroken chain across files without separate state file.

**Alternative to chain-state.json**: The tombstone approach is more robust than a separate pointer file because chain-state.json can get out of sync if process crashes between writing a trace and updating the pointer. With tombstones, the rotation record IS part of the chain.

**NIST AU-9 compliance checklist**: (1) append-only file mode, (2) hash chain with cryptographic binding, (3) manifest sidecar for quick integrity verification, (4) rotation with chain continuity, (5) alerting on chain break detection.

##### API Design — Starlette WebSocket Patterns

**ConnectionManager pattern**: Per-client `asyncio.Queue` (bounded, maxsize=1000) instead of direct `websocket.send()`. Prevents slow clients from blocking the event loop. If queue is full, drop oldest or disconnect.

**EventBuffer**: Bounded `collections.deque(maxlen=N)` for 100ms batched flush. Accumulate events, flush in a single WebSocket frame via `asyncio.sleep(0.1)` loop.

**mTLS with uvicorn**: `uvicorn.Config(ssl_keyfile=..., ssl_certfile=..., ssl_ca_certs=..., ssl_cert_reqs=ssl.CERT_REQUIRED)` for federal tier. Starlette middleware validates client cert DN.

**Graceful shutdown**: Register `app.on_shutdown` handler that closes all WebSocket connections with code 1001 ("going away"), waits for drain, then stops the event loop.

##### Performance — Rolling Window Aggregation

**BucketedWindow design**: Three resolutions — 1h at per-minute buckets (60 slots), 24h at per-hour buckets (24 slots), 7d at per-day buckets (7 slots). Each bucket stores: count, sum, min, max, DDSketch for percentiles.

**DDSketch for streaming percentiles**: P50/P95/P99 with bounded relative error (~1%) using ~2KB memory per sketch. Total memory for all three windows with 6 metrics each: ~2MB. Much better than keeping all raw values.

**Double-buffer for lock-free reads**: Write buffer + read buffer. Writer always appends to write buffer. Periodic swap (atomic reference swap) makes write buffer the new read buffer. Readers never block writers.

**Warm-start from JSONL**: On server startup, scan current day's JSONL file to rebuild in-memory windows. Server restarts invisible — aggregates immediately accurate.

##### Security — Runtime Config Hot-Reload

**ConfigStore pattern**: Immutable config snapshot + atomic reference swap. `Pydantic.model_copy(update={...})` creates new frozen config object. Single `config_ref` atomic swap ensures readers see consistent snapshot.

**Three propagation strategies**: (1) Direct — ConfigController holds reference, callers read on each use (simplest). (2) Callback — `on_config_change(key, callback)` handlers. (3) Event-based — emit via ModuleBus/EventBus.

**Recommendation**: Start with Direct. Add Callback for hot-reload of specific settings (budget limits). Event-based only when ArcAgent modules need to react.

**Audit trail**: Every config mutation emits OTel span + TraceRecord with: who (token identity), what (key path + old value + new value), when (timestamp), where (source IP).

##### UI/UX — Vanilla JS Dashboard Patterns

**RobustWebSocket**: Reconnection class with exponential backoff + jitter, heartbeat ping/pong for silent TCP drop detection, outbound message queue during disconnection, `navigator.onLine` pre-check. Max retries configurable.

**DOMBatcher**: Accumulate-and-flush — never write DOM in WebSocket message handler. All mutations batched through single `requestAnimationFrame` per frame. `DocumentFragment` for bulk row inserts (single reflow).

**Store pattern**: Central state via `EventTarget` + `CustomEvent`. Scoped subscribers per UI section (traces panel, status bar, agent selector). Avoids full re-render on every state change.

**LogTable**: Node cap (200-300 rows) with `deleteRow(0)`. Auto-scroll via `scrollHeight - scrollTop - clientHeight < 4`. "Resume live" sticky button when user scrolls up.

**Number formatting**: Pre-allocated `Intl.NumberFormat` objects — compact notation for tokens, currency for costs, tiered latency ms/s/m. `escapeHTML()` for all untrusted content.

**WebSocket auth**: First-message pattern — connection opens, client sends `{"type":"auth","token":"..."}`, server validates within 5s timeout, sends `auth_ok` or closes with 4001. Token embedded in server-rendered HTML.

**Connection UX**: Four visual states (CONNECTED/CONNECTING/RECONNECTING/DISCONNECTED). Stale-data overlay on metric cards after 30s silence. Live "Xs ago" timer during reconnection. Keep last-known data visible rather than clearing.

**CSS charts**: `width` % on flex children = repaint only (fast). `conic-gradient` for gauges = repaint only but NOT CSS-animatable. Limit `will-change` to <10 elements. Switch to Canvas if >20 elements updating >5/sec.

---

#### Auto-Applied (Federal Mandates)

| # | Decision | Mandated Answer | Citation |
|---|----------|----------------|----------|
| A1 | Trace storage integrity | SHA-256 hash chain, append-only, tamper-evident | NIST 800-53 AU-9 |
| A2 | Audit every LLM call | Every `invoke()` generates a TraceRecord, no opt-out | NIST 800-53 AU-2, AU-12 |
| A3 | Retention period | Configurable. Federal: 90d online + 1yr archive minimum | NIST 800-53 AU-11, FedRAMP |
| A4 | Transport encryption | TLS 1.2+ for arcUI server. mTLS optional (federal tier) | NIST 800-52r2, SC-8 |
| A5 | Config mutations audited | Every config change via control plane logged as audit event | NIST 800-53 AU-2, CM-3 |
| A6 | Default bind address | `127.0.0.1` (localhost only). Explicit opt-in to expose | NIST 800-53 SC-7 |

#### Architecture

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### Data Model

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### API Design

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### Observability

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### Audit & Compliance

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### Security

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### Integration

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### Performance

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### Extensibility

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### Testing

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### Deployment

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### UI/UX

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### Observability (additions)

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### Data Model (additions)

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

#### UI/UX (additions — 2026-03-01, from Mission Control competitive analysis)

| # | Decision | Options Considered | Choice | Rationale |
|---|----------|--------------------|--------|-----------|

_Note: Context window utilization tracking was evaluated but deferred — ArcLLM is stateless per-call. Context accumulation is an ArcAgent/ArcRun session concern, not ArcUI day 1._

#### Tier Behavior Summary

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

---
