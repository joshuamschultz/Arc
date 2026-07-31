# SPEC-025 — Arc Channels Resilience: Solution Design

## Module Boundaries (filtered through pillar 2: Modularity)

This spec touches **four modules** with explicit, narrow contracts. No cross-module logic bleed.

| Module | Owns | Does NOT own |
|---|---|---|
| `arcgateway` | `BasePlatformAdapter` contract, `WebPlatformAdapter`, `SlackAdapter`, `MattermostAdapter` (new), per-`chat_id` ring buffer, audit emission, `seq` counter | UI rendering, polling vs. push semantics, Caddy config, deploy scripts |
| `arcui` | `chat_ws.py` route (already thin proxy), new `dashboard_ws.py` route, service worker registration, browser-side seq tracking, browser-side reconnect logic | Adapter logic, agent execution, audit chain |
| `arcrun` (loop) | Unchanged | Anything in this spec |
| `deploy/aws` | `setup-vm.sh` per-host manifest filtering, `arc-stack.sh` per-agent health independence | Adapter config (lives in `team/<agent>/arcagent.toml`), gateway runtime, anything beyond shell + systemd |

Per the project CLAUDE.md: *"all llm calls are arcllm; loop execution is arcrun; agent with tools, skills, extensions, memory, etc is arcagent."* This spec touches **none** of arcllm or arcrun — those concerns are unaffected.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│ Browser (arcui static + service worker shell)                       │
│                                                                     │
│  ┌──────────────────────┐    ┌──────────────────────┐               │
│  │ messages-page.js     │    │ dashboard-page.js    │               │
│  │  • seq tracking      │    │  • subscribe topics  │               │
│  │  • exp-backoff retry │    │  • single WS         │               │
│  │  • recovery banner   │    │  • no setInterval    │               │
│  └─────────┬────────────┘    └──────────┬───────────┘               │
└────────────┼──────────────────────────────┼─────────────────────────┘
             │ WSS /ws/chat/{agent_id}      │ WSS /ws/dashboard
             │ envelope w/ seq              │ {topic, payload}
┌────────────▼──────────────────────────────▼─────────────────────────┐
│ arcui (Starlette, in-process gateway runtime)                       │
│                                                                     │
│  ┌──────────────────────┐    ┌──────────────────────┐               │
│  │ routes/chat_ws.py    │    │ routes/dashboard_ws  │ NEW           │
│  │ (existing, thin)     │    │ • subscribe/publish  │               │
│  └─────────┬────────────┘    │ • topic registry     │               │
│            │                 │ • per-socket queue   │               │
│            │                 └──────────┬───────────┘               │
└────────────┼──────────────────────────────┼─────────────────────────┘
             │ adapter.ingest()             │ topic.subscribe()
┌────────────▼──────────────────────────────▼─────────────────────────┐
│ arcgateway                                                          │
│                                                                     │
│  ┌─────────────────────┐  ┌──────────────────┐  ┌────────────────┐  │
│  │ adapters/web.py     │  │ adapters/slack.py│  │ adapters/      │  │
│  │ + seq counter       │  │ (existing,       │  │ mattermost.py  │  │
│  │ + ring buffer       │  │  unchanged)      │  │ NEW            │  │
│  └─────────┬───────────┘  └────────┬─────────┘  └────────┬───────┘  │
│            │ on_message            │                     │           │
│            └──────────────┬────────┴─────────────────────┘           │
│                           │                                          │
│                  ┌────────▼──────────────────┐                       │
│                  │ SessionRouter (unchanged) │                       │
│                  └────────┬──────────────────┘                       │
│                           │                                          │
│                  ┌────────▼──────────────────┐                       │
│                  │ Executor → ArcAgent.run() │                       │
│                  └───────────────────────────┘                       │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ telemetry/dashboard_events.py NEW                           │    │
│  │  • Subscribers per topic                                    │    │
│  │  • State diff → publish                                     │    │
│  │  • Throttle policy per topic (e.g. 5s for timeseries)       │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

The brackets and arrows below `WebPlatformAdapter`/`SlackAdapter`/`MattermostAdapter` are byte-identical to what runs today (per SPEC-023 and the audit). New code is **only** the items marked `NEW`.

## Component Designs

### C1 — Web envelope `seq` field + ring buffer (FR-1)

**Server side: `packages/arcgateway/src/arcgateway/adapters/web.py`**

Add to the adapter class:

```python
class WebPlatformAdapter(BasePlatformAdapter):
    # Existing:
    _sockets: dict[str, set[WebSocket]]
    _socket_queues: dict[WebSocket, asyncio.Queue]
    _socket_tasks: dict[WebSocket, asyncio.Task]
    # NEW:
    _seq_counters: dict[str, int]              # chat_id → next seq
    _replay_buffers: dict[str, deque]          # chat_id → ring of last 50 frames

    async def _emit(self, chat_id: str, frame: dict) -> None:
        seq = self._seq_counters.setdefault(chat_id, 0)
        frame["seq"] = seq
        self._seq_counters[chat_id] = seq + 1
        buf = self._replay_buffers.setdefault(chat_id, deque(maxlen=50))
        buf.append(frame)
        await self._fan_out(chat_id, frame)
```

Replay on registration:

```python
async def register_socket(self, ws: WebSocket, agent_did: str, user_did: str, chat_id: str,
                          since_seq: int | None = None) -> None:
    self._sockets.setdefault(chat_id, set()).add(ws)
    # ... existing per-socket queue + drain task ...
    if since_seq is not None:
        buf = self._replay_buffers.get(chat_id, deque())
        missed = [f for f in buf if f.get("seq", -1) > since_seq]
        if missed:
            for frame in missed:
                self._enqueue(ws, frame)
            if missed[0]["seq"] > since_seq + 1:
                # Ring overrun — older frames lost
                self._enqueue(ws, {"type": "recovery_banner", "lost_below_seq": missed[0]["seq"]})
```

**Client side: `packages/arcui/src/arcui/static/assets/messages-page.js`**

```javascript
class ChatConnection {
  #lastSeq = -1;
  #backoffMs = 800;

  async connect(chatId) {
    const url = this.#buildUrl(chatId, this.#lastSeq);  // includes ?since_seq
    this.ws = new WebSocket(url);
    this.ws.onmessage = (ev) => this.#onMessage(ev);
    this.ws.onclose = () => this.#scheduleReconnect(chatId);
  }

  #onMessage(ev) {
    const frame = JSON.parse(ev.data);
    if (frame.type === "recovery_banner") {
      this.#showRecoveryBanner();
      this.#lastSeq = frame.lost_below_seq - 1;
      return;
    }
    if (frame.seq !== undefined) {
      if (this.#lastSeq >= 0 && frame.seq !== this.#lastSeq + 1) {
        // Gap detected — close + reconnect
        this.ws.close(4000, "seq-gap");
        return;
      }
      this.#lastSeq = frame.seq;
    }
    this.#dispatch(frame);
  }

  #scheduleReconnect(chatId) {
    setTimeout(() => this.connect(chatId), this.#backoffMs);
    this.#backoffMs = Math.min(this.#backoffMs * 1.7, 15000);
  }
}
```

`chat_ws.py` accepts an optional `?since_seq=N` query param and forwards it to `register_socket`.

**Module-boundary contract:** the adapter owns `seq` issuance and the ring buffer; the client owns gap detection + reconnect. Server never trusts client `seq` claims for anything but replay-window selection.

### C2 — Slack adapter wire-up for demo agents (FR-2, P0)

**No new code in arcgateway** — the adapter exists. Configuration only:

`team/scap_isso_agent/arcagent.toml` (and similarly for nlit_cora, nlit_soc):

```toml
[platforms.slack]
enabled = true
bot_token_env = "SLACK_BOT_TOKEN"   # references .env
app_token_env = "SLACK_APP_TOKEN"
default_channel = "@scap_isso"
```

`bootstrap.build_for_embedded()` already wires every enabled platform per its config. No code changes there.

**Acceptance test fixture:** `tests/integration/test_dual_adapter_chat.py` — start arcui with Slack mocked, send a Slack inbound, assert the same `SessionRouter` session handles it; assert audit chain shows `platform="slack"` interleaved with `platform="web"` for the same `(user_did, agent_did)`.

### C3 — Service worker (FR-3)

**New file:** `packages/arcui/src/arcui/static/sw.js`

```javascript
const CACHE_VERSION = 'arcui-shell-v1';
const SHELL_PATHS = ['/', '/index.html'];
const ASSETS_PREFIX = '/assets/';
const ALWAYS_LIVE = [/^\/api\//, /^\/ws\//, /^\/artifacts\//];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_VERSION).then((c) => c.addAll(SHELL_PATHS)));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
    )
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (ALWAYS_LIVE.some((re) => re.test(url.pathname))) return;  // network-only
  if (url.pathname.startsWith(ASSETS_PREFIX)) {
    // Cache-first for hashed assets
    event.respondWith(
      caches.match(event.request).then((c) => c || fetch(event.request).then((r) => {
        const clone = r.clone();
        caches.open(CACHE_VERSION).then((cache) => cache.put(event.request, clone));
        return r;
      }))
    );
    return;
  }
  // Network-first for everything else (HTML)
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
```

**Registration:** `packages/arcui/src/arcui/static/main.js` (existing entry):

```javascript
if (import.meta.env?.PROD && 'serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js');
} else if ('serviceWorker' in navigator) {
  // Dev: unregister stale SWs
  navigator.serviceWorker.getRegistrations().then((regs) => regs.forEach((r) => r.unregister()));
}
```

**`SW_DISABLE` env override:** if `window.__SW_DISABLE__` is true (set by the bootstrap script before SW registration), skip registration. Provides an escape hatch if a buggy SW deploy locks operators out.

### C4 — Per-host deploy manifest (FR-4)

**New file:** `deploy/aws/agents.enabled` (one agent name per line, comments allowed):

```
# AWS Lightsail demo VM — agents this host has keys/config for
nlit_cora_agent
nlit_soc_agent
scap_isso_agent
```

**Modified `deploy/aws/setup-vm.sh`** — sketch of new behavior:

```bash
ENABLED_FILE="${REPO_ROOT}/deploy/aws/agents.enabled"
if [ -f "${ENABLED_FILE}" ]; then
  ENABLED_AGENTS=$(grep -v '^#' "${ENABLED_FILE}" | grep -v '^$')
else
  ENABLED_AGENTS="nlit_cora_agent nlit_soc_agent scap_isso_agent"  # default
fi

# Remove any agent dir not in the manifest
for d in "${REPO_ROOT}/team"/*_agent; do
  name=$(basename "$d")
  if ! echo "${ENABLED_AGENTS}" | grep -qx "${name}"; then
    rm -rf "$d"
  fi
done
```

**Modified `deploy/aws/arc-stack.sh`** — per-agent health detection:

```bash
# Today: exit 1 if any agent failed to connect.
# New: exit 0 if at least one agent connected; per-agent state recorded for the roster endpoint.
if [ "${CONNECTED_AGENT_COUNT}" -ge 1 ]; then
  echo "✓ ${CONNECTED_AGENT_COUNT}/${TOTAL_AGENT_COUNT} agents connected"
  exit 0
fi
echo "✗ no agents connected — refusing to start"
exit 1
```

**Roster endpoint already shows per-agent `online`** (per the audit). Add a `degraded` state for agents that are in the manifest but failed to connect — UI distinguishes from "not deployed".

### C5 — Single-transport dashboard (FR-5)

**New file:** `packages/arcgateway/src/arcgateway/telemetry/dashboard_events.py`

```python
class DashboardEventBus:
    """Topic-keyed pub-sub for dashboard widgets.

    Subscribers are sockets; publishers are state aggregators (queue depth,
    circuit-breaker state, etc.) that already exist on the server.
    """
    _subscribers: dict[str, set[asyncio.Queue]]   # topic → queue per socket
    _last_value: dict[str, Any]                   # topic → last published payload

    async def subscribe(self, socket_queue: asyncio.Queue, topics: list[str]) -> None:
        for t in topics:
            self._subscribers.setdefault(t, set()).add(socket_queue)
            # Replay last-known value so the UI doesn't blink empty
            if t in self._last_value:
                socket_queue.put_nowait({"topic": t, "payload": self._last_value[t]})

    async def publish(self, topic: str, payload: Any) -> None:
        self._last_value[topic] = payload
        for q in list(self._subscribers.get(topic, set())):
            try:
                q.put_nowait({"topic": topic, "payload": payload})
            except asyncio.QueueFull:
                _ = q.get_nowait()  # drop-oldest, same pattern as web.py
                q.put_nowait({"topic": topic, "payload": payload})
```

**New file:** `packages/arcui/src/arcui/routes/dashboard_ws.py` — thin proxy:

```python
@router.websocket("/ws/dashboard")
async def dashboard_ws(ws: WebSocket):
    await ws.accept()
    queue = asyncio.Queue(maxsize=100)
    bus = ws.app.state.dashboard_bus
    msg = await ws.receive_json()
    if msg.get("type") != "subscribe":
        await ws.close(code=1003, reason="first frame must be subscribe")
        return
    await bus.subscribe(queue, msg["topics"])
    drain = asyncio.create_task(_drain(ws, queue))
    try:
        async for _ in ws.iter_text():
            pass  # client sends nothing else in v1
    finally:
        drain.cancel()
        # bus.unsubscribe(queue) — left as cleanup
```

**Aggregator wiring** — each existing aggregator (stats, queue, roster, …) gets a single line added to publish on every state change:

```python
# In whatever computes the queue depth today:
await ctx.dashboard_bus.publish("queue", {"depth": new_depth, "by_agent": ...})
```

**Polling endpoints stay for one release behind a feature flag** (`ARCUI_LEGACY_POLLING=true`) so MCP servers / CLI consumers can migrate. After one release, they're deleted.

### C6 — Mattermost adapter (FR-6, v1.2)

**New file:** `packages/arcgateway/src/arcgateway/adapters/mattermost.py`

```python
class MattermostAdapter(BasePlatformAdapter):
    """Mattermost server WebSocket adapter.

    Uses MM's WebSocket API (https://developers.mattermost.com/integrate/reference/websocket/).
    Same per-chat backpressure pattern as web.py. Audit emission on every send.
    """

    def __init__(self, server_url: str, bot_token: str, *, allowed_channels: list[str] | None = None):
        # bot_token is loaded by GatewayConfig from the agent's .env
        # allowed_channels is policy: empty list = DM-only
        ...

    async def connect(self) -> None:
        # Open MM WS, subscribe to events
        ...

    async def ingest_event(self, mm_event: dict) -> None:
        # Map MM "posted" event → InboundEvent → on_message
        ...

    async def send(self, target: DeliveryTarget, message: str) -> None:
        # Post to MM channel API; audit emit; same backpressure model
        ...
```

**Federal posture:** MM tokens are loaded via `arcgateway.vault` (same path as Slack); for `tier=federal`, the adapter validates the MM server URL is on the agency's intranet and refuses outbound DNS to public hosts.

**Test fixture:** `tests/integration/test_mattermost_adapter.py` against `docker-compose` Mattermost (Mattermost ships an OSS docker image suitable for CI).

### C7 — OS-user binding in audit (FR-7)

**Modified file:** `packages/arcui/src/arcui/auth.py`

```python
import os
import pwd
from dataclasses import dataclass

@dataclass(frozen=True)
class SessionStartFields:
    viewer_did: str
    chat_id: str
    agent_did: str
    # NEW:
    uid: int
    username: str

    @classmethod
    def from_token(cls, token: str, agent_did: str, chat_id: str, viewer_did: str) -> "SessionStartFields":
        try:
            pw = pwd.getpwuid(os.getuid())
            uid, username = pw.pw_uid, pw.pw_name
        except KeyError:
            uid, username = -1, "<unknown>"
        return cls(viewer_did=viewer_did, chat_id=chat_id, agent_did=agent_did,
                   uid=uid, username=username)
```

`gateway.session.start` audit event includes both fields. **Caveat:** on Windows there's no `pwd` module; SessionStartFields handles `ImportError` and degrades to `<unknown>` (Windows is not a federal target tier).

## Security Posture (filtered through pillar 3)

| Threat (OWASP LLM/ASI) | This spec's mitigation |
|---|---|
| LLM02 Sensitive Disclosure | Slack/Mattermost adapters route through SessionRouter — same PII/CUI guards as web. No bypass. |
| LLM06 Excessive Agency | Multi-channel input doesn't expand the agent's tool surface — every channel hits the same allowlist. |
| LLM07 System Prompt Leakage | New adapters never see prompts; envelope carries user text + agent reply only (same as SPEC-023). |
| ASI03 Identity Abuse | OS-user binding (FR-7) makes audit attributable to a real human at FedRAMP Low and above. |
| ASI07 Insecure Inter-Agent Comms | Service worker excludes `/api/*` and `/ws/*` from caching — auth-sensitive paths never go to disk. |
| ASI08 Cascading Failures | `seq` gap → reconnect (not silent skew); per-agent health independent of unit health (one bad agent ≠ unit down). |

## Federal Tier Compliance (extends SPEC-023's matrix)

| Tier | After v1.1 (this spec, demo-blockers) | After v1.2 (full spec) |
|---|---|---|
| FedRAMP Low | ⚠️ Partial — needs FR-7 OS user binding | ✅ — FR-7 lands in v1.2 |
| FedRAMP Moderate | ❌ Same as SPEC-023 | ⚠️ Partial — needs token TTL (out of scope) |
| FedRAMP High | ❌ Same as SPEC-023 | ❌ Needs MFA (out of scope) |
| CMMC L1 | ✅ | ✅ |
| Air-gapped DOE | ✅ + Mattermost surface | ✅ |

This spec moves the needle from "personal/enterprise tiers ship as-is" to "FedRAMP Low gates closed for v1.2 + Mattermost air-gap surface added."

## Performance Budget (filtered through pillar 4)

| Component | Budget | Why |
|---|---|---|
| `seq` lookup + emit | < 1ms p99 | In-memory dict; no IO |
| Ring buffer memory | 50 frames × N chat_ids × ~2KB/frame ≈ 100KB/chat | Bounded by `_chat_id` count, which is bounded by adapter session count |
| Service worker bundle | < 5KB minified | Sw.js is straightforward; weight comes from Workbox-style libs we don't include |
| `/ws/dashboard` push latency | < 200ms p95 from publish to socket send | Same per-socket queue pattern as `web.py`; no new bottleneck |
| Mattermost adapter startup | < 1s connect to MM WS | Standard MM client behavior; not a hot path |
| Per-VM agent disk after manifest filter | == sum(enabled agents only), not entire `team/` | Setup-vm explicitly removes |

## Testing Strategy

| Layer | What's tested | Tool |
|---|---|---|
| Unit | `seq` increment, ring overrun, gap-detection logic | pytest |
| Unit | Service worker cache rules (path-matching) | vitest (jsdom) |
| Integration | Dual-adapter chat: web + slack to same agent, audit chain consistent | pytest with mock Slack |
| Integration | `/ws/dashboard` subscribe/publish/replay | pytest with `httpx_ws` |
| Integration | Mattermost adapter against `docker-compose` MM | pytest |
| E2E | Browser auto-reconnects from forced socket close (Playwright) | playwright |
| E2E | Service worker offline shell (Playwright `--offline`) | playwright |
| Deploy fixture | `setup-vm.sh` on a clean VM with `agents.enabled` populated | shellcheck + manual against a Lightsail snapshot |
| Failure-injection | Kill caddy mid-demo, browser recovers within 2s | manual + Playwright network-throttle |
| Compliance | Audit log includes uid/username for every session.start event | pytest, JSONL grep |

Coverage gates per project rules (≥80% line, ≥75% branch, ≥90% for new core code).

## Open Questions

1. **Topic granularity for `/ws/dashboard`.** Is one topic per widget right, or should we coalesce? *Recommendation:* one topic per widget; 9 topics total. Aggregators already exist as 9 separate computations; mapping is clean.
2. **Slack workspace ownership for the demo.** A dedicated workspace controlled by Josh, or a shared @ctgfederal one? *Defer to operator.*
3. **Mattermost adapter authentication mode.** Personal access token (simpler) vs. OAuth bot (more correct). *Recommendation:* PAT for v1.2, OAuth as a follow-up; PAT is sufficient for on-prem federal deployments where the MM admin and the arc operator are the same human.
4. **Should the manifest live in deploy/aws or in deploy/?** Currently AWS-only. *Recommendation:* `deploy/aws/agents.enabled` for now; if Azure or GCP deploys land, we'll see if the manifest generalizes (likely yes — same shape).

These don't block the PLAN. They're calls to make at implementation time.
