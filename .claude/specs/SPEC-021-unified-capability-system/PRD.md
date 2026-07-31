# PRD: Unified Capability System

**Spec**: SPEC-021 | **Type**: integration | **Status**: DRAFT

## Problem

`arcagent` today has **four parallel registration paths** for tools, skills, extensions, and modules:

1. Hardcoded built-in tool list (`arcagent/tools/__init__.py:create_builtin_tools` — 7 tools)
2. TOML-config tools (`[tools.native]` block → `register_native_tools()`)
3. File-discovered extensions (`extensions/*.py` with `extension(api)` factory)
4. Convention modules (`arcagent/modules/<name>/MODULE.yaml` + entry-point class)

Plus skills are managed by a separate `SkillRegistry` with a different discovery surface, and a `make_create_tool_tool` exists for self-modification but is not wired into agent startup.

This produces:

- Branchy startup logic (`agent.py:376-444` has four parallel registration code paths).
- No clear convention taught to the agent — even with `write` and `bash`, the LLM doesn't know `workspace/tools/<name>.py` is the correct destination, doesn't know the `@tool` decorator API, doesn't know about the AST validator's blocked imports.
- Hot reload is incomplete (`agent.reload()` clears extensions and skills but lacks a clean diff/audit story).
- Skills are loosely structured — no required sections, no required frontmatter validation, no quality gate on agent-authored skills.
- No tier-aware policy for self-executing agent code (validators in skill folders, agent-authored Python).

## Goal

Replace all four registration paths with **one unified `CapabilityLoader`** that:

- Discovers two file types only — `.py` with decorator (`@tool` / `@hook` / `@background_task` / `@capability` class) and `.md` skill folders with structured frontmatter.
- Walks four scan roots in precedence order (builtins → global → agent → workspace).
- Emits structured bus events on every lifecycle change.
- Enforces a unified trust model: AST validator + tier-specific TOFU + Sigstore signing for federal.
- Exposes a single `reload()` self-mod surface for the agent.
- Teaches the agent the convention via builtin skills (`create-tool`, `create-skill`, `update-tool`, `update-skill`).
- Supports lifecycle (setup/teardown) for heavy-resource modules (browser, scheduler, memory) without special-casing.

## Non-Goals

- MCP integration. Loader is designed extensible (transport=MCP slot exists), but actual MCP client/server registration is out of scope. Future spec.
- Marketplace registry / Git-URL install sources. `arc module install` v1 supports local files only.
- File watcher / auto-reload. `reload()` is the only self-mod trigger (D-362).
- Backward compatibility shims for old `extension(api)` factory or `MODULE.yaml` runtime parsing. Per CLAUDE.md and D-360, old code is deleted in the same edit.

## Users

- **Agents** — the LLM-driven entity using built-in `reload()` to extend its own capabilities at runtime.
- **Module authors** — Arc team and third parties writing decorator-form Python in folder bundles for `arc module install`.
- **End users (operators)** — humans running `arc agent build`, `arc module enable`, `arc trust approve` to configure agent capabilities.
- **Federal compliance auditors** — verifying SI-7 / SI-7(15) / EO 14028 SBOM coverage on runtime-loaded plugins.

## Functional Requirements

Each requirement is traceable to one or more decisions in `.claude/decisions-log.md` (D-338..D-368) and at least one priority pillar.

### Discovery & Registration

**R-001** — The `CapabilityLoader` SHALL scan four roots in this precedence order on every `reload()`: `arcagent/builtins/capabilities/`, `~/.arc/capabilities/`, `<agent_root>/capabilities/`, `<agent_root>/workspace/.capabilities/`.
- *Trace*: D-340, D-341, D-349 | *Pillar*: simplicity, security
- *Acceptance*: Given capabilities at multiple roots, when `reload()` runs, then later-scanned roots replace earlier-scanned by name and the audit event includes both `kept` and `shadowed` source paths.

**R-002** — The `CapabilityLoader` SHALL recognize exactly two file types: `.py` with one of four decorators (`@tool`, `@hook`, `@background_task`, or `@capability` class) and `.md` skill folders containing `SKILL.md` with required frontmatter.
- *Trace*: D-349, D-356 | *Pillar*: simplicity
- *Acceptance*: Files without supported decorators or malformed frontmatter are skipped with `capability:registration_failed` audit; the agent continues startup.

**R-003** — Decorator metadata SHALL be stamped on the function (`func._arc_capability_meta`) AND registered in a side-effect registry. Rebuilding the registry from import-time stamps SHALL produce identical results to runtime registration.
- *Trace*: D-356 | *Pillar*: simplicity, modularity
- *Acceptance*: Property test — for any `.py` file passing AST validation, `register_from_module(mod) == register_from_stamps(mod)`.

**R-004** — Conflict resolution SHALL branch by capability kind: tools/skills last-wins with audit; hooks fan-out (all subscribers run, ordered by `priority` with `tryfirst`/`trylast` overrides); background tasks last-wins via drain-then-replace (`old.cancel() → await old → start new`).
- *Trace*: D-341 (refined) | *Pillar*: modularity
- *Acceptance*: Two `@tool(name="foo")` registrations → only later is callable, audit emitted. Two `@hook(event="agent:ready")` registrations → both run on event emission. Two `@background_task(name="bar")` registrations → old task cancelled and awaited before new task starts.

**R-005** — `reload()` SHALL return a single human-readable diff string. Format: one line nominal (`reload: +N added (...), ~M replaced (... v→v), -K removed (...), 0 errors`); multi-line only when errors exist.
- *Trace*: D-358 | *Pillar*: simplicity
- *Acceptance*: Snapshot test — calling `reload()` after each kind of change (add/remove/replace/error) produces the expected string.

### Skill Format

**R-010** — Every skill SHALL be a folder under one of the scan roots, containing `SKILL.md` plus optional `references/`, `scripts/`, `templates/`, `assets/` subfolders.
- *Trace*: D-342 | *Pillar*: simplicity, modularity

**R-011** — `SKILL.md` frontmatter SHALL include required fields (`name`, `description`, `triggers`, `tools`, `version`) and optional `model_hint`. Validator SHALL reject skills missing any required field.
- *Trace*: D-342, D-343, D-345 | *Pillar*: security, modularity
- *Acceptance*: Skill with empty `description` → `capability:registration_failed`; skill with all required fields → registers.

**R-012** — `SKILL.md` SHALL contain seven title-case sections (`## Resources`, `## Contract`, `## Knowledge`, `## Steps`, `## Anti Patterns`, `## Examples`, `## Validation`). Validator SHALL accept short sections, reject missing sections, flag filler ("N/A", "none", empty body).
- *Trace*: D-344, D-364 | *Pillar*: simplicity (consistent shape)
- *Acceptance*: Skill missing `## Steps` → `capability:registration_failed` with reason; skill with `## Anti Patterns: N/A` → flagged warning, registers with audit.

**R-013** — The `## Resources` section SHALL be auto-generated by the loader from folder contents on every reload; author-edited content in this section SHALL be replaced.
- *Trace*: D-342 | *Pillar*: simplicity
- *Acceptance*: Loader writes/replaces `## Resources` block; subsequent author edits to that section are overwritten on next `reload()`.

**R-014** — A skill's `tools: [...]` field SHALL declare which tools the skill expects. At register time, validator SHALL check listed tools exist. Federal blocks registration if any missing; enterprise warns; personal info-only.
- *Trace*: D-348 | *Pillar*: security
- *Acceptance*: Skill with `tools: [bash]` and `bash` denied by policy → registers anyway, `skill.tool_dependency_policy_denied` audit emitted, mismatch surfaces only at LLM call-time.

### System Prompt Integration

**R-020** — The `CapabilityRegistry` SHALL inject a single XML manifest into the system prompt at session start via `agent:assemble_prompt` bus event. The manifest SHALL list all tools (with `description`, `when_to_use`, `requires_skill?`, `version`) and all skills (with `description`, `triggers`, `tools`, `version`, `location`).
- *Trace*: D-346 | *Pillar*: simplicity, modularity (extends D-073/D-074 pattern)

**R-021** — A short (~3-line) skill-usage instruction SHALL be injected via the bus at priority 91, alongside the manifest. The instruction SHALL teach: "scan available skills, pick the most specific, read its SKILL.md, never read more than one up front."
- *Trace*: D-347, D-365 | *Pillar*: simplicity (LLM-side instruction)
- *Acceptance*: System prompt at session start contains both the manifest XML and the instruction.

**R-022** — System prompt SHALL be reused across all turns within a single `arcrun.run()` call. `reload()` mid-session SHALL invalidate the registry's prompt cache; the next `run()` SHALL receive the rebuilt prompt.
- *Trace*: D-346, D-358 | *Pillar*: scalability (no per-turn rebuild)

**R-023** — Denied capabilities (failed policy check) SHALL NOT appear in the manifest. The LLM SHALL only see what it can use.
- *Trace*: D-363 | *Pillar*: security, simplicity

### Self-Modification

**R-030** — The agent SHALL have one built-in tool `reload()` that rescans all four roots, registers new/changed capabilities, and returns the diff per R-005. No other self-mod tool SHALL exist.
- *Trace*: D-345, D-362 | *Pillar*: simplicity

**R-031** — Persistence path: agent writes to `<agent_root>/workspace/.capabilities/` (only path agent has write access to per D-340), then calls `reload()`. No separate "create_tool" API that bypasses this.
- *Trace*: D-340, D-345 | *Pillar*: simplicity, security

**R-032** — Built-in skills `create-tool`, `create-skill`, `update-tool`, `update-skill` SHALL ship under `arcagent/builtins/capabilities/skills/` and teach the agent the convention. Each SHALL include `scripts/validate.py` for self-checking.
- *Trace*: D-345 | *Pillar*: modularity (convention is taught, not hardcoded)

**R-033** — `update_tool` / `update_skill` SHALL bump the `version` frontmatter field (semver). The skill body SHALL teach the LLM to choose major/minor/patch.
- *Trace*: D-345, D-357 | *Pillar*: simplicity

### Security & Trust

**R-040** — AST validator SHALL block (in addition to current set): `gi_code`, `gi_yieldfrom`, `tb_frame`; format-string `__format__` invocation patterns; `__init_subclass__` definitions; metaclass `__getitem__` patterns; descriptor protocol side-channels (`__pos__`, `__neg__`, `__get__`, `__set__`); `AttributeError.obj` / `.name` access (Python 3.10+).
- *Trace*: D-353 (enhanced) | *Pillar*: security
- *Acceptance*: For each new bypass category, an exploit POC test confirms validator rejects it.

**R-041** — `pip install arcagent[enterprise]` SHALL provide an OS-level sandbox layer (`arcagent.core.os_sandbox`) using `seccomp` on Linux and `sandbox-exec` on macOS. Self-executing agent-authored code at enterprise tier SHALL run within this sandbox after AST validation passes.
- *Trace*: D-353 (enhanced), D-359 | *Pillar*: security
- *Acceptance*: Enterprise tier with `[security] auto_run_agent_code = true` runs validator scripts inside seccomp/sandbox-exec; calls to `ctypes` raise sandbox violation, not silent escape.

**R-042** — Tier-specific TOFU policy:
- **Personal**: agent-authored Python auto-runs by default; user toggles via `[security] auto_run_agent_code = bool` in `arcagent.toml`.
- **Enterprise**: default-allow trusted (builtins + signed modules); default-deny new agent-authored code; first sight prompts user approval; approval persisted to `[security.validators] approved` list with hash + timestamp + approver.
- **Federal**: default-deny all unsigned; only Sigstore-verified bundles execute; agent-authored .py never executes.
- *Trace*: D-359 (pinned) | *Pillar*: security
- *Acceptance*: Three integration tests, one per tier, asserting the exact behavior.

**R-043** — Trust persistence (`[security.validators] approved` entries) SHALL live in `<agent_root>/arcagent.toml` (or `~/.arc/security/trust.toml` for global), at agent root **outside** `<agent_root>/workspace/`. Agent SHALL have no write access. Approval entries SHALL be written only by the human user via CLI / UI, never by the agent.
- *Trace*: D-359 (pinned), D-340 | *Pillar*: security
- *Acceptance*: Attempt by agent to write to `arcagent.toml` via `write` tool → `TOOL_PATH_OUTSIDE_WORKSPACE` error.

**R-044** — `arc module install <bundle>` at federal tier SHALL verify a Sigstore signature (`<bundle>.tar.gz.sigstore`) against trusted cert-identity and Rekor entry before extracting. Air-gapped federal deployments SHALL use a self-hosted `rekor-server` (configured via `[security] rekor_url`).
- *Trace*: D-352 | *Pillar*: security
- *Acceptance*: Federal tier rejects unsigned bundle; verified bundle installs; offline `rekor_url` configuration works without internet.

### Audit & Observability

**R-050** — `CapabilityLoader` SHALL emit five bus events per lifecycle stage: `capability:added`, `capability:removed`, `capability:replaced`, `capability:registration_failed`, `capability:setup_failed`.
- *Trace*: D-367 (extended) | *Pillar*: modularity, security
- *Acceptance*: For each lifecycle path, the corresponding event fires exactly once with structured payload (name, source path, scan root, kind, version).

**R-051** — Every capability lifecycle event SHALL be audited per NIST 800-53 AU-2/AU-3/AU-9 (auto-applied at federal tier). AuditEvent payload SHALL include scan root, source classification (builtin/module/agent/global), capability name, version, and tier.
- *Trace*: D-350, D-351, D-355 | *Pillar*: security
- *Acceptance*: At federal tier, audit log shows tamper-evident chain entries for every register/unregister/replace.

### Lifecycle

**R-060** — `@capability` class with optional `setup(ctx)` / `teardown()` SHALL be supported. Setup SHALL run on first registration AND on every reload that introduces a new instance (Pulumi `Configure` pattern). Teardown SHALL run on removal AND before replacement.
- *Trace*: D-356 | *Pillar*: modularity
- *Acceptance*: Browser capability with `setup()` opening a Chrome process and `teardown()` closing it, when `reload()` replaces it: old `teardown()` completes before new `setup()` is called.

**R-061** — Teardown order on multi-capability shutdown SHALL be reverse-topological based on declared dependencies. Setup order on startup SHALL be topological. Failure during setup SHALL emit `capability:setup_failed` and roll back already-set-up capabilities in reverse order.
- *Trace*: D-356, D-367 (extended) | *Pillar*: modularity, security
- *Acceptance*: Three-capability dependency chain (A → B → C). On startup: setup order A, B, C. On shutdown: teardown order C, B, A. If B's setup raises: A's teardown runs, C is never set up.

**R-062** — Background tasks (`@background_task(interval=N)`) on reload SHALL drain-then-replace: `old_task.cancel()`, `await old_task` (catching `CancelledError`), then `asyncio.create_task(new_fn())`. No overlap.
- *Trace*: D-356, D-341 (refined) | *Pillar*: scalability, security

**R-063** — `CapabilityRegistry` mutations SHALL be guarded by an `aiorwlock` (asyncio RWLock). Tool calls hold reader lock (concurrent); `reload()` holds writer lock (exclusive). Required because `importlib.reload()` is documented thread-unsafe (CPython #126548).
- *Trace*: D-356, D-362 | *Pillar*: scalability, security
- *Acceptance*: Concurrent tool calls during a `reload()` either complete with the old capability or wait for the writer lock — never see a half-mutated registry.

### `arc` CLI

**R-070** — `arc module list` SHALL display all bundled and installed modules with status (enabled/disabled/installed-but-disabled).
- *Trace*: D-338 | *Pillar*: simplicity
- *Acceptance*: Stable output format suitable for scripting.

**R-071** — `arc module enable <name>` SHALL symlink `arcagent/builtins/modules/<name>/` → `~/.arc/capabilities/<name>/`. `arc module disable <name>` SHALL remove the symlink.
- *Trace*: D-338 | *Pillar*: simplicity, modularity
- *Acceptance*: After `enable`, `reload()` registers the module; after `disable`, `reload()` unregisters it.

**R-072** — `arc module install <bundle>` SHALL accept local files only in v1: `.tgz`, `.zip`, or directory paths. Federal tier requires `.sigstore` sidecar per R-044.
- *Trace*: D-368, D-352 | *Pillar*: simplicity, security

**R-073** — `arc module uninstall <name>` SHALL remove the folder under `~/.arc/capabilities/<name>/`. After uninstall + reload, all the module's capabilities SHALL be unregistered.
- *Trace*: D-338 | *Pillar*: simplicity

**R-074** — `arc trust approve <hash>` SHALL persist a TOFU approval to `arcagent.toml [security.validators] approved` (R-043). `arc trust list` SHALL display current approvals.
- *Trace*: D-359 (pinned) | *Pillar*: security

**R-075** — `arc agent build` SHALL seed a fresh `arcagent.toml` with an empty `[security.validators]` block so users see the structure on day one.
- *Trace*: D-359 (pinned) | *Pillar*: simplicity

## Non-Functional Requirements

**R-080** — `mypy --strict` SHALL pass with zero errors on the new code.
- *Pillar*: simplicity, modularity

**R-081** — `ruff check` SHALL pass with zero errors.
- *Pillar*: simplicity

**R-082** — Test coverage: line ≥80%, branch ≥75%, core ≥90%.
- *Pillar*: simplicity (verifiable correctness)

**R-083** — `arcagent/core/` total LOC SHALL remain under 3,500 after migration (current ~3,900-4,100).
- *Trace*: CLAUDE.md quality gate | *Pillar*: simplicity

**R-084** — `pip-audit` SHALL show zero critical or high vulnerabilities.
- *Pillar*: security

**R-085** — Cyclomatic complexity SHALL be ≤10 per function in new code.
- *Pillar*: simplicity

**R-086** — Cold start (capability discovery + registration) SHALL complete in <500ms for an agent with 50 capabilities.
- *Trace*: CLAUDE.md ("Cold start under 500ms") | *Pillar*: scalability

## Out of Scope (explicit non-goals)

- MCP transport implementation (loader leaves slot for future spec)
- Marketplace registry (`arc module install` from URL or registry index)
- File watcher / auto-reload (D-362)
- Backward compatibility with old `extension(api)` factory (D-360)
- Cross-agent shared trust store beyond per-agent `arcagent.toml`
- Automated bypass-discovery / fuzzer for AST validator (manual POC tests only in v1)

## Open Questions (deferred to /implement)

- Exact format of `arc trust approve` interactive prompt (CLI vs arcui dialog)
- Whether `~/.arc/security/trust.toml` (global trust store) ships in v1 or waits for cross-agent demand
- Whether `@capability` class lifecycle gets explicit `depends_on` field for topological ordering, or infers dependencies from `requires_skill` only

## Success Criteria

- All four old registration paths deleted in same PR (D-360)
- Agent can call `reload()` and see new capabilities immediately
- Agent reading `create-tool` and `create-skill` skills can author and register a new capability end-to-end without human intervention at personal tier
- Federal tier rejects all unsigned agent-authored .py at AST-load time
- All 8 existing modules migrated and pass their existing test suites
- Quality gates green (mypy, ruff, coverage, pip-audit, LOC budget)
