# Changelog

All notable changes to arccmd (the `arc` CLI) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-04-18

SPEC-017 CLI mirror + REPL state mutations.

### Added

- **`arc agent policy layers`** — List policy pipeline layers active for the chosen tier. JSON output.
- **`arc agent policy evaluate`** — Dry-run evaluate a tool call and print the `Decision` payload. Useful for validating rule changes before rolling out.
- **`arc agent completion history`** — Read `loop.completed` events from the workspace audit log. JSON output for CI pipelines.
- **`arc agent schedule list`** — Print persisted schedule state from the proactive engine (replaces the legacy scheduler CLI).
- **`arc agent ui` command** — Launch ArcUI dashboard for real-time agent observability.
- **Telegram setup wizard** — `telegram_setup.py` for guided Telegram bot configuration.

### Changed

- **REPL `/sandbox` and `/strategy`** — Now mutate REPL-local state (`active_sandbox`, `active_strategy`) and emit `repl.sandbox_changed` / `repl.strategy_changed` audit events. Previously printed help only.
- **Agent CLI** — Simplified agent command module; moved UI reporter logic to `arcagent.modules.ui_reporter`.
- **PyPI package name** — Renamed from `arccli` to `arccmd` on PyPI to avoid collision with existing `arc-cli` package.
- **Python version** — Minimum dropped from 3.12 to 3.11.
- **PyPI packaging** — Added `py.typed` marker, GitHub Actions publish workflow.

## [0.2.0] - 2026-02-21

### Added

- **`arc init` command** — Unified initialization wizard with tier-based presets (`open`, `enterprise`, `federal`). Generates `config.toml` with appropriate module defaults per deployment tier. Validates API key availability.
- **`arc llm init` command** — ArcLLM-specific setup with tier presets. Generates provider config, validates API keys, and reports status.
- **`arc team init` command** — Initialize team data directory with entity, channel, and cursor subdirectories. Generates HMAC key for message signing.
- **`arc team status` command** — Team overview showing entity count, channels, messages, audit entries, and HMAC key presence.
- **`arc team config` command** — Display team configuration with JSON output support.
- **`arc team memory` subgroup** — Full team memory management:
  - `arc team memory status` — Entity count, index health, memory config.
  - `arc team memory entities` — List entities with optional type filter.
  - `arc team memory entity ID` — Show single entity details.
  - `arc team memory search QUERY` — BM25 search with wiki-link traversal.
  - `arc team memory rebuild-index` — Force full index rebuild.
  - `arc team memory config` — Show team memory configuration.
- **Bio memory CLI integration** — Registered `bio_memory` module group with `status`, `identity`, `episodes`, `working` subcommands under `arc agent`.
- **Init wizard module** — `init_wizard.py` with tier presets, provider key validation, and TOML config generation.

### Changed

- **Formatting** — Code reformatted with consistent style (dict literals, import ordering).
- **Team module docstring** — Updated to reflect memory subsystem integration.

## [0.1.0] - 2026-02-01

### Added

- Initial release with unified CLI.
- `arc llm` — Provider management, model discovery, direct LLM calls, config inspection, validation.
- `arc agent` — Agent lifecycle (create, build, chat, tools, config, strategies, events).
- `arc run` — Direct arcrun execution (task, exec, version).
- `arc team` — Team messaging (register, send, inbox, drain, channels, threads, actions, cursors).
- `arc ext` — Extension management.
- `arc skill` — Skill listing.
- Global `--json` output support across all commands.
