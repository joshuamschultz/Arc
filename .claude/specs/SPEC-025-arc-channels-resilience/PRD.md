# SPEC-025 — Arc Channels Resilience: Product Requirements

## Problem Statement

The 2026-05-05 SCAP demo failed because:

1. The browser's WebSocket went stale after a server restart and never auto-reconnected — the operator saw a "timeout" with no path to recovery.
2. The demo agent (`scap_isso_agent`) was reachable **only** through arcui. There was no Slack, Mattermost, Telegram, or other chat surface configured. When arcui hung, the operator had nowhere to go.
3. The `arc-stack` systemd unit treated any agent crash as a fatal failure, so unrelated broken-on-this-VM agents (`brad/brian/josh/mosa/my`) tripped the unit even though the three demo agents were healthy.
4. Background dashboard polling — `/api/stats`, `/api/queue`, `/api/team/roster`, etc. — fires every second per browser tab, masking real failures in the access log and inflating the failure surface.

These are not architectural failures of the underlying design (SPEC-023 shipped the right `WebPlatformAdapter` peer model). They are reliability gaps and missing fallback surfaces that the operator hit during a live demo.

## Vision

> **The operator can always reach a served agent.** When one chat surface fails, another works. The browser auto-recovers from network blips without operator intervention. Dashboards reflect reality, not polling artifacts. A broken non-demo agent does not break the demo.

## Audience

| Persona | Pain today | What this fixes |
|---|---|---|
| **Demo operator (live)** | Browser hangs mid-demo; no recovery path | Auto-reconnect on seq-gap; Slack fallback; service-worker-cached static shell |
| **Federal demo audience (DOE/NASA/DoD)** | No FedRAMP-authorized chat surface to reach the agent | Mattermost adapter (FedRAMP High/IL5), Teams sketch (GCC High) |
| **Operations engineer** | systemd churns on broken agents; deploy is one-shot all-or-nothing | Per-host agent manifest; agent-level health independent of unit health |
| **Platform owner (Josh)** | "constantly breaks" — too many failure surfaces, too much polling | Single-transport dashboards; bounded failure surface |
| **Future contributors** | New chat surface = unclear contract (TUI? Page? Adapter?) | Reinforces channels-as-product principle: only path is `BasePlatformAdapter` |

## Use Cases

### UC-1 — Demo recovery from network blip
Operator is mid-demo. Network hiccup. Browser drops the WebSocket. Within 2 seconds, the browser detects the gap (seq-mismatch on next frame OR connection-closed event), auto-reconnects, and replays missed events. Operator sees a brief "reconnecting…" indicator and the chat resumes. No refresh needed.

### UC-2 — Operator falls back to Slack mid-demo
Operator types in arcui. arcui hangs (websocket dead, server overloaded, whatever). Operator opens Slack, types the same prompt to `@scap_isso`, gets the same reply. The trace dashboard in arcui is still informative even when arcui's own chat path is degraded.

### UC-3 — Federal lab evaluation (air-gapped)
DOE Sandia evaluator deploys arc on an air-gapped Linux box. Configures Mattermost as the chat surface (no Slack, no Telegram, no internet). arcui is available locally for trace/audit visualization. Audit chain identical to commercial deployment.

### UC-4 — Production deploy with one broken agent
Engineer pushes 5 agents to production. One has a bad config. systemd marks the unit `active`, the 4 healthy agents are reachable, the broken one is shown as `degraded` in the roster, and the unit does not flap. Engineer fixes the broken agent and redeploys without restarting the world.

### UC-5 — Dashboard reflects reality, doesn't poll
Browser opens the trace page. The page subscribes once to `/ws/dashboard?tabs=stats,queue,roster`. Server pushes updates as state changes. No `setInterval`. Caddy access log shows a steady WebSocket connection per tab, not 20 GET/sec churn.

## Functional Requirements

### FR-1 — Sequence-gap auto-reconnect (P0, demo-blocker)

**Server (WebPlatformAdapter):**
- Every outbound frame (`status`, `tool_call`, `message`, `error`) carries a `seq` integer monotonically increasing per `chat_id`. Counter is in-memory; resets on adapter restart.
- On socket registration, the adapter assigns the current `seq` (the next frame the client will see).

**Client (messages-page.js):**
- Tracks `lastSeq` per `chat_id`. On receipt of a frame with `seq != lastSeq + 1` (and `seq != 0` for fresh sockets), client closes the WS and reconnects with reason `"seq-gap"`.
- On reconnect, server replays the last N events from a per-`chat_id` ring buffer (N=50) so the client catches up. Events older than the ring's tail show as a banner: "Reconnected — earlier messages may be missing." (No silent gaps.)
- Exponential backoff for reconnect: 800 ms → 1.7× → 15 s cap. Same as OpenClaw.

**Acceptance criteria:**
- AC-1.1 — A simulated socket close + 5s gap + reopen reproduces the demo failure today; with this change, the client transparently recovers within 2s and renders the missed frames. **Pillar: Simplicity, Scalability.**
- AC-1.2 — Audit chain on the server logs the disconnect-and-reconnect events with the same `chat_id`; no orphaned `chat_id`s. **Pillar: Security.**
- AC-1.3 — A sequence-gap of 100+ frames (older than the ring buffer) shows a recovery banner instead of silent corruption. **Pillar: Simplicity, Security.**

### FR-2 — At least one non-arcui chat surface per demo agent (P0, demo-blocker)

**Slack adapter wired** for `scap_isso_agent`, `nlit_cora_agent`, `nlit_soc_agent`:
- Operator-managed Slack workspace + bot token in `/home/ubuntu/arc/.env` on the demo VM.
- Each demo agent registers with both `WebPlatformAdapter` AND `SlackAdapter` simultaneously (already validated in SPEC-023 research).
- Operator can DM `@scap_isso` in Slack and get the same response as arcui.

**Acceptance criteria:**
- AC-2.1 — `arc-stack` startup log shows both `web_adapter` and `slack_adapter` registering for the demo agents. **Pillar: Modularity.**
- AC-2.2 — Same prompt, same `chat_id` lineage in audit (one session per `(user_did, agent_did)`), regardless of which adapter delivered the message. **Pillar: Security.**
- AC-2.3 — Killing arcui's process leaves Slack chat fully functional (operator-verified during the next rehearsal). **Pillar: Modularity.**

### FR-3 — Service worker for arcui static shell (P1, demo polish)

**Behavior:**
- Caches `/assets/*` (cache-first for hashed bundles).
- HTML fetched network-first; falls back to cached version if offline (with a "stale" banner).
- **Excludes** `/api/*`, `/ws/*`, `/artifacts/*` — those must always hit the network.
- Registered from `main.js` only in production builds; dev unregisters any leftover SW.

**Acceptance criteria:**
- AC-3.1 — A network blip during the demo (kill caddy for 3s, restore) does not leave the operator with a blank page; the cached HTML + JS rehydrates. **Pillar: Simplicity.**
- AC-3.2 — `/artifacts/digest_*.pdf` requests never hit the cache (always live). **Pillar: Security — fresh artifacts on every load.**

### FR-4 — Per-host agent manifest in deploy (P0, demo-blocker)

**Today:** `setup-vm.sh` rsyncs all of `team/` from laptop to VM. Agents that need keys/config not present on the VM crash, and the systemd unit treats any agent crash as fatal.

**Change:**
- New file: `deploy/aws/agents.enabled` (one agent name per line — `scap_isso_agent`, `nlit_cora_agent`, `nlit_soc_agent`).
- `setup-vm.sh` reads this file, rsyncs only those agent directories, removes any others.
- `arc-stack.sh` collects per-agent health and treats unit health as `pass if any agent connected to UI` rather than `pass if all agents connected`. A roster column shows per-agent health independent of the unit.

**Acceptance criteria:**
- AC-4.1 — On a fresh VM, after rsync + setup-vm, only the agents in `agents.enabled` exist on disk. **Pillar: Simplicity.**
- AC-4.2 — A deliberately broken agent (missing key) shows `degraded` in the roster but does not flap the systemd unit. **Pillar: Modularity, Scalability.**
- AC-4.3 — Removing an agent name from `agents.enabled` and re-running setup-vm cleanly removes the agent's directory and stops its process. **Pillar: Simplicity.**

### FR-5 — Single-transport dashboards (P1, production polish)

**Today:** 9 GET endpoints polled every 1–5 seconds per tab.

**Change — `/ws/dashboard` event subscription:**
- Browser opens `/ws/dashboard` on dashboard page load.
- Sends `{type: "subscribe", topics: ["stats", "queue", "roster", "circuit_breakers", "budget", "performance", "cost_efficiency", "schedule_history", "timeseries"]}`.
- Server pushes per-topic events as the underlying state changes (or on a coarse server-side throttle, e.g. 5s for time-series).
- All polling endpoints removed. Browser stops `setInterval` polling.
- Same per-socket bounded queue + drop-oldest backpressure as `WebPlatformAdapter`.

**Acceptance criteria:**
- AC-5.1 — Caddy access log on the demo VM shows ≤ 5 GET/sec total during a 1-hour idle dashboard session (down from the current ~20). **Pillar: Simplicity, Scalability.**
- AC-5.2 — Removing the polling endpoints does not break any other consumer (CLI, MCP server, etc.) — verified by grep. **Pillar: Modularity.**

### FR-6 — Mattermost adapter (P2, federal demo unblocker)

**New adapter:** `packages/arcgateway/src/arcgateway/adapters/mattermost.py`. Implements `BasePlatformAdapter`. Connects via Mattermost's WebSocket API. Same envelope contract as Slack/Telegram.

**Acceptance criteria:**
- AC-6.1 — Adapter passes the same `BasePlatformAdapter` contract tests as `SlackAdapter`. **Pillar: Modularity.**
- AC-6.2 — Reachable from a Mattermost server running in air-gapped mode (validated against a local docker-compose test fixture). **Pillar: Security — federal posture.**
- AC-6.3 — Audit events carry `platform="mattermost"` and ride the same chain as web/slack/telegram. **Pillar: Security.**

### FR-7 — OS-user binding in audit (P1, FedRAMP Low gating)

From the original brainstorm §2 — already mapped, ship it.

- `arcui/auth.py:SessionStartFields` includes `uid` and `username` from `pwd.getpwuid(os.getuid())`.
- `gateway.session.start` audit events include the OS user.

**Acceptance criteria:**
- AC-7.1 — Audit log on a federal-tier deployment ties every session start to an OS user. **Pillar: Security — NIST AU-3.**

## Non-Functional Requirements

| ID | Requirement | Threshold | Pillar |
|---|---|---|---|
| NFR-1 | Auto-reconnect time after seq-gap | < 2s | Simplicity |
| NFR-2 | Service worker added bundle weight | < 5KB | Simplicity |
| NFR-3 | Per-socket queue depth | maxsize=100 (existing) | Scalability |
| NFR-4 | Dashboard event push latency | < 200ms p95 | Scalability |
| NFR-5 | Mattermost adapter air-gap requirement | zero outbound DNS calls when `tier=federal` | Security |
| NFR-6 | Per-host deploy manifest | enforced by setup-vm; not just documented | Simplicity |

## Out of Scope (deferred)

- **Lit/web-component refactor** of `messages-page.js` — separate spec, not a reliability fix.
- **Teams GCC High adapter** — sketched in SDD only; full implementation is a follow-up spec (~2–3 weeks via Microsoft Bot Framework).
- **Multi-tenancy** for arcui (one operator's agent fleet per arcui instance is the assumed model — same as SPEC-023).
- **NATS executor swap** — orthogonal scaling work; SPEC-023's `AsyncioExecutor` is the v1 ceiling.
- **Token rotation chat_id versioning** — flagged in SPEC-023, deferred again here.
- **File upload in browser** — flagged in SPEC-023, still out of scope.

## Risk Register

| Risk | Severity | Mitigation |
|---|---|---|
| Slack token leakage in `.env` on shared VM | Medium | `chmod 600 .env`; document in DEPLOY.md; rotate token after demos |
| Service worker stale-cache bug locks operator out | High | `SW_DISABLE` env var to bypass; ensure dev unregisters in `main.ts` |
| Per-host manifest forgotten on a new VM | Medium | `setup-vm.sh` defaults to a reasonable list and warns if `agents.enabled` is missing |
| `/ws/dashboard` migration breaks an external consumer | Low | Grep-audit before deletion; keep polling endpoints behind a feature flag for one release |
| Mattermost adapter desyncs on long-poll edge cases | Low | Reuse SPEC-023's per-socket queue pattern; backpressure-tested under load |
| `seq` ring buffer fills on sustained reconnect storm | Low | Cap ring at 50/`chat_id`; client shows recovery banner on overrun |

## Success Metrics

| Metric | Today | Target | How measured |
|---|---|---|---|
| Demo recovery time after network blip | manual page refresh + session loss | < 2s, automatic | Operator stopwatch + Caddy log inspection |
| Number of chat surfaces per production agent | 1 (arcui only) | ≥ 2 (arcui + Slack/Mattermost) | `arc-stack` startup log inspection |
| Caddy access log GET/sec on idle dashboard | ~20 | ≤ 5 | Sample 1-hour window |
| systemd unit health under one-broken-agent | failed | active (with `degraded` agent in roster) | Deliberate fault-injection test |
| FedRAMP Low audit gate (OS user in audit) | absent | present | Audit log inspection |

## Approval Gates (fast-track)

This spec uses fast-track routing — single review at the spec level, then phase-by-phase implementation per PLAN.md. Phase boundaries in PLAN.md are the only mid-implementation approval gates.
