# PLAN: Multi-Agent UI Architecture

**Spec**: SPEC-016 | **Status**: COMPLETE | **Date**: 2026-03-03

## Research Enhancement Summary

Enriched via `/deepen` with 6 parallel research agents covering: WS protocol patterns, ModuleBus event catalog, reconnect/buffering strategies, control plane patterns, CLI patterns, and test infrastructure. Key corrections applied:
- **Event mapping corrected**: No `run:*` or `team:*` prefixed events exist in codebase — arcrun bridges to `agent:pre_*/post_*`
- **Buffer type corrected**: `asyncio.Queue(maxsize=N)` preferred over `collections.deque` for async backpressure
- **Backoff algorithm specified**: Decorrelated jitter (AWS/Netflix standard), not simple exponential
- **Control pattern specified**: Pending map (`dict[str, asyncio.Future]`) for request-response correlation
- **WebSocket access pattern**: Use `ws.app.state` (not `request.app.state`) in WS route handlers

---

## Phase 1: Core Types & Transport Protocol

Foundation types and abstractions that everything else depends on.

**Approval gate**: Phase 1 complete before Phase 2 starts.

### Tasks

- [x] **1.1** Create `packages/arcui/src/arcui/types.py` — UIEvent, ControlMessage, ControlResponse Pydantic models
  - UIEvent: layer (Literal["llm","run","agent","team"]), event_type, agent_id, agent_name, source_id, timestamp, data, sequence
  - ControlMessage: action (Literal["steer","cancel","config","ping","shutdown"]), target, data, request_id
  - ControlResponse: request_id, status, data
  - AgentRegistration: agent_id, agent_name, model, provider, team, tools, modules, workspace, meta, connected_at
  - Tests: `tests/unit/test_types.py` — serialization, validation, layer literals

- [x] **1.2** Create `packages/arcui/src/arcui/transport.py` — UITransport Protocol + InMemoryTransport
  - UITransport Protocol: send_event, send_control, receive, close
  - InMemoryTransport: asyncio.Queue-based implementation for testing
  - Tests: `tests/unit/test_transport.py` — send/receive, close behavior

- [x] **1.3** Create `packages/arcui/src/arcui/transport_ws.py` — WebSocketTransport (client-side)
  - Implements UITransport for agent-side WebSocket connections
  - Reconnect logic: decorrelated jitter backoff (1s → 60s cap)
  - Local buffer: `asyncio.Queue(maxsize=1000)`, flush on reconnect
  - Tests: `tests/unit/test_transport_ws.py` — buffer, backoff calculation

### Research Insights

**Backoff Algorithm — Decorrelated Jitter (AWS/Netflix standard)**:
```python
# NOT simple exponential. Use decorrelated jitter:
sleep = min(cap, random.uniform(base, sleep * 3))
# Starting: base=1.0, cap=60.0
# This spreads reconnect storms better than full jitter
```

**Buffer — Use asyncio.Queue, not deque**:
- `asyncio.Queue(maxsize=1000)` provides native async backpressure via `put_nowait()` raising `QueueFull`
- On `QueueFull`: drop oldest via `get_nowait()` then `put_nowait()` (same pattern as ConnectionManager)
- On reconnect: drain queue via `get_nowait()` loop, send each, re-enqueue failures

**Sequence Numbers**:
- Per-agent monotonic counter (int, starts at 0, increments on each event)
- Server stores `last_sequence` on AgentRegistration for gap detection
- On reconnect: agent sends current sequence, server can detect missed events

**Graceful Shutdown Pattern**:
```python
# Set shutdown event → drain queue → close() → wait_closed()
self._shutdown_event.set()
await self._send_task  # let it drain
await self._ws.close()
```

**Phase 1 total**: ~3 files + 3 test files | Est. ~280 LOC production, ~200 LOC tests

---

## Phase 2: Agent Registry & Auth

Server-side agent management and 3-token auth.

### Tasks

- [x] **2.1** Create `packages/arcui/src/arcui/registry.py` — AgentRegistry
  - AgentEntry: registration, ws reference, per-agent RollingAggregator
  - AgentRegistry: register, unregister, get, list_agents, is_full
  - Max agents (default 100), 429 on exceed
  - Tests: `tests/unit/test_registry.py` — CRUD, capacity limit, unregister cleanup

- [x] **2.2** Modify `packages/arcui/src/arcui/auth.py` — Add agent_token
  - Add agent_token to AuthConfig (auto-generated if not provided)
  - Update validate_token to return "agent" role
  - Tests: update existing auth tests + new agent token tests

### Research Insights

**Registry — Plain dict + asyncio.Lock**:
- `dict[str, AgentEntry]` with `asyncio.Lock()` on compound operations (register/unregister)
- Simple reads (get, list_agents, is_full) don't need lock — dict reads are thread-safe in CPython
- `agent_id` generated server-side via `uuid.uuid4().hex` — agent cannot choose its own identity

**Stale Detection**:
- Track `last_event_at` on AgentRegistration, updated on each received event
- Optional background sweep task (every 60s) to detect stale agents (no events for 5 minutes)
- On stale detection: close WebSocket, trigger unregister

**Auth Pattern — Follows existing `auth.py` exactly**:
- `secrets.token_hex(32)` for auto-generated agent_token
- `hmac.compare_digest()` for constant-time comparison (already used)
- `validate_token()` returns `"agent"` as third role option
- AuthMiddleware already skips WebSocket routes — agent auth is first-message pattern, not middleware

**Phase 2 total**: ~1 new file + 1 modified | Est. ~150 LOC production, ~120 LOC tests

---

## Phase 3: Subscription Manager & Event Pipeline

Server-side filtering for browser clients.

### Tasks

- [x] **3.1** Create `packages/arcui/src/arcui/subscription.py` — SubscriptionManager
  - Subscription model: agents, layers, teams (None = all)
  - SubscriptionManager: set_subscription, matches, broadcast_filtered
  - Replaces direct ConnectionManager.broadcast for browser distribution
  - Tests: `tests/unit/test_subscription.py` — filtering logic, all/partial/none matches

- [x] **3.2** Modify `packages/arcui/src/arcui/event_buffer.py` — UIEvent-aware flushing
  - EventBuffer.push now accepts UIEvent or raw dict
  - Flush loop uses SubscriptionManager.broadcast_filtered instead of ConnectionManager.broadcast
  - Tests: update existing event_buffer tests

### Research Insights

**Subscription Filtering — Per-queue subscription map**:
- `dict[asyncio.Queue, Subscription]` maps each browser client's queue to its subscription
- `matches(queue, event)` checks: agent_id in subscription.agents (or None=all), layer in subscription.layers (or None=all), team in subscription.teams (or None=all)
- `broadcast_filtered(event)`: iterate subscriptions, call `matches()`, put to matching queues only
- Default subscription (no subscribe message): receive everything (backward compatible)

**EventBuffer Integration**:
- Current `EventBuffer._flush()` calls `ConnectionManager.broadcast(batch)`
- New flow: `EventBuffer._flush()` iterates batch, calls `SubscriptionManager.broadcast_filtered(event)` for UIEvents, falls back to `ConnectionManager.broadcast()` for legacy raw dicts
- This preserves backward compatibility during migration

**Phase 3 total**: ~1 new file + 1 modified | Est. ~100 LOC production, ~80 LOC tests

---

## Phase 4: Server Routes — Agent WS & REST

New WebSocket endpoint for agents and REST endpoints for browser control.

### Tasks

- [x] **4.1** Create `packages/arcui/src/arcui/routes/agent_ws.py` — Agent WebSocket endpoint
  - `/api/agent/connect` — accept, first-message auth (agent_token + registration), bidirectional loop
  - Server stamps agent_id on all received events
  - Events → EventBuffer + per-agent aggregator + global aggregator
  - Control messages: read from agent WS, forward responses
  - Heartbeat (30s ping)
  - Disconnect cleanup via AgentRegistry.unregister
  - Tests: `tests/unit/test_agent_ws.py` — auth flow, event handling, disconnect

- [x] **4.2** Create `packages/arcui/src/arcui/routes/agents.py` — REST routes
  - GET /api/agents — list connected agents
  - GET /api/agents/{id} — agent details + per-agent stats
  - POST /api/agents/{id}/control — send control command (operator role required)
  - Control proxy: POST → lookup agent in registry → send via WS → await response (timeout)
  - Tests: `tests/unit/test_agents_routes.py` — list, detail, control with mock registry

- [x] **4.3** Modify `packages/arcui/src/arcui/routes/ws.py` — Browser subscribe support
  - Handle incoming `{"type": "subscribe", ...}` messages from browser
  - Pass subscription to SubscriptionManager
  - Tests: update existing ws tests

- [x] **4.4** Modify `packages/arcui/src/arcui/routes/stats.py` — Per-agent stats
  - Add `?agent_id=` query parameter to stats endpoints
  - Look up per-agent aggregator from registry
  - Tests: update existing stats tests

### Research Insights

**Agent WS Route — Critical implementation details from existing `routes/ws.py`**:
- Access app state via `ws.app.state` (NOT `request.app.state`) — WebSocket routes use `ws` not `request`
- Auth timeout: 5 seconds for first message, close with code 4001 on timeout
- Auth failure: close with code 4003 (invalid token)
- Bidirectional loop pattern: 3 concurrent tasks via `asyncio.wait(FIRST_COMPLETED)` + cancel pending
  - `_send_control(ws, control_queue)` — sends ControlMessages from REST proxy
  - `_heartbeat(ws, 30s)` — ping keepalive
  - `_receive(ws, registry, buffer, aggregator)` — receives UIEvents from agent
- On disconnect: cancel all tasks, `AgentRegistry.unregister(agent_id)`, resolve pending control futures with error

**Control Proxy — Pending Map Pattern**:
```python
# In agents.py REST route:
_pending: dict[str, asyncio.Future] = {}

async def send_control(agent_id, message):
    future = asyncio.get_event_loop().create_future()
    _pending[message.request_id] = future
    await registry.get(agent_id).ws.send_json(message.model_dump())
    try:
        return await asyncio.wait_for(future, timeout=30.0)
    finally:
        _pending.pop(message.request_id, None)

# In agent_ws.py receive loop, when ControlResponse arrives:
if msg_type == "control_response":
    future = _pending.get(msg["request_id"])
    if future and not future.done():
        future.set_result(ControlResponse(**msg))
```
- On agent disconnect: resolve ALL pending futures with `TimeoutError` to unblock REST handlers
- Store pending map on app.state so both routes can access it

**Test Pattern — Starlette TestClient**:
```python
async with client.websocket_connect("/api/agent/connect") as ws:
    # Send auth message
    await ws.send_json({"token": "...", "registration": {...}})
    response = await ws.receive_json()
    assert response["type"] == "auth_ok"
```

**Phase 4 total**: ~2 new files + 2 modified | Est. ~300 LOC production, ~250 LOC tests

---

## Phase 5: Server Wiring

Wire all new components into server.py and create_app().

### Tasks

- [x] **5.1** Modify `packages/arcui/src/arcui/server.py` — Wire new components
  - create_app(): instantiate AgentRegistry, SubscriptionManager
  - Add agent_ws routes and agents REST routes
  - Store registry and subscription_manager on app.state
  - Update serve() if needed
  - Tests: `tests/unit/test_server.py` — verify app.state has new components, routes registered

### Research Insights

**Existing `create_app()` pattern**:
```python
# Current app.state assignments (add new ones in same pattern):
app.state.connection_manager = ConnectionManager()
app.state.aggregator = RollingAggregator(...)
app.state.event_buffer = EventBuffer(...)
app.state.auth_config = auth_config
# ADD:
app.state.agent_registry = AgentRegistry(max_agents=max_agents)
app.state.subscription_manager = SubscriptionManager()
app.state.pending_controls = {}  # shared pending map for control proxy
```

**Route Registration**:
- Agent WS: `WebSocketRoute("/api/agent/connect", agent_ws_endpoint)`
- REST: `Route("/api/agents", agents_list, methods=["GET"])`, etc.
- These go in the same `routes` list passed to `Starlette(routes=...)`

**Phase 5 total**: ~1 modified file | Est. ~40 LOC production, ~30 LOC tests

---

## Phase 6: UIReporter Module (arcagent)

Agent-side module that connects to UI and bridges internal events.

### Tasks

- [x] **6.1** Create `packages/arcagent/src/arcagent/modules/ui_reporter/__init__.py` — UIReporterModule
  - UIReporterConfig: enabled, url, token, reconnect_max_interval, buffer_size
  - Implements Module protocol (name, startup, shutdown)
  - startup: connect to UI via WebSocketTransport, register with identity/capabilities
  - Subscribe to ModuleBus events across all layers:
    - `llm:*` events → UIEvent(layer="llm")
    - `agent:pre_*`, `agent:post_*`, `agent:ready`, `agent:shutdown` etc → UIEvent(layer="agent")
    - Team events (future) → UIEvent(layer="team")
  - Also chain into TelemetryModule.on_event for direct LLM trace records
  - Handle incoming control messages → emit to ModuleBus as `ui:control`
  - Tests: `tests/unit/test_ui_reporter.py` — event wrapping, control forwarding, config validation

### Research Insights

**CRITICAL — Event Mapping Correction**:
The SDD listed `run:*` and `team:*` events, but these don't exist in the codebase:

| ModuleBus Event | Actual Prefix | UIEvent Layer |
|-----------------|---------------|---------------|
| `llm:call_complete` | `llm:` | `"llm"` |
| `llm:config_change` | `llm:` | `"llm"` |
| `llm:circuit_change` | `llm:` | `"llm"` |
| `agent:pre_tool`, `agent:post_tool` | `agent:` | `"run"` (these ARE arcrun events bridged via callback) |
| `agent:pre_plan`, `agent:post_plan` | `agent:` | `"run"` |
| `agent:init`, `agent:ready`, `agent:shutdown` | `agent:` | `"agent"` |
| `agent:pre_respond`, `agent:post_respond` | `agent:` | `"agent"` |
| `agent:error` | `agent:` | `"agent"` |
| (no team events exist yet) | — | `"team"` (future) |

Key insight: arcrun events arrive as `agent:pre_tool`, `agent:post_tool`, etc. UIReporter must distinguish these as `layer="run"` based on event_type (tool/plan events = run layer, lifecycle events = agent layer).

**Module Protocol — Follow PulseModule pattern exactly**:
```python
class UIReporterModule:
    name = "ui_reporter"

    def __init__(self, config: dict[str, Any], workspace: Path, **_kw: Any):
        self._config = UIReporterConfig(**config)
        self._workspace = workspace
        # ...

    async def startup(self, ctx: ModuleContext) -> None:
        # Subscribe with priority=200 (observational, run after business logic)
        ctx.bus.subscribe("llm:*", self._on_llm_event, priority=200)
        ctx.bus.subscribe("agent:*", self._on_agent_event, priority=200)
        # ...

    async def shutdown(self) -> None:
        # Disconnect, flush buffer
```

**Config Loading**:
- Agent TOML: `[modules.ui_reporter.config]` section
- ModuleLoader injects: config dict, workspace Path, plus eval_config, llm_config, team_config, telemetry, agent_name as **kwargs
- UIReporter only needs config + workspace (ignore rest via **_kw)

**Phase 6 total**: ~1 new file | Est. ~200 LOC production, ~150 LOC tests

---

## Phase 7: CLI Changes

New `arc ui` command and migration of `--ui` flag.

### Tasks

- [x] **7.1** Create `packages/arccli/src/arccli/ui.py` — `arc ui start` command
  - Click command group: `arc ui`
  - `arc ui start` — create_app(), uvicorn.run()
  - Options: --port, --host, --viewer-token, --operator-token, --agent-token, --max-agents
  - Print tokens and URL on startup
  - Tests: `tests/unit/test_ui_cli.py` — CLI parsing, defaults

- [x] **7.2** Modify `packages/arccli/src/arccli/main.py` — Register `arc ui` group
  - Import and add ui command group

- [x] **7.3** Modify `packages/arccli/src/arccli/agent.py` — Remove embedded UI, update --ui flag
  - Remove: `_start_ui()`, `_ensure_telemetry_for_ui()`, `_create_trace_store()`, `_inject_pricing()`, `_run_uvicorn_safe()` (~150 LOC)
  - `--ui` flag: sets ui_reporter.enabled=True in agent config (no longer starts embedded server)
  - Update `_serve()` to remove embedded uvicorn logic
  - Tests: verify agent serve still works without --ui, verify --ui doesn't start embedded server

### Research Insights

**CLI Pattern — Follow existing arccli commands**:
```python
@click.group("ui")
def ui():
    """ArcUI dashboard server."""
    pass

@ui.command("start")
@click.option("--port", default=8420, type=int)
@click.option("--host", default="127.0.0.1")
@click.option("--viewer-token", default=None)
@click.option("--operator-token", default=None)
@click.option("--agent-token", default=None)
@click.option("--max-agents", default=100, type=int)
def start(port, host, viewer_token, operator_token, agent_token, max_agents):
    ...

# In main.py:
from arccli.ui import ui
cli.add_command(ui)
```

**Signal Handling — Use event loop directly (not asyncio.run)**:
```python
loop = asyncio.new_event_loop()
shutdown_event = asyncio.Event()
loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
# Use uvicorn.Config + uvicorn.Server pattern (same as existing agent.py)
```

**Functions to Remove from `agent.py`** (lines ~1151-1319):
1. `_start_ui()` — creates app, sets globals, wires LLM events, starts uvicorn
2. `_ensure_telemetry_for_ui()` — walks module stack, chains callback
3. `_create_trace_store()` — instantiates InMemoryTraceStore
4. `_inject_pricing()` — sets global pricing dict
5. `_run_uvicorn_safe()` — uvicorn server wrapper

**Test Pattern — Click CliRunner**:
```python
from click.testing import CliRunner
from arccli.main import cli

def test_ui_start_defaults():
    runner = CliRunner()
    result = runner.invoke(cli, ["ui", "start", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.output
```

**Phase 7 total**: ~1 new file + 2 modified | Est. ~100 LOC production, ~60 LOC tests

---

## Phase 8: Integration Tests

Full pipeline tests with InMemoryTransport.

### Tasks

- [x] **8.1** Create integration test: Agent → UI → Browser event flow
  - UIReporter with InMemoryTransport connects to mock UI server
  - Emits LLM event → verify UIEvent arrives at browser queue via SubscriptionManager
  - Verify per-agent aggregator and global aggregator both ingested
  - File: `tests/integration/test_multi_agent_flow.py`

- [x] **8.2** Create integration test: Control command flow
  - Browser POSTs control to agents route
  - Verify ControlMessage reaches agent via InMemoryTransport
  - Verify ControlResponse returns to browser
  - File: `tests/integration/test_control_flow.py`

- [x] **8.3** Create integration test: Multi-agent scenario
  - 3 agents connect with different capabilities
  - Browser subscribes to specific agent + layer
  - Verify only matching events arrive
  - Verify per-agent stats correct
  - File: `tests/integration/test_multi_agent_subscription.py`

### Research Insights

**Test Infrastructure — No InMemoryTransport exists yet**:
- Must be created in Phase 1 (task 1.2) — this is a new component
- `InMemoryTransport` uses two `asyncio.Queue` instances (one per direction)
- Pair creation: `InMemoryTransport.create_pair()` → returns (client_transport, server_transport)

**Existing Test Patterns**:
- arcui tests: flat structure under `tests/unit/`, Starlette `TestClient` for HTTP/WS
- arcagent tests: three-tier (`tests/unit/core/`, `tests/unit/modules/`, `tests/integration/`)
- All packages use `asyncio_mode = "auto"` in pyproject.toml — no `@pytest.mark.asyncio` needed
- No conftest.py in arcui tests — fixtures defined inline per test file

**ModuleContext for UIReporter tests**:
```python
# Unit tests can use MagicMock:
ctx = MagicMock(spec=ModuleContext)
ctx.bus = MagicMock(spec=ModuleBus)
ctx.config = {"enabled": True, "url": "ws://localhost:8420/api/agent/connect"}

# Integration tests should use real ModuleBus:
bus = ModuleBus()
ctx = ModuleContext(bus=bus, tool_registry=..., config=..., ...)
```

**Integration Test Architecture**:
```
Test creates:
  1. Starlette TestClient with create_app()
  2. InMemoryTransport pair
  3. UIReporter with client-side transport
  4. Browser queue registered with SubscriptionManager

Test flow:
  UIReporter._on_llm_event(mock_event)
    → InMemoryTransport.send_event()
    → server-side receives via InMemoryTransport.receive()
    → EventBuffer.push()
    → SubscriptionManager.broadcast_filtered()
    → browser queue receives UIEvent
```

**Phase 8 total**: ~3 test files | Est. ~300 LOC tests

---

## Summary

| Phase | New Files | Modified Files | Removed LOC | Production LOC | Test LOC |
|-------|-----------|----------------|-------------|----------------|----------|
| 1 | 3 | 0 | 0 | ~280 | ~200 |
| 2 | 1 | 1 | 0 | ~150 | ~120 |
| 3 | 1 | 1 | 0 | ~100 | ~80 |
| 4 | 2 | 2 | 0 | ~300 | ~250 |
| 5 | 0 | 1 | 0 | ~40 | ~30 |
| 6 | 1 | 0 | 0 | ~200 | ~150 |
| 7 | 1 | 2 | ~150 | ~100 | ~60 |
| 8 | 0 | 0 | 0 | 0 | ~300 |
| **Total** | **9** | **7** | **~150** | **~1170** | **~1190** |

## Completion Tracking

- Total tasks: 16
- Completed: 16
- Remaining: 0

## Success Criteria

1. `arc ui start` launches and listens for agents
2. 2+ agents connect simultaneously via WebSocket
3. Browser receives filtered events in real-time
4. Control commands flow browser → UI → agent and back
5. Agent disconnect/reconnect works with buffered events
6. `arc agent serve` without `--ui` — no regression
7. All unit + integration tests pass
8. ruff check clean
9. mypy clean (arcui, arcagent modules, arccli changes)
