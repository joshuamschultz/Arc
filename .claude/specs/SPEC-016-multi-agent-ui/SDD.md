# SDD: Multi-Agent UI Architecture

**Spec**: SPEC-016 | **Date**: 2026-03-03

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    ArcUI Server                         │
│                 (arc ui start)                          │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │ Agent    │  │ Event    │  │ Browser  │             │
│  │ Registry │  │ Buffer   │  │ ConnMgr  │             │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘             │
│       │              │              │                   │
│  ┌────┴──────────────┴──────────────┴─────┐            │
│  │         RollingAggregator              │            │
│  │   (global + per-agent sub-aggs)        │            │
│  └────────────────────────────────────────┘            │
│                                                         │
│  WS /api/agent/connect  ←→  Agent connections          │
│  WS /ws                 →   Browser connections        │
│  REST /api/agents/*     →   Browser queries + control  │
└─────────────────────────────────────────────────────────┘
        ↑                           ↑
        │ WebSocket                 │ WebSocket
   ┌────┴────┐                ┌─────┴─────┐
   │ Agent 1 │                │ Browser   │
   │ UIRep.  │                │ Dashboard │
   └─────────┘                └───────────┘
   ┌─────────┐
   │ Agent 2 │
   │ UIRep.  │
   └─────────┘
```

## Component Design

### 1. UIEvent — Flat Event Envelope (arcui)

**File**: `packages/arcui/src/arcui/types.py` (new)

```python
class UIEvent(BaseModel):
    layer: Literal["llm", "run", "agent", "team"]
    event_type: str                    # e.g., "trace_record", "tool_call", "state_change"
    agent_id: str                      # DID or generated ID
    agent_name: str                    # Human-readable
    source_id: str                     # run_id, call_id, team_id, etc.
    timestamp: str                     # ISO 8601
    data: dict[str, Any]              # Layer-specific payload
    sequence: int                      # Per-agent monotonic counter
```

All events flowing through the UI — from any layer, any agent — use this single envelope. The `layer` field enables filtering. The `sequence` field enables gap detection.

### 2. Agent Registry (arcui)

**File**: `packages/arcui/src/arcui/registry.py` (new)

```python
class AgentRegistration(BaseModel):
    agent_id: str
    agent_name: str
    model: str
    provider: str
    team: str | None = None
    tools: list[str] = []
    modules: list[str] = []
    workspace: str | None = None       # Redacted in federal tier
    meta: dict[str, Any] = {}
    connected_at: str                  # ISO 8601
    last_event_at: str | None = None
    sequence: int = 0                  # Last received sequence

class AgentEntry:
    registration: AgentRegistration
    ws: WebSocket
    aggregator: RollingAggregator      # Per-agent sub-aggregator

class AgentRegistry:
    _agents: dict[str, AgentEntry]     # agent_id → entry
    _max_agents: int                   # Default 100

    def register(agent_id, ws, registration) -> AgentEntry
    def unregister(agent_id) -> None
    def get(agent_id) -> AgentEntry | None
    def list_agents() -> list[AgentRegistration]
    def is_full() -> bool
```

In-memory. No persistence. Agents re-register on UI restart.

### 3. UITransport Protocol (arcui)

**File**: `packages/arcui/src/arcui/transport.py` (new)

```python
class UITransport(Protocol):
    """Abstract transport for agent ↔ UI communication."""
    async def send_event(self, agent_id: str, event: UIEvent) -> None: ...
    async def send_control(self, agent_id: str, message: ControlMessage) -> None: ...
    async def receive(self) -> tuple[str, UIEvent | ControlResponse]: ...
    async def close(self) -> None: ...

class WebSocketTransport:
    """WebSocket implementation of UITransport."""
    # Used by UIReporter (client side) and agent WS route (server side)
```

This enables future NATSTransport without changing any business logic.

### 4. Agent WebSocket Route (arcui)

**File**: `packages/arcui/src/arcui/routes/agent_ws.py` (new)

Protocol:
1. Agent connects to `/api/agent/connect`
2. Server accepts, waits for auth message: `{"token": "...", "registration": {...}}`
3. Server validates agent_token, registers agent, sends `{"type": "auth_ok", "agent_id": "..."}`
4. Bidirectional loop:
   - **Agent → Server**: UIEvent messages → EventBuffer + per-agent aggregator + global aggregator
   - **Server → Agent**: ControlMessage → forwarded from browser REST proxy

Server stamps `agent_id` on all events (server-side binding — agent cannot spoof identity).

### 5. Subscription Manager (arcui)

**File**: `packages/arcui/src/arcui/subscription.py` (new)

```python
class Subscription(BaseModel):
    agents: list[str] | None = None    # None = all agents
    layers: list[str] | None = None    # None = all layers
    teams: list[str] | None = None     # None = all teams

class SubscriptionManager:
    _subscriptions: dict[asyncio.Queue, Subscription]

    def set_subscription(queue, subscription) -> None
    def matches(queue, event: UIEvent) -> bool
    def broadcast_filtered(event: UIEvent) -> None
```

Browser clients send `{"type": "subscribe", "agents": [...], "layers": [...]}` via their WebSocket. SubscriptionManager replaces direct ConnectionManager.broadcast() for browser distribution.

### 6. Control Proxy Routes (arcui)

**File**: `packages/arcui/src/arcui/routes/agents.py` (new)

```
GET    /api/agents              → List connected agents
GET    /api/agents/{id}         → Agent details + stats
POST   /api/agents/{id}/control → Send control command (operator only)
```

Control flow:
1. Browser POSTs `{"action": "cancel", "data": {...}, "request_id": "uuid"}`
2. Route validates operator role
3. Looks up agent in registry
4. Sends ControlMessage to agent's WebSocket
5. Waits for ControlResponse (with timeout)
6. Returns response to browser

### 7. AuthConfig Updates (arcui)

**File**: `packages/arcui/src/arcui/auth.py` (modify)

Add `agent_token` as third token type:

```python
class AuthConfig:
    viewer_token: str
    operator_token: str
    agent_token: str        # NEW — shared secret for agent connections

    def validate_token(token) -> str | None:
        # Returns "operator", "viewer", "agent", or None
```

Server-side binding: when an agent authenticates with agent_token, the server assigns and stamps the agent_id. The agent cannot choose or forge its own identity.

### 8. Multi-Agent Aggregation (arcui)

**File**: `packages/arcui/src/arcui/aggregator.py` (modify)

No structural changes to RollingAggregator itself. Each AgentEntry in the registry gets its own RollingAggregator instance. The existing global aggregator ingests all events. Stats routes gain `?agent_id=` parameter for per-agent drill-down.

### 9. UIReporter Module (arcagent)

**File**: `packages/arcagent/src/arcagent/modules/ui_reporter/__init__.py` (new)

```python
class UIReporterConfig(BaseModel):
    enabled: bool = False              # Opt-in
    url: str = "ws://127.0.0.1:8420/api/agent/connect"
    token: str = ""                    # Agent token
    reconnect_max_interval: float = 60.0
    buffer_size: int = 1000

class UIReporterModule:
    name = "ui_reporter"

    async def startup(ctx: ModuleContext):
        # Subscribe to ModuleBus events across all layers
        # Connect to UI server
        # Start reconnect loop

    async def shutdown():
        # Disconnect, flush buffer

    async def _on_llm_event(ctx: EventContext):
        # Wrap in UIEvent(layer="llm", ...) and send

    async def _on_run_event(ctx: EventContext):
        # Wrap in UIEvent(layer="run", ...) and send

    async def _on_agent_event(ctx: EventContext):
        # Wrap in UIEvent(layer="agent", ...) and send

    async def _on_team_event(ctx: EventContext):
        # Wrap in UIEvent(layer="team", ...) and send

    async def _on_control_message(message: ControlMessage):
        # Forward to ModuleBus for agent to handle
```

Integration points:
- **arcllm events**: Chain into TelemetryModule.on_event (same pattern as current `agent.py:1273`)
- **arcrun events**: Subscribe to ModuleBus events that arcrun bridge emits
- **arcagent events**: Subscribe to agent-level ModuleBus events
- **arcteam events**: Subscribe to team-level events relayed via agent

Reconnect: Exponential backoff (1s → 60s cap). Bounded deque (1000 items) for local buffering during disconnects. Flush on reconnect.

### 10. CLI Changes (arccli)

**New command group**: `arc ui`

```
arc ui start                   # Launch standalone UI server
arc ui start --port 9000       # Custom port
arc ui start --host 0.0.0.0    # Bind to all interfaces
```

**Modified command**: `arc agent serve --ui`

Old behavior: Embeds UI in agent process.
New behavior: Agent's UIReporter module connects to running UI server. The `--ui` flag is shorthand for enabling `[ui]` config.

**Remove**: `_start_ui()`, `_ensure_telemetry_for_ui()`, `_create_trace_store()`, `_inject_pricing()` from `agent.py`. These ~150 lines are replaced by the UIReporter module.

### 11. TOML Configuration

Agent config (`agent.toml`):
```toml
[ui]
enabled = true
url = "ws://127.0.0.1:8420/api/agent/connect"
token = "agent-token-here"
```

UI server config (passed via CLI or env):
```
ARC_UI_PORT=8420
ARC_UI_HOST=127.0.0.1
ARC_UI_VIEWER_TOKEN=...      # Auto-generated if omitted
ARC_UI_OPERATOR_TOKEN=...    # Auto-generated if omitted
ARC_UI_AGENT_TOKEN=...       # Auto-generated if omitted
ARC_UI_MAX_AGENTS=100
```

## Data Flow

### Agent → UI → Browser

```
Agent Process                     UI Server                      Browser
─────────────                     ─────────                      ───────
LLM call completes
  → TelemetryModule.on_event
  → UIReporter._on_llm_event
  → UIEvent(layer="llm", ...)
  → WebSocket send  ──────────→  agent_ws route receives
                                  → server stamps agent_id
                                  → EventBuffer.push()
                                  → per-agent aggregator.ingest()
                                  → global aggregator.ingest()
                                  → EventBuffer flush loop
                                  → SubscriptionManager.broadcast_filtered()
                                  → matching browser queues  ──→  dashboard renders
```

### Browser → UI → Agent (Control)

```
Browser                           UI Server                      Agent Process
───────                           ─────────                      ─────────────
POST /api/agents/{id}/control
  {"action": "cancel"}  ────────→  agents route validates
                                   → operator role check
                                   → registry.get(agent_id)
                                   → send ControlMessage via WS  ──→  UIReporter._on_control
                                   ← wait for response (timeout)       → ModuleBus.emit("ui:control")
                                   ← ControlResponse  ←────────────── → agent handles
                                  → return JSON to browser
```

## File Inventory

### New Files

| File | Package | Purpose | Est. LOC |
|------|---------|---------|----------|
| `arcui/types.py` | arcui | UIEvent, ControlMessage, ControlResponse, AgentRegistration | ~80 |
| `arcui/registry.py` | arcui | AgentRegistry — in-memory agent tracking | ~90 |
| `arcui/transport.py` | arcui | UITransport Protocol + WebSocketTransport | ~100 |
| `arcui/subscription.py` | arcui | SubscriptionManager — server-side event filtering | ~60 |
| `arcui/routes/agent_ws.py` | arcui | Agent WebSocket endpoint | ~120 |
| `arcui/routes/agents.py` | arcui | REST routes for agent list, detail, control | ~80 |
| `arcagent/modules/ui_reporter/__init__.py` | arcagent | UIReporter module | ~200 |
| `arccli/ui.py` | arccli | `arc ui start` CLI command | ~60 |

### Modified Files

| File | Package | Changes |
|------|---------|---------|
| `arcui/auth.py` | arcui | Add agent_token support |
| `arcui/server.py` | arcui | Wire new routes, agent registry, subscription manager |
| `arcui/routes/ws.py` | arcui | Update browser WS to support subscribe messages |
| `arcui/routes/stats.py` | arcui | Add `?agent_id=` parameter for per-agent stats |
| `arccli/agent.py` | arccli | Remove embedded UI code (~150 LOC), `--ui` means connect |
| `arccli/main.py` | arccli | Register `arc ui` command group |

### Removed Code

| File | Lines | What |
|------|-------|------|
| `arccli/agent.py` | 1151-1319 | `_start_ui()`, `_ensure_telemetry_for_ui()`, `_create_trace_store()`, `_inject_pricing()`, `_run_uvicorn_safe()` |

## Testing Strategy

| Type | Coverage | Focus |
|------|----------|-------|
| Unit (70%) | UIEvent serialization, AgentRegistry CRUD, SubscriptionManager filtering, AuthConfig 3-token, ControlMessage routing | Individual component correctness |
| Integration (20%) | UIReporter → InMemoryTransport → AgentRegistry → EventBuffer → SubscriptionManager → Browser queue | Full event pipeline without real WebSocket |
| E2E (10%) | Real WebSocket: agent connect, event stream, control command, disconnect/reconnect | End-to-end protocol correctness |

### InMemoryTransport

For integration tests, implement InMemoryTransport (UITransport protocol) that uses asyncio.Queue instead of WebSocket. This allows testing the full pipeline without network.

## Dependencies

### New Dependencies

None. WebSocket support already available via Starlette. No new packages needed.

### Existing Dependencies Used

- Starlette (WebSocket, routes)
- Pydantic (models)
- uvicorn (server)
- asyncio (transport, buffers)
