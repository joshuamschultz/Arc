# Changelog

All notable changes to arctui will be documented in this file.

## [Unreleased]

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
