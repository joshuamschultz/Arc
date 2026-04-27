# Changelog

All notable changes to arccmd (the `arc` CLI) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-04-26

Major refactor: legacy Click groups removed, command tree reorganized into a single `commands/` package, and full smoke-test coverage of every subcommand.

### Added

- **`commands/` package** — Every top-level group lives in its own module: `agent.py`, `ext.py`, `init.py`, `llm.py`, `run.py`, `spec017.py`, `team.py`, `ui.py`. Replaces the prior flat-file layout (`agent.py`, `ext.py`, `llm.py`, `run.py`, `team.py`, `ui.py`, `init_wizard.py`) at the package root.
- **Smoke tests for every subcommand** — `test_cli_agent_smoke.py`, `test_cli_ext_smoke.py`, `test_cli_init_smoke.py`, `test_cli_llm_smoke.py`, `test_cli_run_smoke.py`, `test_cli_skill_smoke.py`, `test_cli_team_smoke.py`, `test_cli_ui_smoke.py`, plus a deeper `test_agent_run_serve_chat.py` for the agent run/serve/chat flow. Catches argparse regressions and command-tree drift.
- **`docs/cli.md`** — Top-level CLI reference shipping with the repo (replaces stale per-package CLI docs).

### Changed

- **All commands now use argparse plain handlers** — No more Click. Every entry point migrated to argparse subparsers with explicit type hints. Backward-compatible `cli` Click re-export removed.
- **README rewritten** — Replaced ASCII-banner marketing prose with a focused layer-position + command-tree reference.

### Removed

- **Legacy Click implementation** — `main_legacy.py`, the Click-based `cli` group, and the `arc-legacy` console script entry point. The migration window opened during SPEC-017 T1.1.5 closed; legacy callers must update to argparse-shaped handlers.
- **Top-level `agent.py`, `ext.py`, `init_wizard.py`, `llm.py`, `run.py`, `team.py`, `ui.py`** — Migrated under `commands/`. Imports must update accordingly.

## [0.3.2] - prior release line

(Patch releases on the 0.3.x line; superseded by 0.4.0 reorganization.)

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
