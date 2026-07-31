# PRD: ArcUI Zero-Config Launch

**Spec**: SPEC-019 | **Type**: Integration | **Date**: 2026-04-26

## Problem Statement

Today, displaying a registered agent's live LLM calls plus historical traces in the UI requires:
1. Running a single-workspace launch flag, then pasting the viewer token into the browser auth field.
2. Editing each agent's `arcagent.toml` to add a `[modules.ui_reporter]` block, OR using a per-entrypoint flag (which doesn't exist on `chat` or `run`).
3. There is **no path** to view historical traces from more than one agent in a single UI; `JSONLTraceStore` and `aggregator.warm_start()` are 1:1 with a single workspace.

This forces every operator into per-agent configuration even on a single laptop running multiple agents. Federal deployments inherit the same friction with no security upside.

## Goals

1. **Zero-config launch.** `arc ui start` with no flags discovers every registered agent, aggregates their historical traces, opens the browser, and is immediately authenticated.
2. **Zero-config agent push.** Any agent invoked by `run`, `chat`, or `serve` automatically streams events to a running UI when one is reachable.
3. **One code path for all tiers.** Personal MacBook and federal SCIF workstation execute the same launch flow. Security posture is determined by **bind address**, not deployment tier.
4. **Audit completeness.** Every UI session start, agent autoconnect, and credential delivery is logged. No new "anonymous" code paths.

## Non-Goals

- Cross-machine multi-agent UI (registry assumes single host).
- Mutually-trusted browser sessions across users (single-UID model).
- Replacing token-based auth on non-loopback binds.
- Workspace path redaction in the registry — registry is local file, not transmitted.
- Live edits to agent TOML from the UI.

## User Stories

### US-1: Solo developer launches UI
As a developer with 5 registered agents, I run `arc ui start`. My default browser opens to the dashboard, already logged in. I see all 5 agents with their historical traces backfilled. No tokens, no flags, no paste.

### US-2: Federal operator launches UI on SCIF workstation
As a federal operator on an authorized workstation, I run `arc ui start`. The same thing happens: browser opens, dashboard shows all agents. Audit log captures `ui.session_start` with `auth_method: "browser_bootstrap"`, my UID, the loopback address, and a session ID. Auditor reviewing the log sees a complete trail.

### US-3: Operator exposes UI on the network
As an operator, I run `arc ui start --host 0.0.0.0`. The browser does NOT auto-open. The terminal prints the three tokens prominently with a warning. I copy the viewer token and paste it into a remote browser. Audit log captures `ui.session_start` with `auth_method: "manual_token"`.

### US-4: Agent autoconnects to running UI
As an agent process started by `arc agent run`/`chat`/`serve`, I check `~/.arcagent/ui-token` (must be 0600, owned by my UID), probe `ws://127.0.0.1:8420/api/agent/connect`, and connect if both pass. I emit `ui.agent_autoconnect` audit event on success. If either check fails, I run normally with no UI link, no error.

### US-5: Operator opts an agent out of auto-connect
As an operator who wants `brad_agent` silent, I add to `team/brad_agent/arcagent.toml`:
```toml
[modules.ui_reporter]
enabled = false
```
The agent never connects to UI even when token file and URL are present.

### US-6: Operator backfills existing registry
As an operator with 5 agents already registered (no `workspace_path` field), I run `arc team backfill-workspaces`. The command scans `team/*/arcagent.toml`, matches each to a registry entry by id, and updates each entity record with the absolute workspace path. `--dry-run` is the default; `--apply` writes.

## Requirements

### Functional

| ID | Requirement | Pillar | Decision Ref |
|----|-------------|--------|--------------|
| FR-1 | `arc team register` accepts `--workspace <path>`; defaults to `Path.cwd()` if omitted; resolves to absolute path before persisting | Modularity | D-3 |
| FR-2 | `arcteam.types.Entity` gains `workspace_path: str \| None` field; `None` is the only legacy state and is fixed by `backfill-workspaces` | Modularity | D-3 |
| FR-3 | `arc team backfill-workspaces` subcommand scans `team/*/arcagent.toml`, matches by `agent.name == entity.id`, writes `workspace_path` on match; idempotent; `--dry-run` default | Simplicity | D-7 |
| FR-4 | `arc ui start` queries the registry, builds `list[JSONLTraceStore]` from each entity's `workspace_path` (skip entities with `None` or non-existent paths with a warning), wraps in `FederatedTraceStore`, and warm-starts via `aggregator.warm_start_multi(stores)` | Modularity | D-3, D-4 |
| FR-5 | `RollingAggregator.warm_start_multi(stores: list[TraceStore])` reads each store's traces, merges by timestamp, ingests in chronological order; emits log line per workspace processed | Simplicity | D-4 |
| FR-6 | `arc ui start` with default loopback bind: after server starts, call `webbrowser.open(f"http://127.0.0.1:{port}/#auth={viewer_token}")` | Simplicity | D-6 |
| FR-7 | Browser bootstrap (vanilla JS in `static/index.html`): on load, parse `location.hash` for `auth=<token>`, store in localStorage as `arcui_viewer_token`, strip hash from URL, proceed to dashboard | Simplicity | D-6 |
| FR-8 | `arc ui start --host 0.0.0.0` (or any non-loopback): SKIP `webbrowser.open()`, print tokens prominently to stdout with security warning | Security | D-1 |
| FR-9 | `ui_reporter` module: when `enabled` is `true` (the default), probe `~/.arcagent/ui-token` (exists, 0600, owner=current UID) AND HTTP HEAD to `url` (200/405); connect only if both pass; otherwise run silently | Simplicity | D-5 |
| FR-10 | `ui_reporter` opt-out: `[modules.ui_reporter] enabled = false` in agent TOML disables the probe and the connection entirely | Modularity | D-5 |

### Non-Functional

| ID | Requirement | Pillar |
|----|-------------|--------|
| NFR-1 | Zero-arg `arc ui start` cold-start latency ≤ 2 seconds for 5 agents with up to 10,000 historical trace records each | Scalability |
| NFR-2 | `warm_start_multi` memory bounded to ≤ 50MB per workspace processed (read-stream-ingest, no full file load) | Scalability |
| NFR-3 | Backfill command runs in ≤ 1 second for 100 agents | Simplicity |
| NFR-4 | UI bootstrap: hash-token consumption is synchronous on first paint; no flash of unauth state | Simplicity |
| NFR-5 | `ui_reporter` probe overhead at agent startup ≤ 50ms (file stat + one HTTP HEAD) | Scalability |

### Security

| ID | Requirement | Mandate |
|----|-------------|---------|
| SR-1 | `~/.arcagent/ui-token` MUST be 0600 and owned by current UID; agent refuses to read if either fails | NIST AC-3, file capability |
| SR-2 | Viewer token in URL hash MUST be stripped from `location` after read (history.replaceState) before any external request fires | NIST IA-5 (no token in referrer/log) |
| SR-3 | New audit events `ui.session_start` (fields: session_id, uid, remote_addr, auth_method ∈ {browser_bootstrap, manual_token, agent_token}) and `ui.agent_autoconnect` (fields: agent_id, uid, url, reason) | NIST AU-2, AU-3 |
| SR-4 | When bound non-loopback, server MUST NOT emit auto-open URL to logs (prevents leaking viewer token if log shipped) | NIST AU-9, IA-5 |
| SR-5 | All loopback-served REST and WS endpoints continue to validate the viewer/operator token retrieved from localStorage; loopback does NOT bypass token validation, only token *delivery* | NIST AC-3 |
| SR-6 | Workspace paths in registry are file URIs only; no environment variables or `~` shorthand persisted | NIST CM-6 (no late-binding) |
| SR-7 | `[security] tier = "federal"` in `~/.arc/arcagent.toml` MAY override `ui_reporter` default to `enabled = false`, requiring explicit opt-in per agent | Federal policy choice — **same code path**, different default |

### Tier Behavior — Single Path

| Concern | All tiers | Federal-only override |
|---------|-----------|----------------------|
| Bind address default | `127.0.0.1` | unchanged |
| Token issuance | Always; per-launch random unless flag pinned | unchanged |
| Browser auth delivery | URL hash on loopback / manual paste off-loopback | unchanged |
| Agent autoconnect | Probe-and-enable | May default to `enabled = false` via `~/.arc/arcagent.toml` policy |
| Audit emission | All session and connect events | All events MUST sink to `arctrust.audit.SignedChainSink` (currently optional) |
| mTLS on agent WS | Loopback: no; non-loopback: yes | unchanged |

There is **no `if tier == "federal"` branch** in the launch code. All differences are configuration values read from `~/.arc/arcagent.toml`.

## Acceptance Criteria

1. `arc ui start` (no args) on a machine with 5 registered agents (3 with workspaces, 2 without) opens the browser, displays a populated dashboard with traces from the 3 valid workspaces, logs `ui.session_start` with `auth_method: "browser_bootstrap"`, and surfaces no errors for the 2 missing-workspace agents (warnings only).
2. `arc agent chat team/my_agent` with the UI running auto-connects within 50ms; `ui.agent_autoconnect` event in audit log; agent's first LLM call appears in browser within 1 second.
3. `arc agent chat team/my_agent` with the UI **not** running runs normally with one debug log line ("ui-token absent or url unreachable; ui_reporter disabled"), no error.
4. `arc ui start --host 0.0.0.0` does NOT auto-open browser, prints tokens with security warning, accepts manual paste, logs `ui.session_start` with `auth_method: "manual_token"`.
5. `arc team backfill-workspaces --dry-run` reports the 5 existing agents and proposed `workspace_path` values without writing; `--apply` writes them; second `--apply` is a no-op (idempotent).
6. Setting `[modules.ui_reporter] enabled = false` in `team/brad_agent/arcagent.toml` prevents brad_agent from connecting even when token file and URL are present.
7. `mypy --strict` clean across all five packages; `ruff check` clean; new code branch coverage ≥ 75%.
