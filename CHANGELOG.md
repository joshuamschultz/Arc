# Changelog

All notable changes to the Arc monorepo will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### SPEC-056 Mission Control — task-lifecycle, dispatch + coordination hardening

The task system grows from a shared list into a self-driving execution engine, and coordination
signals move onto the team bus. See each package `CHANGELOG.md` for detail.

- **arcstore** — additive `Task` lifecycle fields (timing, retry, review, classification) +
  race-safe `TaskStore` transitions (`finish`/`requeue`/`dead_letter`/`request_cancel`/`route`/
  `approve_review`/`reject_review`/`edit`/`delete`) and decomposition-DAG support.
- **arcagent** — opt-in task-dispatch loop, lifecycle reliability engine (retry/backoff, timeout,
  stuck-reclaim, cancel, dead-letter), decomposition + dependency DAG, auto-routing, opt-in review
  gate, operator notifications; user messages captured into memory; background jobs (policy eval,
  daily-notes, distillation) gated to a turn cadence; owner's-own-channel exempt from the
  Lethal-Trifecta gate.
- **arcmemory** — distillation-input curation (drop mechanical tool plumbing, keep substantive
  content); entity dedup by canonical slug; consolidation gated to an interval; oversized distill
  input chunked.
- **arccli** — `arc memory dedup [--apply]` (merge legacy duplicate memory files); multiline input
  in `arc agent chat` (Enter sends, Shift+Enter newline).
- **arcgateway** — cross-surface slash-command framework (`/new`, `/reset`, `/help`) with session
  epoch rotation; Slack slash-command intake.
- **arcui** — Mission Control board UX (status transitions, cancel/approve/reject, delete),
  editable + human-readable agent schedules, New-session button, markdown-rendered replies,
  multiline composer.

Documentation: `packages/arcagent/docs/tasks-module.md` (full task-system reference),
`docs/config-reference.md` (consolidated config knobs).

## [2026-07-12] — SPEC-056 Mission Control multi-agent task system

A shared task system that turns arc from "agents that message" into "agents that coordinate
work": every agent owns a task list in its own harness, tasks can be created for self or
**assigned** to a teammate (single owner, atomic claim — no double-grab), and the human watches
and steers a team-wide kanban in arcui or from the CLI.

Ships as:

- `arcstore` 0.1.0 → 0.2.0
- `arcteam` 0.5.0 → 0.6.0
- `arc-agent` 0.15.0 → 0.16.0
- `arcui` 0.2.0 → 0.3.0
- `arccmd` (arccli) 0.6.0 → 0.7.0

See each package `CHANGELOG.md` for full detail. Highlights:

### Added

- **arcstore** — a new mutable directory plane (`mutable_records`, completing the SPEC-032
  risk) with an atomic `update_if` conditional write — the single-owner claim primitive
  everything else is built on — plus the `Task` model and `TaskStore`.
- **arcagent** — a `tasks` module (`arcagent.modules.tasks`) exposing ten tools
  (`create_task`/`update_task`/`start_task`/`complete_task`/`fail_task`/`assign_task`/
  `claim_task`/`list_tasks`/`decompose_task`/`set_task_output`) over the shared `TaskStore`,
  plus the SPEC-055 mention-scoped inbox activation gate an assignment notify needs to wake
  only the addressed teammate.
- **arcteam** — a dedicated, signed `MsgType.TASK_ASSIGNED` message type for cross-agent task
  hand-off, sent after the durable arcstore write and adopted idempotently by the recipient.
- **arcui** — a team-wide task kanban (six-column board, priority/owner/blocked/run-link
  cards), a task drawer (activity timeline, structured output, operator at-rest edit, "steer
  owner" instead of an edit form for in-progress cards), and operator-gated mutation routes —
  all reading live off the same shared arcstore `tasks` collection the agents write to.
- **arccli** — `arc task create/list/edit/assign/complete/talk`, the same operations as the
  tools and arcui from the command line, over the same shared store, operator-gated and
  audited.
- **ArcUI Reality Mirror** — the dashboard gained a per-agent **Knowledge**
  view (paged/ranked-search memories + entities with created/recency/
  importance/source metadata, link navigation, operator edit/delete via
  a new `arcmemory.operator.MemoryOperator` facade — REQ-087: no SQL
  outside arcmemory), a **workspace file editor** (rendered markdown +
  operator edit, verbatim server errors, a signature-stale warning after
  saving a signed artifact — the UI never signs on an agent's behalf), a
  **Channels** view (live `arcteam` channel list + create/add/remove
  member, wired through a new `arcui/messaging.py` service construction
  that previously left `/api/team/channels` returning `[]` on every
  deployment), and a **Capabilities** view that renders each skill/tool's
  real loader verdict (loaded/denied/unsigned/invalid) instead of a UI
  guess, via a new `arcagent.capabilities.inventory` seam shared with
  agent startup itself so verdicts are posture-faithful at every tier.
  Every UI-originated mutation now audits through one emission point
  (`emit_mutation_audit`), never a partial success. See
  [`docs/deploy/single-node.md`](docs/deploy/single-node.md#dashboard-capabilities-reality-mirror).
- **`claude-sonnet-5`** added to the Anthropic catalog and made the
  default model (1M context, 128K max output, tools/vision/thinking,
  $3/$15 per MTok).

### Fixed

Rough edges surfaced by a live manufacturing customer test (4 agents on real
part/vendor/BOM data), fixed at the repo level so all future builds inherit them:

- **F1 — bare `arc` crashed on non-TTY stdin.** Piped/CI/`arc < file` invocations threw a
  prompt_toolkit `KeyError` from the raw-mode REPL. `arccli` now prints help and exits 0 when
  stdin is not a terminal.
- **F6 — `@tool` was only importable from a private module.** `tool`/`hook`/`background_task`/
  `capability` are now re-exported from `arcagent.tools`; the scaffold, `arc ext create`, and the
  create-tool skill templates use the public path.
- **F7 — no public accessor for stamped tool metadata.** Added `arcagent.tools.capability_meta(fn)`.
- **F8 — `@tool` accepted a sync function silently.** `@tool`/`@hook`/`@background_task` now raise
  `TypeError` at decoration time on a non-`async def`.
- **F9 — an unreachable NATS server dumped a ~30-line traceback** on every solo-agent run before
  degrading. The messaging bootstrap now bounds the connect, quiets nats-py's async error callback,
  and degrades to the in-memory bus with a single clean warning.

Rough edges surfaced by a live single-node + four-agent-fleet deployment (DGX Spark), fixed the
same way — at the repo level, verified live, not patched around:

- **DM pairing was completely inert.** `SessionRouter` never received a `PairingStore` (the
  interceptor was a permanent no-op) and `verify_and_consume` demanded an operator signature no
  command ever produced. Now: the store is built and wired from `[pairing].db_path`, approvals
  persist to SQLite so `arc gateway pair approve` in another process takes effect without IPC,
  `arc identity init` self-registers an operator via `arctrust.trust_store.register_operator()`,
  and a `PairingInterceptor`/adapter type mismatch (silently swallowed by a broad `except`) is
  fixed. `arcgateway-slack` gets the same fix Telegram already had — unauthorized users were
  silently dropped instead of forwarded into the pairing flow.
- **Standalone `arcgateway start` now refuses at every tier**, not just some. Personal/enterprise
  had no working `agent_factory`; federal's `SubprocessExecutor` worker ignored the requested
  `--did` and always loaded a fixed-path config, so it would have served the *wrong* agent
  identity on any multi-agent gateway. The embedded gateway (`arc ui start --team-root`) is the
  only agent-execution path today, at every tier, and standalone fails closed with a message
  naming both failure modes and the correct invocation, instead of echo-stubbing silently.
- **A federal-tier subprocess worker resolved its own config from three fixed paths**, ignoring
  the `--did` it was given — any multi-agent deployment ran as whatever agent happened to sit
  there. The worker now resolves `--did` through a team-root DID index and verifies the loaded
  config's identity before constructing the agent; a mismatch fails closed with an audited
  `worker.did_mismatch` event instead of silently serving the wrong identity.
- **`arcgateway-telegram` wasn't a declared root dependency** — a bare `uv sync` (not
  `--all-packages`) silently skipped the default remote adapter. Pinned alongside `arcmemory`/
  `arcskill` for the same reason. Also fixed: `arc init`'s Telegram config generator wrote
  `bot_token_env` (Slack's field name) instead of `token_env`, and both agent scaffolders omitted
  `[modules.skills]` entirely, so `arcskill`/the improver shipped off by default despite being the
  declared default adapter.
- **Multi-word CLI commands were unreachable** unless shell-quoted as one token — `arc gateway pair
  approve` (and every other space-separated `CommandDef` name) matched only `argv[0]` against the
  registry. New longest-prefix dispatch fixes both the one-shot and REPL paths.
- **`arc agent tools` showed 1 of 15 registered tools** — it globbed only the agent's own
  `capabilities/*.py`, missing builtins, global, workspace, and every enabled module's
  capabilities. `arc ext inspect` had the same gap. Both now share one registry builder that
  mirrors real agent-startup precedence.
- **`arc team create-channel`/`update-entity` didn't exist** — the only way to add a second
  channel or fix a mis-set entity name/role was a hand-written service-API script. Both are now
  first-class CLI commands; `create-channel` refuses a duplicate name instead of silently
  overwriting membership (the underlying service method had no such guard either — fixed at the
  service layer too, so no caller can trigger the clobber, CLI or not).
- **`HTTP 400: temperature is deprecated for this model`** on `claude-sonnet-5` — the model
  rejects any non-default `temperature`. A new per-model `supports_temperature` catalog flag lets
  the Anthropic adapter drop the parameter from the wire body, including explicitly-passed values.
- **UI audit events never reached the log** — `arc ui start` never configured logging, so every
  `ui.mutation`/`ui.session_start` audit event and adapter connect/auth-reject line was silently
  dropped (`uvicorn`'s `log_level` only covers `uvicorn`'s own loggers). Logging is now configured
  first at startup; a new `--verbose` flag raises the root level, matching `arc agent serve`.

### Security

Findings from the same live deployment, closed at the root cause:

- **CRITICAL — cross-agent identity bleed (live-exploited).** `arcagent.builtins.capabilities`
  held per-agent state (workspace, identity **including the private key**, audit sink, tier) as
  plain module globals under a single-agent-per-process assumption the embedded gateway (up to 32
  concurrent cached `ArcAgent`s in one process) had already broken — one agent's `startup()`
  silently rebound the globals for every other agent's in-flight tool calls. On the reference
  deployment, one agent's `create_skill` signed with a *different* agent's private key while
  routing and transcripts stayed correct, making the bleed invisible without a trace-attribution
  audit. Fixed with per-task `ContextVar`s (zero call-site changes) and a deterministic forced-
  interleaving reproducer that fails pre-fix, passes post-fix. The same pattern was then swept
  across all 16 remaining module `_runtime.py` files, with a new AST architecture test that fails
  CI on any `global` statement inside one — the vulnerability *class*, not just the exploited
  instance, is now closed and guarded against recurrence.
- **CRITICAL — the ContextVar fix itself broke every second turn.** `SessionRouter` spawns a
  fresh `asyncio.Task` per inbound turn while agents stay cached; `ContextVar` bindings set in
  turn 1's task were invisible to turn 2's sibling task, so every conversation failed with "runtime
  not configured" starting on message two — a regression unit tests couldn't see (they always run
  `configure()` and the tool call in the same task). Closed with a build/bind split: state is
  built once at startup and re-bound at the top of every real dispatch entry point. Both
  invariants — second-turn success *and* the original cross-agent isolation — are now pinned
  together through the real `SessionRouter` + cached agents, not simulated.
- **Path-taking self-modification tools bypassed the workspace boundary.** The six read/write/
  edit/ls/find/grep tools already enforced workspace confinement via `resolve_workspace_path()`
  (silently — no audit on denial); the four self-mod tools (`create_tool`/`update_tool`/
  `create_skill`/`update_skill`) built paths by direct join, bypassing the choke point entirely.
  Live incident: an agent installed a skill and a secrets file into a **sibling agent's**
  workspace. All ten tools now route through the same audited choke point (denial now emits
  `tool.workspace_path.denied` with the offending path); self-mod tools are additionally confined
  to `<workspace>/capabilities` and validate names against traversal.
- **A pasted API token was written verbatim to a workspace file.** All six content-writing tools
  now run every payload through a secret-shaped-content guard (pattern match + a keyword-anchored
  heuristic for prefixless keys) and deny with an audited `tool.secret_write.denied` event; a new
  `store_secret` tool takes no value parameter by construction and returns tier-appropriate
  operator guidance instead.
- **Personal tier ignored valid self-signatures.** `TofuLayer` denied even artifacts the loader
  had freshly re-verified against the agent's own pinned key unless `auto_run_agent_code` was
  globally enabled — the root cause behind every freshly-scaffolded agent's `calculator.py` being
  dead on arrival. Signed now allows at personal tier; unsigned still denies.
- **`write`/`edit` silently invalidated a signed artifact's signature.** Hand-editing a signed
  capability file broke its `.arcsig` sidecar with no warning, and the next load failed closed.
  A new refresh-only-if-previously-signed hook at the runtime choke point re-signs on write/edit
  without ever initiating signing on an ordinary file. Related: mutation tools previously silently
  no-op'd or crashed when signing failed after a successful write — they now report an explicit
  "UNSIGNED — will be denied at next load" warning and audit `tool.artifact_unsigned` instead of
  letting the caller believe the write fully succeeded.
- **arcmemory's episodic table gained columns with no migration path.** `salience`/`entities`
  were added to a `CREATE TABLE IF NOT EXISTS` — a no-op against every already-existing database,
  so every pre-existing agent's memory pipeline threw `OperationalError` on every capture/recall,
  fleet-wide. A generalized `_ensure_columns` self-migration (`PRAGMA table_info` → `ALTER TABLE
  ADD COLUMN`) runs idempotently at connect, verified with a fixture hand-written from the exact
  pre-migration schema.

### Changed

- **Memory ON by default in scaffolded agents.** `arc agent create` and `arc init` now write
  `[modules.memory].config.brain = "arcmemory"` (matching all three SPEC-047 blueprints), and
  `arcmemory` ships in the full-stack (`arcmas`) and workspace dev installs. A fresh agent now
  has a working Brain out of the box: zero-LLM capture writes daily-log bullets to
  `workspace/memory/daily-log/YYYY-MM-DD.md`, the episodic index to `workspace/memory/index.db`,
  and the entity graph each turn. Consolidation (entity cards + facts + insights) stays opt-in
  via `distill_provider`. The framework *code* default (no `[modules.memory]` config at all)
  remains `none`, so federal absent-config deployments stay memory-off unless a config or
  blueprint opts in. Fixes the prior state where a scaffolded agent had `brain = "none"` and
  `arcmemory` was not installed, so `workspace/memory/` was never created and nothing persisted.
- **Agent scaffold trimmed to used directories.** `arc agent create` no longer materializes the
  unused `workspace/{notes,entities,archive,library/*}` dirs (no runtime code read them); it
  scaffolds only `capabilities/` + `sessions/`. `workspace/memory/` is created lazily by
  arcmemory on first write. The daily-log (`memory/daily-log/`) — not the old empty `notes/` —
  is the per-day context journal.
- **`scripts/arc-stack.sh` repaired against current code.** Dropped the removed `--agent-token`
  flag (which hard-failed `arc ui start`) and the removed `ui_reporter` handshake probe (which
  reported `0/N connected` and exited 1 on every start); agent liveness is now "process survived
  boot," matching the SPEC-026 arcstore read-on-demand model (no agent-side push wire).
- **README + documentation rebrand** — Root and per-package READMEs realigned to the CTG Federal brand system (navy `#002550` → azure `#0073FE` blues with a single orange `#F68D2E` accent, replacing the prior rainbow Tailwind palette). New TUI-framed banner on the root README.
- **Architecture diagram corrected and redrawn** — The dependency graph now reflects the real layered direction (every edge points down toward the `arctrust` / `arcstore` foundation: `arcrun → arcllm`, `arcagent → arcrun`, surfaces → agent, entry → surfaces). Added a branded SVG stack diagram at `docs/assets/arc-architecture.svg`. `arcstore` (operational storage) is now shown as a foundation package alongside `arctrust`; mermaid `classDef` colors switched to the brand palette.

## [2026-07-08] — SPEC-044 skill self-improvement, SPEC-047 extensibility, simplification sweep

Ships as:

- `arc-agent` 0.13.1 → 0.15.0
- `arccmd` (arccli) 0.5.1 → 0.6.0
- `arcskill` 0.1.2 → 0.2.0

See each package `CHANGELOG.md` for full detail. Highlights:

### Added

- **SPEC-044 — `arcskill` becomes the optional skill self-improvement supercharger.**
  The `arcagent/modules/skill_improver/` logic relocated to `arcskill.improver` (no-legacy;
  arcagent source net **down** ~2,400 LOC) and grew into a code-repairing, golden-task-gated,
  bounded, reversible self-modification system: `BundlePatch`/`LLMCodeMutator` code-repair
  mutation, a hard golden-task eval acceptance gate (judge only ranks), per-tier `ChangeBound`
  edit budgets, a nudge → usage → retire `SkillLifecycle`, and an integrity chain that
  re-signs + re-verifies every patched file. arcagent gained the thin `arcagent.skilladapt`
  seam (`SkillAdapter` Protocol + `NullSkillAdapter` + config-select `none`/`arcskill`/signed
  BYO) — arcagent runs skills fine on its own; installing `arcskill` and selecting it
  supercharges them.
- **SPEC-047 — extensibility as a first-class product property.** Generalized the Brain
  (SPEC-041) and SkillAdapter (SPEC-044) select-one seams into one `arcagent.extension`
  (`ExtensionPoint` + `select_extension`) mechanism covering four families (`brain`/`skills`
  select-one, `tools`/`hook-builds` scan-many). Added signed, versioned TOML **blueprints**
  (`arcagent.blueprints`) — three provenance-trusted packaged presets
  (`personal-assistant`/`enterprise-ops`/`federal-analyst`) plus signed `~/.arc/blueprints/`
  user presets — and `arcagent.tiers` (`RelaxableKnob` + `resolve_tier_floor`), the one
  declared config-relaxable tier surface. New `arccli` operator surface: `arc blueprint
  list/show/apply/verify/sign`, `arc ext inspect/verify`, `arc init --blueprint`.
- **Tier vocabulary unified to `personal`/`enterprise`/`federal` everywhere** — `open` is
  removed (no alias) from `arc init`, `arcllm.toml`, `arcagent.toml`, and `gateway.toml`.

### Changed

- **Simplification sweep (−18.7k LOC)** — a cross-package refactor that deleted dead and
  unwired code and wired several previously-inert features onto their real execution paths:
  the legacy `browser`/`scheduler`/`session`/`voice`/`pulse`/`web`/`policy`/`memory_acl`
  `Module` classes and their duplicate tooling layers are gone in favor of the live
  capability-loader path (`web_search`/`web_extract` now actually work in production);
  `arcagent.core.metrics`, `settings_manager.py`, and `protocols.py` dead lifecycle code
  removed; 7 legacy `create_tool` factories and the dead `DynamicToolLoader` sandbox deleted;
  duplicated formulas (exponential backoff, provider-name regexes, canonical-JSON signing)
  deduped into shared helpers (`arctrust` now owns one canonical JSON serializer, adopted
  cross-package with a byte-identity test); `RootTokenBudget` (LLM10) now enforced on the
  real spawn paths; `arcteam` gained `TeamFileStore` path-traversal hardening and dropped the
  unwired `Roster`/presence surface; `arcui` dropped the dead agent-control path and vestigial
  agent auth role (there is no more on-disk UI token file — tokens live only in the running
  process).

## [2026-04-26] — Major monorepo refactor

Cross-cutting refactor that promotes `arctrust` to the canonical leaf for the four pillars (Identity, Sign, Authorize, Audit), splits orchestration cleanly between `arcrun` (loop) and `arcagent` (spawn primitives), removes legacy duplicates, hardens audit emission to a single point, and lifts `arcskill` to a real public release. Implements ADR-019 four-pillar universality.

Ships as:

- `arc-agent` 0.3.0 → 0.4.0
- `arccmd` (arccli) 0.3.2 → 0.4.0
- `arcllm` 0.3.0 → 0.4.0
- `arcrun` 0.4.0 → 0.5.0
- `arcteam` 0.2.0 → 0.3.0
- `arcui` 0.1.0 → 0.2.0
- `arcgateway` 0.1.0 → 0.2.0
- `arctrust` 0.1.0 → 0.2.0
- `arcskill` 0.0.1 → 0.1.0
- `arcmas` 0.2.0 → 0.3.0
- `arcmodel` / `arcprompt` / `arctui` 0.0.1 → 0.0.2 (scaffolding refresh)
- root `arc` 0.1.0 → 0.2.0

See each package CHANGELOG for the per-package detail. Highlights:

### Added

- **arctrust grows into the leaf shared library** — `AgentIdentity`, `ChildIdentity`, `KeyPair`, `AuditEvent`, `JsonlSink`, `SignedChainSink`, `PolicyPipeline`, `build_pipeline`. arcagent / arcrun / arcgateway / arcllm / arcteam / arcskill / arcui all depend on arctrust; arctrust never imports from them. 176 tests, 99% coverage.
- **`arcagent.orchestration` package** — Owns `spawn`, `spawn_many`, `RootTokenBudget`, `SpawnResult`, `SpawnSpec`, `make_spawn_tool`, `SPAWN_GUIDANCE`. Sits between arcrun (pure loop) and `modules/delegate` (LLM-facing tool with policy + identity + audit).
- **arcrun streaming runtime** — `streams.run_stream()` yields `TokenEvent`, `ToolStartEvent`, `ToolEndEvent`, `TurnEndEvent`. Pure arcrun — no LLM-level streaming required.
- **arcskill signed install pipeline** — Public release: fetch → Sigstore + Rekor verify → CRL check → AST/regex/semgrep/bandit scan → sandbox dry-run → atomic activation → lock-file entry. 342 tests, 86% coverage.
- **arcgateway audit module** — Canonical `arctrust`-backed emission for every pairing, runner, adapter, delivery, and execution event.
- **arcui `UIBridgeSink` + `reporter.py`** — Connects an arctrust audit stream from a running agent to the live dashboard. `arc agent serve --ui` now works as a one-liner.
- **arcllm layered config** — Packaged defaults overlaid by user `${ARC_CONFIG_DIR:-~/.arc}/arcllm.toml`; deep-merge dicts, replace lists/scalars.
- **arccli `commands/` package** — Each top-level group in its own module; full smoke-test coverage for every subcommand.
- **`docs/cli.md`** — Top-level CLI reference shipping with the repo.

### Changed

- **All security-relevant audit events route through `arctrust.audit.emit`** — Single canonical schema; sinks fan out (`JsonlSink`, `SignedChainSink`, `UIBridgeSink`). No package constructs raw audit dicts anymore.
- **Identity moved out of arcagent into arctrust** — `core/identity.py` and `core/trust_store.py` removed; arcagent imports from arctrust. Eliminates the SPEC-018 §HIGH-1 latent circular dependency.
- **Tool policy pipeline migrated to arctrust** — `arcagent/core/tool_policy.py` shrunk from 614 LOC to a thin shim around `arctrust.policy`.
- **`ArcAgent.__init__` now requires DID at every tier** — ADR-019 four-pillar universality. Personal/enterprise/federal differ only in stringency.
- **Spawn primitives moved from arcrun to arcagent** — `arcrun.builtins.spawn` removed; lives at `arcagent.orchestration.spawn`. arcrun stays a pure loop.
- **arccli legacy Click removed** — All commands use argparse plain handlers; `main_legacy.py` and the `arc-legacy` console script are gone.
- **All package READMEs rewritten** — ASCII-banner marketing prose replaced with focused layer-position + public-surface references.

### Removed

- **Hundreds of `* 2.py` / `* 2.yaml` macOS Finder duplicate files** — Cleaned up across arcagent (`delegate/`, `memory_acl/`, `user_profile/`, `voice/`, `web/`, `skill_improver/nudge/`, `tool_policy_layers 2.py`, `browser/`), arcskill, arctui, arcgateway, and others. No functional change.
- **arcagent legacy identity / trust_store modules** — Migrated to arctrust.
- **arcrun spawn builtin** — Migrated to `arcagent.orchestration`.
- **arccli legacy Click implementation** — `main_legacy.py`, `arc-legacy` entry point.
- **Stale duplicate docs** — `docs/voice-air-gap-setup 2.md`, `docs/arcgateway/* 2.md` cleanup.

### Security

- **ADR-019 Four Pillars Universal** — Identity, Sign, Authorize, Audit enforced at every tier (personal / enterprise / federal). Tier is stringency metadata, not a gate. `UnsafeNoOp` skill verification bypass eliminated. Pairing signature required at every tier.
- **Audit single-point-of-emission** — All packages route through `arctrust.audit.emit`; schema cannot drift.
- **arcui bearer-token enforcement on every API route** — Federal-first zero-trust posture; `/api/*` requires a valid token (401 on missing/invalid). Agent tokens scoped — rejected on non-agent REST paths (403, ASI03). `/api/health` exempt for liveness probes.
- **arcskill: no tier bypass** — Verification cannot be skipped at any tier. Test enforced (`test_no_tier_bypass.py`).

---

## [2026-04-18] — SPEC-017 Arc Core Hardening

Federal-first hardening pass across the Arc monolith. Production-grade tool policy pipeline, dynamic tool surface with layered defense, unified proactive scheduling engine, Prometheus metrics, and tier-aware self-modification. Ships as:

- `arc-agent` 0.2.0 → 0.3.0
- `arcrun` 0.3.0 → 0.4.0
- `arccmd` 0.2.0 → 0.3.0

See each package CHANGELOG for the detailed per-package breakdown. Highlights:

### Added

- **Tool Policy Pipeline** (arcagent) — 5-layer first-DENY-wins, fail-closed evaluator with LRU cache (p95 < 1ms). Tier-aware composition: Federal=5, Enterprise=4, Personal=1. Shadow mode + restricted mode for air-gapped / stale-bundle situations.
- **Dynamic Tool Surface** (arcagent) — `@tool` decorator, `DynamicToolLoader`, AST validator rejecting 9 CVE-cited bypass categories, scrubbed `RESTRICTED_BUILTINS`, deny-by-default origin-allowlisted egress proxy.
- **Self-modification tools** (arcagent) — `create_skill`, `improve_skill`, `create_tool`, `create_extension`, `list_artifacts`, `reload_artifacts`. Tier gates: federal denies dynamic code (NIST 800-53 SI-7(15), CM-5, CM-8); enterprise approval; personal allowed.
- **Unified Proactive Engine** (arcagent) — Replaces `pulse` + `scheduler` modules. Min-heap timer, drift-free reschedule, heartbeat isolation, per-schedule circuit breaker, timezone + DST handling, `LeaderElection` Protocol.
- **Parallel tool dispatch** (arcrun) — Read-only batches execute concurrently via `asyncio.gather` bounded by semaphore; state-modifying or implicit-dep-colliding batches run sequential. Submission-order results preserved.
- **`task_complete` builtin** (arcrun) — Structured loop-termination signal. Budget caps (`max_turns`, `max_cost_usd`) enforced with automatic `failed` completion on breach.
- **Prometheus metrics** (arcagent) — In-process `MetricRegistry` with counters/gauges/histograms + text exposition + audit-sink adapters. Zero external deps.
- **Capability-composition safety** — `ForbiddenCompositionChecker` rejects batches whose combined capability tags match a forbidden set (e.g. `file_read + network_egress = exfiltration`).
- **CLI mirror** (arccli) — `arc agent policy`, `arc agent completion`, `arc agent schedule` subcommands for scriptable access.
- **Adversarial security suite** — 42 tests covering import bypass, frame traversal, dynamic exec, `sys.modules` access, codec attacks, `__builtins__` mutation, starred unpacking, capability composition.
- **Runbook** — `packages/arcagent/docs/runbooks/spec-017-operations.md`.

### Removed

- **Legacy `modules/pulse/` and `modules/scheduler/` modules** (arcagent) — Functionality migrated to `modules/proactive/`. Per SPEC-017 R-040: no compat shim.

### Fixed

- **ArcLLM `on_event` bridge wiring** — `create_arcllm_bridge()` now actually runs; ArcLLM events (`llm_call`, `config_change`, `circuit_change`) reach the Module Bus.
- **`ArcAgent.shutdown()`** — Closes the httpx client so connection pools release deterministically.
- **Module loader** — Checks `enabled` before validating `entry_point` (lets descriptor-only `MODULE.yaml` files coexist without breaking startup).
- **Messaging `ack` byte_pos** — Stores the real stream end offset in cursor instead of the prior `byte_pos=0` that forced full rescans.

### Security

- **NIST 800-53 AU-2** — Every policy evaluation, self-mod action, schedule tick, and completion event audit-logged with agent DID + rule ID.
- **NIST 800-53 SI-7(15), CM-5, CM-8** — Federal tier refuses dynamic tool/extension creation at the tool level, BEFORE the loader is consulted.
- **OWASP LLM02 / ASI02 / ASI05** — Policy pipeline on every tool call, AST validator (CVE-2023-37271 generator-frame bypass, CVE-2025-68668 ctypes FFI bypass, etc.), deny-by-default egress proxy.

---

## [Pre-2026-04-18] — prior Unreleased

Multi-agent observability platform, vault-backed secrets, strategy prompt provider, messaging integrations, and continued security hardening.

---

### ArcAgent

#### Added
- Vault-backed secret resolution for extension API (`api.get_secret()`).
- Strategy prompt provider integration — ArcRun guidance merges into agent system prompt.
- UI reporter module for real-time agent observability via ArcUI WebSocket.
- Messaging module with unified Slack and Telegram integrations.
- Slack module with bidirectional bot and setup runbook.
- Telegram module with bot integration.
- Skill improver module for autonomous skill evolution.
- Pulse module with per-check circuit breakers for health monitoring.
- Bio memory enhancements — daily notes, entity helpers, facts, deep consolidator. All entity mutations now batch-promoted to team shared knowledge.

#### Fixed
- DID persistence across agent restarts — identity survives stop/start cycles.
- Azure Key Vault backend accepts `cache_ttl_seconds` in constructor.
- Pulse engine prompt reworded to avoid Azure content filter jailbreak detection.
- Slack error handling improvements for 400/content filter/rate limit responses.

#### Security
- DID files written with `0o600` permissions.

---

### ArcLLM

#### Changed
- OpenAI adapter auto-converts `system` → `developer` role for o-series reasoning models.

#### Fixed
- Timeout configuration in dependency specification.

#### Security
- Trace store file permissions hardened to `0o600`/`0o700` (NIST AU-9).
- Hash chain tamper detection on startup — verifies last 10 records (NIST AU-10).
- Provider name input validation prevents module injection (ASI-04, NIST SI-10).

---

### ArcRun

#### Added
- Strategy prompt provider — strategies expose `prompt_guidance` and `get_strategy_prompts()` public API for model-facing guidance.

---

### ArcUI

#### Added
- Historical trace loading on page refresh from JSONL trace store.
- Real timeseries chart data via `/api/stats/timeseries`.
- Tool call display with arguments in trace detail panel.
- Single trace JSON export.
- Multi-agent WebSocket transport, agent registry, and subscription manager.
- Agent routes for listing, detail, and status queries.
- Event buffer with overflow policy for bursty agent traffic.
- Authentication middleware for API and WebSocket connections.
- ArcLLM config routes for runtime inspection and mutation.

#### Changed
- Server architecture refactored from single-agent trace viewer to multi-agent observability platform.

#### Security
- API input validation on all endpoints (trace ID, cursor, filters, window, format).
- Audit logging on all API requests and WebSocket connections.

#### Fixed
- WebSocket connection status stuck on "Connecting".
- Pulse transport event type handling.

---

### ArcCLI

#### Added
- `arc agent ui` command for launching ArcUI dashboard.
- Telegram setup wizard.

#### Changed
- PyPI package renamed from `arccli` to `arccmd` (name collision).

---

### ArcTeam

#### Changed
- File store updates and public API export refinements.

---

### Monorepo

#### Changed
- Version alignment: pyproject.toml, `__version__`, and changelogs now consistent across all packages.
- `__version__` added to arcllm and arcrun `__init__.py`.
- Python minimum version dropped from 3.12 to 3.11 across all packages.

---

## [0.3.0] - 2026-03-01

New LLM provider adapters, model catalog refresh, rate-limit-aware retry, code quality hardening, and PyPI publishing infrastructure.

---

### ArcLLM `0.3.0`

#### Added
- **4 new provider adapters** — Azure OpenAI (commercial + GCC-High), Google Gemini, Cohere, and xAI (Grok). Total adapters: 15.
- **QueueModule** — Bounded concurrency with backpressure (`max_concurrent`, `call_timeout`, `max_queued`). Send-time-only timeouts with OTel instrumentation.
- **CircuitBreakerModule** — Per-provider CLOSED/OPEN/HALF_OPEN state machine with configurable thresholds, cooldown, and event emission.
- **TraceStore** — Append-only, SHA-256 hash-chained LLM call recording with `JSONLTraceStore` (daily rotation, cursor pagination, chain verification). RFC 8785 canonical JSON hashing.
- **ConfigController** — Runtime config get/set with atomic swap, immutable snapshots, change callbacks, and audit trail.
- **Rate-limit-aware retry** — Dedicated `rate_limit_max_retries` (default: 6) for 429 responses with `Retry-After` header support.
- **Provider TOML catalogs** — Azure OpenAI, Google, Cohere, xAI with full model specs and pricing.
- **Queue error types** — `QueueFullError`, `QueueTimeoutError` for structured error handling.

#### Changed
- All provider model catalogs updated to latest models and pricing (Anthropic Claude 4.6, OpenAI GPT-4o/o-series, Mistral, Groq, etc.).
- Anthropic default model updated to `claude-sonnet-4-6`.
- Module stack order updated: Otel → Queue → Telemetry → CircuitBreaker → Audit → Security → Retry → Fallback → RateLimit.
- `load_model()` API expanded with `on_event`, `trace_store`, `agent_label`, `circuit_breaker`, `queue` parameters.
- Comprehensive ruff lint configuration added.

---

### ArcRun `0.3.0`

#### Changed
- Code formatting and lint compliance across all source files.
- Import modernization: `typing.Callable` → `collections.abc.Callable` (PEP 585).
- `asyncio.TimeoutError` → builtin `TimeoutError` (Python 3.11+).
- Comprehensive ruff lint configuration added.

---

### Monorepo

#### Added
- **ArcPrompt package** — Placeholder package with PyPI publish workflow.
- **PyPI publishing infrastructure** — GitHub Actions workflows for all packages.

#### Changed
- Root ruff config expanded with per-file ignores for tests and walkthroughs.
- README updated with expanded project overview.
- Workspace config updated to include arcprompt.

---

## [0.2.0] - 2026-02-21

Security hardening, budget enforcement, tamper-evident audit trails, biological memory, team knowledge management, and CLI initialization across the full stack.

---

### ArcLLM `0.2.0`

#### Added
- **Budget enforcement** — Per-scope spend tracking with calendar period resets (monthly/daily). Pre-flight cost estimation, post-call deduction, and configurable enforcement modes (`block`, `warn`, `log`).
- **Classification-aware routing** — `RoutingModule` routes LLM calls to providers/models based on data classification level. CUI to cleared providers, unclassified to cost-optimized providers.
- **Budget error type** — `ArcLLMBudgetError` with scope, limit type, and dollar amounts for caller-side decision making.
- **Security test suite** — Adversarial tests for budget manipulation (negative cost injection, Unicode homoglyph attacks, concurrent manipulation) and routing bypass attempts.

#### Changed
- Budget config merged into `[modules.telemetry]`. Removed standalone `[modules.budget]` section.
- Telemetry module extended with pre-check/post-deduct budget flow and OpenTelemetry span attributes.

#### Security
- Budget scope validation with NFKC normalization prevents homoglyph attacks.
- Cost clamping to `max(0.0, cost)` prevents negative cost injection.
- Thread-safe accumulator design for PEP 703 free-threading readiness.

---

### ArcRun `0.2.0`

#### Added
- **Tamper-evident event chain** — SHA-256 hash chain on all events with `verify_chain()` API. Meets NIST 800-53 AU-9/AU-10.
- **Immutable events** — `Event` is now `frozen=True` with `MappingProxyType` data. No post-emission tampering.
- **Container sandbox** — Docker-isolated Python execution via `make_contained_execute_tool()`. Memory limits, CPU quotas, network isolation, read-only filesystem.
- **Sandbox error hierarchy** — `SandboxError` base with `TimeoutError`, `OOMError`, `RuntimeError`, `UnavailableError` subtypes.
- **Adversarial test suite** — 36 tests across 8 attack categories (prompt injection, path traversal, steering injection, tool injection, resource exhaustion, spawn depth bomb, event tampering, timing attacks).
- **Security documentation** — Threat model, NIST 800-53 mapping, and adversarial test catalog.

#### Changed
- Events now carry `sequence`, `prev_hash`, and `event_hash` fields for chain integrity.
- `EventBus` maintains thread-safe hash chain state.

---

### ArcAgent `0.2.0`

#### Added
- **Biological memory module** — Long-term identity-aware memory with identity manager, working memory, consolidator, and retriever. Tracks agent personality, episodic experiences, and session-scoped reasoning.
- **Shared text sanitizer** — Centralized ASI-06 defense with NFKC normalization, zero-width character stripping, and control character removal.
- **Bio memory CLI** — `arc agent bio_memory status|identity|episodes|working`.
- **Integration and unit tests** — Full test coverage for biological memory lifecycle and retrieval accuracy.

#### Changed
- Entity extractor refactored to use shared sanitizer (DRY).

---

### ArcCLI `0.2.0`

#### Added
- **`arc init`** — Unified initialization wizard with tier-based presets (`open`, `enterprise`, `federal`). Generates configs, validates API keys.
- **`arc llm init`** — ArcLLM-specific setup with provider config generation.
- **`arc team init`** — Team data directory setup with HMAC key generation.
- **`arc team status`** — Team overview (entities, channels, messages, audit entries).
- **`arc team config`** — Team configuration display.
- **`arc team memory`** — Full team memory management (status, entities, entity, search, rebuild-index, config).
- **Bio memory CLI** — `arc agent bio_memory` module group.

---

### ArcTeam `0.2.0`

#### Added
- **Team memory subsystem** — Institutional knowledge management with:
  - Entity storage with markdown frontmatter
  - BM25 search with wiki-link graph traversal
  - Index manager with dirty-state tracking
  - Promotion gate for agent-to-team knowledge transfer
  - Data classification types (CUI/FOUO/Unclassified)
  - Standalone `arc-memory` CLI

#### Changed
- Added `python-frontmatter` and `rank-bm25` dependencies.
- Added `arc-memory` entry point.

---

## [0.1.0] - 2026-02-01

Initial release of the Arc monorepo.

### ArcLLM `0.1.0`
- 11 LLM provider adapters with direct HTTP (no SDKs).
- Opt-in module system: retry, fallback, rate limiting, telemetry, audit, security, OpenTelemetry.
- PII redaction, HMAC request signing, vault integration.
- Config-driven provider management via TOML.

### ArcRun `0.1.0`
- Core execution loop with ReAct and CodeExec strategies.
- Deny-by-default sandbox with tool allowlists.
- Full event audit trail on every action.
- Dynamic tool registry, steering, context transforms.

### ArcAgent `0.1.0`
- Ed25519 cryptographic identity with W3C DID format.
- Token-budgeted context manager with tiered compaction.
- Tool registry with 4-transport architecture.
- Event-driven module bus, session persistence, skill discovery.
- Memory module with hybrid search and entity extraction.

### ArcCLI `0.1.0`
- Unified `arc` CLI for LLM, agent, run, team, extension, and skill management.
- Global `--json` output support.

### ArcTeam `0.1.0`
- Four collaboration primitives: messaging, tasks, knowledge base, file store.
- Universal addressing via typed URIs.
- Append-only audit trail (NIST 800-53 compliant).
- `StorageBackend` protocol with file and memory backends.
