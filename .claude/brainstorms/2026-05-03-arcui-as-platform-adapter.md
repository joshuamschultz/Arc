---
topic: ArcUI as a Platform Adapter — Web Chat + Knowledge UI
date: 2026-05-03
status: deepened
decided: 2026-05-03
deepened: 2026-05-06
trigger: NLIT demo prep — need remote-accessible live chat with served agents
related:
  - .claude/NLIT2026-Demo-PRD.md
  - .claude/brainstorms/2026-04-27-nlit-demo-local-build.md
  - packages/arcgateway/src/arcgateway/adapters/base.py (BasePlatformAdapter Protocol)
  - packages/arcgateway/src/arcgateway/adapters/{slack,telegram}.py (reference impls)
  - packages/arcgateway/src/arcgateway/session.py (SessionRouter)
  - packages/arcgateway/src/arcgateway/executor.py (Executor / Delta / InboundEvent)
  - packages/arcgateway/src/arcgateway/stream_bridge.py (Delta → adapter.send fan-out)
  - demo/messages.html (visual reference, Slack-style DM/channels)
  - demo/knowledge.html (visual reference, knowledge viewer)
---

# ArcUI as a Platform Adapter

## Deepening Summary

**Deepened on:** 2026-05-03
**Sections enhanced:** 8 (Process model, Identity, chat_id, Multi-browser, Envelope, TOML, Send/reconnect, Knowledge contract, Test plan)
**Solutions referenced:** 2 — `2026-04-18-tier-must-flow-through-construction.md`, `2026-02-21-arcrun-phase4-hardening-review-learnings.md`
**Skills matched:** architecture-pattern-enforcer, architecture-performance-budgeter, testing-test-pyramid-designer
**Research agents:** 4 parallel (WS adapter patterns, federal compliance, knowledge UI, in-process scalability)

### Build-Plan Changes (Not Hedges — Actual Decisions)

1. **`WebPlatformAdapter.send()` does NOT use `asyncio.gather` over raw `ws.send_json()`.** It enqueues to a per-socket bounded queue (`maxlen=100`, drop-oldest) drained by a background per-socket task. This is the same `safe_enqueue` pattern already used by `arcui/ws_helpers.py:93-103`. One slow browser cannot stall the turn.
2. **`chat_id` derivation moves to `arcui/routes/chat_ws.py`, NOT inside the adapter.** The route owns the viewer token (a secret); the adapter never sees it. `register_socket()` takes the already-computed `chat_id` as an argument.
3. **`gateway_bootstrap.build_for_embedded()` lives in `arcgateway.bootstrap`, NOT `arcui.gateway_bootstrap`.** This preserves the import direction (`arcui → arcgateway`, never reversed). arcui's lifespan calls the helper; arcui owns no gateway composition logic.
4. **Single source of truth for the viewer DID formula:** new file `arcgateway/identity.py` with `derive_viewer_did(token: str) -> str`. Used by both `chat_ws.py` and any future arctrust integration. Eliminates the SHA-256 duplication smell.
5. **Idle-timeout enforcement is a per-socket inactivity monitor task spawned by the adapter at `register_socket` time.** Not the OS, not Starlette defaults. One owner, one mechanism, configurable via `[platforms.web].idle_timeout_seconds`.
6. **`code-review-graph` knowledge query uses `list_graph_stats_tool()` (~100ms), NOT `get_architecture_overview_tool()`** (slow). 500ms timeout, not 2s.
7. **arcui must self-host fonts** (Inter, JetBrains Mono WOFF2 in `arcui/static/assets/fonts/`). The current `demo/knowledge.html` references `fonts.googleapis.com` — that is an **air-gap blocker** for DOE labs and must not survive into the production page.
8. **Audit emission is synchronous and BEFORE the send attempt** in `WebPlatformAdapter.send()`. This eliminates the ordering ambiguity where a socket disconnects between the failed send and the audit emission. Outcome (`delivered` / `dropped` / `error`) is captured per-socket and batched into one event per turn with a `breakdown` field.

### New Risks Discovered (Not in Original Brainstorm)

- **Hot-reload breaks every active browser WS connection.** `arc ui start --reload` for dev is incompatible with stable demo sessions if arcui hosts the gateway runtime. Acceptable for this demo (no live audience during dev iteration); flagged as a follow-up if dev workflow becomes painful.
- **Token in URL hash is captured by reverse proxies / WAF logs.** `index.html:13-26` strips the hash with `history.replaceState` *after* read, but the initial HTTP request (with the hash in the URL) is logged by Cloudflare/WAF/SIEM systems before it reaches arcui. **Fix:** swap to `POST /api/auth/bootstrap` with the token in the JSON body. Effort: 3 hours. Blocking for FedRAMP Moderate; acceptable for this demo.
- **`AuthConfig`'s in-memory token registry won't scale to multi-instance arcui.** Tokens are process-lifetime and not shared across instances. When the multi-instance NATS path lands, swap to a shared token store (Redis or signed JWTs verified per-request).
- **Browser localStorage is XSS-readable.** `index.html:20` writes the viewer token to `localStorage`. Any XSS in arcui's static assets reads the token. **Fix:** swap to `sessionStorage` (clears on tab close). Effort: 2 hours. Should ship before the demo if practical.
- **OOM in any agent kills the entire arcui process** under in-process hosting with `AsyncioExecutor`. Federal-tier `SubprocessExecutor` mitigates this for federal deployments; personal/enterprise tiers accept the risk. Documented, not blocked.
- **Token rotation creates orphaned `chat_id`s.** If the operator regenerates the viewer token, the new `chat_id = sha256(new_token:agent_did)` is different from the old one. Old WebSockets registered under the old `chat_id` keep receiving the *agent's old session output* until they disconnect. **Fix:** version the `chat_id` map and emit a `gateway.chat_id.rotated` audit event when the operator rotates. Out of scope for v1; flagged.

### Federal Compliance Verdict (One-Glance)

| Tier | Without fixes | With Fix #1 (OS user binding) | With Fix #2 (token TTL) | With Fix #3 (TOTP) |
|---|---|---|---|---|
| FedRAMP Low | ❌ AU-3 | ✅ | ✅ | ✅ |
| FedRAMP Moderate | ❌ AU-3, IA-5 | ⚠️ Partial | ✅ | ✅ |
| FedRAMP High | ❌ IA-2, IA-5 | ❌ | ❌ | ✅ |
| CMMC L1 | ✅ | ✅ | ✅ | ✅ |
| CMMC L2 | ❌ AC-3 | ✅ | ✅ | ✅ |
| Air-gapped DOE | ✅ Today | ✅ | ✅ | ✅ |

**Headline:** ships air-gap-ready today. One fix (OS user binding via `pwd.getpwuid(os.getuid()).pw_name` in `auth.py`'s `SessionStartFields`) gets us to FedRAMP Low. The path to FedRAMP Moderate is mapped and bounded.

---

## Why

For the NLIT demo (and as the right long-term architecture), ArcUI must be able to **DM a served agent** — type a prompt, see streamed response, files materialize in the workspace pane in real time. The audience watches one URL, the demo never leaves the browser, the trace dashboard already shows everything happening underneath.

The wrong way to build this is a shortcut: an arcui HTTP route that imports `arcagent` directly and calls `ArcAgent.run()`. That's a layering violation — it makes arcui know about ArcAgent, duplicates session/queue/policy logic that already lives in arcgateway, and gives us two paths for "send a message to an agent" (gateway path for Slack/Telegram, arcui path for the browser).

The **right way**: ArcUI is another platform. Slack, Telegram, ArcUI — three peers, each implementing `BasePlatformAdapter`. The browser sends a message; arcui forwards it into the adapter; the adapter calls `SessionRouter.handle()`; the same executor + agent runs as for any other platform; the response comes back through `StreamBridge` → `WebPlatformAdapter.send()` → over the WebSocket → into the chat pane.

This makes the messaging-page UI a thin client. It also means cross-platform identity, audit, pairing, queue depth, and policy enforcement all work for browser chat *for free*, because all five run inside SessionRouter.

---

## Mental Model

**ArcUI is a chat platform whose "remote service" is the user's browser.** The adapter doesn't dial out — it accepts inbound WebSocket connections, registers each one, and treats them as the equivalent of a Telegram/Slack channel.

| Slack/Telegram | ArcUI Web Adapter |
|---|---|
| Bot token | n/a (auth via existing arcui viewer token) |
| Long-poll / RTM socket | Inbound WS connection from browser |
| `chat_id` (Slack channel / Telegram chat) | Generated per-WS connection ID (or stable per `(viewer_token, agent_did)` pair) |
| `user_id` resolution → `user_did` | `user_did = did:arc:viewer:<sha256(viewer_token)>` |
| `adapter.send(target, message)` posts to Slack API | `adapter.send(target, message)` pushes JSON to the WS keyed by `target.chat_id` |
| `connect()` opens RTM socket | `connect()` is a no-op (server-side; clients arrive when they arrive) |

Architecturally clean. Operationally one new file in arcgateway, one new file in arcui, and a UI page.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ Browser                                                          │
│  Messages page (channel list, chat pane, thread panel)           │
└────────────────────────────┬─────────────────────────────────────┘
                             │  WSS  /ws/chat/{agent_id}
┌────────────────────────────▼─────────────────────────────────────┐
│ arcui (Starlette, in-process gateway runtime)                    │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │ routes/chat_ws.py  — thin proxy, no business logic      │     │
│  │  • Auth check (existing AuthMiddleware)                 │     │
│  │  • web_adapter.register_socket(ws, agent_did, user_did) │     │
│  │  • async for msg in ws:                                 │     │
│  │      await web_adapter.ingest(chat_id, msg.text)        │     │
│  └────────────────────────┬────────────────────────────────┘     │
│                           │                                       │
│  ┌────────────────────────▼────────────────────────────────┐     │
│  │ arcgateway.adapters.web.WebPlatformAdapter              │     │
│  │  • Implements BasePlatformAdapter                       │     │
│  │  • Map: chat_id → WebSocket                             │     │
│  │  • ingest() → InboundEvent → on_message(event, self)    │     │
│  │  • send(target, msg) → JSON push to ws[target.chat_id]  │     │
│  └────────────────────────┬────────────────────────────────┘     │
└───────────────────────────┼──────────────────────────────────────┘
                            │  on_message
┌───────────────────────────▼──────────────────────────────────────┐
│ arcgateway.SessionRouter (unchanged)                             │
│  • Pairing interceptor                                           │
│  • Per-session queue (max 100, idle TTL 1h)                      │
│  • Race-safe session spawn                                       │
│  • → executor.run(event) → AsyncIterator[Delta]                  │
│  • → StreamBridge fans Delta back to adapter.send_with_id        │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                AsyncioExecutor (agent_factory loads agents
                  from team/<name>_agent/arcagent.toml)
                            │
                       ArcAgent.run(text)
```

**Key property:** the arrows below `WebPlatformAdapter` are byte-identical to what already runs for Slack/Telegram. We are not building new agent-execution plumbing — we are giving the existing plumbing a new front door.

---

## Decisions to Make

### 1. Process model

**Decision:** Option A — arcui hosts the gateway runtime in-process. One daemon, one process to start, no IPC.

**Pillar trace:**
- Simplicity: one process, one startup command, no inter-process protocol to design or debug.
- Modularity: `WebPlatformAdapter` is a standalone class in `arcgateway.adapters.web`; the bootstrap wiring in `arcui/gateway_bootstrap.py` is the only arcui-specific glue. Extracting to a separate daemon later is a pure operational change — the adapter is identical.
- Security: in-process eliminates an IPC trust boundary that would need its own mTLS, identity, and audit. Fewer surfaces = fewer vulnerabilities. The gateway's four pillars (ID, sign, authorize, audit) apply identically in-process.
- Scalability: for the personal/enterprise use case (one arcui, one user), in-process is the right ceiling. The `NATSExecutor` path is the horizontal-scale answer when it materialises; that's an executor swap, not a process topology change.

**Implementation:** `arcui/gateway_bootstrap.py` constructs `AsyncioExecutor(agent_factory=...)` + `SessionRouter` + `WebPlatformAdapter` + `StreamBridge` and stores all four on `app.state` inside the Starlette lifespan context. `create_app()` accepts an optional `gateway_config: GatewayConfig | None` parameter; when provided, `gateway_bootstrap.build(app, config)` is called at lifespan startup. The chat WS route reads `request.app.state.web_adapter`.

**Follow-ups:**
- When multi-instance is needed, swap `AsyncioExecutor` for `NATSExecutor` — process topology is unchanged.
- Federal tier: `GatewayRunner.from_config` already selects `SubprocessExecutor`; the bootstrap passes the same config path, so federal stringency is automatic.

### Research Insights

**Verdict (in-process scalability research):** Option A confirmed as production architecture, not a demo shortcut. The throughput ceiling is dominated by **LLM call latency, not WebSocket fan-out**. Three calibrated numbers:

| Scenario | Concurrent sessions | Msg/min ceiling | Bottleneck |
|---|---|---|---|
| 4-core laptop, cloud LLM | 20–40 | 240–480 | LLM HTTP RTT (1–3s) |
| 16-core lab workstation, local Ollama | 100–200 | 1200–2400 | SessionRouter queue depth (max 100/session) |
| m5.4xlarge cloud VM | 200–400 | 2400–4800 | Same as workstation |

**10k msg/min target:** 500–1000 msg/min/instance × 10–20 instances. Trivial for `AsyncioExecutor` because LLM latency dominates the per-turn duration; you only need 2–4 concurrent sessions per instance. The `NATSExecutor` swap is for **execution distribution**, not WebSocket fan-out (which stays per-arcui-instance).

**Air-gap impact (counter-intuitive):** local Ollama on a 16-core lab workstation runs 6–10× faster than cloud LLM. The in-process design's ceiling is **higher** in air-gapped DOE deployments, not lower. The SessionRouter queue depth (`max 100/session` per `session.py:208-209`), not the LLM, becomes the next constraint.

**Module-boundary correction:** `gateway_bootstrap` should live in `arcgateway.bootstrap.build_for_embedded()`, NOT in `arcui/gateway_bootstrap.py`. arcui currently does not own gateway composition; moving composition into arcui reverses the layering. New file:

```python
# packages/arcgateway/src/arcgateway/bootstrap.py
async def build_for_embedded(
    team_root: Path,
    gateway_config: GatewayConfig,
) -> tuple[AsyncioExecutor, SessionRouter, WebPlatformAdapter, StreamBridge]:
    """Build in-process gateway components for embedding in arcui's lifespan."""
```

arcui's `server.py` lifespan calls this once at startup and stores the four components on `app.state`. Build Order step 3 is updated accordingly.

**Multi-adapter coexistence:** `[platforms.web]` + `[platforms.slack]` + `[platforms.telegram]` all run simultaneously inside arcui-hosted mode. `_wire_adapters()` in `arcgateway/cli.py:101-193` is platform-agnostic; each adapter gets its own input source (Slack RTM, Telegram long-poll, browser WS). No port conflicts. Same SessionRouter, same StreamBridge fan-out, same agent. **A user on Telegram and a user on the browser can hit the same `agent_did` simultaneously**; SessionRouter handles them as two separate sessions keyed by their respective `user_did`.

**Surprising edge cases:**
- **Hot reload kills every browser WS** (Slack/Telegram reconnect transparently; browsers see "connection lost"). Acceptable for the demo — flagged as a follow-up if dev workflow becomes painful.
- **`SubprocessExecutor` startup stall** (federal tier): first message after a worker crash adds 100–500ms latency for respawn. A separate-daemon model would pre-warm workers; in-process accepts the cold start.
- **OOM in any agent kills entire arcui** under personal/enterprise (`AsyncioExecutor`). Federal tier with `SubprocessExecutor` isolates this. Documented, accepted at the personal/enterprise tier.
- **Implicit single-tenancy:** one arcui instance = one operator's agent fleet. Multi-tenancy ("Group A and Group B share a workstation") would require either multiple arcui instances or a tenant-aware SessionRouter — neither in v1.

**References:** `packages/arcgateway/src/arcgateway/runner.py:199-247`, `packages/arcgateway/src/arcgateway/executor.py:290-310` (NATSExecutor stub), `packages/arcgateway/src/arcgateway/cli.py:101-193`.

---

### 2. User identity in the WS

**Decision:** Anonymous-but-stable: `user_did = "did:arc:viewer:" + hashlib.sha256(viewer_token.encode()).hexdigest()[:16]`.

**Pillar trace:**
- Simplicity: derivable in one line from the token already in the request; no round-trip to an identity service.
- Modularity: `build_session_key(agent_did, user_did)` already produces a stable cross-session key — the formula works unchanged with this DID.
- Security: the DID is deterministic per token (same user across browser refreshes = same session = same audit trail), but the viewer token is never logged and the DID cannot be reversed to the token (SHA-256 preimage). Meets NIST AU-3 (auditable identity per session).
- Scalability: zero coordination — each arcui process derives the DID independently from the token it holds.

**Implementation:** In `routes/chat_ws.py`, derive `user_did` from `request.state` (the token is already present after `AuthMiddleware`). `user_did = "did:arc:viewer:" + hashlib.sha256(token.encode()).hexdigest()[:16]`. Pass to `web_adapter.register_socket(ws, agent_did, user_did)`.

**Follow-ups:** When arctrust DID issuance is available, `chat_ws.py` can call `arctrust.resolve_or_issue(viewer_token) -> did` in place of the hash. The rest of the stack (SessionRouter, audit) is unchanged because the DID string format is identical.

### Research Insights

**Federal verdict (compliance research):** the design **fails NIST AU-3** out of the box. The viewer DID attributes audit events to a token hash, not to an OS-identified user. Five fixes are mapped:

| # | Fix | Effort | Tier unlocked |
|---|---|---|---|
| 1 | Bind DID to OS user via `pwd.getpwuid(os.getuid()).pw_name` in `SessionStartFields`; add `uid`/`username` fields to the `gateway.session.start` audit event | 1h | FedRAMP Low |
| 2 | Token TTL + revocation list (`viewer_token_ttl_seconds=3600`, `_revoked_tokens: set[str]`) in `arcui/auth.py` | 4h | FedRAMP Moderate (combined with #1) |
| 3 | Conditional TOTP/U2F MFA at `tier=federal` before WS upgrade | 6h | FedRAMP High |
| 4 | `localStorage` → `sessionStorage` in `index.html:20`; eliminate XSS-readable token storage | 2h | Reduces token exposure |
| 5 | Replace URL-hash auth bootstrap with `POST /api/auth/bootstrap`; eliminate WAF/proxy log capture of token | 3h | FedRAMP Moderate gating |

**Air-gap verdict:** YES, ships air-gap-ready as specified. `AuthConfig.validate_token()` is in-memory constant-time comparison, no outbound calls. The OS authentication boundary (Windows/macOS login) is the primary security boundary in DOE labs; the viewer token is the second factor that "the human at the keyboard authorized this agent action." This is the *principle of least authentication* in air-gapped contexts — the perimeter does most of the work.

**arctrust migration verdict:** the one-line swap **is** clean, but with one hidden assumption to lock in: `arctrust.resolve_or_issue(token)` must be **deterministic** — same token, same returned DID, every call. `build_session_key()` (`session.py:102-121`) hashes `(agent_did, user_did)` to produce the session key, so any randomness in the DID string (timestamp, nonce) would split sessions. Document the determinism requirement on the arctrust API surface before the swap.

**Comparable references:**
- **JupyterHub** (the federal lab standard): signed JWT with `sub=username, exp=...`, OS-user binding via `sub`, refresh via OAuth refresh_token. Strongest reference for "right answer at federal scale."
- **HashiCorp Vault**: configurable lease duration with token renewal flow + explicit revocation endpoint. Practical reference for our token TTL design.
- **Open WebUI / LibreChat**: localStorage + HTTPS only — the "consumer" approach we are explicitly rejecting.

**Module-boundary correction (consequential):** the SHA-256 derivation appears in two places (arcui's `chat_ws.py` for WebSocket use; conceptually anywhere arctrust would later read it). **Create a single helper:**

```python
# packages/arcgateway/src/arcgateway/identity.py (new file)
def derive_viewer_did(viewer_token: str) -> str:
    """Derive a stable viewer DID from an arcui-issued viewer token.
    
    Single source of truth. Swap to arctrust.resolve_or_issue() at migration.
    """
    return "did:arc:viewer:" + hashlib.sha256(viewer_token.encode()).hexdigest()[:16]
```

**Build Order step 0** is added: create `arcgateway/identity.py` with this helper. arcui's `chat_ws.py` imports it. No duplication, one swap point for arctrust.

**References:** `packages/arcui/src/arcui/auth.py` (token issuance + `SessionStartFields`), `packages/arcui/src/arcui/static/index.html:13-26` (URL hash bootstrap — known leak point), `packages/arcgateway/src/arcgateway/session.py:102-121` (session keying — format-agnostic, safe for arctrust swap).

**Solutions archive match:** `2026-04-18-tier-must-flow-through-construction.md` — tier values must flow through construction, not per-call. Same principle applies here: the operator-issued token is constant over the arcui process lifetime; do NOT pass it through every request derivation. Compute the DID once at session start (already the design); document that the formula is process-lifetime-stable.

---

### 3. `chat_id` derivation

**Decision:** Stable per `(viewer_token, agent_did)` — `chat_id = hashlib.sha256(f"{viewer_token}:{agent_did}".encode()).hexdigest()[:16]`.

**Pillar trace:**
- Simplicity: one-liner, no state to persist, deterministic on both sides.
- Modularity: `SessionRouter.build_session_key` already does the same SHA-256-truncation pattern — this is consistent.
- Security: same preimage-resistance argument as the user DID. The viewer token never appears in the chat_id. A different token (different user) gets a different chat_id and thus a separate SessionRouter session — no cross-user session bleed.
- Scalability: stateless derivation means any arcui instance that holds the token can derive the correct chat_id without coordination.

**Implementation:** `WebPlatformAdapter.register_socket(ws, agent_did, user_did, viewer_token) -> chat_id`. The `chat_id` is computed inside `register_socket` and returned to the caller for use in the ingest loop.

### Research Insights

**Consequential correction:** the **viewer token is a secret** and should not enter the adapter. The Slack adapter does not handle Slack tokens; the Telegram adapter does not handle bot tokens past `connect()`. Both adapters work in terms of *platform user_ids*, never in terms of secrets. By the same principle:

**The route — not the adapter — derives `chat_id`.**

```python
# arcui/routes/chat_ws.py (the route owns auth/secrets)
chat_id = hashlib.sha256(f"{viewer_token}:{agent_did}".encode()).hexdigest()[:16]
user_did = arcgateway.identity.derive_viewer_did(viewer_token)
adapter.register_socket(ws, agent_did, user_did, chat_id)
```

The adapter's signature changes:

```python
# Was: register_socket(ws, agent_did, user_did, viewer_token) -> chat_id
# Is:  register_socket(ws, agent_did, user_did, chat_id) -> None
```

The adapter **never touches `viewer_token`**. If a future audit event in the adapter needs to be keyed by `chat_id`, it is keyed by `chat_id` — the secret never appears in adapter internals or audit metadata.

**Why this matters:** `arcgateway/audit.py` already has a `target` field on every audit event. With the original design, an audit event from inside `WebPlatformAdapter` could accidentally include `chat_id` plus other context that, combined, narrows toward token recovery. With the corrected boundary, the adapter only ever sees the *output* of the hash; reversing the hash to the token is computationally infeasible.

**`chat_id` collision under token rotation (new edge case):** if the operator regenerates the viewer token mid-session, `sha256(new_token:agent_did)` is different. Old WebSockets registered under the old `chat_id` are orphaned. **Mitigation:** version the chat_id map and emit `gateway.chat_id.rotated` audit. Out of scope for v1; flagged as a follow-up. Token rotation is rare (session-lifetime tokens); this is an edge case to document, not preemptively engineer.

**References:** `packages/arcgateway/src/arcgateway/adapters/slack.py:389-396` (Slack uses platform user_id, not the bot token, for keying); `packages/arcgateway/src/arcgateway/adapters/telegram.py:613-626` (same pattern).

---

### 4. Multiple browsers, same chat

**Decision:** Confirmed — `WebPlatformAdapter.send()` broadcasts to all registered sockets for a `chat_id`. If two browser tabs hold the same `viewer_token` and open the same agent DM, both receive the response.

**Pillar trace:**
- Simplicity: one loop over `_sockets[chat_id]` — no conditional, no "primary" socket concept.
- Modularity: StreamBridge calls `adapter.send(target, message)` with no knowledge of socket count; fan-out is entirely inside the adapter, as it should be.
- Security: both sockets are authenticated with the same token — same identity, same trust level. No elevation possible.
- Scalability: expected fan-out is 1–3 tabs. `asyncio.gather` over the sends makes them concurrent and non-blocking.

**Implementation:** `_sockets: dict[str, set[WebSocket]]`. `send()` does `await asyncio.gather(*[ws.send_json(payload) for ws in self._sockets.get(target.chat_id, set())])`. Dead sockets are silently removed (catch `WebSocketDisconnect` / `RuntimeError` per socket, remove from the set, do not raise).

### Research Insights

**Consequential correction:** `asyncio.gather` over raw `ws.send_json()` calls is the **wrong fan-out primitive**. One slow consumer (laptop on bad wifi, mobile browser sleeping) blocks the gather, which blocks the turn-completion path, which blocks the next turn for that session. The arcui codebase already has the right pattern in `packages/arcui/src/arcui/ws_helpers.py:93-103` — `safe_enqueue` to a per-socket `deque(maxlen=N)` with drop-oldest semantics, drained by a background per-socket task.

**Revised fan-out model:**

```python
class WebPlatformAdapter:
    _sockets: dict[str, set[WebSocket]]            # chat_id → sockets
    _socket_queues: dict[WebSocket, asyncio.Queue] # per-socket bounded queue, maxsize=100
    _socket_tasks: dict[WebSocket, asyncio.Task]   # background drain task per socket

    async def send(self, target, message):
        sockets = list(self._sockets.get(target.chat_id, set()))
        for ws in sockets:
            queue = self._socket_queues.get(ws)
            if queue is None:
                continue
            try:
                queue.put_nowait(payload)  # non-blocking
            except asyncio.QueueFull:
                # drop-oldest: discard one, then enqueue
                _ = queue.get_nowait()
                queue.put_nowait(payload)
                self._audit("gateway.message.dropped", {"reason": "backpressure"})
```

A separate per-socket `_drain_loop(ws, queue)` task awaits `queue.get()` and does the actual `ws.send_json(payload)`. Drain tasks are cancelled on `unregister_socket(ws)`. **Result:** turn completion never blocks on socket I/O.

**Audit emission ordering:** the original spec emits the audit event **after** the send. Under fan-out, this creates ambiguity (one socket succeeded, one failed → two events in arbitrary order, same `turn_id`, different outcomes). Revised: emit **one** audit event per turn with a `breakdown` payload:

```json
{
  "action": "gateway.message.delivered",
  "target": "chat_id:turn_id",
  "outcome": "partial",
  "breakdown": {"delivered_to": 2, "dropped": 1, "reasons": ["backpressure"]}
}
```

This matches the pattern in `2026-02-21-arcrun-phase4-hardening-review-learnings.md` (immutability + hash-chain): one event, one canonical record per turn.

**Existing patterns to reuse:**
- `arcui/ws_helpers.py:93-103` (`safe_enqueue` with drop-oldest)
- `arcui/connection.py` (`ConnectionManager` already does per-connection queues)
- `arcui/event_buffer.py` (bounded ring buffer for replay on reconnect)

The web adapter does **not** reinvent these — it composes them.

**Edge case (laptop sleep race):** browser closes the WS without sending a close frame. Starlette doesn't surface `WebSocketDisconnect` until the next `receive_text()`. The drain loop, however, raises `RuntimeError: WebSocket is not connected` on `send_json()`. Catch this in the drain loop and call `unregister_socket(ws)` — do not raise.

---

### 5. Capability surface beyond text

**Decision:** Confirmed as specified.
- File uploads: deferred. `BasePlatformAdapter` has no `send_file` method; adding one is a separate design effort.
- Rich content: markdown rendering is a browser-side concern — the adapter sends plain text; `messages-page.js` pipes it through the existing `markdown.js` renderer.
- Tool-call visibility: `Delta(kind="tool_call")` renders as an inline `<span class="tool-badge">tool: {name}</span>` in the chat pane. The adapter sends a separate JSON envelope with `"type": "tool_call"` (see envelope spec below). The browser appends it inline before the final reply.

**Implementation:** Browser JS in `messages-page.js` handles `type: "tool_call"` frames by appending a badge to the active message bubble. No server-side change needed beyond the envelope spec below.

---

## Things to Flesh Out

### 6. WebSocket message envelope

Both directions use JSON frames. The WebSocket is authenticated via the existing `AuthMiddleware` token before the upgrade (the `Authorization` header is sent on the initial HTTP upgrade request by the browser's fetch polyfill). No auth frame is needed in-band.

#### Inbound (browser → adapter)

```json
{
  "type": "message",
  "text": "What is the capital of France?",
  "client_seq": 1
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `type` | `"message"` | yes | Only `"message"` in v1. Extensible for future `"command"` etc. |
| `text` | string | yes | User's plaintext input. Max 8192 chars; adapter enforces. |
| `client_seq` | integer | no | Client-assigned monotonic sequence number. Echoed back in the outbound ack for ordering. |

#### Outbound (adapter → browser)

Four frame types. The browser dispatches on `type`.

**`status` frame** — sent immediately on message receipt, before the agent turn starts:
```json
{
  "type": "status",
  "status": "thinking",
  "client_seq": 1,
  "ts": "2026-05-03T14:22:01.123Z"
}
```

**`tool_call` frame** — emitted once per `Delta(kind="tool_call")` as they arrive:
```json
{
  "type": "tool_call",
  "tool": "read_file",
  "args": "path=/workspace/plan.md",
  "turn_id": "a1b2c3d4",
  "ts": "2026-05-03T14:22:02.456Z"
}
```

**`message` frame** — the agent's full reply, sent once the turn completes:
```json
{
  "type": "message",
  "from": "agent",
  "text": "The capital of France is Paris.",
  "turn_id": "a1b2c3d4",
  "client_seq": 1,
  "audit_hash": "sha256:abcdef01...",
  "ts": "2026-05-03T14:22:03.789Z"
}
```

**`error` frame** — sent when the agent turn fails:
```json
{
  "type": "error",
  "code": "agent_error",
  "message": "Agent execution failed",
  "turn_id": "a1b2c3d4",
  "ts": "2026-05-03T14:22:03.789Z"
}
```

| Field | Present in | Notes |
|---|---|---|
| `type` | all | Dispatch key. |
| `ts` | all | ISO 8601 UTC. Wall clock + monotonic diff captured at emission. |
| `turn_id` | `tool_call`, `message`, `error` | UUID from `Delta.turn_id`. Correlates frames within one agent turn. |
| `client_seq` | `status`, `message` | Echoed from inbound frame for client-side ordering. |
| `audit_hash` | `message` | SHA-256 of `f"{turn_id}:{text}"` — browser-verifiable tamper evidence. Matches the `SignedChainSink` entry emitted by the audit pipeline for this turn. |

The `audit_hash` is computed by `WebPlatformAdapter.send()` before pushing the frame and is also emitted via `arcgateway.audit.emit_event` so the server-side chain and the browser-visible hash are derived from the same input.

### Research Insights

**Audit emission ordering correction:** compute `audit_hash` and emit the audit event **before** the send-attempt, not after. The hash is over the *intended* payload, which is what the audit chain should record regardless of delivery outcome. Delivery outcome is a separate breakdown field on the same audit event (see Section 4 Research Insights — single-event-per-turn pattern).

**Frame envelope additions identified by research:**

- **`status: "thinking"` frame** — already in spec. Good.
- **Add `status: "queued"` frame** — emitted when SessionRouter queues the message because a prior turn is still in flight (`session.py:208-209` enforces `max=100/session`). The browser can surface "queued (position 3/100)" so users understand why the agent isn't responding immediately.
- **Add `error.code` taxonomy** — minimum set: `agent_error`, `policy_denied`, `quota_exceeded`, `queue_full`, `agent_unavailable`. The browser can render different UI per code.
- **Drop `client_seq` from outbound `tool_call` frames** — tool calls are server-emitted, not request-correlated. Echo `client_seq` only on `status` and `message` frames where it actually correlates with an inbound request.

**Replay-attack consideration:** the inbound `client_seq` is monotonic per browser connection. A replayed frame with the same `client_seq` should be detectable. `WebPlatformAdapter.ingest()` SHOULD track the highest seen `client_seq` per `chat_id` and reject lower-or-equal sequence numbers. Reference: HashiCorp Vault's request-signing pattern. Effort: 1 hour. Hardens against a captured-and-replayed message.

**Frame size cap:** add `max_frame_bytes = 65536` to the adapter. Reject inbound frames over this size with an `error` frame; refuse outbound frames over this size with truncation + audit event. Mirrors Slack's 40k message limit and Telegram's 4096 char limit. Without this cap, the WebSocket is a DoS surface.

**Reference systems:**
- **LangSmith trace UI** uses a similar four-type frame model (`status`, `tool_call`, `message`, `error`) with a `parent_run_id` field for hierarchical correlation. We don't need hierarchy in v1, but if/when agents call other agents, `parent_turn_id` becomes the right field.
- **AutoGen Studio** streams tool calls as separate frames (matches our design) but also emits a `tool_result` frame. Our `Delta(kind="tool_call")` collapses call+result into one delta. Consider splitting in v2 for richer UI.

---

### 7. `[platforms.web]` TOML shape

Mirrors the telegram/slack block pattern in `arcgateway/config.py`. No credentials (auth is via the existing arcui viewer token).

```toml
[platforms.web]
enabled = true
# DID of the agent this web adapter serves. Omit to fall back to [gateway].agent_did.
agent_did = "did:arc:org:agent/concierge"
# Maximum concurrent WebSocket connections. Default 50.
max_connections = 50
# Idle timeout in seconds. Connections silent for longer than this are closed. Default 3600.
idle_timeout_seconds = 3600
```

Config model (`arcgateway/config.py`):

```python
class WebPlatformConfig(BaseModel):
    enabled: bool = False
    agent_did: str | None = None
    max_connections: int = 50
    idle_timeout_seconds: int = 3600
```

`GatewayConfig.platforms` grows a `web: WebPlatformConfig = WebPlatformConfig()` field.

`effective_agent_did("web")` already works because `GatewayConfig.effective_agent_did(platform)` falls back to `[gateway].agent_did` when the platform-level DID is `None`.

In `arcgateway/cli.py`, `_wire_adapters()` gains a web block after Slack:

```python
if config.platforms.web.enabled:
    from arcgateway.adapters.web import WebPlatformAdapter
    agent_did = config.effective_agent_did("web")
    web_adapter = WebPlatformAdapter(
        on_message=runner.session_router.handle,
        agent_did=agent_did,
        max_connections=config.platforms.web.max_connections,
        idle_timeout_seconds=config.platforms.web.idle_timeout_seconds,
    )
    runner.add_adapter(web_adapter)
```

arcui's `gateway_bootstrap.py` instantiates the adapter the same way but reads config from the same TOML via `GatewayConfig.from_toml`.

### Research Insights

**Bootstrap location correction:** the helper that builds gateway components for embedding lives in `arcgateway.bootstrap.build_for_embedded()`, NOT `arcui/gateway_bootstrap.py`. The original spec placed it in arcui; research confirmed this reverses the layering. arcui's `server.py` lifespan calls the helper; arcui owns no gateway composition.

**Idle-timeout enforcement clarification:** the `idle_timeout_seconds` field is **enforced by a per-socket inactivity monitor task spawned by the adapter at `register_socket` time** — not by Starlette, not by the OS TCP timeout, not by uvicorn. One owner, one mechanism:

```python
async def _socket_inactivity_monitor(self, ws: WebSocket, chat_id: str):
    while ws in self._socket_queues:
        if time.monotonic() - self._last_activity[ws] > self.idle_timeout_seconds:
            await ws.close(code=1000, reason="idle")
            self.unregister_socket(ws)
            return
        await asyncio.sleep(60)  # re-check every minute
```

`self._last_activity[ws]` is updated by `ingest()` (inbound activity) and the drain loop (outbound activity).

**Connection limit clarification:** `max_connections` is **per adapter instance**, not per chat_id. With `max_connections=50` and average 2 sockets per chat_id, the adapter holds ~25 active chats. At 50 sockets the next `register_socket` call rejects with an HTTP 429 from the route. Document this; do NOT silently drop the oldest registration.

**Add `max_frame_bytes`** field to the TOML:

```toml
[platforms.web]
enabled = true
agent_did = "did:arc:org:agent/concierge"
max_connections = 50
idle_timeout_seconds = 3600
max_frame_bytes = 65536  # NEW — rejects oversized inbound frames; truncates outbound
```

`WebPlatformConfig` gets a `max_frame_bytes: int = 65536` field. Inbound frames over the cap return an `error` frame with `code: "frame_too_large"`. Outbound frames over the cap are truncated with an audit event.

---

### 8. `WebPlatformAdapter.send()` fan-out and reconnect handling

```python
async def send(
    self,
    target: DeliveryTarget,
    message: str,
    *,
    reply_to: str | None = None,
) -> None:
    sockets = set(self._sockets.get(target.chat_id, set()))  # snapshot
    if not sockets:
        # Browser disconnected before response arrived. Log and return — do not
        # raise. The session already completed; re-delivery on reconnect is
        # handled by the reconnect path below.
        _logger.info(
            "WebPlatformAdapter.send: no sockets for chat_id=%s — response discarded",
            target.chat_id,
        )
        self._audit("gateway.message.dropped", {"chat_id": target.chat_id, "reason": "no_socket"})
        return

    audit_hash = _compute_audit_hash(target.chat_id, message)
    payload = {
        "type": "message",
        "from": "agent",
        "text": message,
        "audit_hash": audit_hash,
        "ts": _utcnow(),
    }
    dead: set[WebSocket] = set()
    results = await asyncio.gather(
        *[_safe_send(ws, payload) for ws in sockets],
        return_exceptions=True,
    )
    for ws, result in zip(sockets, results):
        if isinstance(result, Exception):
            dead.add(ws)
            _logger.debug("WebPlatformAdapter.send: removing dead socket chat_id=%s", target.chat_id)

    if dead:
        surviving = self._sockets.get(target.chat_id, set()) - dead
        if surviving:
            self._sockets[target.chat_id] = surviving
        else:
            self._sockets.pop(target.chat_id, None)
```

**Reconnect behavior when browser drops mid-response:**

The response is NOT queued for re-delivery. Rationale: the agent turn already completed — the `Delta(kind="done")` was emitted and the SessionRouter session closed. Queuing would require durable storage (wrong scope) and the user has no expectation of delivery to a disconnected tab.

On reconnect, the browser opens a new WebSocket. `register_socket()` derives the same `chat_id` (same token + agent DID) and re-registers. The chat history is rendered from the browser's local message store (the JS layer maintains an in-memory array of sent/received frames indexed by `turn_id`). If the browser was closed entirely, the session is cold-started: the user re-prompts.

This is the same behavior as Telegram/Slack: if you close the app mid-delivery, you see the message when you re-open because the platform buffered it. We are not a platform with durable push; we are a thin WS pipe. No durable buffer in v1.

**`_safe_send` helper:**

```python
async def _safe_send(ws: WebSocket, payload: dict[str, Any]) -> None:
    """Send JSON to a WebSocket; raises on failure so gather can collect it."""
    await ws.send_json(payload)
```

### Research Insights — Pseudocode Replacement

**The `asyncio.gather` + `_safe_send` pattern shown above is REPLACED.** Per Section 4 Research Insights, the production pattern is per-socket bounded queue with drop-oldest, drained by a background per-socket task. The pseudocode now is:

```python
async def send(
    self,
    target: DeliveryTarget,
    message: str,
    *,
    reply_to: str | None = None,
) -> None:
    sockets = list(self._sockets.get(target.chat_id, set()))
    if not sockets:
        self._audit("gateway.message.dropped", {
            "chat_id": target.chat_id,
            "reason": "no_socket",
        })
        return

    audit_hash = _compute_audit_hash(target.chat_id, message)
    payload = {
        "type": "message",
        "from": "agent",
        "text": message,
        "audit_hash": audit_hash,
        "ts": _utcnow(),
    }
    # Audit BEFORE the enqueue — the chain records intended delivery,
    # not just succeeded delivery. Outcome breakdown is per-socket below.
    breakdown = {"delivered": 0, "dropped_backpressure": 0, "dead": 0}

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

    self._audit("gateway.message.delivered", {
        "chat_id": target.chat_id,
        "audit_hash": audit_hash,
        "outcome": "ok" if breakdown["delivered"] > 0 else "all_dropped",
        "breakdown": breakdown,
    })


async def _drain_loop(self, ws: WebSocket, queue: asyncio.Queue) -> None:
    """Per-socket background task: drain the queue to ws.send_json."""
    try:
        while True:
            payload = await queue.get()
            try:
                await ws.send_json(payload)
                self._last_activity[ws] = time.monotonic()
            except (WebSocketDisconnect, RuntimeError):
                # Socket dead — terminate this drain loop.
                self.unregister_socket(ws)
                return
    except asyncio.CancelledError:
        return
```

**Reconnect behavior — confirmed correct:** no re-delivery. The browser maintains `turn_id`-indexed local message store; on reconnect, the chat history is rendered from local state. If the browser was closed entirely, the session is cold-started. Same as Telegram/Slack mid-delivery on a closed app — except those platforms have durable buffers, and we explicitly do not.

**Edge case (audit ordering under fanout):** original spec emitted one event per socket → out-of-order under async scheduling. Revised to one event per turn with `breakdown` field. See Section 4 Research Insights.

---

### 9. Identity migration plan

The anonymous-but-stable DID (`did:arc:viewer:<sha256[:16]>`) is **production-ready for personal and enterprise tiers**. It satisfies:
- Stable cross-session identity (same token = same DID = same SessionRouter session).
- Audit attribution (the DID appears in all audit events; it is deterministic from the token without exposing the token).
- Non-repudiation at the tier level: the token is issued once by `AuthConfig` and stored nowhere except the URL hash / operator record.

**Migration to arctrust-issued DID** (no timeline, but here is the exact path):

1. `arctrust` gains a `resolve_or_issue(viewer_token: str) -> str` function that maps the token to an arctrust DID, creating one if the token has never been seen.
2. `routes/chat_ws.py` calls `arctrust.resolve_or_issue(token)` instead of the inline SHA-256 derivation. The signature is identical in shape — `user_did: str`.
3. The rest of the stack (SessionRouter, StreamBridge, audit) is unchanged.
4. Existing sessions migrate automatically: the first reconnect after the arctrust integration goes live issues a new DID. The prior anonymous DID is orphaned (old session history is not migrated — that is an explicit non-goal; sessions are ephemeral).

The code change is a one-line swap in `chat_ws.py`. There is no migration helper, no compatibility shim.

---

### 10. Knowledge page data contract

`GET /api/knowledge/{agent_id}` returns a single JSON object. `agent_id` is the agent's directory name (same key used by `team_roster`).

```json
{
  "agent_id": "concierge",
  "agent_did": "did:arc:org:agent/concierge",
  "memory": {
    "entries": [
      {
        "filename": "context.md",
        "size_bytes": 4096,
        "modified_at": "2026-05-03T10:00:00Z",
        "preview": "First 200 chars of the file..."
      }
    ],
    "total_bytes": 4096
  },
  "workspace": {
    "tree": [
      {"path": "plan.md", "size_bytes": 1024, "modified_at": "2026-05-03T09:00:00Z"},
      {"path": "outputs/report.txt", "size_bytes": 2048, "modified_at": "2026-05-03T11:00:00Z"}
    ],
    "total_files": 2,
    "total_bytes": 3072
  },
  "graph": {
    "available": true,
    "node_count": 142,
    "edge_count": 387,
    "last_indexed": "2026-05-03T08:00:00Z"
  }
}
```

**Field notes:**

| Field | Source | Notes |
|---|---|---|
| `memory.entries` | `arcgateway.fs_reader` scoped to `team/<agent_id>_agent/memory/` | Preview is first 200 chars of the file. Omit binary files. |
| `workspace.tree` | `arcgateway.fs_reader` scoped to `team/<agent_id>_agent/workspace/` | Flat list; max 200 entries. Pagination deferred. |
| `graph.available` | Check if `code-review-graph` MCP server has indexed this agent's repo | `false` if the graph server is not reachable or the repo is not indexed. |
| `graph.node_count` / `edge_count` | `code-review-graph` `get_architecture_overview` | Only populated when `available: true`. |
| `graph.last_indexed` | `code-review-graph` metadata | ISO 8601 UTC. |

**Error responses:**
- `404` if `agent_id` is not in the roster.
- `200` with `graph.available: false` if the graph server is down — do not 500.

**Implementation:** `arcui/routes/knowledge.py`. Route handler reads memory and workspace via `arcgateway.fs_reader` (already used by agent-detail routes). Graph stats are fetched via the `code-review-graph` MCP tool with a 2-second timeout; on timeout or error, return `{"available": false}`.

### Research Insights

**Air-gap blocker identified:** `demo/knowledge.html:7-8` references `https://fonts.googleapis.com`. **This must NOT survive into the production page.** Self-host Inter and JetBrains Mono WOFF2 in `packages/arcui/src/arcui/static/assets/fonts/`. Reference inline in the page:

```html
<style>
  @font-face { font-family: "Inter"; src: url("assets/fonts/inter.woff2") format("woff2"); }
  @font-face { font-family: "JetBrains Mono"; src: url("assets/fonts/jetbrains-mono.woff2") format("woff2"); }
</style>
```

Add this as a Build Order task: "self-host fonts before any page references them."

**Code-graph query correction:** the brainstorm specifies "2-second timeout" on the `code-review-graph` MCP call. The right tool is `list_graph_stats_tool()` (no arguments, ~100ms response). The brainstorm's wording suggests `get_architecture_overview_tool()` (slow, builds community structure). **Use `list_graph_stats_tool()` with a 500ms timeout, not 2s.** The fast call is sufficient for the contract:

```json
{
  "graph": {
    "available": true,
    "node_count": 142,
    "edge_count": 387,
    "languages": ["python", "markdown"],
    "last_indexed": "2026-05-03T08:00:00Z"
  }
}
```

**Contract additions (5 fields, prioritized):**

1. **`context.memory_used_tokens` + `context.memory_percent_of_window`** — operators need to see when an agent's memory is silently truncated. Compute from `arcagent.core.config.LLMConfig.max_tokens` and the loaded memory size at render time.

```json
{
  "context": {
    "model": "claude-3-5-sonnet-20241022",
    "input_tokens": 200000,
    "memory_used_tokens": 45230,
    "memory_percent_of_window": 22.6,
    "truncation_policy": "recency",
    "last_truncation_at": null
  }
}
```

2. **`workspace.tree[].type` + `children` (lazy-load)** — flat list of 50k files is useless. Directories are returned with `type: "dir"`, `child_count`, and `children: null`. Browser opens a directory → WebSocket message `{"action":"expand","path":"..."}` → server returns `{"path":"...","children":[...]}` appended to tree. Hard cap of 10k loaded entries per page render.

3. **`memory.entries[].classification`** — UNCLASS / CUI / SECRET — surfaced from frontmatter or filename convention. Federal demos require this.

4. **`memory.entries[].created_by`** — agent name that wrote the entry (for multi-agent teams). Surfaced from audit events; falls back to file mtime + os.stat owner if unavailable.

5. **`memory.recent_events`** — bounded ring (10 most recent file changes in `memory/`) for "what just happened" timeline. Streamed via existing `FileChangeBridge` (no new infrastructure).

**Real-time updates — yes, via existing infrastructure.** `arcgateway.file_events.default_bus` + `arcgateway.fs_watcher.WatcherManager` + `arcui/file_change_bridge.py` already exist (referenced in `arcui/server.py` imports). The Knowledge page subscribes to `agent_id:memory` events. Cost: zero new infrastructure. Effect: edits to `team/<agent_id>_agent/memory/` light up the page in real time.

**Module-boundary verdict:** `code-review-graph` MCP call lives in `arcui/routes/knowledge.py`, **not arcgateway**. Rationale: arcgateway is a message/session router; it does not own UI-specific data assembly. The Knowledge page is a UI concern; the contract is a UI concern; the MCP call is a UI-data convenience. arcgateway stays minimal.

**Audit/activity placement:** belongs on its own page (`/audit`), not on Knowledge. Knowledge is *state* (what the agent knows); audit is *action* (who did what). Different retention models, different search patterns, different consumers. A compact "recent turns" footer on the Knowledge page linking to `/traces?agent={agent_id}` is acceptable. Out of scope for v1 of the Knowledge page — the audit/traces page already exists separately.

**Patterns to steal from reference systems:**
- **Letta**: context budget meter (memory_used / context_window as a progress bar). Adopted in contract addition #1.
- **LangSmith**: timeline swim-lanes (tool → LLM → tool vertical Gantt). Defer to v2 — out of scope, but worth recording.
- **AutoGen Studio**: live log tail with WS push, search, filter. Foundation already exists via `FileChangeBridge`; defer surface UI to v2.
- **Smol Developer / Aider**: file diff summary (`+5 -3 lines per file`). Out of scope unless agents become git-aware.
- **Vault**: relative timestamps ("2m ago"), not absolute ISO. Browser-side formatting; trivial.

**Cross-agent knowledge diff:** explicitly out of scope for v1. Multi-agent comparison requires a team selector, conflict resolution rules, and entity dedup — three unresolved design problems. Single-agent Knowledge first.

**References:** `demo/knowledge.html:7-8` (font CDN — air-gap blocker), `packages/arcui/src/arcui/file_change_bridge.py` (real-time infrastructure already exists), `packages/arcgateway/src/arcgateway/fs_reader.py` (memory + workspace listing — already used by agent-detail).

---

### 11. Test plan

#### Unit tests — `tests/unit/test_web_adapter.py`

| Test case | What it verifies |
|---|---|
| `test_register_socket_returns_stable_chat_id` | Same `(viewer_token, agent_did)` → same `chat_id` on two calls |
| `test_register_socket_different_tokens_different_chat_ids` | Different tokens → different chat_ids (no session bleed) |
| `test_unregister_socket_removes_from_set` | After unregister, `_sockets[chat_id]` no longer contains the socket |
| `test_unregister_last_socket_removes_chat_id_key` | When the last socket for a chat_id unregisters, the key is removed |
| `test_ingest_builds_correct_inbound_event` | `ingest(chat_id, text)` calls `on_message` with correct `platform="web"`, `agent_did`, `user_did`, `session_key` |
| `test_ingest_empty_text_rejected` | Empty string is rejected before calling `on_message` |
| `test_ingest_oversized_text_rejected` | Text > 8192 chars is rejected |
| `test_send_fans_out_to_all_sockets` | Two sockets registered for the same chat_id both receive the JSON frame |
| `test_send_no_sockets_is_noop` | `send()` with no registered sockets returns without raising |
| `test_send_removes_dead_socket` | If one socket raises on `send_json`, it is removed; the other receives the message |
| `test_send_audit_hash_in_payload` | The outbound `"message"` frame contains a non-empty `audit_hash` |
| `test_connect_and_disconnect_are_noops` | `connect()` and `disconnect()` complete without error or side effects |
| `test_tool_call_delta_sends_tool_call_frame` | A `Delta(kind="tool_call", content="read_file path=x")` → `{"type": "tool_call", "tool": "read_file"}` frame sent to sockets |

#### Integration tests — `tests/integration/test_web_adapter_session.py`

| Test case | What it verifies |
|---|---|
| `test_browser_message_reaches_echo_executor` | WebSocket message → adapter ingest → SessionRouter.handle → AsyncioExecutor (echo stub) → StreamBridge → adapter.send → WS receive. End-to-end frame flow with a real (in-memory) SessionRouter. |
| `test_reconnect_preserves_session_key` | Disconnect + reconnect with the same token → same `chat_id` → SessionRouter recognizes as the same session (no new session spawned if prior session still active). |
| `test_concurrent_messages_queue_correctly` | Two messages sent before the first turn completes → second is queued by SessionRouter → delivered in order. Verifies the race-condition guard from `session.py` still holds with the web adapter. |
| `test_send_after_ws_disconnect_is_noop` | WebSocket disconnects during agent turn → `send()` returns without raising → audit event with `reason: no_socket` emitted. |
| `test_broadcast_to_two_sockets` | Two WebSockets with the same `chat_id` → both receive the response frame. |

#### E2E tests — `tests/e2e/test_web_chat_e2e.py`

| Test case | What it verifies |
|---|---|
| `test_full_chat_turn_with_real_agent` | Start arcui with `gateway_bootstrap` wired to a real agent from `team/`; open WS; send prompt; receive `status:thinking` then `message` frame with non-empty `text`. Requires `team/` directory with at least one agent configured. Marked `@pytest.mark.e2e` and skipped in CI unless `ARC_E2E=1`. |
| `test_auth_required_on_ws_upgrade` | WS upgrade without `Authorization` header → HTTP 401 before upgrade completes. |
| `test_invalid_token_rejected` | WS upgrade with invalid token → 401. |
| `test_knowledge_api_returns_correct_shape` | `GET /api/knowledge/{agent_id}` returns all required fields with correct types. |

### Research Insights — Additional Test Cases

The original test plan covers happy-path adapter behavior. Research surfaced gaps in **fan-out resilience, identity boundaries, and quota enforcement.** Add these test cases:

#### Unit (`test_web_adapter.py` additions)

| Test case | What it verifies |
|---|---|
| `test_send_under_backpressure_drops_oldest` | Queue full → next `send()` evicts oldest queued frame, enqueues new, emits audit with `reason: "backpressure"` |
| `test_drain_loop_handles_websocket_disconnect_cleanly` | Drain loop catching `WebSocketDisconnect` calls `unregister_socket()` and exits without raising |
| `test_oversized_inbound_frame_rejected` | Inbound frame > `max_frame_bytes` → adapter sends `error` frame with `code: "frame_too_large"` and does NOT call `on_message` |
| `test_inactivity_monitor_closes_idle_socket` | After `idle_timeout_seconds` of no inbound or outbound, socket closes with code 1000 reason "idle" |
| `test_max_connections_rejects_overflow` | Adapter at `max_connections=N` rejects the (N+1)th `register_socket` call |
| `test_client_seq_replay_rejected` | Inbound frame with `client_seq <=` highest-seen for that chat_id → rejected with `error` frame |
| `test_audit_emit_breakdown_under_partial_fanout` | One socket healthy, one queue full → single `gateway.message.delivered` event with `breakdown.dropped_backpressure: 1, breakdown.delivered: 1` |
| `test_register_socket_does_not_accept_viewer_token` | The adapter's `register_socket` signature takes `chat_id` (already-derived), not `viewer_token` (secret) |

#### Integration (`test_web_adapter_session.py` additions)

| Test case | What it verifies |
|---|---|
| `test_multi_adapter_routes_per_platform_user_did` | One agent has Slack adapter + Web adapter both registered. Slack user msg + Web user msg arrive concurrently → SessionRouter creates two separate sessions keyed by their respective `user_did`s, no cross-talk |
| `test_audit_chain_records_intended_delivery_not_succeeded_only` | When all sockets fail, the audit chain still has the `gateway.message.delivered` event with `outcome: "all_dropped"` (not omitted) |
| `test_hot_reload_drops_browser_ws_cleanly` | Lifespan exit triggers `_shutdown_adapters()` which sends close frames to all browser WS connections; no zombie sockets |

#### E2E (`test_web_chat_e2e.py` additions)

| Test case | What it verifies |
|---|---|
| `test_air_gap_no_external_calls_during_chat_turn` | With network isolated (mock DNS to fail) and a local Ollama LLM, full chat turn completes with no outbound HTTP call. Verify `httpx.AsyncClient.get` is never called against external hostnames during the turn. |
| `test_self_hosted_fonts_load_offline` | Static asset request for `assets/fonts/inter.woff2` returns 200 with correct content-type. No `fonts.googleapis.com` reference in any served HTML. |
| `test_knowledge_endpoint_graph_unavailable_returns_200` | When `code-review-graph` MCP server is down, `GET /api/knowledge/{agent_id}` returns 200 with `graph.available: false` (NOT 500) |

**Solutions archive cross-reference:** `2026-02-21-arcrun-phase4-hardening-review-learnings.md` highlights immutability + hash-chain patterns. The new test `test_audit_emit_breakdown_under_partial_fanout` is the direct application — one event per turn, deterministic content, hash-chain-friendly.

---

### 12. Channels (Slack-style multi-agent rooms)

**Decision:** Channels come **after** federal hardening, not before.

**Pillar trace:**
- Simplicity: DMs are already the simplest model that satisfies the demo. Channels require multi-recipient `DeliveryTarget` semantics, a channel-membership model, and UI affordances — none of which are needed for single-agent DMs.
- Modularity: channels require changes to `DeliveryTarget` (multi-chat_id fan-out), `SessionRouter` (shared session concept), and the web adapter. These are three separate modules. The change is well-defined but not small.
- Security: channels introduce cross-user visibility in a shared room. The federal hardening work (FIPS, signed allowlists, hard `max_turns`) must be complete before we can reason confidently about what a user in a shared channel can see. Adding multi-user rooms before the auth model is locked is a security debt that compounds.
- Scalability: channels at federal scale require NATS routing (one agent, many subscribers). That is blocked on `NATSExecutor` — same dependency as channels.

**Sequencing:** federal hardening → `NATSExecutor` → channels. Each stage is independently shippable.

---

### 13. ttyd subdomain auth

**Decision:** Interactive mode (full shell access), gated by the existing operator token passed as a query parameter to ttyd's `--credential` flag. Read-only is not worth implementing — a read-only terminal is less useful than the agent trace dashboard that already exists.

**Pillar trace:**
- Simplicity: ttyd has a built-in `--credential user:pass` flag. Pass the operator token as the password. One flag, no custom middleware.
- Modularity: the auth gate lives entirely in the `demo-tunnel.sh` script — no arcui code change.
- Security: the operator token already has the highest arcui privilege. Reusing it for ttyd means one secret to manage, not two. The cloudflared tunnel provides TLS termination; ttyd listens only on loopback (not exposed to the network directly). Anyone who can read the operator token from the URL bar already has operator arcui access — the risk surface is identical. The terminal subdomain (`term.blackarcsystems.com`) is not linked from any public page.
- Scalability: single-user demo scenario. One ttyd instance per machine.

**Implementation in `scripts/demo-tunnel.sh`:**

```bash
# Start ttyd with operator token as HTTP basic auth password.
# Only binds to 127.0.0.1 — never exposed directly.
ttyd \
  --port 7681 \
  --interface 127.0.0.1 \
  --credential "arc:${ARC_OPERATOR_TOKEN}" \
  --writable \
  bash &
TTYD_PID=$!
```

The cloudflared config routes `term.blackarcsystems.com → http://127.0.0.1:7681`. The browser receives an HTTP Basic Auth prompt before the terminal loads. The operator token is already in the user's clipboard from the arcui startup output.

**Risk acknowledgment:** anyone who obtains the operator token URL can execute arbitrary commands in the demo shell. This is acceptable for a live, attended demo where the presenter controls the screen. For any deployment beyond the demo, ttyd must be replaced with a proper terminal gateway with session recording and time-limited tokens.

---

## Build Order

> Updated post-deepening. Steps reflect the corrected module boundaries (no SHA-256 duplication, no gateway composition in arcui, no font CDN, no `asyncio.gather` fan-out).

0. **`arcgateway/identity.py`** — single source of truth for the viewer DID formula
   - `derive_viewer_did(viewer_token: str) -> str` — `"did:arc:viewer:" + sha256(token)[:16]`.
   - Used by `chat_ws.py` and any future arctrust integration.
   - One unit test: same input → same output; format matches `did:arc:viewer:[0-9a-f]{16}`.

1. **`arcgateway/adapters/web.py`** + unit tests (revised per Section 4 / 8 Research Insights)
   - Implements `BasePlatformAdapter`.
   - Public surface: `register_socket(ws, agent_did, user_did, chat_id) → None`, `unregister_socket(ws) → None`, `ingest(chat_id, text) → None`. **Adapter never sees `viewer_token`.**
   - Internal:
     - `_sockets: dict[chat_id, set[WebSocket]]`
     - `_socket_queues: dict[WebSocket, asyncio.Queue]` (maxsize=100, drop-oldest)
     - `_socket_tasks: dict[WebSocket, asyncio.Task]` (per-socket drain loops)
     - `_socket_meta: dict[WebSocket, (chat_id, agent_did, user_did)]`
     - `_last_activity: dict[WebSocket, float]` (monotonic time)
     - `_inbound_seq: dict[chat_id, int]` (last seen `client_seq` for replay rejection)
   - `connect()`/`disconnect()` no-ops at server level. `disconnect()` cancels all drain tasks and closes all sockets cleanly.
   - `send()` enqueues per-socket; drain loop awaits queue and handles disconnect.
   - `_socket_inactivity_monitor` task spawned per `register_socket` call.
   - Audit: ONE event per turn with `breakdown` field — never per-socket.
   - Frame-size cap (`max_frame_bytes`) enforced in `ingest()` and in the drain loop.
   - Unit tests per Section 11 Research Insights additions.

2. **`arcgateway/cli.py`** — wire `[platforms.web]` config block
   - Mirror the existing telegram/slack patterns at lines 124-194.
   - Default enabled at personal tier; explicit opt-in at federal.
   - `agent_did` per-platform (matches existing `effective_agent_did` plumbing).
   - Pass `max_frame_bytes`, `max_connections`, `idle_timeout_seconds` from config to adapter constructor.

3. **`arcgateway/bootstrap.py`** — `build_for_embedded()` (NEW LOCATION — was arcui)
   - `async def build_for_embedded(team_root: Path, gateway_config: GatewayConfig) -> tuple[AsyncioExecutor, SessionRouter, WebPlatformAdapter, StreamBridge]`.
   - Constructs all four components pre-wired.
   - Caller (arcui's lifespan) stores on `app.state`.
   - arcui's `server.py` lifespan grows ~10 LOC of glue. arcui owns no gateway composition.

4. **`arcui/routes/chat_ws.py`** — `/ws/chat/{agent_id}` (revised)
   - ~140 LOC.
   - Auth check via existing middleware.
   - Resolve `agent_did` from `agent_id` via the existing `roster_provider`.
   - **Compute `chat_id` and `user_did` in the route**, not in the adapter:
     ```python
     viewer_token = request.state.viewer_token
     user_did = arcgateway.identity.derive_viewer_did(viewer_token)
     chat_id = sha256(f"{viewer_token}:{agent_did}".encode()).hexdigest()[:16]
     web_adapter.register_socket(ws, agent_did, user_did, chat_id)
     ```
   - Loop: `async for raw in ws.iter_text(): await web_adapter.ingest(chat_id, json.loads(raw)['text'])`.
   - On disconnect: `web_adapter.unregister_socket(ws)`.

5. **Self-host fonts** — `arcui/static/assets/fonts/`
   - Add Inter (regular/medium/semibold/bold) and JetBrains Mono (regular/medium) WOFF2 files.
   - Update `index.html` and any new pages to use `@font-face` referencing local files.
   - **Air-gap blocker if skipped** — DOE labs cannot resolve `fonts.googleapis.com`.

6. **Messages page** — `index.html` block + `messages-page.js` + sidebar entry in `arc-shell.js`
   - DM list (left): GET `/api/agents`, render online/offline.
   - Chat pane (center): WS connection per active DM, append messages as they arrive.
   - Thread/refs panel (right): show session_key, recent tool_calls, audit hash.
   - Visual reference: `demo/messages.html` (trim the multi-agent scenario fluff).
   - Honor the four outbound frame types (`status`, `tool_call`, `message`, `error`) plus the new `status: "queued"` variant.
   - Local message store keyed by `turn_id` for reconnect history.

7. **Knowledge page** — `index.html` block + `knowledge-page.js` + sidebar entry
   - GET `/api/knowledge/{agent_id}` returns: memory dir listing, workspace tree (lazy-load directories), graph stats (`list_graph_stats_tool` with 500ms timeout).
   - Subscribes to existing `FileChangeBridge` for real-time `memory/` updates.
   - Surface context budget meter, classification badges, creator attribution per Section 10 Research Insights.
   - Visual reference: `demo/knowledge.html` (with fonts swapped to local).

8. **`scripts/demo-tunnel.sh`** — cloudflared + ttyd
   - `cloudflared tunnel create arc-demo` (one-time, requires login).
   - `~/.cloudflared/config.yml` routes:
     - `demo.blackarcsystems.com` → `http://localhost:8765` (arcui)
     - `term.blackarcsystems.com` → `http://localhost:7681` (ttyd)
   - Script starts arcui, ttyd, and cloudflared, prints the public URLs.

### Federal-Compliance Track (Parallel, Not Blocking the Demo)

These are **not** demo-day tasks — they are mapped here so the path to FedRAMP Low/Moderate is bounded.

- **F1.** `arcui/auth.py` — add `uid`/`username` to `SessionStartFields` (1h, FedRAMP Low gate).
- **F2.** `arcui/auth.py` — add `viewer_token_ttl_seconds` and `_revoked_tokens` set; `arc ui revoke` command (4h, FedRAMP Moderate).
- **F3.** `arcui/static/index.html:20` — `localStorage` → `sessionStorage` (2h, XSS hardening).
- **F4.** Replace URL-hash auth bootstrap with `POST /api/auth/bootstrap` (3h, eliminates WAF log capture of token).
- **F5.** Federal-tier MFA gate (TOTP/U2F) before WS upgrade (6h, FedRAMP High).

---

## Threat Surface (CLAUDE.md mapping)

| Threat | Mitigation in this design |
|---|---|
| LLM01 Prompt Injection | Browser input is treated as platform-adjacent content, never as system instruction. SessionRouter doesn't elevate it. |
| LLM06 Excessive Agency | The same per-agent allowlists / policy checks that gate Slack/Telegram requests gate browser requests — *because they go through the same SessionRouter*. |
| ASI03 Identity Abuse | `user_did` is deterministic from viewer token. Audit logs see one identity per browser session. |
| ASI07 Insecure Inter-Agent Comms | The adapter ↔ SessionRouter call is in-process; the browser ↔ adapter call is WSS (TLS via cloudflared). No plaintext leg. |
| LLM07 System Prompt Leakage | No prompt content traverses the adapter or the WS — both carry user text and agent reply only. |
| ASI10 Rogue Agent | Existing audit emission unchanged. Telemetry sees `platform="web"` events the same way it sees `platform="telegram"`. |

---

## Out of Scope (For Now)

- Token-level streaming (blocked on ArcAgent run() exposing an async iterator).
- File upload from browser into agent workspace.
- Multi-tenant viewer DIDs / arctrust integration.
- Federal-tier `WebPlatformAdapter` hardening (FIPS-validated TLS termination, signed manifest of the static bundle).
- Channels / group chat (Slack-style multi-agent rooms). Adapter only does DMs in v1; channels would require multi-recipient `DeliveryTarget` semantics.
- Knowledge graph interactive viz (defer to v2 of knowledge page).

---

## Success Criteria for the Demo

1. Open `https://demo.blackarcsystems.com`.
2. Click into Messages.
3. See list of served agents (from `team/`).
4. Click one → chat pane opens.
5. Type a prompt → spinner → response renders → workspace files appear in real time on the agent-detail page.
6. Audit dashboard shows the full event chain with `platform="web"`.
7. Switch to Knowledge → see that agent's memory and graph stats.
8. Throughout: terminal subdomain available as a fallback if the chat UI hits trouble live.

---

## Deepening — 2026-05-06 (OpenClaw learnings + post-demo reckoning)

**Trigger:** the 2026-05-05 SCAP demo failed. Operator quote: *"this UI seems way too complicated and constantly breaks. I just need something that works consistently."* OpenClaw's architecture (vanilla Lit + Vite, single-WebSocket transport, channels-as-product) was surfaced as a reliability comparison. Verdict below: the v1 architecture in this brainstorm is sound and **already shipped**, but adjacent surfaces (dashboard polling, missing service worker, no fallback chat surface) cause the failure modes the operator hit.

### Implementation reality check (audit verdict)

The brainstorm's core decisions **are already in production code**. The surprising discovery from the audit:

| Component | File | Status | Notes |
|---|---|---|---|
| `WebPlatformAdapter` | `arcgateway/adapters/web.py` (444 LOC) | ✅ Built | Per-socket bounded queues, drop-oldest, monotonic seq validation, audit hooks — all the consequential corrections from §4 are in. |
| `arcgateway.bootstrap.build_for_embedded()` | `arcgateway/bootstrap.py` (314 LOC) | ✅ Built | Returns `EmbeddedGateway` named tuple as designed. |
| `derive_viewer_did` | `arcgateway/identity.py` (38 LOC) | ✅ Built | Single source of truth for the SHA-256 derivation. |
| `chat_ws.py` thin proxy | `arcui/routes/chat_ws.py` (172 LOC) | ✅ Built | Adapter is secret-free; route owns `viewer_token` and computes `chat_id`/`user_did`. |
| `messages-page.js` | `arcui/static/assets/messages-page.js` (522 LOC) | ⚠️ Single-WS but no service worker, polls roster every 5s | Vanilla JS state-machine reconnect; no PWA shell. |
| Dashboard polling endpoints | `arcui/routes/stats.py`, `team_pages.py` | ⚠️ Legacy pattern | Caddy access log shows ~20 GETs/sec across `/api/stats`, `/api/queue`, `/api/team/roster`, `/api/circuit-breakers`, `/api/budget`, `/api/performance`, `/api/cost-efficiency`, `/api/schedule-history`, `/api/stats/timeseries`. None use the WS event channel. |

**Conclusion:** the brainstorm's architecture is not the problem. Two adjacent issues are:

1. **The chat path is correct, but the dashboard surrounding it is loud and stateless.** Polling churn doesn't break chat directly, but it inflates the failure surface (any of those endpoints 500ing affects the user's perception of "the UI").
2. **arcui is the only chat seat in production.** Slack and Telegram adapters exist but aren't wired in for the demo agents — so when the WebSocket got stale, the operator had nowhere to go.

### 2026-05-05 demo failure post-mortem (root causes)

| Symptom | Root cause | Severity |
|---|---|---|
| Browser chat "timed out" | Stale WebSocket left over from before the stack restart; browser was holding a defunct socket. No sequence-gap detection meant the client never auto-reconnected. | High — directly killed the demo |
| `arc-stack` systemd reported failure on every restart | `brad/brian/josh/mosa/my` agents got rsync'd from laptop without their identity keys; agent crash → script exit 1 → systemd treats as fatal. The 3 demo agents were healthy. | Medium — masked health of working agents |
| Operator had no fallback when UI froze | Demo agents only registered with `WebPlatformAdapter`; no Slack/Telegram channel was active. arcui *was* the only seat. | High — eliminated recovery path |
| Dashboard polling churn in access log (~1/sec across 9 endpoints) | Legacy `/api/*` per-feature endpoints; messages-page.js polls `/api/team/roster` every 5s; team-page polls timeseries. None coalesced through the WS. | Low — cosmetic, but increases the failure surface |
| `/api/stats` returns whether agents are connected — independent of whether they actually crashed | The "online" overlay is process-presence, not turn-readiness. A crashed agent can still appear "online" in the roster for a brief window. | Medium — UI shows "available" when it isn't |

**The architectural fix from the brainstorm doesn't address the chat-stale-WS or the no-fallback-channel issues directly.** Both need a delta.

### OpenClaw patterns worth borrowing (priority order)

Filtered through the four pillars; only patterns that solve the failure modes above or that simplify a known surface are listed. Each comes with a concrete file pointer in the OpenClaw repo so the implementation reference is verifiable.

#### 1. Sequence-gap detection in the WS envelope ⭐ highest leverage

**Pillar:** Simplicity (one integer per frame), Scalability (eliminates a class of stuck-client bugs).

OpenClaw's `event` frames carry an integer `seq` (`ui/src/ui/gateway.ts`). The browser tracks `lastSeq`; if `received > lastSeq + 1`, it triggers `onGap` → reconnect with reason `"seq-gap"`. This is the mechanism that catches the exact failure the operator hit on 2026-05-05 — a stale socket that's missed events but hasn't been told it's dead.

**Concrete delta to the brainstorm's envelope spec (§6):** add `seq` (monotonic per-`chat_id` integer) to the outbound `tool_call`, `message`, `error`, and `status` frames. `WebPlatformAdapter` increments per-`chat_id` on every send; if a stale socket joins and emits a stale `seq` ack, the adapter reconnect loop kicks in.

**Effort:** ~1 day. ~30 LOC in `web.py`, ~40 LOC in `messages-page.js`. **Blocks production reliability.**

#### 2. Make at least one non-arcui channel a tested demo seat

**Pillar:** Modularity (already-decided peer relationship), Security (audit goes through SessionRouter regardless of channel), and the new principle below.

The brainstorm proves arcui, Slack, and Telegram are interchangeable peers. The 2026-05-05 demo had only arcui wired — there was no alternative route to the agent when the WS stalled. **The operator should always have at least two chat surfaces to the same agent, one of which is not arcui.**

For commercial demos: Slack (existing adapter, needs only a workspace + token).
For federal demos: Mattermost (new adapter, FedRAMP High via FedHIVE, IL5, JWICS-deployed — see §"Federal demo posture" below).
For air-gap testing: CLI fallback (`arc agent run` already works).

**Effort:** Slack wire-up = 1 day (config-only since adapter exists). Mattermost adapter = ~2 weeks new code (different from §6).

#### 3. Retire dashboard polling — single transport, push-based events

**Pillar:** Simplicity (one transport per page), Scalability (server-pushed, no thundering-herd polling).

OpenClaw's `event` channel carries chat, presence, sessions, agents, tools, AND control-plane updates over **one** WebSocket (`handleGatewayEventUnsafe()` in `ui/src/ui/app-gateway.ts` dispatches on 12+ event names). Zero `/api/*` REST polling for live data.

For arc, this means the dashboard widgets — stats, queue, roster, circuit-breakers, budget, performance, cost-efficiency, schedule-history, timeseries — should subscribe to a `/ws/dashboard` channel that pushes updates when state changes. The browser stops hammering the server with GETs every second; one WebSocket carries every dashboard payload as it changes.

**Effort:** non-trivial. Each existing endpoint has a server-side aggregation that has to become an event source. Estimated 1–2 weeks across the 9 endpoints. **Lower priority than #1 and #2** — it's quiet improvement, not a fix to a demo-killer.

#### 4. Service worker for static shell

**Pillar:** Simplicity (browser doesn't need to re-download bundles when the WS reconnects).

OpenClaw's `ui/public/sw.js` does cache-first for hashed `/assets/*`, network-first for HTML, and **explicitly excludes `/api/*`, `/rpc*`, `/plugins/*`** from caching. The result: a network blip never leaves the user with a blank screen — assets are cached, only the live data path needs to recover.

**Effort:** ~1 day. The cache list, exclusion rules, and registration are straightforward; arcui's static bundle is already hash-named via the build.

#### 5. Lit web components for the messages page

**Pillar:** Modularity (smaller debug surface).

`messages-page.js` is 522 LOC of imperative DOM updates. The Lit equivalent (`@state()` decorated properties on a single `LitElement` subclass, render delegated to a pure render function) would compress to ~250 LOC with no manual DOM manipulation. **But this is a refactor for clarity, not a fix to any production failure.** Defer until the higher-leverage items (#1–#4) are done.

**Effort:** ~3 days. **Lowest priority.**

#### 6. JSON5 config (or just stop adding config formats)

**Pillar:** Simplicity, but only if we don't already have TOML.

OpenClaw uses JSON5 for relaxed JSON with comments and trailing commas. Arc already uses TOML for `arcagent.toml`, which has the same affordances. **Skip this pattern** — it would introduce a third config format. Note logged for completeness; not adopted.

### Updated principles (delta to the brainstorm)

The original brainstorm's principles are sound. Two are added; one is sharpened.

**NEW principle — "The agent must remain reachable when the UI is broken."**
This is the channels-as-product principle. arcui is one peer of N, not THE seat. If arcui hangs, the operator can still talk to the agent via Slack, Mattermost, Teams, Telegram, or the CLI. **At least one non-arcui channel must be wired and tested for every production agent.** The 2026-05-05 demo failure mode (operator stuck because arcui was unresponsive) cannot recur if this principle holds.

**NEW principle — "One transport per page, no per-feature polling."**
A page has a single connection that carries every dynamic update. Initial state is served by an initial REST call OR the WS replays state on connect. After that, every change is a server-pushed event over the same transport. **No setInterval polling against `/api/*` for live data.** This collapses N failure modes into 1, matches OpenClaw's contract, and dramatically reduces the access-log noise that masks real problems.

**SHARPENED principle — "Audit trail must be truthful by construction" (existing — extended).**
Original applied to the in-process audit chain. Extended: now also applies to client-server frame ordering. Every outbound frame carries a `seq` tied to the audit event's hash chain; a client that detects a `seq` gap is guaranteed to have missed at least one audit-emitted state change and must reconnect rather than silently fall behind. Sequence-gap detection (#1 above) is how this becomes operational.

### Build-order delta (post-deepening)

**Original brainstorm's order:** WebPlatformAdapter → chat_ws → bootstrap → identity → envelope. **All ✅ done.**

**New priorities given the post-demo reality:**

| # | Item | Effort | Blocks |
|---|---|---|---|
| 1 | Add `seq` to outbound web envelope; gap-detection + auto-reconnect in messages-page.js | 1 day | Production reliability for any operator-facing demo |
| 2 | Wire Slack adapter for at least one demo agent (`scap_isso_agent` is the obvious candidate) — operator gets a non-arcui chat surface | 1 day (config-only; adapter exists) | The "no fallback" failure mode |
| 3 | Service worker for arcui static shell | 1 day | Network-blip recovery |
| 4 | Stop rsync'ing broken-on-this-VM agents to AWS — fix the deploy script to deploy a manifest of "agents-this-host-supports", not the entire `team/` dir | 0.5 day | systemd-fatal cascades |
| 5 | OS-user binding in `SessionStartFields` (already mapped in the original §2) | 1 hour | FedRAMP Low |
| 6 | Migrate `/api/stats`, `/api/queue`, `/api/team/roster`, etc. to a `/ws/dashboard` event subscription | 1–2 weeks | Polling churn / cosmetic but improves stability under load |
| 7 | Mattermost adapter (new) — federal/air-gap demo path | ~2 weeks | DOE lab demos |
| 8 | Lit refactor of `messages-page.js` | ~3 days | Code clarity, lower failure surface |

**Items 1–4 are demo-blockers. Items 5–8 are production-grade follow-ups.** Item 1 alone would have prevented the 2026-05-05 demo failure. Item 2 would have given the operator a recovery path while item 1 was being fixed.

### Federal demo posture (refined matrix)

The original §"Federal Compliance Verdict" addressed the arcui adapter only. The deepening adds the multi-channel matrix:

| Audience | Recommended primary surface | Why | Effort to enable |
|---|---|---|---|
| DOE national labs (air-gapped) | **Mattermost (on-prem)** | FedRAMP High via FedHIVE, IL4/IL5/IL6, JWICS-proven, Platform One CATO | New adapter (~2w) |
| DoD / civilian agency on M365 | **Microsoft Teams (GCC High bot)** | M365 GCC High = FedRAMP High + IL5; agency already runs the productivity suite, single boundary | New adapter (~2–3w via Bot Framework) |
| Federal but not air-gapped, not on M365 | **GovSlack** | FedRAMP High (FR2230252267, Jan 2024); but bot needs separate GovSlack Marketplace approval | Adapter exists; marketplace approval is the blocker |
| Commercial enterprises | **Slack Enterprise Grid** (existing adapter) | FedRAMP Moderate covers state/local & lower-tier; broad enterprise installed base | Already built |
| Lab evaluation / personal | **arcui** | Custom UI, simplest local boundary, runs everywhere | Already built (this brainstorm) |
| Always-available fallback | **CLI (`arc agent run`)** | Zero web surface; works under any failure of the others | Already built |

**Operator implication:** every production deployment should have **arcui + at least one chat channel + CLI** wired for the same agent. The chat channel is the operator's recovery path when arcui has problems; arcui is the spectator's recovery path when the chat client has problems; CLI is the engineer's recovery path when both are down.

### Solutions archive matches

Two prior solutions reinforce decisions in this deepening:

- **`2026-04-18-tier-must-flow-through-construction.md`** — *"audit trail must be truthful by construction."* The sharpened principle above (seq tied to audit chain) is the same lesson: derive the audit-relevant value once, capture it in closure, and have every emission carry it consistently. The operator's lost session on 2026-05-05 was a case where the browser believed it had a continuing session that the server had already terminated — an audit-truth divergence that #1 above eliminates.
- **`2026-02-21-arcrun-phase4-hardening-review-learnings.md`** — *"individual try/except per cleanup step,"* *"observer callback outside lock."* The per-socket bounded queue + per-socket drain task pattern in `web.py` already follows this. The deepening doesn't add new violations.

### Predecessor-brainstorm consistency

- **`2026-04-27-nlit-demo-local-build.md`** — local Arc agent + Obsidian vault. Resolved by this brainstorm: web access is via WebPlatformAdapter, not by exposing Obsidian.
- **`2026-02-17-arcteam-messaging.md`** — JSONL streams, pull model, 15-field envelope. The web envelope here is a superset of that envelope's chat-specific fields. **No conflict;** `seq` (added in this deepening) is a new field, not a replacement.
- **`2026-02-16-agent-messaging.md`** — Telegram as the phone-accessible interface. The deepening's principle "agent reachable when UI is broken" makes the Telegram adapter operationally relevant again, not just a parallel surface.

### Closing posture

The architecture is right. The implementation is largely shipped. The 2026-05-05 demo failed because the chat path didn't auto-recover from a stale socket and because the operator had no second seat. **Both are 1-day fixes** (items 1 and 2 above). The remaining items are quality-of-life — service worker, polling retirement, Lit refactor — and are valuable but not blocking.

The OpenClaw comparison validates the brainstorm's architecture rather than contradicting it: their channels-as-product model is exactly what was already decided here. The borrow-list (sequence-gap, service worker, push-based dashboard, Lit) is small and targeted — not a rewrite, a hardening.

**Ready for `/specify`.** The PRD should target items #1–#4 from the build-order delta as the v1.1 release, with #5–#8 as v1.2.

