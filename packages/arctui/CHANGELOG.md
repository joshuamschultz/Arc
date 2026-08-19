# Changelog

All notable changes to arctui will be documented in this file.

## [Unreleased]

## [0.2.0] - 2026-08-19

SPEC-058: arctui becomes a **viewpoint**, not an agent owner. The gateway owns the
single `ArcAgent`; the TUI attaches to it over `/ws/chat` instead of constructing one
in-process. SPEC-064 adds terminal-owned connector setup.

### Added

- **Attach-or-serve resolver (`serve.py`)** — `ensure_gateway` probes `/api/health`
  and either attaches to a running gateway or spawns a detached
  `arc ui start --no-browser` and attaches once healthy. Returns one `Endpoint`.
  Owns viewer-token mint/persist/read (0600 local token reuse).
- **`GatewayChatClient` (`gateway_client.py`)** — WebSocket `ChatTransport` onto the
  gateway's `/ws/chat/{agent_id}` route (token handshake, block-at-turn agent reply).
- **`ChatTransport` / `TurnEvent` seam (`transport.py`)** — the app renders against
  this Protocol, decoupled from the concrete client so a future in-process or NATS
  transport drops in unchanged.
- **Agent roster (`roster.py`)** — `resolve_agent` / `list_agents` enumerate and
  select agents from the arc roster (`arcgateway.team_roster`), the same discovery
  arcui uses. `--agent`, `--team` (global fleet `~/.arc/<name>`), and
  `--root`/`--team-root` flags.
- **Folder-trust on launch (`trust.py`)** — launching `arc tui` in a project offers
  to grant that directory to the served agent by appending it to `allowed_paths` in
  the agent's `arcagent.toml` (persistent; declined by default on non-interactive
  stdin).
- **`/connect` and `/connections` (SPEC-064 — `connect.py`, `connect_screen.py`)** —
  connector setup collected in masked Textual modals, never routed to the arccli
  registry handler (its `getpass` reads the terminal Textual owns — D-586). Install,
  probe, and rollback live behind `arcagent.connections.Connections`; a credential
  passes through and is never kept.

### Changed

- **`ArcTUI` (`app.py`)** — no longer constructs an `ArcAgent`; drives turns through
  the injected `ChatTransport` and renders the `TurnEvent` stream. Header sub-title
  shows the attach context (agent · gateway · turns).
- **`entry.py`** — resolves the roster agent, offers folder-trust, resolves the
  gateway endpoint, opens a `GatewayChatClient`, and runs the TUI against it.

### Fixed

- **Declared the `websockets>=13` dependency** — `gateway_client` uses
  `websockets.asyncio.client`; it was imported without being declared.

## [0.1.0] - 2026-07-08

SPEC-027: arctui grows from scaffolding into a working single-process Textual TUI client.

### Added

- **`ArcTUI` (`app.py`)** — the main Textual `App`, running in the same process and asyncio
  event loop as `ArcAgent` (no subprocess split, no Node/Ink bridge). Composes
  `TranscriptView` (left, scrollable) + `ActivityView` (right) + `InputComposer` (bottom).
- **`TranscriptView` (`transcript.py`)** — renders the conversation with live token rendering
  (`start_streaming`/`append_delta`/`finish_streaming`) as `arcrun.StreamEvent` token events
  arrive.
- **`ActivityView` (`activity.py`)** — surfaces `ArcAgent` bus events (tool calls, turn
  boundaries) live.
- **`InputComposer` (`input_composer.py`)** — user input widget; non-slash input drives
  `agent.run(...)` as a Textual `@work` task, slash commands dispatch to a command registry
  (`command_completer.py` provides autocomplete).
- **`entry.py`** — `arc tui` / `arc-tui` entry point, registered lazily into the `arccli`
  command registry so a missing optional Textual dependency never breaks unrelated `arc`
  subcommands. Boots in **no-agent mode** (status message, no live output) if no
  `arcagent.toml` is found.
- **Simplification sweep follow-through** — the `4ef1fa0` `arcui` dead-agent-control-path
  cleanup touched `app.py`; no behavior change to the TUI client itself.

## [0.0.2] - 2026-04-26

### Changed

- **README** — Refreshed; clarifies the package is scaffolding only (no public API). Future scope: Textual-based TUI client connecting to a running arcagent or arcui dashboard.
- **Cleanup** — Removed stray `* 2.py` duplicate files left behind by macOS Finder.

### Status

- Scaffolding only — package installs but exposes no public API beyond `__version__`.
