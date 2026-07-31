# SDD: ArcUI LLM Telemetry (SPEC-015)

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         ArcLLM                                   │
│                                                                   │
│  load_model() ──► Module Stack:                                  │
│    on_event ─┐    OtelModule → SecurityModule → RetryModule      │
│              │    → CircuitBreakerModule(NEW) → TelemetryModule   │
│              │    → RoutingModule → Adapter                       │
│              │                                                    │
│              │  TelemetryModule.invoke():                         │
│              │    1. Budget pre-check                             │
│              │    2. Record phase_timings                         │
│              │    3. Build TraceRecord (+ raw bodies if enabled)  │
│              │    4. TraceStore.append(record)                    │
│              ├──5. on_event(record)  ─────────────────┐          │
│              │    6. Budget post-deduct                │          │
│              │                                         │          │
│  ConfigController ◄──── get/set config ────────┐      │          │
│  TraceStore ◄──── append/query/verify ──┐      │      │          │
│  CircuitBreakerModule ◄── state query ──┤      │      │          │
│  BudgetAccumulator ◄── spend query ─────┤      │      │          │
└──────────────────────────────────────────┤──────┤──────┤──────────┘
                                           │      │      │
┌──────────────────────────────────────────┤──────┤──────┤──────────┐
│                         ArcUI            │      │      │          │
│                                          ▼      ▼      ▼          │
│  Starlette App:                                                   │
│    /           → StaticFiles (HTML/CSS/JS)                       │
│    /ws         → WebSocketEndpoint                                │
│    /api/traces → REST query TraceStore                           │
│    /api/config → REST get/set ConfigController                   │
│    /api/circuit-breakers → REST query CircuitBreakerModule       │
│    /api/budget → REST query BudgetAccumulator                    │
│    /api/stats  → REST query RollingAggregator                    │
│    /api/export → REST CSV/JSON export                            │
│    /api/cost-efficiency → REST query RollingAggregator           │
│                                                                   │
│  Server internals:                                                │
│    ConnectionManager → per-client Queue → WS broadcast           │
│    EventBuffer → 100ms batched flush                             │
│    RollingAggregator → BucketedWindow (1h/24h/7d)               │
│    AuthMiddleware → bearer token, viewer/operator roles          │
│                                                                   │
│  on_event handler:                                                │
│    TraceRecord → RollingAggregator.ingest()                      │
│                → EventBuffer.push()                              │
│                → ConnectionManager.broadcast()                    │
└───────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────┐
│  Browser (vanilla JS)                                             │
│    RobustWebSocket → Store → DOMBatcher → DOM                    │
│    Tabs: Overview | Traces | Cost                                 │
└───────────────────────────────────────────────────────────────────┘
```

## 2. Component Design

### 2.1 TraceStore (ArcLLM) — REQ-001

**Location**: `packages/arcllm/src/arcllm/trace_store.py`

```python
class TraceRecord(BaseModel):
    """Single LLM call record. Pydantic model, frozen."""
    trace_id: str                           # UUID4 hex
    timestamp: str                          # ISO 8601 UTC
    provider: str                           # e.g. "anthropic"
    model: str                              # e.g. "claude-sonnet-4-20250514"
    agent_label: str | None = None          # From attach_llm(label=...)
    budget_scope: str | None = None

    # Request (optional per tier config)
    request_body: dict[str, Any] | None = None   # {messages, tools, params}

    # Response (optional per tier config)
    response_body: dict[str, Any] | None = None  # {content, tool_calls, usage, stop_reason}

    # Telemetry (always present)
    duration_ms: float
    cost_usd: float
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    stop_reason: str
    status: Literal["success", "error", "timeout"] = "success"
    error: str | None = None

    # Sub-phase timing
    phase_timings: dict[str, float] = {}    # key → ms

    # Event type (LLM call vs config change vs circuit state)
    event_type: Literal["llm_call", "config_change", "circuit_change"] = "llm_call"
    event_data: dict[str, Any] | None = None  # For non-llm events

    # Hash chain
    prev_hash: str                          # SHA-256 hex, "0"*64 for genesis
    record_hash: str                        # SHA-256 of canonical(self) + prev_hash

class TraceStore(Protocol):
    async def append(self, record: TraceRecord) -> None: ...
    async def query(
        self, *, limit: int = 50, cursor: str | None = None,
        provider: str | None = None, agent: str | None = None,
        status: str | None = None,
        start: str | None = None, end: str | None = None,
    ) -> tuple[list[TraceRecord], str | None]: ...  # records, next_cursor
    async def get(self, trace_id: str) -> TraceRecord | None: ...
    async def verify_chain(self, start_seq: int = 0) -> bool: ...
    async def close(self) -> None: ...
```

**JSONLTraceStore**:
- File path: `{workspace}/traces/traces-{YYYY-MM-DD}.jsonl`
- Append with `jcs` canonical JSON for hash computation, `orjson` for serialization
- Daily rotation: last record = tombstone `{"event_type":"rotation","next_file":"...","chain_hash":"..."}`
- First record of new file references previous file's last hash
- `query()` reads from newest file backward, cursor = `"{date}:{line_number}"`
- `verify_chain()` validates hash linkage across files

### 2.2 on_event Callback (ArcLLM) — REQ-002

**Location**: `packages/arcllm/src/arcllm/registry.py` (modify `load_model()`)

```python
def load_model(
    provider: str,
    model: str | None = None,
    *,
    on_event: Callable[[TraceRecord], None] | None = None,  # NEW
    trace_store: TraceStore | None = None,                   # NEW
    # ... existing params ...
) -> LLMProvider:
```

TelemetryModule receives `on_event` and `trace_store` via config dict. After computing telemetry:
1. Build `TraceRecord` from response + timing + budget data
2. If `trace_store`: `await trace_store.append(record)`
3. If `on_event`: `on_event(record)` — called OUTSIDE any locks

### 2.3 ConfigController (ArcLLM) — REQ-003

**Location**: `packages/arcllm/src/arcllm/config_controller.py`

```python
class ConfigController:
    """Runtime get/set for LLM configuration. Immutable snapshot + atomic swap."""

    def __init__(self, provider: LLMProvider, config: dict[str, Any]):
        self._snapshot = ConfigSnapshot.from_config(config)  # Frozen Pydantic
        self._provider = provider
        self._on_change: list[Callable] = []

    def get_snapshot(self) -> ConfigSnapshot: ...
    def patch(self, updates: dict[str, Any], *, actor: str) -> ConfigSnapshot: ...
    def on_change(self, callback: Callable) -> None: ...

class ConfigSnapshot(BaseModel, frozen=True):
    model: str
    temperature: float
    max_tokens: int
    daily_budget_limit: float | None
    monthly_budget_limit: float | None
    failover_chain: list[str]
```

`patch()` validates, creates new frozen snapshot via `model_copy(update=...)`, atomic swap, emits TraceRecord with `event_type="config_change"`.

### 2.4 CircuitBreakerModule (ArcLLM) — REQ-004

**Location**: `packages/arcllm/src/arcllm/modules/circuit_breaker.py`

```python
class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreakerModule(BaseModule):
    """Per-provider circuit breaker. Wraps inner adapter."""

    def __init__(self, config: dict[str, Any], inner: LLMProvider):
        self._failure_threshold: int    # default 5
        self._cooldown_seconds: float   # default 30
        self._half_open_max: int        # default 1
        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._last_failure_time: float | None = None
        self._half_open_calls: int = 0

    async def invoke(self, messages, tools, **kwargs) -> LLMResponse:
        # OPEN: check cooldown → transition to HALF_OPEN or raise
        # HALF_OPEN: allow probe call, on success → CLOSED, on fail → OPEN
        # CLOSED: pass through, on fail → increment, on threshold → OPEN

    def get_state(self) -> dict[str, Any]:
        """Queryable state for REST API."""
        return {
            "provider": self._inner.name,
            "model": self._inner.model_name,
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "last_failure_time": self._last_failure_time,
            "success_rate_1h": self._success_rate(),
        }
```

Wired into module stack between RetryModule and TelemetryModule. RetryModule handles transient retries; CircuitBreakerModule prevents calls to unhealthy providers entirely.

### 2.5 ArcUI Server — REQ-007, REQ-008, REQ-009, REQ-010

**Location**: `packages/arcui/src/arcui/`

```
arcui/
├── __init__.py          # serve(), attach_llm(), __version__
├── server.py            # Starlette app factory, uvicorn runner
├── routes/
│   ├── ws.py            # WebSocket endpoint
│   ├── traces.py        # /api/traces, /api/traces/{id}
│   ├── config.py        # /api/config
│   ├── stats.py         # /api/stats, /api/circuit-breakers, /api/budget
│   └── export.py        # /api/export
├── auth.py              # Bearer token middleware, role checking
├── connection.py        # ConnectionManager (per-client Queue)
├── event_buffer.py      # EventBuffer (bounded deque, 100ms flush)
├── aggregator.py        # RollingAggregator (BucketedWindow + DDSketch)
└── static/
    ├── index.html        # Telemetry page
    ├── assets/
    │   ├── arc-platform.css   # Design system (ported from demo)
    │   ├── arc-shell.js       # Shell (adapted for real data)
    │   ├── ws-client.js       # RobustWebSocket
    │   ├── store.js           # Central state store
    │   ├── dom-batcher.js     # requestAnimationFrame batching
    │   ├── log-table.js       # Auto-scroll table with node cap
    │   ├── formatters.js      # Number formatting, escapeHTML
    │   └── connection-ui.js   # Connection status banner
    └── favicon.ico
```

**Server startup flow:**
1. `serve(llm=model)` or `attach_llm(instance, label="agent-1")`
2. Build Starlette app with routes
3. Wire `on_event` callback from each attached LLM → `EventBuffer.push()` + `RollingAggregator.ingest()`
4. Start uvicorn on configured host:port
5. ConnectionManager broadcasts batched events to all WS clients

**Auth middleware:**
```python
class AuthMiddleware:
    async def __call__(self, request, call_next):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not token and request.url.path.startswith("/api/"):
            # Check tier — personal viewer may skip token
            ...
        role = self._validate_token(token)  # "viewer" | "operator" | None
        request.state.role = role
        return await call_next(request)
```

### 2.6 Rolling Aggregation — REQ-012

**Location**: `packages/arcui/src/arcui/aggregator.py`

```python
class BucketedWindow:
    """Fixed-size ring buffer of time buckets."""
    def __init__(self, bucket_count: int, bucket_duration_seconds: int): ...
    def ingest(self, record: TraceRecord) -> None: ...
    def snapshot(self) -> dict: ...  # Returns current aggregates

class RollingAggregator:
    """Three windows: 1h (60 x 1min), 24h (24 x 1hr), 7d (7 x 1day)."""
    def __init__(self): ...
    def ingest(self, record: TraceRecord) -> None:
        # Update all three windows + per-agent + per-provider counters
    def stats(self, window: str = "24h") -> dict: ...
    def warm_start(self, store: TraceStore) -> None:
        # Read today's JSONL, replay into windows
```

DDSketch for streaming percentiles (~2KB per sketch). Total memory ~2MB for all windows.

**Model Cost Efficiency (REQ-015):**
```python
def cost_efficiency(self, window: str = "24h") -> dict:
    """Compute per-model cost efficiency from aggregated trace data.

    Uses real input/output token split pricing from ArcLLM provider configs.
    Returns: {
        models: [{model, total_cost, total_tokens, cost_per_token, request_count}],
        cheapest_model: str,
        most_used_model: str,
        potential_savings_usd: float,
        potential_savings_pct: float,
    }
    """
```

### 2.7 Frontend Architecture — REQ-011, REQ-013

**State flow:**
```
WebSocket message → Store.setState() → scoped subscribers → DOMBatcher.write() → DOM
```

**Store shape:**
```javascript
{
  connection: 'DISCONNECTED',     // 4 states
  reconnectAttempt: 0,
  lastMessageAt: null,
  activeTab: 'overview',          // overview | traces | cost
  selectedAgent: null,
  filterProvider: 'all',
  filterStatus: 'all',
  traces: [],                     // last 300
  stats: {},                      // from /api/stats
  circuitBreakers: [],            // from /api/circuit-breakers
  budgets: [],                    // from /api/budget
  config: {},                     // from /api/config
  totalCost: 0,
  totalTokens: 0,
  // Cost efficiency (REQ-015)
  modelEfficiency: [],            // {model, total_cost, total_tokens, cost_per_token, request_count}
  costOptimization: {             // Calculated server-side
    cheapest_model: null,
    most_used_model: null,
    potential_savings_usd: 0,
    potential_savings_pct: 0,
  },
}
```

**Tab content:**

| Tab | Components | Data Source |
|-----|-----------|-------------|
| Overview | 4 stat cards, token volume bars, cost breakdown, circuit breaker table | WebSocket stream + /api/stats |
| Traces | Filter dropdowns, trace table (node-capped), trace detail expand (request/response + span timeline) | WebSocket + /api/traces/{id} |
| Cost | Per-agent bar chart, per-provider bars, budget gauges, model efficiency table ($/token ranking), optimization alert (>20% savings), cheapest vs most-used comparison | /api/stats + /api/budget + /api/cost-efficiency |

### 2.8 ArcAgent Bridge — REQ-014

**Location**: `packages/arcagent/src/arcagent/core/agent.py` (add alongside existing `create_arcrun_bridge`)

```python
def create_arcllm_bridge(bus: ModuleBus) -> Callable[[TraceRecord], None]:
    """Create on_event callback that forwards TraceRecords to ModuleBus."""
    loop = asyncio.get_event_loop()
    _pending: set[asyncio.Task] = set()

    def _handler(record: TraceRecord) -> None:
        event_map = {
            "llm_call": "llm:call_complete",
            "config_change": "llm:config_change",
            "circuit_change": "llm:circuit_change",
        }
        event = event_map.get(record.event_type, "llm:unknown")
        task = loop.create_task(bus.emit(event, record.model_dump()))
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    return _handler
```

## 3. File Changes Summary

| Package | File | Action | LOC Est. |
|---------|------|--------|----------|
| arcllm | `trace_store.py` | NEW | ~300 |
| arcllm | `config_controller.py` | NEW | ~150 |
| arcllm | `modules/circuit_breaker.py` | NEW | ~120 |
| arcllm | `modules/telemetry.py` | MODIFY | +80 (phase timing, raw bodies, TraceRecord build) |
| arcllm | `registry.py` | MODIFY | +30 (on_event, trace_store params) |
| arcui | `__init__.py` | REWRITE | ~40 |
| arcui | `server.py` | NEW | ~120 |
| arcui | `routes/ws.py` | NEW | ~80 |
| arcui | `routes/traces.py` | NEW | ~100 |
| arcui | `routes/config.py` | NEW | ~60 |
| arcui | `routes/stats.py` | NEW | ~80 |
| arcui | `routes/export.py` | NEW | ~60 |
| arcui | `routes/cost_efficiency.py` | NEW | ~50 |
| arcui | `auth.py` | NEW | ~80 |
| arcui | `connection.py` | NEW | ~80 |
| arcui | `event_buffer.py` | NEW | ~50 |
| arcui | `aggregator.py` | NEW | ~200 |
| arcui | `static/index.html` | NEW | ~380 |
| arcui | `static/assets/*.css` | NEW | ~1200 (ported from demo) |
| arcui | `static/assets/*.js` | NEW | ~600 (7 JS files) |
| arcagent | `core/agent.py` | MODIFY | +25 (arcllm bridge) |
| **Total** | | | **~3,835** |

## 4. Dependencies

**New for arcui:**
- `starlette>=0.40` — ASGI framework
- `uvicorn[standard]>=0.30` — ASGI server
- `jcs>=0.2` — RFC 8785 canonical JSON (for arcllm TraceStore)

**Optional:**
- `orjson>=3.10` — Fast JSON serialization (performance extra)
- `ddsketch>=3.0` — Streaming percentiles (for aggregator)

**Already available:**
- `pydantic>=2.0` — Data models
- `opentelemetry-api` — Tracing
- `httpx` — Used by adapters

## 5. Security Considerations

| Concern | Mitigation |
|---------|-----------|
| WebSocket injection | All client content escaped via `escapeHTML()`. Server never executes client-sent data. |
| Token in URL | First-message auth, not query params. Token embedded in server-rendered HTML. |
| Raw body exposure | Configurable per tier. Viewer role can see bodies but not modify. |
| Config mutation | Operator role only. Audit trail on every change. |
| Hash chain tampering | SHA-256 + jcs canonical JSON. `verify_chain()` API for integrity checks. |
| Bind address | Default localhost. Explicit config to expose. |

## 6. Testing Strategy

| Level | Scope | Tools |
|-------|-------|-------|
| Unit | TraceStore, ConfigController, CircuitBreakerModule, aggregator, auth | pytest, mock |
| Integration | Server routes, WebSocket flow, on_event wiring | pytest + httpx AsyncClient + websockets |
| E2E | Browser connects, sees live traces, filters work | Playwright |
| Security | Token validation, role enforcement, hash chain integrity | pytest |
| Performance | 1000 traces/sec throughput, <50ms WS broadcast latency | pytest-benchmark |
