# arcui

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Real-time multi-agent dashboard: Starlette server reading the shared `arcstore` Observe plane, plus a React SPA. Operator/viewer two-token auth.

## Layer

**Surface / dashboard.** Depends on `arcllm`, `arcagent`, `arcgateway`, `arcstore`, `arcteam`, `arctrust`, `arcskill`, Starlette/uvicorn. Launched mainly via `arc ui start` / `arc ui tail` (`arccli`).

## Layout

```
src/arcui/                 # Python server
  server.py / observe.py / auth.py / registry.py / messaging.py
  routes/                  # HTTP + workflow routes, …
  static/                  # Built SPA (committed for air-gap — no CDN)

web/                       # React 19 + Vite + shadcn/Tailwind
  src/app/ / pages/ / components/ / hooks/ / lib/
  → npm run build → src/arcui/static/
```

## Entry points

`create_app`, `serve`, `attach_llm` from `arcui`. Remaining WS: `/ws/chat/{agent_id}`, `/ws/team` (not a live agent push telemetry pipeline).

## Package rules

- SPEC-026: **read-on-demand** from arcstore — do not reintroduce agent live-push `/ws` telemetry.
- Two tokens only (viewer / operator); process-memory — no on-disk token file.
- SPEC-022: **zero** direct `team/` filesystem / `watchfiles` in arcui — only via `arcgateway.fs_reader` (`tests/test_arcui_no_team_imports.py`).
- Built static assets are committed (air-gap). UI change → edit `web/`, build into `static/`, commit artifacts.
- Workflow execution must not require this process — RunnerHost is in `arcgateway`.

## Tests

`packages/arcui/tests/{unit,integration,e2e}/` plus route-level tests.

## Working here

Frontend work stays under `web/`. Server routes under `routes/`. Prefer Observe/query patterns over inventing new push channels.
