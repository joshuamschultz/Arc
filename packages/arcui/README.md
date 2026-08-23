<div align="center">

# 📊 arcui

### **Real-Time Multi-Agent Dashboard**
*Read-on-demand from the shared arcstore record. Two-token auth. Filter by layer, agent, or team. Watch your fleet think in near-real-time.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-002550.svg)](https://opensource.org/licenses/Apache-2.0)
[![Tests](https://img.shields.io/badge/tests-386%2B-0055BC.svg)](#status)
[![Strict mypy](https://img.shields.io/badge/mypy-strict-0073FE.svg)](#status)
[![Starlette](https://img.shields.io/badge/Starlette-WebSocket-0073FE.svg)](#)

</div>

---

## ✨ What is arcui?

`arcui` is the dashboard. Run it once. Point it at your team directory. Watch your fleet work — and steer it.

It's a Starlette server, backed by `arcstore`'s PostgreSQL operational store plus its durable
spool + WORM record (the **Observe plane**), that renders a UI for LLM calls, tool invocations, costs, runs, tasks, and audit
events read back on demand — agents don't need an opt-in reporter module to show up. It both
**observes** (read-on-demand REST from the PostgreSQL-backed arcstore operational store) and **operates** (operator-gated
mutations: edit config, tasks, channels, files, prompts; approve gated calls; chat with a
running agent). It backs `arc ui start`, which also embeds a gateway (web-chat WS + optional
Slack/Telegram) and the workflow runner.

`arcagent` runs fully headless **without** `arcui` — the dashboard is optional; nothing imports
it to show up.

> 📡 **Reads on demand from the shared PostgreSQL-backed arcstore record. Only `/ws/chat` and `/ws/team` are live sockets. Two-token auth (viewer/operator).**

---

## ⭐ Top Features

What makes `arcui` the definitive real-time agent observability dashboard:

### **Read-On-Demand Architecture**
- **No live push wire** — Agents don't push events into the dashboard; `arcui` reads on-demand from shared `arcstore` record (eliminates dangling connections from crashed agents)
- **Warm start automatic** — Fresh dashboard starts with full history; runs `StoreIngest` over existing WORM files on every read
- **Real-time fleet monitoring** — Watch your entire agent fleet think in near-real-time; every LLM call, tool invocation, and audit event visible

### **Operator Controls**
- **Two-token role separation** — Viewer token (read-only) and operator token (mutations); prevents unauthorized config changes
- **In-place mutations** — Edit config, tasks, channels, files, and prompts directly from the dashboard; all changes audited
- **HITL approvals** — Approve gated tool calls and skill installations from the UI; full context provided
- **Durable Agent Inbox** — Operator-only thread search, read receipts, replies, and handoff create/resolve controls over ArcTeam mail; gateway sessions are not projected here

### **Multi-Layer Interface**
- **Business-first navigation** — Work, Govern, Watch, Advanced, System sections (2027 control-plane redesign); technical names only in detail views
- **15 specialized pages** — Agent detail, Tasks (Mission Control), Approvals, Audit (Security), Run River, Workflows, Knowledge, Model usage, and more
- **React 19 + shadcn/ui** — Air-gap friendly with self-hosted fonts; no CDN dependency; built output committed for zero Node requirement

### **WebSocket Support**
- **Two live sockets** — `/ws/chat/{agent_id}` for interactive chat, `/ws/team` for team stream with `@mentions`
- **No event push pipeline** — Removed per SPEC-026 FR-5; eliminates dangling connections from crashed agents
- **Token-scoped access** — Both sockets require viewer/operator tokens; no anonymous access

### **Terminal Integration**
- **JSONL streaming** — `arc ui tail` outputs structured events to stdout; pipe into `jq`, `grep`, or log tools
- **Layer filtering** — Filter by `llm`, `agent`, `run`, or `team` layer; focus on specific concerns
- **Agent targeting** — Filter by DID or team group; monitor specific agents or teams

---

## 🏗️ Where It Fits

```mermaid
flowchart TB
    classDef surface fill:#5A9CFF,stroke:#003B82,color:#002550
    classDef agent fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef llm fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef found fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef other fill:#E9EAEB,stroke:#7F7F7F,color:#0B1220

    arcagent[arcagent]:::agent -->|writes| arcstore[arcstore<br/>durable spool + WORM]:::found
    arcstore -.->|PostgreSQL Observe plane, read on demand| arcui
    arcllm[arcllm<br/>JSONLTraceStore]:::llm -->|attach_llm| arcui
    arcgateway[arcgateway<br/>fs / roster data plane]:::surface -->|in-process read| arcui
    arcui[arcui<br/>Starlette · WebSocket · UI]:::surface --> Browser[🖥 Browser]:::other
    arcui --> Terminal[⌨ arc ui tail]:::other
```

Depends on `arcllm` (for `JSONLTraceStore`), `arcgateway` (the read-only fs / roster data
plane), and `arcstore` (the PostgreSQL Observe-plane store it reads from). Nothing needs to import
`arcui` to show up in it — `arcagent` writes through `arcstore` like everything else; `arccli`
depends on `arcui` only for the `arc ui` commands.

---

## 🚀 Install

```bash
pip install arcmas              # arcui is included in the meta package
```

---

## 🎬 Two-Terminal Quickstart

```bash
# Terminal 1 — run an agent (writes activity through arcstore as it goes)
arc agent serve my-agent

# Terminal 2 — start the dashboard, pointed at the team directory the agent lives in
arc ui start --team-root ./team --show-tokens
# Output:
#   Viewer token:   abc123...
#   Operator token: def456...
#   ArcUI dashboard: http://127.0.0.1:8420
```

Open http://127.0.0.1:8420 with the **viewer** token — on a loopback bind the browser opens
pre-authenticated, no copy-paste needed. Or stream to terminal:

```bash
# Terminal 3 — JSONL stream of every event
arc ui tail --viewer-token <token> --layer llm
```

---

## 🧪 Quick Example (Python)

```python
from arcui import create_app
import uvicorn

app = create_app()
# Viewer/operator tokens print to stdout on first run (masked unless --show-tokens);
# they live only in the running process, no on-disk token file.
uvicorn.run(app, host="127.0.0.1", port=8420)
```

Or attach an arcllm model directly:

```python
from arcui import create_app, attach_llm
from arcllm import load_model

app = create_app()
model = load_model("anthropic", telemetry=True, audit=True)
attach_llm(app, model)
```

---

## 🔐 Two-Token Auth

`arcui` uses two role-separated tokens. **Both are auto-generated at startup if you don't supply them**, and printed masked (`--show-tokens` prints them in full). Tokens live only in the running process — there is no on-disk token file and no separate "agent" token or role.

| Token | Lets You |
|---|---|
| **viewer** | Read events, view the dashboard, run `arc ui tail` |
| **operator** | Mutate runtime config (PATCH `/api/arcllm-config`, `/api/config`), see unredacted LLM config |

```bash
# Auto-generate tokens
arc ui start --show-tokens

# Supply your own
arc ui start \
  --viewer-token mytoken \
  --operator-token optoken
```

> ⚠️ `arc ui tail` requires `--viewer-token` explicitly.

Agents don't push events into the dashboard over a token-authenticated channel at all (SPEC-026 FR-5): `arcui` is a pure **reader** of the durable record. It runs `StoreIngest` over the shared `arcstore` spool + WORM files (everything `arcllm`/`arcrun`/`arcagent` already wrote, whether or not `arcui` was running) into the shared PostgreSQL operational store, then serves read-on-demand REST from that store. There is no live push wire, so there's nothing for a compromised or crashed agent to leave dangling.

---

## 📟 CLI Commands

```bash
# Start
arc ui start                                  # default 127.0.0.1:8420
arc ui start --port 9000
arc ui start --host 0.0.0.0                   # bind all interfaces (careful!)
arc ui start --show-tokens                    # print full tokens
arc ui start --max-agents 500                 # default 100
arc ui start --team-root ./team               # agent-discovery root (SPEC-022 routes)
arc ui start --gateway-config ./gateway.toml  # enable Slack/Telegram; default enables the web platform
arc ui start --no-browser                     # headless / CI
arc ui start --no-chat                        # disable the in-process web chat platform

# Stream events
arc ui tail --viewer-token <t>                # all events
arc ui tail --viewer-token <t> --layer llm    # filter to LLM layer
arc ui tail --viewer-token <t> --layer agent  # filter to agent layer
arc ui tail --viewer-token <t> --layer run    # filter to arcrun loop layer
arc ui tail --viewer-token <t> --layer team   # filter to team layer
arc ui tail --viewer-token <t> --agent did:arc:acme:.../   # filter by agent
arc ui tail --viewer-token <t> --group research-team       # filter by team
```

---

## 🧱 Public API

```python
from arcui import (
    create_app,            # Starlette factory (see kwargs below)
    serve,                 # convenience: create_app + uvicorn.run
    attach_llm,            # connect an arcllm model so its traces read live
)

# create_app is keyword-only. Everything defaults to a safe, standalone app:
#   auth_config, config_controller, agent_info, max_agents=100,
#   team_root, gateway_config, messaging_service, team_post_forwarder,
#   team_stream_interval=1.0, data_dir, workspace_dir,
#   allow_external_task_refs=False,          # ADR-019 tier = stringency
#   workflow_control_plane, gate_control_plane  # None → workflow routes 503
```

### How agent data reaches the dashboard

There is no live push wire from the agent process (SPEC-026 FR-5 tore it out — see
**Two-Token Auth** above). `arcui` runs its own `StoreIngest` over the shared `arcstore`
spool + WORM files and serves everything read-on-demand from the shared PostgreSQL operational store, so any
agent that already wrote to the shared store shows up whether or not `arcui` was running when
it did. `arc ui start --team-root <dir>` just points `arcui` at the right `arcstore` data
dir / team directory to read from — nothing is registered or authenticated from the agent
side, and there is no corresponding flag on `arc agent serve`.

---

## 🪟 What You See

The dashboard surfaces:

- **Live event stream** — every LLM call, tool call, turn boundary
- **Per-agent state** — current task, last response, tool/skill counts, DID
- **Costs** — running USD per agent, per session, per provider
- **Audit trail** — searchable, filterable, exportable
- **Team view** — multiple agents grouped by team membership
- **Layer toggles** — show only LLM events, only tool events, only audit events, etc.

`arc ui tail` gives you the same data as JSONL on stdout — pipe it into `jq`, `grep`, or any structured-log tool.

### Pages (path-routed)

The dashboard is a React single-page app with path-based routing. Bookmark a route and the deep-link reopens to it. The 2027 control-plane redesign wave regrouped the nav around what the operator does — **Work**, **Govern**, **Watch**, **Advanced**, **System** — with the technical package names kept only inside detail views. The screens below are business-first labels over the same read-on-demand data.

| Page (label) | Path | What it shows |
|------|------|--------|
| Home | `/home` | Today landing — fleet at a glance, recent activity, open loops |
| Fleet | `/agents` | `/api/team/roster` — status / current action / signed-today cards |
| Agent Detail | `/agents/:id/:tab` | Per-agent tabs: Overview · Identity · Sessions · LLM · Skills · Tools · Tasks · Schedules · Policy · Prompts · Connect · Trust · Knowledge · Runs · Inbox (durable AgentMail) |
| Chat | `/messages` | Slack-style agent chat (`/ws/chat/{id}`), inline HITL approvals, team channels (`/ws/team`) |
| Tasks | `/tasks` | Mission Control kanban + per-status filters (`/api/team/tasks`) |
| Approvals | `/approvals` | Pending trifecta / gate approvals with full request context |
| Pending capabilities | `/gated` | Agent-authored / operator-added skills & tools awaiting review |
| Rules | `/policy` | `/api/team/policy/{bullets,stats}` — fleet-wide ACE policy bullets |
| Audit | `/security` | The signed ledger — `/api/team/audit`, policy denials, connection panel |
| Activity (Run River) | `/arcrun` | Agentic-loop runs — two-pane signed action trace + run-replay drawer + honest run status |
| Workflows | `/workflows`, `/workflows/:id` | SPEC-061 ArcFlow DAGs — graph, run history, gate resolution (503 when no control plane) |
| Knowledge | `/knowledge` | Context budget, memory, entities, procedures, workspace tree |
| Model usage (ArcLLM) | `/arcllm` | LLM telemetry — overview charts + live Calls table with per-call drawer (`/api/stats`, `/api/traces`) |
| Tools & Skills | `/tools-skills` | Fleet tools matrix + skills directory (durable enumeration) |
| Connections | `/connections` | Shared connectors + per-agent grants |
| Settings | `/settings` | arcllm config (PATCH `/api/arcllm-config`), operator-gated |

### WebSockets

There is **no** `/ws` event-push / `subscribe:agent` feed — SPEC-026 FR-5 removed the live push
pipeline (EventBuffer, SubscriptionManager, the per-agent `file_change` bridge), and the REST
views read the PostgreSQL-backed `arcstore` operational store on demand instead. Two WebSockets remain:

| Socket | Direction | Purpose |
|--------|-----------|---------|
| `/ws/chat/{agent_id}` | bidirectional | interactive chat with a running agent |
| `/ws/team` | read-only stream + one-way post | drains the arcteam bus to the browser (frames carry handles, mark `@mentions`); a `{"type": "post", "channel": ..., "text": ...}` frame is forwarded and signed as the human entity. `viewer`/`operator` tokens only. |

### Frontend (`web/`)

The frontend is a **React 19 + shadcn/ui + Tailwind v4** SPA under `packages/arcui/web/`, styled in the 2027 control-plane design language — a graphite neutral surface with a sparingly-used emerald accent (OKLCH tokens, full light + dark). It's built with Vite straight into `src/arcui/static/`, which the Starlette server serves unchanged (`Route("/", _index)` + `Mount("/assets")`). The built output is committed, so `pip install` / `arc ui start` need no Node toolchain.

Air-gap-friendly: **no CDN dependency**. Fonts (Hanken Grotesk, Bricolage Grotesque, IBM Plex Mono, Lora) are self-hosted and bundled by Vite. `sw.js` is a one-time kill-switch service worker that unregisters any previously-installed caching SW (Vite content-hashing plus a per-startup `{{ARC_BUILD_ID}}` cache-bust handle staleness).

```bash
cd packages/arcui/web
npm install
npm run build      # → ../src/arcui/static/  (commit the output)

# Dev loop: HMR against a running backend
arc ui start --no-browser --show-tokens   # terminal 1 (note the viewer token)
npm run dev                                # terminal 2 — proxies /api + /ws to :8420
```

Key modules: `lib/api.ts` (bearer client), `hooks/use-chat.ts` and `hooks/use-team-stream.ts` (the `/ws/chat` + `/ws/team` WebSocket clients), and reusable components `data-table.tsx` / `file-tree.tsx` / `trace-drawer.tsx` / `run-replay-drawer.tsx` / `policy-bullet.tsx`.

---

## 🛡️ Security Architecture

### Token Separation

Two tokens with two different scopes prevents the common "anyone with the URL can do anything" failure mode. A read-only viewer can't mutate the runtime LLM/app config or see unredacted config secrets.

### No Token Persistence

Both tokens are generated fresh in the running process on every `arc ui start` (or supplied explicitly via `--viewer-token`/`--operator-token`) and printed masked unless `--show-tokens`. There is no on-disk token file and no separate agent-facing token or role — see **Two-Token Auth** above for why agents don't need one.

### Default Bind Address

`arc ui start` defaults to `127.0.0.1` — **not** `0.0.0.0`. The dashboard is local-by-default. Binding all interfaces requires explicit `--host 0.0.0.0`, and you should put it behind mTLS or a reverse proxy when you do.

### Warm Start Is Automatic

There's no explicit "replay" flag: `arcui` uses `StoreIngest` over the shared `arcstore` spool + WORM files and PostgreSQL operational store on every read, so a freshly-started dashboard already has the full durable history — including everything written while `arcui` wasn't running — without any live agents needing to backfill state.

---

## 📋 Compliance Mapping

| NIST 800-53 | What `arcui` Provides |
|---|---|
| AU-2 | Live event surface for human review of all agent operations |
| AU-9 | Read-only display of audit events; cannot modify the underlying log |
| AU-11 | Long-term retention via the shared `arcstore` durable record; automatic warm start on restart |
| SI-4 | Near-real-time monitoring, read-on-demand from the `arcstore` mirror |
| SC-13 | Two-token role-separated auth |

| OWASP Agentic | Mitigation |
|---|---|
| ASI09 (Trust Exploitation) | Every agent action surfaced with attribution; humans can spot impersonation in real time |
| ASI10 (Rogue Agents) | Behavioral monitoring at the human-readable layer; anomalies become immediately visible |

---

## 🧪 Status

```bash
uv run --no-sync pytest packages/arcui/tests
```

- **Tests:** 386+
- **Type check:** `mypy --strict` clean
- **Lint:** `ruff check` clean

---

## 📄 License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
