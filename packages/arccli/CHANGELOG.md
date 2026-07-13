# Changelog

All notable changes to arccmd (the `arc` CLI) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`arc memory dedup [--apply] <workspace…>`** — operator maintenance that merges memory cards
  whose slugs diverged before slug canonicalization landed (e.g. `Custom ERP.md` / `custom_erp.md`
  / `custom-erp.md` for one real thing). Groups variant files by their canonical slug (via
  `arcmemory.canonical_slug`, the same function the store uses), merges each group into the single
  canonical file (facts/cues/instances unioned, richest metadata wins, confidence/use-count
  combined), and deletes the variants. Dry-run by default; `--apply` writes. A path that is itself
  a workspace is cleaned directly; otherwise it is treated as a root and searched for nested
  workspaces, so a whole fleet dir cleans in one call. Restart each agent after applying so its
  index rebuilds and stale rows drop.
- **Multiline input in `arc agent chat`** — in a TTY, Enter submits and Shift+Enter / Alt+Enter
  insert a newline (prompt_toolkit multiline session), so multi-paragraph prompts compose without
  premature submission. Non-interactive stdin (pipes) still reads a line at a time.

## [0.7.0] - 2026-07-12

SPEC-056 Mission Control, Phase F: `arc task` — the same operations the tools and arcui expose,
from the command line.

### Added

- **`arc task create/list/edit/assign/complete/talk`** — CLI surface over the same shared
  arcstore `TaskStore` seam agents and arcui use (`resolve_data_dir()/store/arcui.db`, not
  arccli's separate `arcstore.db`), so a CLI-created task is immediately visible to an agent's
  own `open_store` and to the arcui kanban. Human writes are operator-gated (the invoking
  `--actor` entity must carry the `operator` role) and audited through a WORM sink. `edit`
  refuses an `in_progress` task (409-parity with the arcui route — "steer it with `arc task
  talk`, not edit"). `assign` goes through `TaskStore`'s atomic single-owner guard. `talk`
  resolves the task's owner and steers it via a signed team message — never a task edit, matching
  the same edit-at-rest / steer-in-flight rule as the tools and arcui.
- **`arc team create-channel <name>`** — standalone or team-scoped (`--members` explicit, or
  defaults to the team's members when `--team` is given and `--members` is omitted). Refuses a
  duplicate channel name via a pre-check rather than silently overwriting membership (the
  underlying `MessagingService.create_channel` has no such guard itself — fixed at the service
  layer too, task #22).
- **`arc team update-entity <ref> [--name] [--roles]`** — fixes a mis-set name/role on an
  already-registered entity without going through `register`'s strict create-only path (which
  correctly refuses duplicates — existing callers, including `arc agent create`'s
  auto-registration, rely on that guard). Omitted fields are left untouched; DID/handle never
  change.
- **`--verbose` on `arc ui start`** — raises the root log level, matching `arc agent serve`'s
  convention.

### Fixed

- **Bare `arc` no longer crashes on non-TTY stdin (F1)** — piped/CI/`arc < file` invocations print
  help and exit 0 instead of throwing a prompt_toolkit `KeyError` from the raw-mode REPL.
- **`arc ui start` never configured logging.** Every `ui.mutation`/`ui.session_start` audit event
  and every adapter connect/auth-reject line was silently dropped — `uvicorn`'s `log_level` only
  covers `uvicorn`'s own loggers. Logging is now configured first at startup: root stays at
  `WARNING` (quiet), `arcui.audit` + `arcgateway.adapters` + the three top-level adapter packages
  are raised to `INFO`.
- **Multi-word commands were unreachable except shell-quoted as one token.** Both dispatch sites
  (one-shot and REPL) matched only `argv[0]` against the registry, so `arc gateway pair approve`
  and every other space-separated `CommandDef` name effectively didn't work as documented. New
  `resolve_command_and_args` does longest-prefix matching over the static registry (ambiguity
  impossible by construction).
- **`arc agent tools` showed 1 of 15 registered tools.** It globbed only the agent's own
  `capabilities/*.py`, missing builtins/builtins-skills/global/workspace and every enabled
  module's `capabilities.py`. `arc ext inspect` had the same gap. Both now share one
  `build_capability_registry` that mirrors real agent-startup precedence and gains a source
  column. Deliberately does **not** boot a real `ArcAgent` for this (startup connects modules to
  live services — the wrong side effect for a listing command); `arc agent build --check`/
  `status` keep their intentionally narrower self-authored view.
- **The federal-tier subprocess worker resolved the wrong agent's identity.** It accepted `--did`
  but ignored it, loading config from three fixed paths instead — any multi-agent deployment ran
  as whatever agent happened to be there. The worker now resolves `--did` through a team-root DID
  index (new `--team-root` flag) and verifies the loaded config's identity against the requested
  DID before constructing the agent; a mismatch fails closed with an audited
  `worker.did_mismatch` event instead of silently serving the wrong identity.
- **`arcgateway-telegram` wasn't a declared root dependency** — a bare `uv sync` silently skipped
  it. Pinned in root `pyproject.toml` alongside `arcmemory`/`arcskill`. Also fixed: `arc init`'s
  Telegram config generator wrote `bot_token_env` (Slack's field name) instead of `token_env`, and
  both `arc init` and `arc agent create` omitted `[modules.skills]` entirely, so the declared
  default skills adapter (`arcskill`) shipped off on every freshly scaffolded agent.

### Changed

- **Scaffold/`arc ext create` templates use the public tool import** — `from arcagent.tools import
  tool` instead of the private `arcagent.tools._decorator` (F6).
- **`arc agent create` / `arc init` default memory ON** — both now scaffold
  `[modules.memory].config.brain = "arcmemory"` (matching the SPEC-047 blueprints), so a fresh
  agent captures daily-log bullets (`workspace/memory/daily-log/YYYY-MM-DD.md`), the episodic
  index, and the entity graph out of the box. Set `brain = "none"` for a memory-less agent.
- **`arc agent create` scaffold trimmed** — only the directories the runtime reads
  (`capabilities/`, `sessions/`) are created; the unused `workspace/{notes,entities,archive,library/*}`
  dirs are gone. `workspace/memory/` is created lazily by arcmemory on first write.

## [0.6.0] - 2026-07-08

SPEC-047 — the operator surface for extensibility: preset-config blueprints, extension-point
inspection, and a tier-vocabulary cleanup.

### Added
- `arc blueprint` — `list` / `show` / `apply [--agent] [--dry-run]` / `verify` / `sign`.
  `apply` verifies (fail-closed above personal), deep-merges the preset UNDER the target's
  existing config (preserving identity + user keys — NOT a clobber-write), floors the tier by
  stringency-max, materializes the concrete `arcagent.toml`, and audits the apply (operator
  WORM sink at enterprise/federal, else structured log).
- `arc ext inspect` / `arc ext verify` — the four-family extension-point view (selected /
  available / signed) over the live config + `CapabilityRegistry`. `verify` exits non-zero on a
  refusal (federal change-control gate). Folded into the existing `arc ext` (no colliding
  top-level command).
- `arc init --blueprint <name>` — bootstrap from a preset, deep-merged UNDER the init defaults.

### Changed
- **Tier vocabulary unified to `personal` everywhere.** `arc init` no longer accepts `open`
  (removed, no alias); all three generated files (`arcllm.toml`, `arcagent.toml`, `gateway.toml`)
  now use the security vocab `personal` / `enterprise` / `federal`, fixing the arcllm.toml leak.
  The generated `arcagent.toml` is now assembled as a dict and serialized, enabling the
  `--blueprint` deep-merge.
- `arccli.__version__` now derives from the installed distribution metadata (fallback literal).

### Security
- **Blueprint apply/verify pins to the deployment operator key (adversarial-review HIGH-1).**
  `apply_to_disk`, `arc blueprint show/verify/list`, and `arc init --blueprint` resolve the
  operator public key read-only via the new `operator_public_key(arc_dir)` and pass it to
  `resolve_blueprint`/`list_blueprints`, so above personal a user preset must be signed by the
  operator key (`arc blueprint sign`), not merely self-consistent. An unresolvable operator key
  denies fail-closed.
- **`arc blueprint apply --dry-run` no longer writes a false WORM audit record (MED-2).**
  `audit_apply` moved inside the `if not dry_run` write guard — a dry run now writes no config
  and emits no `blueprint.applied` record.
- **`arc ext inspect`/`verify` pins capability signed-status to the agent DID key (HIGH-1)**, so a
  wrong-key self-signed capability is labeled unsigned rather than falsely signed.

## [0.5.1] - 2026-07-06

SPEC-037: WORM/audit signing goes through the arctrust `Signer` seam.

### Changed
- `arc run` direct-audit WORM sink and the `arc team` `AuditLogger` build a `Signer` from the operator key (`load_operator_key().into_signer()`) instead of a raw seed / shared HMAC key.
- `arc team init` now bootstraps the operator audit key (asymmetric authority) instead of generating a `.hmac_key`; `arc team status` reports operator-key presence.

## [0.5.0] - 2026-07-06

SPEC-053: `arc init` generates the deployment operator key (audit authority).

### Added
- **`arc init` operator key** — generates a fresh Ed25519 operator keypair (if none exists) at `<config-dir>/operator/operator.key` (private key `0600`, dir `0700`), idempotent on re-run. Personal tier is silent/zero-config; enterprise/federal print the operator public-key fingerprint for out-of-band recording. All crypto is delegated to `arctrust.OperatorKey` (`arccli.commands.operator`).

### Changed
- The `arc run` direct-run WORM audit sink is now signed by the operator key, not the caller's DID seed (audited subject ≠ audit authority).
- Version synced across `pyproject.toml` and `arccli.__init__` (was drifted at 0.4.1 / 0.4.0).

## [0.4.1] - 2026-07-05

### Added

- **`[execution] relax_isolation` agent config** — Agent `config.toml` now supports a `personal`-tier-only `[execution]` block to relax the `execute_python` isolation floor down to a bare host subprocess (`"off"`/`"none"`/`"local"`) or leave it at the container default. Rejected at `enterprise`/`federal`. `arc` reads the agent's `[security] tier` and this config and forwards both to arcrun on every `execute_python` call (SPEC-036).

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
