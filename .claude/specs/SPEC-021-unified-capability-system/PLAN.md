# PLAN: Unified Capability System

**Spec**: SPEC-021 | **Status**: PHASES 1–4 COMPLETE | **Phases**: 6
**Branch**: `feature/SPEC-021-unified-capability-system`
**Constraint**: Per D-360, phases 1-4 land in the **same PR/commit**. Phase 5 (CLI + federal extras) and phase 6 (validation) may follow as separate commits if needed but ideally land together.

Tracking: `[ ]` pending, `[x]` complete, `[~]` in progress.

---

## Phase 1: New Infrastructure

**Goal**: Land `CapabilityLoader`, `CapabilityRegistry`, decorator extensions, AST validator additions, OS sandbox, TOFU policy layer. Nothing yet uses them — they sit alongside the old paths.

### Tasks

- [x] **1.1** Extend `@tool` decorator with new fields per D-356/D-357
  - File: `packages/arcagent/src/arcagent/tools/_decorator.py`
  - Reqs: R-003, R-033
  - Components: C-003
  - Add fields to `ToolMetadata`: `when_to_use`, `requires_skill`, `version`, `examples`, `model_hint`. Stamp `func._arc_capability_meta = CapabilityMetadata(kind="tool", ...)` (rename from `_arc_tool_meta`).
  - Tests: existing decorator tests pass with new fields default-empty; new test for each field round-tripping into `CapabilityMetadata`.

- [x] **1.2** Add `@hook`, `@background_task`, `@capability` class decorators
  - File: `packages/arcagent/src/arcagent/tools/_decorator.py`
  - Reqs: R-003, R-004, R-060, R-062
  - Components: C-003
  - `@hook(event, priority, tryfirst, trylast)` stamps `kind="hook"`. `@background_task(name, interval)` stamps `kind="background_task"`. `@capability(name, depends_on)` is a class decorator stamping the class with `kind="capability"`; methods inside the class can be `@tool`-decorated and bind at registration.
  - Tests: each decorator stamps the right metadata; `@capability` class with `@tool` method registers both the class lifecycle and the method tool.

- [x] **1.3** Extend AST validator with new bypass categories per D-353
  - File: `packages/arcagent/src/arcagent/tools/_dynamic_loader.py`
  - Reqs: R-040
  - Components: C-004
  - Add to `_BLOCKED_ATTRIBUTES`: `gi_code`, `gi_yieldfrom`, `tb_frame`, `__init_subclass__`, `__class_getitem__`, `__pos__`, `__neg__`, `__get__`, `__set__`. Add `visit_FormattedValue` to detect format-string attribute-access bypasses. Add `visit_ClassDef` to reject classes defining `__init_subclass__` or with metaclass defining `__getitem__`. Add `visit_Attribute` check rejecting `AttributeError.obj` / `AttributeError.name`.
  - Tests: one POC exploit per CVE category in `tests/security/test_ast_bypasses.py` — each must raise `ASTValidationError`.

- [x] **1.4** Add AST cache (MD5+mtime) before validation/import
  - File: `packages/arcagent/src/arcagent/tools/_dynamic_loader.py`
  - Reqs: R-001, R-086
  - Components: C-004
  - `_ast_cache: dict[Path, tuple[str, float]]` keyed by path → (md5, mtime). Skip re-validation if unchanged. Cache invalidated per-file on every reload that detects mtime change.
  - Tests: second validation of unchanged file is a cache hit (returns immediately, does not re-parse); changed file re-validates.

- [x] **1.5** Implement `CapabilityRegistry`
  - File: `packages/arcagent/src/arcagent/core/capability_registry.py` (new)
  - Reqs: R-004, R-020, R-022, R-050, R-063
  - Components: C-002, C-007
  - Four kind-specific dicts (`_tools`, `_skills`, `_hooks`, `_tasks`, `_capabilities`); `aiorwlock` around all reads and writes; `format_for_prompt()` builds XML manifest with cache invalidated on register/unregister; emits five lifecycle bus events.
  - Tests: lock semantics (concurrent reads + exclusive write); manifest XML golden test; cache invalidation on register; fan-out for hooks; drain-then-replace for background tasks; `to_arcrun_tools()` matches existing format.

- [x] **1.6** Implement `CapabilityLoader`
  - File: `packages/arcagent/src/arcagent/core/capability_loader.py` (new)
  - Reqs: R-001, R-002, R-005, R-061, R-063
  - Components: C-001
  - Four-root scan in precedence order; per-file flow (cache → AST validate → TOFU → sandbox → instantiate → register); diff string formatter per R-005; topological setup, reverse-topo teardown.
  - Tests: scan precedence (workspace overrides agent overrides global overrides builtins); diff format (single-line nominal, multi-line on errors); topo ordering with synthetic dependency chain; rollback on setup failure.

- [x] **1.7** Implement `OsSandbox` wrapper (enterprise extras)
  - File: `packages/arcagent/src/arcagent/core/os_sandbox.py` (new)
  - Reqs: R-041
  - Components: C-005
  - Protocol + `make_sandbox(tier)` factory. Linux `SeccompSandbox` using `pyseccomp`; macOS `SandboxExecSandbox` writing per-scope profile to temp file and execing via `sandbox-exec`. Returns `None` for personal tier.
  - Tests: integration test with `ctypes.CDLL(None)` call inside sandbox raising sandbox violation; integration test with allowed file read inside `scope_path` succeeding.
  - Optional dep: `pyseccomp` declared in `[enterprise]` extras in `pyproject.toml`.

- [x] **1.8** Implement `TofuLayer` policy layer
  - File: `packages/arcagent/src/arcagent/core/tool_policy.py` (extend) or new `tofu_layer.py`
  - Reqs: R-042, R-043
  - Components: C-006
  - `evaluate(ctx, target)` returns `Decision.ALLOW` / `Decision.DENY` / `Decision.NEW_SIGHTING`. Reads `[security.validators]` from `arcagent.toml`. Trust file path is agent-root, never workspace.
  - Tests: per-tier decision matrix; new sighting at enterprise → `NEW_SIGHTING`; persisted hash → `ALLOW`; tampered hash → `DENY`; trust-file write attempt by agent (via `write` tool) → fails with `TOOL_PATH_OUTSIDE_WORKSPACE`.

- [x] **1.9** Add `[security.validators]` TOML schema
  - File: `packages/arcagent/src/arcagent/core/config.py`
  - Reqs: R-042, R-043
  - Components: C-006
  - Pydantic models: `ValidatorEntry` (name, hash, approver, timestamp), `ValidatorsConfig` (auto_run_agent_code, approved). Nested under `SecurityConfig`.
  - Tests: TOML round-trip; default empty; entries persist correctly.

- [x] **1.10** Add five capability lifecycle bus events
  - File: `packages/arcagent/src/arcagent/core/capability_loader.py`, `capability_registry.py`
  - Reqs: R-050, R-051
  - Components: C-007
  - `capability:added`, `capability:removed`, `capability:replaced`, `capability:registration_failed`, `capability:setup_failed`. Emission inline at relevant moments. Audit emission via `arctrust.audit.emit` per NIST AU-2.
  - Tests: each event fires exactly once with expected payload on each lifecycle path.

### Phase 1 exit criteria

- All 10 tasks complete.
- New code has unit + integration tests.
- `mypy --strict` and `ruff check` clean on new files.
- Coverage ≥ 95% on new code.
- Old code paths still work; nothing regressed.

---

## Phase 2: Builtin Capabilities

**Goal**: Migrate the 7 file/exec tools and ship 5 self-mod tools (reload, create_tool, create_skill, update_tool, update_skill) plus their accompanying skills under `arcagent/builtins/capabilities/`. Nothing in the agent yet uses them.

### Tasks

- [x] **2.1** Port read/write/edit/bash/grep/find/ls to decorator form
  - Files: `packages/arcagent/src/arcagent/builtins/capabilities/{read,write,edit,bash,grep,find,ls}.py` (new)
  - Reqs: R-002
  - Components: C-009
  - Move logic from `arcagent/tools/{read,write,edit,bash,grep,find,ls}.py` into `arcagent/builtins/capabilities/`. Wrap with `@tool(...)` decorator. Keep behavior identical (signatures, return values, error format). The old files can stay for now (Phase 4 deletes them).
  - Tests: all existing tool tests pass on the new locations; identical input/output.

- [x] **2.2** Implement `reload` tool
  - File: `packages/arcagent/src/arcagent/builtins/capabilities/reload.py` (new)
  - Reqs: R-005, R-030
  - Components: C-001, C-009
  - `@tool(name="reload", classification="state_modifying", ...)`. Body delegates to `agent_context.capability_loader.reload()` and returns the diff string.
  - Tests: end-to-end — call reload after writing a new tool; diff string mentions the new tool.

- [x] **2.3** Implement `create_tool` and `create_skill` tools
  - Files: `packages/arcagent/src/arcagent/builtins/capabilities/{create_tool,create_skill}.py` (new)
  - Reqs: R-031, R-032
  - Components: C-009
  - `create_tool(name, source)`: writes to `<workspace>/.capabilities/<name>.py`, runs DynamicToolLoader.load to validate, but does NOT auto-call reload (LLM calls reload separately). Returns success message with path.
  - `create_skill(name, frontmatter, body)`: scaffolds folder under `<workspace>/.capabilities/skills/<name>/`. Generates SKILL.md with frontmatter + 7 sections (empty placeholder for ## Resources, which loader auto-fills). Includes `references/`, `scripts/`, `templates/`, `assets/` directories.
  - Tests: create_tool persists file in workspace; create_skill scaffolds folder structure; both fail if name already exists in workspace.

- [x] **2.4** Implement `update_tool` and `update_skill` tools
  - Files: `packages/arcagent/src/arcagent/builtins/capabilities/{update_tool,update_skill}.py` (new)
  - Reqs: R-033
  - Components: C-009
  - `update_tool(name, new_source, version_bump)` / `update_skill(name, new_body, version_bump)`. Reads existing file, writes new content, bumps semver per `version_bump` arg (`major`/`minor`/`patch`). Validates before writing.
  - Tests: version bumps correctly; validator rejects malformed update.

- [x] **2.5** Author `create-tool` skill folder
  - Files: `packages/arcagent/src/arcagent/builtins/capabilities/skills/create-tool/{SKILL.md, references/*.md, scripts/validate.py, templates/tool.py.template}` (new)
  - Reqs: R-010, R-011, R-012, R-032
  - Components: C-009
  - SKILL.md teaches the `@tool` decorator API, file layout, when to use create-tool vs update-tool, AST validator gotchas. Required 7 sections. References include decorator-fields.md (full field list with examples), ast-blocked-list.md (what the validator rejects + why), examples-good-and-bad.md (3 good, 3 anti-pattern). scripts/validate.py runs the AST validator and emits clear errors.
  - Tests: validator script exits 0 on valid template, exits non-zero with clear error on malformed; SKILL.md passes loader's frontmatter + sections check.

- [x] **2.6** Author `create-skill` skill folder
  - Files: `packages/arcagent/src/arcagent/builtins/capabilities/skills/create-skill/{SKILL.md, references/*.md, scripts/validate.py, templates/skill-folder/...}` (new)
  - Reqs: R-010, R-011, R-012, R-032
  - Components: C-009
  - SKILL.md teaches frontmatter spec, section rubric (what's a good ## Steps vs. anti-pattern), when to make a skill vs. tool. References include frontmatter-spec.md and section-rubric.md. Templates include skill-folder/SKILL.md.template + scripts/validate.py.template.
  - Tests: validator passes on the template; validator emits clear error on missing field / missing section / "N/A" filler.

- [x] **2.7** Author `update-tool` and `update-skill` skill folders
  - Files: `packages/arcagent/src/arcagent/builtins/capabilities/skills/{update-tool,update-skill}/SKILL.md` + scripts
  - Reqs: R-033
  - Components: C-009
  - Each teaches: when to bump major vs. minor vs. patch (LLM judgment per D-357), how to verify post-update, what to roll back on failure.

- [x] **2.8** Skill validator implementation
  - File: `packages/arcagent/src/arcagent/core/capability_loader.py` (extend) or new `skill_validator.py`
  - Reqs: R-011, R-012, R-013, R-014
  - Components: C-001, C-002
  - Frontmatter required fields check; required 7 sections check; filler detection ("N/A", "none", empty body); auto-generate `## Resources` from folder contents; tool-existence validation per `tools:` field.
  - Tests: every error path has a test; auto-generated Resources section is correct for synthetic folder structure; `tools` field mismatch emits `skill.tool_dependency_policy_denied` audit at correct tier severity.

### Phase 2 exit criteria

- 12 builtin capabilities (7 tools ported + 5 self-mod tools + 4 self-mod skills) under `arcagent/builtins/capabilities/`.
- Each accompanying skill passes its own validate.py.
- Loader can scan `builtins/capabilities/` and register them; integration test confirms.

---

## Phase 3: Module Migrations

**Goal**: Rewrite each existing `arcagent/modules/<name>/` to decorator form. Delete `MODULE.yaml`. Module-specific tests still pass.

Each module is its own task. Per the inventory in research:

- [x] **3.1** Migrate `memory` module
  - Path: `packages/arcagent/src/arcagent/modules/memory/`
  - Reqs: R-002, R-004, R-060
  - Components: C-010
  - Convert `MarkdownMemoryModule.startup()` registrations to: `@hook` × 6 (assemble_prompt, pre_tool, post_tool, post_respond, pre_compaction, shutdown), N × `@tool` (memory operations), `@background_task` for entity extractor. Delete `MODULE.yaml`. Move into `capabilities.py`.
  - Tests: existing `tests/unit/modules/memory/` suite passes on new form.

- [x] **3.2** Migrate `scheduler` module
  - Path: `packages/arcagent/src/arcagent/modules/scheduler/`
  - Reqs: R-002, R-060, R-062
  - Components: C-010
  - `@capability` class wrapping `SchedulerEngine` (setup opens engine, teardown stops timer + drains in-flight). 4 × `@tool` (CRUD). `@hook("agent:ready")` to start engine.
  - Tests: existing scheduler tests pass; engine teardown gracefully cancels in-flight schedule firings.

- [x] **3.3** Migrate `browser` module
  - Path: `packages/arcagent/src/arcagent/modules/browser/`
  - Reqs: R-002, R-060
  - Components: C-010
  - `@capability` class wrapping CDP client (setup spawns Chrome / connects, teardown closes process). N × `@tool` for browse/click/screenshot.
  - Tests: existing browser tests pass; Chrome process cleanly shuts down on capability teardown.

- [x] **3.4** Migrate `voice` module
  - Path: `packages/arcagent/src/arcagent/modules/voice/`
  - Reqs: R-002
  - Components: C-010
  - 2 × `@tool` (transcribe, synthesize) + 2 × `@hook` (voice.transcribe.request, voice.synthesize.request).
  - Tests: existing voice tests pass.

- [x] **3.5** Migrate `telegram` module
  - Path: `packages/arcagent/src/arcagent/modules/telegram/`
  - Reqs: R-002, R-062
  - Components: C-010
  - `@background_task(interval=...)` for poll loop. `@tool` (notify_user). `@hook` × 3 (agent:shutdown, schedule:completed, schedule:failed).
  - Tests: existing telegram tests pass; poll loop drains cleanly on background_task replacement.

- [x] **3.6** Migrate `slack` module
  - Path: `packages/arcagent/src/arcagent/modules/slack/`
  - Reqs: R-002, R-060
  - Components: C-010
  - `@capability` class for WebSocket lifecycle. `@tool` (slack_notify_user). `@hook` × 3 (agent:shutdown, agent:ready, schedule:failed).
  - Tests: existing slack tests pass.

- [x] **3.7** Migrate `policy` module
  - Path: `packages/arcagent/src/arcagent/modules/policy/`
  - Reqs: R-002
  - Components: C-010
  - `@hook` × 3 only (post_respond, assemble_prompt, shutdown). No tools, no lifecycle. Pure subscriber.
  - Tests: existing policy tests pass.

- [x] **3.8** Migrate `ui_reporter` module
  - Path: `packages/arcagent/src/arcagent/modules/ui_reporter/`
  - Reqs: R-002, R-050
  - Components: C-010
  - 17 × `@hook` (or `@capability` class with internal dispatch). Subscribes to all `agent:*`, `llm:*`, and new `capability:*` events. Maps each to arcui WebSocket emission.
  - Tests: existing ui_reporter tests pass; new `capability:*` events also reach arcui.

### Phase 3 exit criteria

- All 8 modules migrated.
- All existing module tests pass without modification (just relocated to import from new paths).
- No `MODULE.yaml` files remain in `arcagent/modules/`.

---

## Phase 4: Delete Old Infrastructure (same edit as Phase 1-3)

**Goal**: Remove the old four-path system. Per D-360 / CLAUDE.md "no legacy", this lands in the same PR/commit as Phases 1-3.

### Tasks

- [x] **4.1** Delete `arcagent/core/extensions.py` (611 LOC) and its tests
  - Files removed: `core/extensions.py`, `tests/unit/core/test_extensions.py`
  - Verify: no remaining imports of `ExtensionLoader`, `ExtensionAPI`.

- [x] **4.2** Delete `arcagent/core/module_loader.py` (257 LOC) — runtime path
  - Files removed: `core/module_loader.py`, related runtime tests.
  - Note: the YAML-parsing logic moves to arccli for `arc module install` packaging metadata only — not runtime.

- [x] **4.3** Delete `MODULE.yaml` runtime parsing in `agent.py`
  - File: `core/agent.py`
  - Reqs: R-002, R-004
  - Remove `_load_modules_by_convention` (lines ~964-981). Remove import of `ModuleLoader`. Remove `_load_modules_by_convention` call in startup (~line 444).

- [x] **4.4** Delete the four-path startup branch in `agent.py`
  - File: `core/agent.py`
  - Reqs: R-001, R-002
  - Remove lines ~376-444 (built-in tool list registration, `register_native_tools` call, `ExtensionLoader` construction, `_load_modules_by_convention` call). Replace with single call to `CapabilityLoader.scan_and_register()`.

- [x] **4.5** Delete `register_native_tools` from `tool_registry.py`
  - File: `core/tool_registry.py`
  - Reqs: R-002
  - Remove `register_native_tools` method (lines ~491-515). Remove `_validate_module_path`. Remove `NativeToolEntry` import.

- [x] **4.6** Delete `[tools.native]` and `[extensions]` TOML schema
  - File: `core/config.py`
  - Reqs: R-002
  - Remove `NativeToolEntry`, `ExtensionEntry`, `ExtensionConfig`. Remove `native: dict[str, NativeToolEntry]` field from `ToolsConfig`. Remove `_ENV_DENYLIST_PREFIXES` entries `tools__native`, `tools__process`.

- [x] **4.7** Delete hardcoded tool list in `tools/__init__.py`
  - File: `tools/__init__.py`
  - Reqs: R-002
  - Remove `create_builtin_tools()` function. The 7 tools' implementations move to `arcagent/builtins/capabilities/` per Phase 2.1. Tools original file paths in `tools/` may either remain (if any other code imports them directly) or be deleted; preference is delete to enforce single-source.

- [x] **4.8** Delete `arcagent/tools/tool_tools.py`
  - File: `tools/tool_tools.py`
  - Reqs: R-030
  - In-memory `make_create_tool_tool` is replaced by file-persisting `create_tool` + `reload`.

- [x] **4.9** Delete `arcagent/core/skill_registry.py`
  - File: `core/skill_registry.py`
  - Reqs: R-010, R-011, R-012
  - Replaced by `SkillEntry` kind in `CapabilityRegistry`. Remove `_setup_skill_prompt_injection` from agent.py.

- [x] **4.10** Replace `_setup_tool_prompt_injection` + `_setup_skill_prompt_injection` with `_setup_capability_prompt_injection`
  - File: `core/agent.py`
  - Reqs: R-020, R-021
  - Components: C-008
  - Single subscriber at priority 85 calls `CapabilityRegistry.format_for_prompt()`. New subscriber at priority 91 injects `SKILL_USAGE_INSTRUCTION` constant.

- [x] **4.11** Update `agent.reload()` to delegate to `CapabilityLoader.reload()`
  - File: `core/agent.py`
  - Reqs: R-030
  - Remove old reload body (lines ~822-857). Replace with `return await self._capability_loader.reload()`.

- [x] **4.12** Delete obsolete bus events (`agent:extensions_loaded`, `agent:skills_loaded`)
  - Files: `core/agent.py`
  - Note: `ui_reporter` module's subscription to these is removed in Phase 3.8.

### Phase 4 exit criteria

- Migration validation matrix in SDD all green:
  - No `MODULE.yaml` files in `arcagent.modules/`
  - `arcagent/core/extensions.py` does not exist
  - `arcagent/core/module_loader.py` does not exist
  - `arcagent/core/skill_registry.py` does not exist
  - `arcagent/tools/tool_tools.py` does not exist
  - Hardcoded `create_builtin_tools` list removed
  - `[tools.native]` and `[extensions]` TOML blocks removed
- `arcagent/core/` total LOC < 3,500.
- All tests pass (unit + integration + e2e).

---

## Phase 5: `arc` CLI + Federal Path

**Goal**: Operator-facing CLI for module/trust management. Sigstore verification at federal install. Documentation updates.

### Tasks

- [ ] **5.1** Implement `arc module list/enable/disable/install/uninstall` commands
  - Files: `packages/arccli/src/arccli/commands/module.py` (new), `arccli/commands/__init__.py` (register)
  - Reqs: R-070, R-071, R-072, R-073
  - Components: C-011
  - `list`: scan `arcagent/builtins/modules/`, `~/.arc/capabilities/`, show enabled/disabled. `enable`: symlink builtins/modules/<name>/ → ~/.arc/capabilities/<name>/. `disable`: rm symlink. `install <bundle>`: extract .tgz/.zip/dir; verify Sigstore at federal. `uninstall`: rm -rf with confirmation.
  - Tests: e2e shell test for full lifecycle (install → enable → disable → uninstall).

- [ ] **5.2** Implement `arc trust approve/list` commands
  - File: `packages/arccli/src/arccli/commands/trust.py` (new)
  - Reqs: R-074
  - Components: C-011
  - `approve <hash>`: prompt for confirmation, append `[[security.validators.approved]]` entry to `arcagent.toml` with timestamp + approver email. `list`: print current approved entries.
  - Tests: TOML round-trip; `arc trust list` after `arc trust approve` shows the new entry.

- [ ] **5.3** Extend `arc agent build` to seed `[security.validators]` block
  - File: `packages/arccli/src/arccli/commands/agent.py` (extend existing `build`)
  - Reqs: R-075
  - Components: C-011
  - Template includes empty `[security.validators]\nauto_run_agent_code = true\napproved = []` (with comments explaining).
  - Tests: `arc agent build foo` produces `arcagent.toml` containing the block.

- [ ] **5.4** Sigstore verification module
  - File: `packages/arccli/src/arccli/sigstore_verify.py` (new)
  - Reqs: R-044
  - Components: C-011
  - `verify_bundle(bundle_path, *, cert_identity, oidc_issuer, rekor_url)`. Uses `sigstore-python` lib (declared in `[federal]` extras). Falls back to self-hosted `rekor_url` for air-gapped.
  - Tests: integration test with valid signature; tampered signature; missing signature.
  - Optional dep: `sigstore` declared in `[federal]` extras in `pyproject.toml`.

- [ ] **5.5** Update `pyproject.toml` extras
  - File: `pyproject.toml` (root and `packages/arcagent/pyproject.toml`)
  - Reqs: R-041, R-044
  - Components: C-005, C-011
  - `[project.optional-dependencies]` adds `enterprise = ["pyseccomp", ...]` and `federal = ["sigstore", "pyseccomp", ...]`. `[enterprise]` is a subset of `[federal]`.

- [ ] **5.6** Update CLAUDE.md and ADRs to reflect new architecture
  - Files: project-root `CLAUDE.md`, `packages/arcagent/CLAUDE.md`, relevant ADRs in `architecture/`
  - Reqs: documentation
  - Components: docs
  - Project-structure section: replace mentions of `extensions/`, `MODULE.yaml`, `[tools.native]` with `capabilities/` model. Update threat-surface mitigations table for ASI04/ASI05 to reference Sigstore + TOFU. Update LOC budget table.

- [ ] **5.7** Update affected runbooks
  - Files: `docs/runbooks/spec-017-operations.md`, any module-authoring docs
  - Reqs: documentation
  - Replace extension-authoring section with capability-authoring; reference builtin `create-tool` / `create-skill` skills.

### Phase 5 exit criteria

- `arc module` and `arc trust` commands work end-to-end (e2e tests green).
- Federal-tier installation refuses unsigned bundles.
- CLAUDE.md and ADRs reflect new architecture.

---

## Phase 6: Validation & Cutover

**Goal**: All quality gates green. Migration validation matrix verified. Performance benchmarks met.

### Tasks

- [ ] **6.1** Migration validation matrix
  - Source: SDD "Migration Validation Matrix" section
  - Action: run each assertion's command; verify all pass.

- [ ] **6.2** Run full test suite
  - Command: `pytest --cov=arcagent --cov=arccli --cov=arctrust`
  - Reqs: R-082
  - Verify: line ≥80%, branch ≥75%, core ≥90%.

- [ ] **6.3** Run security test suite
  - Files: `tests/security/test_ast_bypasses.py`, `tests/security/test_os_sandbox.py`
  - Reqs: R-040, R-041
  - Each CVE-category POC must be rejected. Each tier's TOFU behavior verified.

- [ ] **6.4** Run performance benchmarks
  - Files: `tests/performance/test_capability_discovery.py`
  - Reqs: R-086
  - Cold start with 50 capabilities < 500ms; AST cache hit ratio ≥95% on second reload.

- [ ] **6.5** Type check + lint
  - Commands: `mypy --strict packages/arcagent packages/arccli`, `ruff check`
  - Reqs: R-080, R-081
  - 0 errors.

- [ ] **6.6** Dependency audit
  - Command: `pip-audit`
  - Reqs: R-084
  - 0 critical/high vulnerabilities.

- [ ] **6.7** Core LOC check
  - Command: `find packages/arcagent/src/arcagent/core -name '*.py' | xargs wc -l`
  - Reqs: R-083
  - Total < 3,500.

- [ ] **6.8** End-to-end agent self-modification test
  - Files: `tests/e2e/test_agent_self_extends.py`
  - Reqs: R-030, R-031, R-032, R-033 (success criteria)
  - Personal-tier agent reads `create-tool` skill, writes new tool, calls `reload()`, sees diff, calls new tool successfully — all without human intervention.

- [ ] **6.9** Tier-specific behavior e2e tests
  - Files: `tests/integration/test_tofu_per_tier.py`
  - Reqs: R-042
  - Three tests, one per tier, asserting the exact behavior described in the SDD tier variations table.

- [ ] **6.10** Cutover review with /review
  - Action: run `/review SPEC-021` for post-implementation review (security audit, performance check, documentation review, ADR generation if architectural learnings discovered).

### Phase 6 exit criteria

- Every quality gate green.
- Every requirement R-NNN traced to a passing test.
- Migration validation matrix all green.
- Ready for merge to `main` (after `/review`).

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| AST validator misses a bypass category not in our list | medium | high | Phase 6.3 maintains a CVE-mapped POC test per known category; new categories added as discovered |
| OS sandbox profile too strict, breaks legitimate validator scripts | medium | medium | Phase 1.7 starts with permissive profile, tightens after observing real validate.py needs |
| `aiorwlock` writer starvation under heavy tool-call load | low | medium | Acceptable per CLAUDE.md scalability target; reload() is operator-initiated, infrequent |
| Module migration breaks an existing test we don't know about | medium | medium | Phase 3 runs each module's existing test suite; no new tests required |
| Big-bang PR is too large for review | medium | medium | Pre-arrange reviewer awareness; split commits within PR by phase for readability |
| Federal Sigstore + Rekor self-host doesn't work in air-gap | medium | high | Phase 5.4 includes integration test against locally-running `rekor-server` binary |
| Agent writes invalid Python, AST validator catches but error message is unclear | low | low | Phase 1.3 + Phase 2.5 includes "good error messages" as test criterion |

## Estimated Effort

| Phase | Estimated tasks | Estimated time |
|-------|----------------|----------------|
| Phase 1 | 10 tasks (new infrastructure) | 3-5 days |
| Phase 2 | 8 tasks (builtins + skills) | 2-3 days |
| Phase 3 | 8 tasks (module migrations, ~1 per day) | 4-6 days |
| Phase 4 | 12 tasks (deletions + rewires) | 1-2 days |
| Phase 5 | 7 tasks (CLI + federal + docs) | 2-3 days |
| Phase 6 | 10 tasks (validation) | 1-2 days |
| **Total** | **55 tasks** | **13-21 days** |

This is consistent with brainstorm + decisions-log.md estimates of "one of the largest specs in the project."

## Next Steps

After PLAN approved:
- `/implement SPEC-021` — execute phases sequentially with TDD per phase
- `/validate SPEC-021` — run validation if/when needed before implementation
