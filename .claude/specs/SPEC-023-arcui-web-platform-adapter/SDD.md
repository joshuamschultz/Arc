# SPEC-023 — Solution Design Document

> Status: draft · Pillar priority: Simplicity → Modularity → Security → Scalability
> Source of truth for detail: `.claude/brainstorms/2026-05-03-arcui-as-platform-adapter.md` (deepened, 1080 lines)

---

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│ Browser                                                          │
│  Messages page (DM list, chat pane, thread/refs panel)           │
│  Knowledge page (memory, workspace tree, graph stats)            │
└───────────────────────────────┬──────────────────────────────────┘
                                │  WSS  /ws/chat/{agent_id}
                                │  HTTPS /api/agents, /api/knowledge/{agent_id}
┌───────────────────────────────▼──────────────────────────────────┐
│ arcui (Starlette + uvicorn, in-process gateway runtime)          │
│                                                                  │
│  routes/chat_ws.py   — thin proxy, owns viewer_token             │
│  routes/knowledge.py — calls fs_reader + code-review-graph MCP   │
│  routes/agents.py    — existing                                  │
│                                                                  │
│  app.state populated by arcgateway.bootstrap.build_for_embedded: │
│    web_adapter, slack_adapter*, telegram_adapter*,               │
│    session_router, executor, stream_bridge                       │
│                                                                  │
│  *adapters present only when [platforms.X].enabled in config     │
└────────────────────────────┬─────────────────────────────────────┘
                             │  on_message callback
┌────────────────────────────▼─────────────────────────────────────┐
│ arcgateway                                                       │
│                                                                  │
│  adapters/web.py    — WebPlatformAdapter (this spec)             │
│  adapters/slack.py  — existing                                   │
│  adapters/telegram.py — existing                                 │
│  bootstrap.py       — build_for_embedded() (this spec)           │
│  identity.py        — derive_viewer_did() (this spec)            │
│  config.py          — + WebPlatformConfig                        │
│  cli.py             — + [platforms.web] wiring                   │
│  session.py         — UNCHANGED                                  │
│  executor.py        — UNCHANGED (AsyncioExecutor / Subprocess)   │
│  stream_bridge.py   — UNCHANGED                                  │
│  audit.py           — UNCHANGED                                  │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                       AsyncioExecutor → ArcAgent.run() → Result
```

**Property:** the arrows below `WebPlatformAdapter` are byte-identical to what already runs for Slack/Telegram. New platform, same plumbing.

## 2. Module Boundaries

### 2.1 Allowed imports

| Importer | Imports | Rationale |
|---|---|---|
| `arcui.*` | `arcgateway.*`, `arcllm.*` | arcui is a frontend over the gateway/runtime layer |
| `arcgateway.adapters.web` | `arcgateway.{executor,session,delivery,audit,telemetry}` | Adapter consumes gateway primitives |
| `arcgateway.bootstrap` | `arcgateway.{adapters,executor,session,stream_bridge}` | Composition root |
| `arcui/routes/chat_ws.py` | `arcgateway.{adapters.web, identity}` | Route is the input source for the web adapter |
| `arcui/routes/knowledge.py` | `arcgateway.fs_reader`, MCP tools (via standard MCP client) | Knowledge is a UI concern |

### 2.2 Forbidden imports

| Importer | MUST NOT import | Reason |
|---|---|---|
| `arcui.*` | `arcagent.*` | The whole point of the adapter pattern |
| `arcgateway.*` | `arcui.*` | One-way layering |
| `arcgateway.adapters.*` | `arcui.*`, `arcagent.*` | Adapters are platform abstractions; agent execution is the executor's job |
| `arcgateway.adapters.web` | `arcgateway.bootstrap` | Adapter is a leaf; bootstrap composes |

### 2.3 Module boundary tests

A new test in `tests/architecture/test_imports.py` shall use `archunit`-style assertions to verify these constraints. Failure of any test is a release blocker.

## 3. Component Design

### 3.1 `arcgateway.identity`

**Purpose:** single source of truth for the viewer DID derivation formula.

**Surface:**
```python
def derive_viewer_did(viewer_token: str) -> str:
    """Return a stable DID for an arcui-issued viewer token.

    Format: did:arc:viewer:<16 lowercase hex chars>.
    Deterministic — same input always produces same output.

    When arctrust DID issuance ships, replace the body with
    arctrust.resolve_or_issue(viewer_token); callers unchanged.
    """
    return "did:arc:viewer:" + hashlib.sha256(viewer_token.encode()).hexdigest()[:16]
```

**Tests:** determinism, format regex, distinct inputs produce distinct outputs.

### 3.2 `arcgateway.adapters.web.WebPlatformAdapter`

**Implements:** `BasePlatformAdapter` (Protocol in `arcgateway.adapters.base`).

**Constructor:**
```python
def __init__(
    self,
    *,
    on_message: Callable[[InboundEvent, "WebPlatformAdapter"], Awaitable[None]],
    agent_did: str,
    max_connections: int = 50,
    idle_timeout_seconds: int = 3600,
    max_frame_bytes: int = 65536,
    audit_emitter: Callable[[str, dict], None] | None = None,
) -> None: ...
```

**Public surface:**
```python
async def connect(self) -> None: ...                       # no-op
async def disconnect(self) -> None: ...                    # cancel all tasks, close sockets
async def send(self, target: DeliveryTarget,
               message: str, *, reply_to: str | None = None) -> None: ...
async def send_with_id(self, target: DeliveryTarget,
                       message: str) -> str | None: ...    # default impl returns None

def register_socket(self, ws: WebSocket, agent_did: str,
                    user_did: str, chat_id: str) -> None: ...
def unregister_socket(self, ws: WebSocket) -> None: ...
async def ingest(self, chat_id: str, text: str,
                 client_seq: int | None = None) -> None: ...
```

**Internal state:**
```python
_sockets: dict[str, set[WebSocket]]                # chat_id → sockets
_socket_meta: dict[WebSocket, tuple[str, str, str]] # ws → (chat_id, agent_did, user_did)
_socket_queues: dict[WebSocket, asyncio.Queue]     # ws → bounded queue (maxsize=100)
_socket_tasks: dict[WebSocket, list[asyncio.Task]] # ws → [drain_task, idle_task]
_last_activity: dict[WebSocket, float]             # ws → monotonic seconds
_inbound_seq: dict[str, int]                       # chat_id → highest seen client_seq
_n_connections: int                                # for max_connections enforcement
```

**Send fan-out (per FR-7, brainstorm §8 Research Insights):**

```python
async def send(self, target, message, *, reply_to=None):
    sockets = list(self._sockets.get(target.chat_id, set()))
    audit_hash = sha256(f"{turn_id}:{message}".encode()).hexdigest()
    breakdown = {"delivered": 0, "dropped_backpressure": 0, "dead": 0}

    if not sockets:
        self._audit("gateway.message.dropped",
                    {"chat_id": target.chat_id,
                     "reason": "no_socket",
                     "audit_hash": audit_hash})
        return

    payload = {"type": "message", "from": "agent",
               "text": message, "audit_hash": audit_hash,
               "ts": _utcnow()}

    for ws in sockets:
        queue = self._socket_queues.get(ws)
        if queue is None:
            breakdown["dead"] += 1
            continue
        try:
            queue.put_nowait(payload)
            breakdown["delivered"] += 1
        except asyncio.QueueFull:
            try:
                _ = queue.get_nowait()
                queue.put_nowait(payload)
                breakdown["dropped_backpressure"] += 1
            except Exception:
                breakdown["dead"] += 1

    self._audit("gateway.message.delivered",
                {"chat_id": target.chat_id,
                 "audit_hash": audit_hash,
                 "breakdown": breakdown})
```

**Per-socket drain loop:**
```python
async def _drain_loop(self, ws: WebSocket, queue: asyncio.Queue) -> None:
    try:
        while True:
            payload = await queue.get()
            try:
                await ws.send_json(payload)
                self._last_activity[ws] = time.monotonic()
            except (WebSocketDisconnect, RuntimeError):
                self.unregister_socket(ws)
                return
    except asyncio.CancelledError:
        return
```

**Inactivity monitor:**
```python
async def _inactivity_monitor(self, ws: WebSocket) -> None:
    while ws in self._socket_queues:
        idle = time.monotonic() - self._last_activity.get(ws, 0)
        if idle > self.idle_timeout_seconds:
            with contextlib.suppress(Exception):
                await ws.close(code=1000, reason="idle")
            self.unregister_socket(ws)
            return
        await asyncio.sleep(60)
```

**Ingest:**
```python
async def ingest(self, chat_id, text, client_seq=None):
    if not text:
        raise ValueError("empty text")
    if len(text.encode("utf-8")) > self.max_frame_bytes:
        raise ValueError("frame too large")
    if client_seq is not None:
        last = self._inbound_seq.get(chat_id, -1)
        if client_seq <= last:
            raise ValueError("replay")
        self._inbound_seq[chat_id] = client_seq

    meta = self._meta_for_chat_id(chat_id)
    if meta is None:
        return  # socket already unregistered
    agent_did, user_did = meta

    event = InboundEvent(
        platform="web",
        chat_id=chat_id,
        thread_id=None,
        user_did=user_did,
        agent_did=agent_did,
        session_key=build_session_key(agent_did, user_did),
        message=text,
        raw_payload={"client_seq": client_seq},
    )
    await self._on_message(event, self)
```

### 3.3 `arcgateway.bootstrap.build_for_embedded`

**Purpose:** composition root for in-process gateway runtime, callable by arcui's lifespan.

```python
async def build_for_embedded(
    team_root: Path,
    gateway_config: GatewayConfig,
) -> EmbeddedGateway:
    """Return the four components arcui needs to host the runtime.

    EmbeddedGateway is a NamedTuple of (executor, session_router,
    web_adapter, stream_bridge). Slack/Telegram adapters are also
    instantiated when their [platforms.X] blocks are enabled, but
    arcui only needs to expose web_adapter via app.state.
    """
```

**Composition steps:**
1. Build `agent_factory` that loads `ArcAgent` from `team_root/<name>_agent/arcagent.toml` (mirrors `arccli._load_arcagent`).
2. Build `executor = AsyncioExecutor(agent_factory)` (or `SubprocessExecutor` if tier=federal).
3. Build `session_router = SessionRouter(executor=executor, ...)`.
4. Build `stream_bridge = StreamBridge(session_router)`.
5. For each enabled `[platforms.X]` block, build the corresponding adapter with `on_message=session_router.handle` and register via `session_router.register_adapter(adapter)`. Only `web` is required for arcui-hosted mode; slack/telegram are optional and additive.
6. Return the named tuple.

### 3.4 `arcui/routes/chat_ws.py`

**Endpoint:** `WebSocket /ws/chat/{agent_id}`.

**Pseudocode:**
```python
async def chat_ws(ws: WebSocket) -> None:
    await ws.accept()
    role = ws.scope["state"].get("role")
    if role not in ("viewer", "operator"):
        await ws.close(code=4401, reason="auth required")
        return

    agent_id = ws.path_params["id"]
    agent = ws.app.state.roster_provider().get(agent_id)
    if agent is None:
        await ws.close(code=4404, reason="agent not found")
        return

    viewer_token = ws.scope["state"]["viewer_token"]
    user_did = derive_viewer_did(viewer_token)
    chat_id = sha256(f"{viewer_token}:{agent.did}".encode()).hexdigest()[:16]

    web_adapter = ws.app.state.web_adapter
    try:
        web_adapter.register_socket(ws, agent.did, user_did, chat_id)
    except WebAdapterFull:
        await ws.close(code=4429, reason="too many connections")
        return

    try:
        async for raw in ws.iter_text():
            try:
                frame = json.loads(raw)
                if frame.get("type") != "message":
                    continue
                await web_adapter.ingest(
                    chat_id,
                    frame["text"],
                    client_seq=frame.get("client_seq"),
                )
            except (ValueError, json.JSONDecodeError) as exc:
                await ws.send_json({
                    "type": "error",
                    "code": "malformed",
                    "message": str(exc),
                    "ts": _utcnow(),
                })
    except WebSocketDisconnect:
        pass
    finally:
        web_adapter.unregister_socket(ws)
```

**Note:** the route never passes `viewer_token` to the adapter — only the already-derived `chat_id` and `user_did`.

### 3.5 `arcui/routes/knowledge.py`

**Endpoint:** `GET /api/knowledge/{agent_id}`.

**Response shape:** see SDD §6.

**Implementation:**
1. Resolve agent from roster; 404 if not found.
2. List `team/<agent_id>_agent/memory/` via `arcgateway.fs_reader` with previews.
3. List `team/<agent_id>_agent/workspace/` via `arcgateway.fs_reader`. Top-level expansion only; directories returned with `children: null` and `child_count`. Cap at 200 entries; client requests deeper expansion via path-scoped follow-up calls.
4. Compute `context.memory_used_tokens` from agent's `LLMConfig.max_tokens` and the memory size.
5. Call `code-review-graph.list_graph_stats_tool()` via MCP client with 500ms timeout. On timeout/error, set `graph.available = false`.
6. Read recent file events from `FileChangeBridge` event ring scoped to `memory/`; surface 10 most recent.
7. Return JSON.

### 3.6 Messages page (`messages-page.js` + `index.html` block)

**Page lifecycle:**
1. On `messages` route active, fetch `GET /api/agents`.
2. Render DM list (left) with online/offline.
3. On DM selection: open WebSocket `/ws/chat/{agent_id}`.
4. On user submit: send `{"type": "message", "text": "...", "client_seq": <monotonic>}`.
5. On inbound frame:
   - `status:thinking` → show typing indicator.
   - `status:queued` → show "queued (n)".
   - `tool_call` → append inline badge to active bubble.
   - `message` → finalize bubble with text + audit_hash; reveal in thread panel.
   - `error` → show error toast.
6. On disconnect: show banner; auto-reconnect with backoff.
7. Maintain `localMessages: Map<turn_id, MessageEntry>` for reconnect history.

### 3.7 Knowledge page (`knowledge-page.js` + `index.html` block)

**Page lifecycle:**
1. On `knowledge?agent=<id>` route active, fetch `GET /api/knowledge/{agent_id}`.
2. Render five panels:
   - Context budget meter (`memory_used / context_window` progress bar).
   - Memory entries table (filename, type, size, classification, created_by, modified_at, preview).
   - Workspace tree with click-to-expand for directories.
   - Code-graph stats card (or "graph unavailable" if `available: false`).
   - Recent memory events timeline.
3. Subscribe to existing arcui WebSocket `/ws` with `subscribe: agent:{id}:memory` to receive real-time `FileChangeEvent`s; partial re-render on receive.

## 4. Configuration

### 4.1 `[platforms.web]` block

```toml
[platforms.web]
enabled = true
agent_did = "did:arc:org:agent/concierge"  # optional; falls back to [gateway].agent_did
max_connections = 50
idle_timeout_seconds = 3600
max_frame_bytes = 65536
```

**Pydantic model (added to `arcgateway/config.py`):**
```python
class WebPlatformConfig(BaseModel):
    enabled: bool = False
    agent_did: str | None = None
    max_connections: int = Field(default=50, ge=1, le=10000)
    idle_timeout_seconds: int = Field(default=3600, ge=60, le=86400)
    max_frame_bytes: int = Field(default=65536, ge=1024, le=1048576)
```

`PlatformsConfig` gains `web: WebPlatformConfig = WebPlatformConfig()`.

### 4.2 CLI wiring (`cli.py`)

Pattern mirrors Slack/Telegram blocks (`cli.py:101–193`):
```python
if config.platforms.web.enabled:
    from arcgateway.adapters.web import WebPlatformAdapter

    agent_did = config.effective_agent_did("web")
    web_adapter = WebPlatformAdapter(
        on_message=runner.session_router.handle,
        agent_did=agent_did,
        max_connections=config.platforms.web.max_connections,
        idle_timeout_seconds=config.platforms.web.idle_timeout_seconds,
        max_frame_bytes=config.platforms.web.max_frame_bytes,
    )
    runner.add_adapter(web_adapter)
```

## 5. Data Contracts

### 5.1 WebSocket message envelope

#### Inbound (browser → adapter)
```json
{ "type": "message", "text": "...", "client_seq": 1 }
```

#### Outbound (adapter → browser) — four frame types

`status` (immediately on receipt, or when queued behind a prior turn):
```json
{ "type": "status", "status": "thinking" | "queued",
  "client_seq": 1, "ts": "2026-05-03T14:22:01.123Z" }
```

`tool_call`:
```json
{ "type": "tool_call", "tool": "read_file",
  "args": "path=/workspace/plan.md",
  "turn_id": "a1b2c3d4", "ts": "2026-05-03T14:22:02.456Z" }
```

`message`:
```json
{ "type": "message", "from": "agent", "text": "...",
  "turn_id": "a1b2c3d4", "client_seq": 1,
  "audit_hash": "sha256:abcdef01...",
  "ts": "2026-05-03T14:22:03.789Z" }
```

`error`:
```json
{ "type": "error", "code": "agent_error" | "policy_denied"
        | "quota_exceeded" | "queue_full" | "agent_unavailable"
        | "frame_too_large" | "malformed",
  "message": "...", "turn_id": "a1b2c3d4",
  "ts": "2026-05-03T14:22:03.789Z" }
```

### 5.2 Knowledge response

```json
{
  "agent_id": "concierge",
  "agent_did": "did:arc:org:agent/concierge",
  "context": {
    "model": "claude-3-5-sonnet-20241022",
    "input_tokens": 200000,
    "memory_used_tokens": 45230,
    "memory_percent_of_window": 22.6,
    "truncation_policy": "recency",
    "last_truncation_at": null
  },
  "memory": {
    "entries": [
      {
        "filename": "context.md",
        "type": "text",
        "size_bytes": 4096,
        "modified_at": "2026-05-03T10:00:00Z",
        "classification": "UNCLASS",
        "created_by": "concierge",
        "preview": "First 200 chars..."
      }
    ],
    "total_bytes": 4096,
    "recent_events": [
      {
        "timestamp": "2026-05-03T10:22:01Z",
        "event_type": "entry_added",
        "filename": "context.md",
        "size_bytes": 4096,
        "by_agent": "concierge"
      }
    ]
  },
  "workspace": {
    "tree": [
      {"path": "plan.md", "type": "file",
       "size_bytes": 1024, "modified_at": "2026-05-03T09:00:00Z"},
      {"path": "notes/", "type": "dir",
       "child_count": 47, "modified_at": "2026-05-03T11:00:00Z",
       "children": null}
    ],
    "total_files": 5847,
    "truncated": true
  },
  "graph": {
    "available": true,
    "node_count": 142,
    "edge_count": 387,
    "languages": ["python", "markdown"],
    "last_indexed": "2026-05-03T08:00:00Z"
  }
}
```

## 6. Sequence Diagrams

### 6.1 Successful chat turn

```
Browser  chat_ws  WebAdapter  SessionRouter  Executor  ArcAgent
   │        │         │            │            │         │
   │ WS ────►│         │            │            │         │
   │ accept  │ regr    │            │            │         │
   │◄────────│────────►│ register   │            │         │
   │         │         │ socket     │            │         │
   │ {msg} ─►│ ingest  │            │            │         │
   │         │────────►│            │            │         │
   │         │         │ build      │            │         │
   │         │         │ InboundEv  │            │         │
   │         │         │───────────►│ handle()   │         │
   │         │         │            │ (queue,    │         │
   │         │         │            │  pairing,  │         │
   │         │         │            │  policy)   │         │
   │         │         │            │───────────►│ run()   │
   │         │         │            │            │────────►│
   │         │         │            │            │ awaits  │
   │         │         │            │            │ LLM     │
   │         │         │            │            │◄────────│
   │         │         │            │  Delta     │ Result  │
   │         │         │            │◄───────────│         │
   │         │         │ send()     │            │         │
   │         │         │◄───────────│            │         │
   │         │         │ enqueue    │            │         │
   │         │         │ + audit    │            │         │
   │ {msg} ◄─│ drain   │            │            │         │
   │         │         │            │            │         │
```

### 6.2 Knowledge fetch

```
Browser   /api/knowledge   fs_reader   MCP code-graph
   │           │              │             │
   │ GET ─────►│              │             │
   │           │ list memory  │             │
   │           │─────────────►│             │
   │           │◄─ entries ───│             │
   │           │ list ws tree │             │
   │           │─────────────►│             │
   │           │◄─ tree ──────│             │
   │           │ list_graph_stats_tool      │
   │           │ (500ms tout) │             │
   │           │─────────────────────────►  │
   │           │◄─ stats ───────────────────│
   │           │ assemble     │             │
   │ JSON ◄────│              │             │
```

## 7. Threat Model

| Threat | Mitigation | Test |
|---|---|---|
| LLM01 prompt injection | Browser input is platform-adjacent content; `SessionRouter` does not elevate user input to system instruction. Same as Slack/Telegram. | Inherited integration test |
| LLM06 excessive agency | Per-agent allowlists / policy checks gate browser requests because they go through `SessionRouter`. | `test_policy_denied_via_web` |
| LLM07 system prompt leakage | No prompt content traverses adapter or WS. | Adapter unit test verifies envelope shape |
| ASI03 identity abuse | `user_did` is deterministic from viewer token. Audit logs see one identity per browser session. | `test_register_socket_does_not_accept_viewer_token` |
| ASI07 insecure inter-agent comms | Adapter ↔ SessionRouter is in-process; browser ↔ adapter is WSS via reverse proxy or cloudflared. | E2E TLS check |
| ASI10 rogue agent | Existing audit emission unchanged; telemetry sees `platform="web"` like `platform="telegram"`. | `test_audit_emits_platform_web` |
| Replay attack | `client_seq` monotonicity check in `ingest()`. | `test_client_seq_replay_rejected` |
| DoS via oversized frames | `max_frame_bytes` cap. | `test_oversized_inbound_frame_rejected` |
| DoS via too many connections | `max_connections` cap → HTTP 429. | `test_max_connections_rejects_overflow` |
| Slow consumer stalls turn | Per-socket bounded queue + drop-oldest. | `test_send_under_backpressure_drops_oldest` |

## 8. Test Strategy

### 8.1 Unit tests — `tests/unit/test_web_adapter.py`

(Per brainstorm §11 + Research Insights additions)

- `test_register_socket_*` — 4 cases (stable chat_id, distinct tokens, unregister, last-socket cleanup)
- `test_ingest_*` — 5 cases (correct InboundEvent, empty rejected, oversized rejected, replay rejected, unknown chat_id silently noop)
- `test_send_*` — 5 cases (fan-out, no sockets, dead socket, audit_hash present, backpressure drop-oldest)
- `test_drain_loop_*` — 2 cases (handles WebSocketDisconnect, exits on cancel)
- `test_inactivity_monitor_closes_idle_socket`
- `test_max_connections_rejects_overflow`
- `test_register_socket_does_not_accept_viewer_token` (signature compliance)
- `test_audit_emit_breakdown_under_partial_fanout` (one event per turn)
- `test_connect_and_disconnect_are_noops`
- `test_tool_call_delta_sends_tool_call_frame`

Target: **>95% line coverage** on `adapters/web.py`.

### 8.2 Integration tests — `tests/integration/test_web_adapter_session.py`

- `test_browser_message_reaches_echo_executor` (full pipe with echo executor)
- `test_reconnect_preserves_session_key`
- `test_concurrent_messages_queue_correctly` (race-condition guard from session.py)
- `test_send_after_ws_disconnect_is_noop`
- `test_broadcast_to_two_sockets`
- `test_multi_adapter_routes_per_platform_user_did` (web + slack same agent)
- `test_audit_chain_records_intended_delivery_not_succeeded_only`
- `test_hot_reload_drops_browser_ws_cleanly`

### 8.3 E2E tests — `tests/e2e/test_web_chat_e2e.py`

(`@pytest.mark.e2e`, skipped unless `ARC_E2E=1`)

- `test_full_chat_turn_with_real_agent` (requires `team/`)
- `test_auth_required_on_ws_upgrade`
- `test_invalid_token_rejected`
- `test_knowledge_api_returns_correct_shape`
- `test_air_gap_no_external_calls_during_chat_turn`
- `test_self_hosted_fonts_load_offline`
- `test_knowledge_endpoint_graph_unavailable_returns_200`

### 8.4 Architecture tests — `tests/architecture/test_imports.py`

- `test_arcui_does_not_import_arcagent`
- `test_arcgateway_does_not_import_arcui`
- `test_adapters_do_not_import_arcui_or_arcagent`
- `test_web_adapter_does_not_import_bootstrap`

## 9. Quality Gates

Per `CLAUDE.md`:

| Gate | Threshold |
|---|---|
| Line coverage on new files | ≥90% (core adapter ≥95%) |
| Branch coverage | ≥75% |
| `ruff check` | 0 errors |
| `mypy --strict` on new files | 0 errors |
| Critical/high pip-audit vulns | 0 |
| Core LOC | < 3,500 (this spec adds ~600; remains within budget) |

## 10. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Hot reload breaks browser WS during dev | Acceptable for v1; documented. Long-term: separate-daemon mode. |
| Token in URL hash logged by reverse proxies | Federal Track Fix #5 — `POST /api/auth/bootstrap` |
| `localStorage` is XSS-readable | Federal Track Fix #4 — switch to `sessionStorage` |
| OOM in agent kills entire arcui (personal/enterprise) | Federal tier auto-uses `SubprocessExecutor` for isolation |
| Token rotation orphans `chat_id` | Documented edge case; v2 versioning |
| `code-review-graph` MCP unavailable | Endpoint returns `graph.available: false` (200 OK) |
| Air-gap font loading | Self-host WOFF2 in `assets/fonts/` (FR-26, NFR-9) |

## 11. Open Questions

**None at SDD signoff.** Every decision is resolved in the deepened brainstorm. If implementation surfaces a new question, log it in README.md "Learnings" and resolve before phase boundary.

## 12. Reference

- Brainstorm: `.claude/brainstorms/2026-05-03-arcui-as-platform-adapter.md`
- Solutions cross-referenced:
  - `.claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md` (tier values flow at construction)
  - `.claude/solutions/security-issues/2026-02-21-arcrun-phase4-hardening-review-learnings.md` (immutability + hash-chain audit)
- Source files (existing):
  - `packages/arcgateway/src/arcgateway/adapters/{base,slack,telegram}.py`
  - `packages/arcgateway/src/arcgateway/{session,executor,stream_bridge,runner,cli,config}.py`
  - `packages/arcui/src/arcui/{server,auth}.py`
  - `packages/arcui/src/arcui/static/{index.html,assets/arc-shell.js,assets/ws-helpers.js}`
  - `demo/{messages,knowledge}.html` (visual reference; air-gap-fix required)

---

## 13. Architecture Decisions (post-implementation)

These ADRs were captured during the post-implementation review (2026-05-04)
when three small architectural mismatches surfaced as user-visible bugs.
Each documents a principle the codebase now enforces, not an aspirational
direction.

### ADR-13.1 — arcllm traces + arcagent sessions are the single source of truth for chat history

**Context.** During SPEC-023 development we briefly considered building a
parallel ``chat_history.jsonl`` store (per ``chat_id``) so the Messages
page could replay prior conversation on reconnect. ArcAgent already
writes every chat turn to ``<workspace>/sessions/<sid>.jsonl`` via
``SessionManager``, and ArcLLM writes every model call to
``<agent_root>/traces/traces-YYYY-MM-DD.jsonl`` via ``JSONLTraceStore``.
Adding a third store would have meant three places to query, three
places to keep consistent, and three audit trails to reconcile.

**Decision.** **Reject the parallel store.** Sessions ARE the chat
history. The Messages page loads prior turns from
``GET /api/agents/{id}/sessions/{chat_id}`` (existing endpoint).
The arcllm telemetry view reads from the same JSONLTraceStore the
agent writes to. No new persistence layer is introduced by SPEC-023.

**Consequences.**

- One source of truth, three views (Sessions tab / Messages page /
  arcllm telemetry).
- Audit / replay / compaction logic stays in arcagent and arcllm
  where it belongs; arcui is a read-only consumer.
- Deleting an agent's workspace removes its chat history, sessions,
  and traces atomically — no stragglers.

### ADR-13.2 — `chat_id == build_session_key(agent_did, user_did)`

**Context.** The original SDD §FR-17 specified
``chat_id = sha256(viewer_token + agent_did)[:16]`` (per-tab uniqueness).
The arcgateway SessionRouter independently computes
``session_key = build_session_key(agent_did, user_did)``
(per-(agent, user) uniqueness). Two different 16-char hex identifiers
both flowing through the same call path. The mismatch surfaced as
"history disappears": the Messages page asked for
``/api/agents/.../sessions/<chat_id>``, but the session was actually
written under ``<session_key>``.

**Decision.** **chat_id IS session_key.** ``chat_ws_endpoint`` now
computes ``chat_id = build_session_key(agent_did, user_did)`` directly.
One conversation per ``(agent, user)`` pair across web, slack, and
telegram. Reconnects from any platform resume the same conversation.

**Consequences.**

- Slack-style UX: same DM no matter which tab/device/platform.
- Reduced viewer_token surface — the token feeds
  ``derive_viewer_did(token)`` only; ``chat_id`` hashes the derived
  ``user_did``, not the raw token.
- One identifier flows from the URL → adapter → SessionRouter →
  SessionManager → /api/agents endpoint without translation.

### ADR-13.3 — Embedded-agent fleet registration lives in arcui (`embedded_agents.py`)

**Context.** ArcUI auto-builds an embedded gateway runtime when a
``team_root`` is present. Agents loaded via that runtime are real
ArcAgent instances that answer chat traffic — but they were not
registered in ``app.state.agent_registry`` (the in-memory store the
LIVE counter and ``/api/agents`` endpoint read from), so the dashboard
showed "0 LIVE" while users were actively chatting.

The fleet registry is arcui state. Wrapping the executor's
``agent_factory`` to register loaded agents would normally belong in
the bootstrap (composition root), but the bootstrap module lives in
``arcgateway`` and must not import ``arcui`` (one-way layering, SDD §2.2).

**Decision.** A new module ``arcui/embedded_agents.py`` owns the
bridge. It calls ``executor.set_agent_factory()`` (public setter, see
ADR-13.4) on the bootstrap-built factory at lifespan startup. The
wrapper:

1. Caches loaded agents in a bounded LRU (``_DEFAULT_CACHE_MAX = 32``)
   so federal-tier deployments cannot grow unbounded.
2. Single-flights concurrent first-turns per ``agent_did`` via
   per-DID ``asyncio.Lock`` so different agents load in parallel.
3. Registers each first-load in ``app.state.agent_registry`` so the
   LIVE counter reflects reality.

**Consequences.**

- Module placement matches data ownership (registry is arcui state).
- Boundary direction preserved: ``arcui → arcgateway → arcagent``.
- Adding a non-chat embedded surface (e.g. ``arc agent serve``
  in-process) would reuse the same wrapper.

### ADR-13.4 — Public setters for `SessionRouter.adapter` and `AsyncioExecutor.agent_factory`

**Context.** Two-step construction is intrinsic to the gateway runtime:
``SessionRouter`` needs an adapter for outbound delivery; each adapter
needs ``session_router.handle`` for inbound. Building both at once is
impossible — the bootstrap composes the router first, then each
adapter, then wires them. Originally bootstrap mutated
``session_router._adapter`` directly. The same pattern repeated in
``embedded_agents`` for replacing the executor's agent factory. Both
were flagged as monkey-patching by the principled-coder review.

**Decision.** Both ``SessionRouter`` and ``AsyncioExecutor`` now expose
public setters (``set_adapter``, ``set_agent_factory``) and a public
read accessor (``agent_factory`` property). Bootstrap and
``embedded_agents`` use them; no callers touch private attributes.

**Consequences.**

- "No monkey-patching" rule (CLAUDE.md) holds.
- Hot-reload patterns (replacing an adapter at runtime) are explicitly
  supported by the public surface — documented as the intended use of
  the setters.
- New runtime composers (multi-instance, NATSExecutor) inherit the
  same wiring contract.

### ADR-13.5 — Traces always at `<agent_root>/traces/`, never in workspace

**Context.** ``arcllm.JSONLTraceStore`` documents (and intends) that
traces live OUTSIDE the agent's workspace tool sandbox per NIST AU-9
non-repudiation requirements. ArcAgent's ``_ensure_model`` was passing
``self._workspace`` (the workspace directory) to ``JSONLTraceStore``,
which then created ``<workspace>/traces/`` — inside the sandbox.
arcui's federated trace store reads from ``<agent_root>/traces/``
(correct location), so chats wrote traces but the dashboard never saw
them.

**Decision.** ``ArcAgent._ensure_model`` passes
``self._workspace.parent`` (i.e. the agent root) to ``JSONLTraceStore``.
Stale ``<workspace>/traces/`` directories were migrated and removed.

**Consequences.**

- NIST AU-9 compliance restored: tamper-evident trace chain lives
  outside the sandbox the agent's tools can write to.
- arcui's "Telemetry" view sees every chat-induced LLM call without
  any additional plumbing — the trace store is the bridge.
- The `arcllm.JSONLTraceStore` docstring is now load-bearing — its
  contract ("argument is the agent root, not the workspace") is the
  one all callers must respect.
