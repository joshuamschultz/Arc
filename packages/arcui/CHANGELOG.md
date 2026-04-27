# Changelog

All notable changes to ArcUI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
