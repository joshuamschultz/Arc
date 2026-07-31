# SPEC-023 — Product Requirements Document

> Status: draft · Type: integration · Pillar priority: Simplicity → Modularity → Security → Scalability

---

## 1. Background

ArcUI today (per SPEC-015 / SPEC-022) is a browser dashboard for telemetry, fleet visibility, and agent inspection. There is no way for an operator to **send a message to an agent from the browser** — chat input is reserved for Slack, Telegram, or the CLI.

For the NLIT 2026 demo (and as the right long-term architecture), the operator must be able to:
- Open arcui in the browser
- Click into a "Messages" page
- See the list of served agents
- Send a prompt to an agent and see the response in the same UI
- See the agent's memory, workspace, and code-graph stats on a "Knowledge" page

The wrong way to build this is a shortcut path that imports `arcagent` from `arcui` directly. That violates the package layering, duplicates session/queue/policy logic from `arcgateway`, and gives the system two paths for "deliver a message to an agent" (the existing Slack/Telegram path, and a new arcui-specific path).

The right way is to make ArcUI a **third chat platform**, peer to Slack and Telegram. A new `WebPlatformAdapter` in `arcgateway` implements `BasePlatformAdapter`, accepts inbound WebSocket connections, and routes through the existing `SessionRouter`. Cross-platform identity, audit, pairing, queue depth, and policy enforcement apply for free.

This PRD specifies the requirements for that adapter, the in-process gateway-runtime bootstrap, the chat WebSocket route, the Messages page, the Knowledge page, and the federal-compliance hardening track.

## 2. Stakeholders

| Stakeholder | Concern |
|---|---|
| **Operator** (NLIT presenter, lab user) | Reliably DM agents, see their memory and workspace, demo on stage without flakiness. |
| **Federal compliance** (DOE/NASA security) | Air-gap viability today; bounded path to FedRAMP Moderate via documented fixes. |
| **Future maintainer** | Module boundaries are obvious; the adapter pattern is consistent with Slack/Telegram. |
| **Multi-instance future** | NATSExecutor swap is the only change needed for horizontal scale; no architectural rewrite. |

## 3. User Stories

| ID | As a… | I want to… | So that… |
|---|---|---|---|
| US-1 | demo operator | open arcui and chat with an agent in my browser | I can present without leaving one URL |
| US-2 | demo operator | see the agent's response stream into the chat pane | the audience knows the agent is working |
| US-3 | demo operator | see what files the agent created or knows about | I can illustrate the agent's reasoning |
| US-4 | lab researcher (DOE) | run arcui with no internet access | I can use it inside an air-gapped lab |
| US-5 | federal compliance officer | trust that every browser chat turn lands in the audit chain with an identified user | I can certify the system at FedRAMP Low/Moderate |
| US-6 | platform engineer | enable Slack, Telegram, and Web at the same time | one agent serves three frontends without code branches |
| US-7 | future operator | swap to multi-instance scale | I do not need a rewrite — only an executor swap |

## 4. Functional Requirements

> Format: EARS (Easy Approach to Requirements Syntax). Every FR maps to at least one pillar.

### 4.1 Web platform adapter

**FR-1.** When the gateway runtime starts with `[platforms.web].enabled = true`, the system shall register a `WebPlatformAdapter` instance with the `SessionRouter`. *(Modularity)*

**FR-2.** The `WebPlatformAdapter` shall implement `BasePlatformAdapter` exactly — `connect()`, `disconnect()`, `send()`, optional `send_with_id()` — with no extension methods exposed to `SessionRouter`. *(Modularity, Simplicity)*

**FR-3.** When `connect()` is called, the adapter shall complete promptly without dialing any remote service. When `disconnect()` is called, the adapter shall close all registered sockets cleanly and cancel all per-socket drain tasks. *(Simplicity)*

**FR-4.** When the route hands a new WebSocket to `register_socket(ws, agent_did, user_did, chat_id)`, the adapter shall: store the socket under `_sockets[chat_id]`, create a per-socket bounded `asyncio.Queue` (maxsize=100), spawn a per-socket drain task, spawn a per-socket inactivity-monitor task. *(Simplicity, Modularity)*

**FR-5.** When `unregister_socket(ws)` is called, the adapter shall remove the socket from all internal maps, cancel the drain and inactivity-monitor tasks, and close the WebSocket if not already closed. *(Simplicity)*

**FR-6.** When `ingest(chat_id, text)` is called, the adapter shall:
- Reject the call if `text` is empty or longer than `max_frame_bytes`.
- Reject the call if the inbound `client_seq` is less than or equal to the highest seen `client_seq` for that `chat_id`.
- Build an `InboundEvent` with `platform="web"`, the stored `agent_did`/`user_did`, the `chat_id` as the platform `chat_id`, and the `session_key` computed via `build_session_key(agent_did, user_did)`.
- Call `on_message(event, self)` to hand off to the `SessionRouter`.
*(Security, Modularity)*

**FR-7.** When `send(target, message)` is called, the adapter shall:
- Compute `audit_hash = sha256(f"{turn_id}:{message}")`.
- Emit a single audit event `gateway.message.delivered` per turn (NOT per socket) with a `breakdown` field summarizing per-socket outcomes (`delivered`, `dropped_backpressure`, `dead`).
- For each socket registered for `target.chat_id`, enqueue the JSON payload onto the per-socket queue, dropping the oldest queued frame on `QueueFull` and emitting an audit event with `reason: "backpressure"`.
- Return without waiting for socket I/O completion (the drain task is responsible for actual delivery).
*(Simplicity, Security)*

**FR-8.** When the per-socket drain task encounters a `WebSocketDisconnect` or `RuntimeError`, it shall call `unregister_socket(ws)` and exit without propagating the exception. *(Simplicity)*

**FR-9.** When the per-socket inactivity-monitor task observes that the socket has been silent for longer than `idle_timeout_seconds`, it shall close the WebSocket with code 1000 reason "idle" and unregister the socket. *(Simplicity)*

**FR-10.** When the adapter has reached `max_connections` and a new `register_socket` call arrives, the adapter shall reject the call with a documented exception so the route returns HTTP 429 to the client. *(Scalability, Security)*

### 4.2 Identity helper

**FR-11.** A new module `arcgateway.identity` shall expose `derive_viewer_did(viewer_token: str) -> str` returning `"did:arc:viewer:" + sha256(viewer_token)[:16]`. This is the single source of truth for the formula. *(Modularity)*

**FR-12.** When arctrust DID issuance becomes available, only `derive_viewer_did` shall change — the rest of the pipeline (SessionRouter, audit, pairing) shall be unmodified because all consumers depend on the DID *string format*, not the derivation. *(Modularity)*

### 4.3 In-process gateway runtime bootstrap

**FR-13.** A new module `arcgateway.bootstrap` shall expose `async build_for_embedded(team_root: Path, gateway_config: GatewayConfig) -> tuple[AsyncioExecutor, SessionRouter, WebPlatformAdapter, StreamBridge]`. *(Modularity)*

**FR-14.** When arcui's Starlette lifespan starts and `gateway_config` is non-None, the lifespan shall call `build_for_embedded`, store the four components on `app.state`, and tear them down on shutdown. *(Simplicity, Modularity)*

**FR-15.** When `[platforms.web]`, `[platforms.slack]`, and `[platforms.telegram]` are all enabled simultaneously, the bootstrap shall register all three adapters with the same `SessionRouter`. *(Modularity)*

### 4.4 Chat WebSocket route

**FR-16.** The route `/ws/chat/{agent_id}` shall accept WebSocket upgrade requests only after `AuthMiddleware` has validated the `Authorization` header with a viewer or operator token. Unauthenticated upgrades shall return HTTP 401 before the upgrade completes. *(Security)*

**FR-17.** When a WebSocket upgrade succeeds, the route shall:
- Resolve `agent_did` from `agent_id` via the existing `roster_provider`. Return 404 if the agent is not in the roster.
- Compute `user_did = derive_viewer_did(viewer_token)`.
- Compute `chat_id = sha256(f"{viewer_token}:{agent_did}")[:16]`.
- Call `web_adapter.register_socket(ws, agent_did, user_did, chat_id)`.
- Loop: parse each inbound JSON frame and call `web_adapter.ingest(chat_id, frame['text'])`.
- On disconnect: call `web_adapter.unregister_socket(ws)`.
*(Simplicity, Security, Modularity)*

**FR-18.** The route shall NOT pass `viewer_token` into `web_adapter`. The adapter is secret-free. *(Security)*

### 4.5 WebSocket message envelope

**FR-19.** Inbound frames shall be JSON objects with required fields `type` (only `"message"` in v1), `text` (≤8192 chars). Optional `client_seq` for ordering. Oversized or malformed frames shall produce an outbound `error` frame with `code: "frame_too_large"` or `code: "malformed"` and shall NOT call `on_message`. *(Security)*

**FR-20.** Outbound frames shall be one of four types: `status` (`thinking` or `queued`), `tool_call`, `message`, `error`. Every frame shall include `ts` (ISO-8601 UTC). The `message` frame shall include `audit_hash` (SHA-256 of `f"{turn_id}:{text}"`). *(Security, Simplicity)*

### 4.6 TOML configuration

**FR-21.** The gateway config (`GatewayConfig` in `arcgateway/config.py`) shall accept a `[platforms.web]` block with fields: `enabled` (bool, default false), `agent_did` (optional string, falls back to `[gateway].agent_did`), `max_connections` (int, default 50), `idle_timeout_seconds` (int, default 3600), `max_frame_bytes` (int, default 65536). *(Simplicity)*

### 4.7 Messages page (UI)

**FR-22.** The Messages page (`messages` in arc-shell PAGES) shall:
- Render a DM list (left pane) populated from `GET /api/agents` with online/offline indicators.
- On DM selection, open one WebSocket connection to `/ws/chat/{agent_id}` for that agent.
- Render the chat pane (center) with user and agent message bubbles.
- Render the thread/refs panel (right) with `session_key`, recent `tool_call` frames, and the `audit_hash` of the most recent message.
- Indicate `status: "thinking"` and `status: "queued"` frames as visual cues.
- Render `tool_call` frames as inline badges within the active message bubble.
- Maintain a local message store keyed by `turn_id` so that reconnects re-render history without server roundtrip.
*(Simplicity)*

### 4.8 Knowledge page (UI)

**FR-23.** A new endpoint `GET /api/knowledge/{agent_id}` shall return a JSON object with shape per SDD §6 (memory entries with previews, workspace tree with lazy-load directories, code-graph stats with `available: false` fallback, context budget, recent memory events). *(Modularity, Simplicity)*

**FR-24.** The Knowledge page shall subscribe to `FileChangeBridge` events scoped to `team/<agent_id>_agent/memory/` and update the memory list in real time without page reload. *(Simplicity)*

**FR-25.** When `code-review-graph` MCP server is unreachable, the endpoint shall return HTTP 200 with `graph.available: false` (NOT 500). The MCP query shall use `list_graph_stats_tool()` with a 500ms timeout. *(Simplicity, Security)*

### 4.9 Self-hosted fonts

**FR-26.** All arcui pages (existing and new) shall reference fonts from `assets/fonts/` (Inter, JetBrains Mono in WOFF2). No `fonts.googleapis.com` reference shall exist in any served HTML or CSS. *(Security — air-gap)*

## 5. Non-Functional Requirements

### 5.1 Performance / scalability

**NFR-1.** Single arcui process on a 4-core laptop with cloud LLM shall sustain 20–40 concurrent agent sessions and 240–480 msg/min. *(Scalability)*

**NFR-2.** Single arcui process on a 16-core lab workstation with local Ollama shall sustain 100–200 concurrent agent sessions and 1200–2400 msg/min. *(Scalability)*

**NFR-3.** When 10–20 instances are coordinated via NATSExecutor (future), the system shall sustain 10k+ msg/min total. The change required is the executor swap; the WebPlatformAdapter and route are unchanged. *(Scalability)*

**NFR-4.** A single slow WebSocket consumer (browser on degraded network) shall NOT block other WebSocket consumers in the same `chat_id` group nor block agent turn completion. Verified by per-socket bounded queue + drain task design. *(Scalability)*

### 5.2 Security

**NFR-5.** The viewer token shall never appear in audit events, adapter internals, log output, or `chat_id` derivation visible to the adapter. The route is the only component holding the token. *(Security)*

**NFR-6.** Every chat turn shall produce exactly one `gateway.message.delivered` audit event with `audit_hash`, regardless of fan-out outcome. The audit chain shall be deterministic. *(Security)*

**NFR-7.** The system shall pass the OWASP LLM01 (prompt injection), LLM06 (excessive agency), LLM07 (system prompt leakage), ASI03 (identity abuse), ASI07 (insecure inter-agent comms), ASI10 (rogue agents) controls per the brainstorm threat-surface mapping — because all enforcement happens inside `SessionRouter`. *(Security)*

### 5.3 On-prem / air-gap

**NFR-8.** The system shall function in a fully air-gapped environment with no outbound network calls during a chat turn. The only network call is to the operator-configured LLM endpoint (which can be local Ollama/vLLM/TGI). *(Security, Simplicity)*

**NFR-9.** No served HTML or static asset shall reference an external CDN or font host. All assets shall be self-hosted in `arcui/static/assets/`. *(Security)*

### 5.4 Compliance (mapped, not blocking core)

**NFR-10.** The system shall ship with the air-gap-ready baseline. The path to FedRAMP Low (Fix #1: OS user binding) shall be implemented as part of the core build IF time permits before the demo; otherwise as the first task in the Federal Track. *(Security)*

**NFR-11.** The path from FedRAMP Low → Moderate → High shall be five named code changes (Fixes #1–#5 in PLAN Federal Track), not a redesign. *(Modularity, Security)*

### 5.5 Observability

**NFR-12.** Every web-adapter operation (register/unregister/ingest/send/disconnect/idle-close) shall emit a structured audit event to `arcgateway.audit`. *(Security)*

**NFR-13.** The Messages page shall display the latest `audit_hash` so the operator (or auditor) can cross-reference with the `SignedChainSink` server-side log. *(Security)*

## 6. Out of Scope

Per brainstorm §"Out of Scope (For Now)":

- **Token-level streaming** — blocked on `ArcAgent.run()` exposing an async iterator. Replies arrive complete with a `status: "thinking"` indicator.
- **File upload from browser into agent workspace** — separate design effort; `BasePlatformAdapter` has no `send_file` method.
- **arctrust-issued DID** — anonymous-but-stable DID is production-ready for personal/enterprise tiers; arctrust swap is a one-line change in `derive_viewer_did` when arctrust ships.
- **Federal-tier WebSocket hardening** (FIPS-validated TLS termination, signed manifest of static bundle) — Federal Track items, not blocking the core.
- **Channels / group chat** (Slack-style multi-agent rooms) — sequenced after federal hardening + NATSExecutor.
- **Knowledge graph interactive visualisation** — Knowledge page v1 surfaces structured panels only; interactive graph defers to v2.
- **Cross-agent knowledge diff** — multi-agent comparison is a separate design problem.
- **Token rotation handling** — orphaned `chat_id`s on token rotation are an accepted edge case; rotation is rare.

## 7. Success Criteria

**Demo-day (2026-05-04) success:**

1. Open `https://demo.blackarcsystems.com` (or local equivalent) in a browser.
2. Navigate to Messages.
3. See the list of agents from `team/`.
4. Click an agent → chat pane opens.
5. Send a prompt → see `status: "thinking"` → see `message` frame with the agent's response.
6. Workspace files appear in real time on agent-detail or Knowledge page (via existing `FileChangeBridge`).
7. Audit dashboard shows the full event chain with `platform="web"`.
8. Switch to Knowledge → see the agent's memory entries, workspace tree, and graph stats.
9. ttyd subdomain available as a fallback if the chat UI hits trouble live.

**Production success (independent of demo timing):**

10. All 26 functional requirements pass acceptance.
11. All 13 non-functional requirements pass acceptance.
12. `pytest tests/unit/test_web_adapter.py tests/integration/test_web_adapter_session.py` is green.
13. `mypy --strict packages/arcgateway/ packages/arcui/` is clean.
14. `ruff check` is clean across both packages.
15. `pip-audit` reports no critical/high vulnerabilities.
16. Air-gap E2E test passes (`ARC_E2E_AIRGAP=1 pytest tests/e2e/test_air_gap.py`).
17. `arc gateway start` (separate-daemon mode) and `arc ui start` (in-process mode) both work; the adapter is identical.

## 8. Compliance Mapping

| Control | Pillar | Coverage |
|---|---|---|
| **NIST SP 800-53 AU-3** (auditable identity) | Security | Fix #1 (OS user binding) — Federal Track |
| **NIST SP 800-53 IA-5** (authenticator management) | Security | Fix #2 (token TTL + revocation) — Federal Track |
| **NIST SP 800-63-3 AAL-2** (multi-factor auth) | Security | Fix #3 (TOTP/U2F) — Federal Track |
| **NIST SP 800-53 SC-8** (transmission confidentiality) | Security | TLS 1.2+ (cloudflared or reverse proxy); WSS only |
| **NIST SP 800-53 SC-13** (cryptographic protection) | Security | SHA-256 for DID/chat_id; Ed25519 audit chain (existing arctrust) |
| **CMMC L2 AC.L2-3.1.x** | Security | Existing pairing interceptor in SessionRouter |
| **OWASP LLM01–LLM10** | Security | Inherited from SessionRouter — same enforcement as Slack/Telegram |
| **OWASP ASI01–ASI10** | Security | Inherited from SessionRouter |
| **FedRAMP Low** | Security | Air-gap today; Fix #1 closes AU-3 gap |
| **FedRAMP Moderate** | Security | Fix #1 + #2 + #5 (POST bootstrap) |
| **FedRAMP High** | Security | All five fixes |

## 9. Acceptance Criteria Summary

| Pillar | FRs | NFRs |
|---|---|---|
| Simplicity | 2, 3, 4, 5, 7, 8, 9, 14, 17, 20, 21, 22, 23, 24, 25 | 4, 8 |
| Modularity | 1, 2, 4, 6, 11, 12, 13, 14, 15, 17, 23 | 11 |
| Security | 6, 7, 10, 16, 17, 18, 19, 20, 25, 26 | 5, 6, 7, 8, 9, 10, 12, 13 |
| Scalability | 10 | 1, 2, 3, 4 |

Every FR/NFR maps to at least one pillar. None of the acceptance criteria require code outside the four packages identified in §Approach Summary.
