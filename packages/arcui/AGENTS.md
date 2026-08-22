# arcui

> **Build standards:** repo root [`AGENTS.md`](../../AGENTS.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Multi-agent dashboard that **observes and operates** the fleet: a Starlette server reading the shared `arcstore` Observe plane (read-on-demand REST) plus operator-gated mutations, served with a React 19 SPA (2027 control-plane design language — graphite + emerald). Two-token auth (viewer / operator). Backs `arc ui start`, which also embeds a gateway (web chat + optional Slack/Telegram) and the workflow runner. `arcagent` must run headless without it.

## Layer

**Surface / dashboard.** Views and interacts with `arcagent` through public seams; it does not bypass the agent to call ArcRun or ArcLLM. It may depend on `arcgateway`, `arcstore`, `arcteam`, `arctrust`, and `arcskill`. Launched mainly via `arc ui start` / `arc ui tail` (`arccli`).

## Layout

```
src/arcui/                 # Python server
  server.py                # Starlette app factory (create_app / serve) + lifespan
  observe.py               # read-on-demand queries over the arcstore mirror
  observe_stats.py         # windowed stat aggregation (SQL-side)
  registry.py              # in-process agent registry
  embedded_agents.py       # embedded fleet load + gateway runtime wiring
  messaging.py             # arcteam messaging service constructed at startup
  team_stream.py           # /ws/team bus observer + stream hub
  workflow_plane.py        # workflow / gate control-plane seam (SPEC-061)
  auth.py                  # AuthConfig, AuthMiddleware, SessionTracker
  audit.py                 # UI-originated mutation audit (single emission point)
  prompt_signing.py / query_validators.py / schemas.py / types.py / ws_helpers.py
  routes/                  # HTTP + WS routes (agents, tasks, knowledge, workflows,
                           #   approvals, connectors, keys, trust, gateway, chat_ws,
                           #   team_ws, …); routes/agent_detail/ = per-agent tabs
  static/                  # Built SPA (committed for air-gap — no CDN)

web/                       # React 19 + Vite + shadcn/Tailwind v4
  src/app/ (nav, router) / pages/ / components/ / hooks/ / lib/
  → npm run build → src/arcui/static/
```

## Entry points

`create_app`, `serve`, `attach_llm` from `arcui` (all keyword-only; `create_app` defaults to a safe standalone app). Only two WebSockets remain — `/ws/chat/{agent_id}` (bidirectional agent chat) and `/ws/team` (read-only bus stream + one-way human post). Neither is an agent live-push telemetry pipeline (SPEC-026 FR-5 removed it).

## Package rules

- SPEC-026: **read-on-demand** from arcstore — do not reintroduce agent live-push `/ws` telemetry.
- Use `import arcagent` and its public facade; never make ArcAgent depend on this dashboard.
- Two tokens only (viewer / operator); process-memory — no on-disk token file.
- SPEC-022: **zero** direct `team/` filesystem / `watchfiles` in arcui — only via `arcgateway.fs_reader` (`tests/test_arcui_no_team_imports.py`).
- Built static assets are committed (air-gap). UI change → edit `web/`, build into `static/`, commit artifacts.
- **Restart the server after any `web/` rebuild.** The dashboard HTML is read once into `app.state.index_html` at startup (in-memory cache) and served from there; a fresh build on disk is not picked up until the process restarts. Deep links can 404 until then — verify via in-app nav.
- Workflow execution must not require this process — RunnerHost is in `arcgateway`.

## Tests

`packages/arcui/tests/{unit,integration,e2e}/` plus route-level tests.

## Working here

Frontend work stays under `web/`. Server routes under `routes/`. Prefer Observe/query patterns over inventing new push channels.