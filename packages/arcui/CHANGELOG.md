# Changelog

All notable changes to ArcUI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Historical trace loading** — Page load now fetches last 200 traces from `/api/traces` and populates the log table. Traces persist across browser refresh via JSONL trace store.
- **Timeseries API** — `/api/stats/timeseries` endpoint returns per-bucket data for real chart rendering. Token volume chart now shows real aggregated data instead of placeholder bars.
- **Tool call display** — Trace detail panel shows tool calls with formatted name + arguments. "Tools" column added to log table.
- **Single trace export** — Export button in trace detail panel downloads individual trace as JSON.
- **Trace detail panel** — Summary bar (provider, model, duration, cost, tokens), collapsible raw request/response JSON, span timeline visualization.

### Security

- **API input validation (NIST SI-10)** — All API endpoints now validate inputs at the boundary:
  - Trace ID validated against `[a-f0-9]{32}` hex UUID format.
  - Cursor format validated against `YYYY-MM-DD:line_number` pattern.
  - Filter params (provider, agent, status) validated for safe characters only.
  - Window param allowlisted to `{1h, 24h, 7d}` on stats and cost-efficiency routes.
  - Export format param allowlisted to `{json, csv}`.

### Fixed

- **WebSocket "Connecting" stuck** — Removed premature `this._state = CONNECTED` assignment in `ws-client.js` that suppressed the `statechange` event dispatch. Connection banner now updates correctly.
