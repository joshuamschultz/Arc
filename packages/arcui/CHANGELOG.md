# Changelog

All notable changes to ArcUI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-07-12

SPEC-056 Mission Control, Phase D: the dashboard gets a team-wide task kanban and a per-agent
task list, both reading live off the shared arcstore `tasks` collection.

### Added

- **Task kanban + per-agent task list** — `Observe.tasks(owner_did, status)` reads the arcstore
  mutable `tasks` collection; `/api/team/tasks` and `/api/agents/{id}/tasks` are re-pointed off
  the old `tasks.json` reader onto arcstore (the dead reader is deleted). New operator-gated
  `routes/tasks.py` (`POST`/`PATCH`, mirroring `files_write.py`) refuses a viewer (`403`) and an
  edit on an `in_progress` task (`409`, "steer the owner" instead of editing), and audits every
  mutation through `emit_mutation_audit`. `Observe.audit` gains a `?target=` filter so a task's
  activity timeline (created/assigned/started/completed/failed) reads straight off the existing
  audit stream. The server wires `app.state.task_store` onto the same `store/arcui.db` `Observe`
  already reads — one store, no second source of truth.
- **Frontend: kanban board, task drawer, agent Tasks tab** — a six-column task board
  (`task-board.tsx`) with priority/owner/blocked/run-link cards (`task-card.tsx`); a task drawer
  (`task-drawer.tsx`) with the activity timeline, structured output, operator at-rest edit, and a
  "Steer owner" action (via the SPEC-055 `MentionComposer`) in place of an edit form for
  in-progress cards; a create-task sheet; a tasks page with status/priority/owner/tag filters, a
  metrics row, and a counts strip; and a new Tasks tab on the agent-detail view.

Reality Mirror — the dashboard becomes a live window into each agent's own on-disk state,
not a synced copy, with operator-gated mutations where it makes sense (T-702–717):

- **Knowledge view** (T-708/709, COMP-002) — eight new routes under
  `/api/agents/{agent}/knowledge` serve paged memories (`?q=` ranked search), entry detail +
  links, entities + links, entirely through the new `arcmemory.operator.MemoryOperator` facade
  (zero SQL in arcui, REQ-087). `PATCH` accepts text/importance/salience (edit/set-metadata),
  `DELETE` removes — operator-token-gated, audited, never a partial success (REQ-089). An empty
  store reads `200`/`total=0` (lazy DB create); an unreadable store surfaces the real exception as
  `503` verbatim rather than a synthesized state field (REQ-097). Frontend: browse/search with
  metadata columns (created, recency, importance, source), link navigation, operator edit/delete,
  distinct empty-vs-unreadable states.
- **Workspace file editor** (T-716, COMP-012) — `PUT /api/agents/{id}/files/read` saves a file
  under the same rules an agent lives by: canonical-path confinement to the selected agent's root
  (absolute/`../`/symlink escapes → 400) and the secret-content guard (mirrored from arcagent's).
  Operator-token only. If the file has a signed `.arcsig` sidecar the response reports
  `signature_stale` — the UI holds no agent identity and never signs server-side. Frontend:
  rendered markdown + edit mode, verbatim server errors, a prominent stale-signature warning.
- **Channel management** (T-707/T-710) — `POST` create (`409` on duplicate via the service's own
  guard, not just the CLI's), member add/remove resolving agent refs through the arcteam registry.
  Operator-only, every outcome audited, `503` when the service is unwired. A single new
  `emit_mutation_audit` seam is now the one emission point for every UI-originated mutation
  (actor role, session id, target, operation, outcome). Frontend: real channel list with a
  service-unavailable state distinct from empty, create-channel dialog, member add/remove.
- **arcteam messaging wired into the embedded server** (T-706, COMP-004) — `/api/team/channels`
  returned `[]` on every live deployment because nothing ever set
  `app.state.messaging_service`. New `arcui/messaging.py` constructs the service directly from
  arcteam primitives at server startup and closes it on shutdown; the operator signer loads
  read-only (`generate_if_absent=False` — an observer never mints the deployment key).
- **Capability inventory views** (T-704/705, COMP-007, T-711) — new `/api/agents/{id}/capabilities`
  route surfaces every skill/tool across all four scan roots with the loader's own verdict
  (`loaded`/`unsigned`/`invalid`/`error`/a `Decision` value) captured verbatim at each terminal
  decision site in `arcagent` — never a UI guess, and posture-faithful to the agent's real
  tier/pinned-key/import-policy at load time. Fixed alongside: agent-authored and operator-added
  skills (written to `capabilities/skills/<name>`) were never loading at all — only the builtins
  root had a skills subdirectory scan root; now all three writable roots (global/agent/workspace)
  get one. Frontend: skills/tools tables render verdicts as status badges with source-root chips
  and denial-detail popovers; the stale skill-drawer/skills-browser components and their
  now-dead endpoint shapes are deleted.

This entire surface reaches into `arcagent` through exactly one seam
(`arcagent.capabilities.inventory`), imported lazily to avoid a hard package cycle (arcagent
already depends on arcui for `UIBridgeSink`) — enforced by an architecture test that fails on any
other `arcagent` import from this package.

### Fixed

- **Agent status read from a file only the non-canonical `arc-stack.sh` ever wrote.**
  `~/.arcagent/agent-state.json` was stale or absent on every embedded deployment; status now
  derives entirely from the embedded agent registry (`online` was already honest; the unused
  `degraded` field — no live data source — is removed; frontend shows Online/Idle instead).

### Removed

- **Dead agent-control path + vestigial agent auth role (simplification sweep)** — there was
  no live push wire from agent processes into the dashboard (SPEC-026 FR-5 already made
  `arcui` a pure reader of the `arcstore` durable record), so the unused third "agent" auth
  role/token, the unwired `audit_buffer`, and several dead `observe` methods are deleted.
  Auth is two roles (`viewer`/`operator`) only — there was never an on-disk token file for
  either.

### Changed

- **`observe`/`observe_stats` windowed stats computed SQL-side** — `arcstore` backends
  (`base.py`/`memory.py`/`sqlite.py`) gained the aggregation so `arcui` no longer pulls a full
  window into Python to compute it.
- **`registry.get_tools` decomposed** into smaller helpers (no behavior change).

## [0.2.0] - 2026-04-26

Major refactor: full multi-agent observability platform, hardened auth, and `UIBridgeSink` for live agent telemetry.

### Added

- **`UIBridgeSink`** (`bridge.py`) — Connects an arctrust audit stream from a running agent to the live dashboard. Replaces ad-hoc event-forwarding code; lets `arc agent serve --ui` work as a one-liner.
- **`reporter.py`** — In-process reporter primitive used by agents to emit dashboard events without coupling to FastAPI.
- **Historical trace loading** — Page load now fetches last 200 traces from `/api/traces` and populates the log table. Traces persist across browser refresh via JSONL trace store.
- **Timeseries API** — `/api/stats/timeseries` endpoint returns per-bucket data for real chart rendering. Token volume chart now shows real aggregated data instead of placeholder bars.
- **Tool call display** — Trace detail panel shows tool calls with formatted name + arguments. "Tools" column added to log table.
- **Single trace export** — Export button in trace detail panel downloads individual trace as JSON.
- **Trace detail panel** — Summary bar (provider, model, duration, cost, tokens), collapsible raw request/response JSON, span timeline visualization.
- **Agent WebSocket transport** — Per-agent WebSocket connections for real-time event streaming from ArcAgent UI reporter module.
- **Agent registry** — Multi-agent lifecycle tracking with registration, heartbeat, and status management.
- **Agent routes** — REST API for agent listing, detail, and status queries.
- **Subscription manager** — Topic-based event subscription with filtering and fan-out.
- **Event buffer** — Bounded in-memory event buffer with overflow policy for bursty agent traffic.
- **Authentication middleware** — Token-based auth for API and WebSocket connections.
- **ArcLLM config routes** — REST endpoints for runtime LLM configuration inspection and mutation.
- **Test suite** — `test_bridge.py`, `test_layer_flows.py`, `test_llm_depth.py`, `test_standalone_launch.py`, `test_ui_tail.py` for the new bridge/reporter/auth code paths.
- **README** — First README in the package; layer position + public surface reference.

### Changed

- **Auth security model** — `/api/*` routes require a valid bearer token; missing or invalid → 401. Agent tokens rejected on non-agent REST paths with 403 (ASI03). `/api/health` exempt for liveness probes. `/api/agent/*` paths handle first-message auth at the WebSocket endpoint via `authenticate_ws()` instead of HTTP-layer auth.
- **Server architecture** — Refactored from single-agent trace viewer to multi-agent observability platform with connection management, audit logging, and typed event system.
- **Transport layer** — Separated WebSocket transport into general (`ws.py`) and agent-specific (`agent_ws.py`) handlers.
- **PyPI packaging** — Added `py.typed` marker, updated dependencies, GitHub Actions publish workflow.

### Security

- **Bearer-token enforcement on every API route** — Federal-first zero-trust posture; defaults closed.
- **Agent-token scope** — Agent tokens cannot reach non-agent REST paths; aligns with ADR-019 four-pillar Authorize at every tier.

### Security

- **API input validation (NIST SI-10)** — All API endpoints now validate inputs at the boundary:
  - Trace ID validated against `[a-f0-9]{32}` hex UUID format.
  - Cursor format validated against `YYYY-MM-DD:line_number` pattern.
  - Filter params (provider, agent, status) validated for safe characters only.
  - Window param allowlisted to `{1h, 24h, 7d}` on stats and cost-efficiency routes.
  - Export format param allowlisted to `{json, csv}`.
- **Audit logging** — All API requests and WebSocket connections logged with structured audit events.

### Fixed

- **WebSocket "Connecting" stuck** — Removed premature `this._state = CONNECTED` assignment in `ws-client.js` that suppressed the `statechange` event dispatch. Connection banner now updates correctly.
- **Pulse transport** — Fixed event type handling and test coverage for pulse heartbeat messages.
