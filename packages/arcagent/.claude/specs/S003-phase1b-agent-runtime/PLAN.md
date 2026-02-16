# PLAN: Phase 1b — Agent Runtime

**Spec ID**: S003
**Status**: COMPLETE
**Last Updated**: 2026-02-15
**Build Order**: Tools → Skills → Extensions → Settings → CLI (B1b-019)
**Completed Tasks**: 23/23

---

## Phase 1: Additional Tools (grep, find, ls)

**Goal**: Three new workspace-scoped search tools following the existing `create_tool(workspace) → RegisteredTool` pattern.

**Depends on**: Nothing (uses existing tool_registry, _validation)

### Task 1.1: Extend resolve_workspace_path with allowed_paths

- [x] Add `allowed_paths: list[Path] | None = None` parameter to `resolve_workspace_path()` in `tools/_validation.py`
- [x] If path is outside workspace but inside an allowed_path, permit it
- [x] Add tests for allowed_paths behavior (inside, outside, empty list)
- [x] Existing tests must still pass (backward compatible)

**Files**: `tools/_validation.py`, `tests/unit/tools/test_validation.py`

### Task 1.2: Implement grep tool

- [x] Create `tools/grep.py` with `create_tool(workspace) → RegisteredTool`
- [x] INPUT_SCHEMA: pattern (required, string), path (string), glob_filter (string), max_results (integer, default 100)
- [x] Use `Path.rglob()` for file discovery with optional glob filter
- [x] Use `re.search()` per line, return `file_path:line_number: matching_line`
- [x] Skip binary files (null byte check on first 8KB)
- [x] Max file size 5MB, max results 100
- [x] Workspace-scoped via `resolve_workspace_path()`
- [x] TDD: 13 tests (pattern matching, binary skip, workspace boundary, size limits, max results, glob filter)

**Files**: `tools/grep.py`, `tests/unit/tools/test_grep.py`

### Task 1.3: Implement find tool

- [x] Create `tools/find.py` with `create_tool(workspace) → RegisteredTool`
- [x] INPUT_SCHEMA: pattern (required, string), path (string), max_results (integer, default 200)
- [x] Use `Path.glob(pattern)` within workspace scope
- [x] Return paths relative to workspace, sorted by mtime (newest first)
- [x] Max results 200
- [x] TDD: 11 tests (glob matching, sort order, workspace boundary, max results)

**Files**: `tools/find.py`, `tests/unit/tools/test_find.py`

### Task 1.4: Implement ls tool

- [x] Create `tools/ls.py` with `create_tool(workspace) → RegisteredTool`
- [x] INPUT_SCHEMA: path (string, default workspace root)
- [x] Use `Path.iterdir()`, sort dirs first then files alphabetically
- [x] Format: `d  dirname/` and `f  filename (size)`
- [x] Workspace-scoped via `resolve_workspace_path()`
- [x] TDD: 11 tests (directory listing, type indicators, workspace boundary)

**Files**: `tools/ls.py`, `tests/unit/tools/test_ls.py`

### Task 1.5: Register new tools in create_builtin_tools

- [x] Add grep, find, ls imports and calls to `tools/__init__.py`
- [x] Verify all 7 built-in tools (read, write, edit, bash, grep, find, ls) register correctly
- [x] Run full test suite — all 105 tool tests pass

**Files**: `tools/__init__.py`, `tests/unit/tools/test_init.py`

### Phase 1 Verification
- [x] All tool tests pass (105 tests)
- [x] `mypy --strict` passes on new tool files
- [x] `ruff check` clean on new tool files
- [x] Workspace boundary enforced (path-outside-workspace test)
- [x] Size limits enforced (OOM prevention test)

---

## Phase 2: Skill Registry

**Goal**: Discover SKILL.md files, parse frontmatter, inject names into system prompt.

**Depends on**: Phase 1 (read tool used for on-demand skill content)

### Task 2.1: Add SkillError to error hierarchy

- [x] Add `SkillError(ArcAgentError)` to `core/errors.py`
- [x] Component: `"skill_registry"`
- [x] Also added `ExtensionError` and `SettingsError` in same pass

**Files**: `core/errors.py`

### Task 2.2: Implement SkillRegistry core

- [x] Create `core/skill_registry.py`
- [x] `SkillMeta` dataclass: name, description, version, author, requires, tags, category, file_path
- [x] `SkillRegistry.__init__()` — empty cache
- [x] `discover(workspace, global_dir)` — scan directories for .md files
- [x] YAML frontmatter parsing via `yaml.safe_load()` on content between `---` delimiters
- [x] Required fields: name, description. Skip files with parse errors (warn, don't crash).
- [x] `get_skill(name)` — lookup by name
- [x] `clear()` — reset cache
- [x] `skills` property — list of all discovered SkillMeta
- [x] TDD: 19 tests across 5 test classes

**Files**: `core/skill_registry.py`, `tests/unit/core/test_skill_registry.py`

### Task 2.3: Implement format_for_prompt and prompt injection

- [x] `format_for_prompt()` returns XML-formatted skill list
- [x] Format: `<available-skills><skill name="...">description</skill>...</available-skills>`
- [x] Empty string if no skills discovered
- [x] Subscribe to `agent:assemble_prompt` event to inject `sections["skills"]`
- [x] Add `"skills"` to `_SECTION_ORDER` in `context_manager.py` (between "notes" and "policy")

**Files**: `core/skill_registry.py`, `core/context_manager.py`

### Task 2.4: Agent-created skill re-scan

- [x] `rescan_agent_created(workspace)` — targeted scan of `workspace/skills/_agent-created/` only
- [x] Merges newly found skills into cache without clearing existing

**Files**: `core/skill_registry.py`, `tests/unit/core/test_skill_registry.py`

### Phase 2 Verification
- [x] All skill registry tests pass (19 tests)
- [x] YAML frontmatter with all field combinations parses correctly
- [x] Malformed YAML skipped with warning (no crash)
- [x] System prompt includes skill list in correct position
- [x] `mypy --strict` passes
- [x] `ruff check` clean

---

## Phase 3: Extension System

**Goal**: Discover, load, and sandbox Python extensions. Factory function pattern.

**Depends on**: Phase 1 (tools pattern), Phase 2 (skill registry exists for completeness)

### Task 3.1: Add ExtensionError and config models

- [x] Add `ExtensionError(ArcAgentError)` to `core/errors.py`
- [x] Add `ExtensionEntry(BaseModel)` to `core/config.py`: sandbox_mode, enabled
- [x] Add `ExtensionConfig(BaseModel)` to `core/config.py`: paths, extensions, global_dir
- [x] Add `extensions: ExtensionConfig` to `ArcAgentConfig`
- [x] Add `allowed_paths: list[str]` to `ToolConfig`

**Files**: `core/errors.py`, `core/config.py`

### Task 3.2: Implement ExtensionAPI

- [x] Create `core/extensions.py`
- [x] `ExtensionManifest` dataclass: name, source, sandbox_mode, tools_registered, hooks_registered, load_time_ms
- [x] `ExtensionAPI.__init__(tool_registry, bus, workspace, sandbox_mode, extension_name)`
- [x] `register_tool(tool)` — delegates to ToolRegistry.register(), tracks in manifest
- [x] `on(event, handler)` — delegates to ModuleBus.subscribe(), tracks in manifest
- [x] `workspace` property — read-only Path
- [x] Source tagging: tools get `source="extension:{name}"`, hooks get `module_name="ext:{name}"`

**Files**: `core/extensions.py`, `tests/unit/core/test_extensions.py`

### Task 3.3: Implement ExtensionLoader discovery

- [x] `ExtensionLoader.__init__(tool_registry, bus, telemetry, config)`
- [x] `discover_and_load(workspace, global_dir)` — scan directories in order
- [x] Discovery order: workspace/extensions/, global_dir, config paths
- [x] Each .py file loaded via `importlib.util.spec_from_file_location()` (safe, no exec/eval)
- [x] Locate factory: `getattr(module, "extension")` — skip if missing
- [x] Create ExtensionAPI, call factory(api), record manifest
- [x] Audit event for each load: name, source, tools_registered, hooks_registered

**Files**: `core/extensions.py`, `tests/unit/core/test_extensions.py`

### Task 3.4: Implement sandbox modes

- [x] Workspace mode (default): ExtensionAPI.register_tool validates tool workspace scope
- [x] Strict mode: Context manager patches builtins.open, subprocess.run/Popen during factory call. Restricts filesystem to workspace, blocks subprocess. Restored in finally block.

### Task 3.5: Implement error isolation and hot reload

- [x] Each extension loads in try/except — failure logs error, emits audit, continues
- [x] `clear()` — remove tools with `source.startswith("extension:")`, remove hooks with `module_name.startswith("ext:")`
- [x] Direct dict manipulation on tool_registry.tools and bus._handlers for clearing
- [x] `importlib.invalidate_caches()` called during clear
- [x] TDD: 22 tests across 7 test classes (API, loader, error isolation, hot reload, audit, strict sandbox, entry points)

**Files**: `core/extensions.py`, `tests/unit/core/test_extensions.py`

### Task 3.6: Entry point discovery

- [x] `_discover_entry_points()` — uses `importlib.metadata.entry_points(group="arcagent.extensions")` with Python 3.9-3.11 compat
- [x] `_load_entry_points(workspace)` — iterates entry points, calls `.load()` for factory, creates ExtensionAPI, records manifest with `source="entry_point:{name}"`
- [x] Error isolation: bad entry point `.load()` or factory failure logs error, emits audit, continues
- [x] TDD: 4 tests (load, bad factory, audit, load failure)

### Phase 3 Verification
- [x] Self-extension test: write extension.py → reload → tool available → execute
- [x] Error isolation: bad extension doesn't prevent others
- [x] Audit: all load events emitted
- [x] Hot reload: clear → re-discover works cleanly
- [x] `mypy --strict` passes
- [x] `ruff check` clean

---

## Phase 4: Settings Manager

**Goal**: Runtime settings overlay on frozen config. Persists to TOML.

**Depends on**: Phase 1-3 complete (settings can change tool_timeout, model, etc.)

### Task 4.1: Add SettingsError and config models

- [x] Add `SettingsError(ArcAgentError)` to `core/errors.py` (done in Phase 2)

**Files**: `core/errors.py`

### Task 4.2: Implement SettingsManager core

- [x] Create `core/settings_manager.py`
- [x] `MUTABLE_KEYS` class var with key→type mapping (model, compaction_threshold, tool_timeout, log_level)
- [x] `BLOCKED_KEYS` class var for security-sensitive keys (identity, vault, keys, did, key_dir)
- [x] `__init__(config, telemetry, bus, config_path)` — load overlay from [settings] in TOML
- [x] `get(key)` — overlay first, then config resolution
- [x] `set(key, value)` — validate key, validate type, update overlay, persist, audit, emit
- [x] `_resolve_from_config(key)` — map flat key to nested config path
- [x] `_validate_key(key)` — must be in MUTABLE_KEYS and not in BLOCKED_KEYS
- [x] `_validate_type(key, value)` — value must match expected type

**Files**: `core/settings_manager.py`, `tests/unit/core/test_settings_manager.py`

### Task 4.3: Implement TOML persistence

- [x] `_persist_to_toml()` — read existing TOML, update [settings] section, write back
- [x] Handle case where [settings] section doesn't exist yet (append)
- [x] Handle case where [settings] exists (replace via regex)
- [x] Only write non-empty overlay values
- [x] TDD: 12 tests across 5 test classes (get, set, type validation, blocked keys, persistence)

**Files**: `core/settings_manager.py`, `tests/unit/core/test_settings_manager.py`

### Phase 4 Verification
- [x] All settings tests pass (12 tests)
- [x] Overlay takes priority over config
- [x] Type validation rejects wrong types
- [x] Blocked keys rejected
- [x] TOML persistence round-trips correctly
- [x] Audit events emitted on change
- [x] `mypy --strict` passes
- [x] `ruff check` clean

---

## Phase 5: CLI Wiring (Agent Integration)

**Goal**: Wire everything into ArcAgent. startup/run/chat/shutdown/reload.

**Depends on**: Phases 1-4 complete

### Task 5.1: Wire extensions, skills, settings into startup()

- [x] Add instance variables: `_extension_loader`, `_skill_registry`, `_settings`
- [x] After existing step 7 (session manager), add:
  - Step 8: SettingsManager initialization
  - Step 9: SkillRegistry.discover() + prompt injection setup
  - Step 10: ExtensionLoader.discover_and_load()
- [x] Emit `agent:extensions_loaded` and `agent:skills_loaded` events
- [x] All 29 existing agent unit tests still pass

**Files**: `core/agent.py`

### Task 5.2: Implement reload() method

- [x] `async def reload(self)` — clear extensions, clear skills, re-discover both
- [x] Built-in tools (read/write/edit/bash/grep/find/ls) are NOT cleared on reload
- [x] Only extension-registered tools and skills are cleared and re-discovered

**Files**: `core/agent.py`

### Task 5.3: Add properties and shutdown updates

- [x] `skills` property → list of SkillMeta
- [x] `settings` property → SettingsManager or None
- [x] `shutdown()` — add extension/skill cleanup (before existing bus.shutdown)

**Files**: `core/agent.py`

### Task 5.4: Self-extension integration test

- [x] Create `tests/integration/test_self_extension.py`
- [x] 7 tests: write extension → reload → tool available → execute → audit events → hooks work → builtin tools survive → bad extension isolation → old tools cleared
- [x] PRD §4.1 acceptance criteria met

**Files**: `tests/integration/test_self_extension.py`

### Task 5.5: Skill discovery integration test

- [x] Create `tests/integration/test_skill_discovery.py`
- [x] 10 tests: 3 skills discovered → injected in prompt → XML format → cached → cleared on shutdown → rediscovered on reload → malformed skipped → agent-created rescan → no skills safe → settings accessible
- [x] PRD §4.2 acceptance criteria met

**Files**: `tests/integration/test_skill_discovery.py`

### Phase 5 Verification
- [x] All integration tests pass (17 tests)
- [x] Self-extension acceptance criteria met
- [x] Skill discovery acceptance criteria met
- [x] Tool scope acceptance criteria met (Phase 1 covers)
- [x] Settings runtime change acceptance criteria met
- [x] Full test suite passes: **532 tests, 0 failures**
- [x] `mypy --strict` passes on all 30 source files
- [x] `ruff check` clean on all S003 files (pre-existing issues in other files)
- [x] Core LOC: 3,269 (269 over 3,000 budget — see note below)

---

## Implementation Notes

### Core LOC Budget

Core is at 3,269 LOC — 269 over the 3,000 budget (9% overage). Options to address:
1. Move `session_manager.py` (257 LOC) out of core/ into a `services/` directory → brings to 3,012
2. Move `protocols.py` (41 LOC) into a utilities module → brings to 2,971
3. Consolidate `settings_manager.py` into `config.py` (saves ~30 LOC boilerplate)
4. Accept overage given the significant security (strict sandbox) and extensibility (entry points) gains

### Deferred Items

None — all tasks complete.

### Test Summary

| Phase | Unit Tests | Integration Tests | Total |
|-------|-----------|-------------------|-------|
| 1: Tools | 49 (grep=13, find=11, ls=11, validation=14) | — | 49 |
| 2: Skills | 19 | — | 19 |
| 3: Extensions | 22 | — | 22 |
| 4: Settings | 12 | — | 12 |
| 5: CLI Wiring | 29 (existing agent) | 17 (7 self-ext + 10 skill) | 46 |
| **New tests** | **102** | **17** | **119** |

### New Files Created

| File | LOC | Purpose |
|------|-----|---------|
| `arcagent/tools/grep.py` | ~110 | Workspace-scoped regex search |
| `arcagent/tools/find.py` | ~80 | Workspace-scoped glob file finder |
| `arcagent/tools/ls.py` | ~80 | Workspace-scoped directory listing |
| `arcagent/core/skill_registry.py` | 160 | Skill discovery, frontmatter parsing, prompt injection |
| `arcagent/core/extensions.py` | 450 | Extension loading, factory pattern, hot reload, strict sandbox, entry points |
| `arcagent/core/settings_manager.py` | 186 | Runtime settings overlay, TOML persistence |

### Modified Files

| File | Changes |
|------|---------|
| `arcagent/core/agent.py` | +110 LOC: startup steps 8-10, reload(), skills/settings properties, shutdown cleanup |
| `arcagent/core/config.py` | +40 LOC: ExtensionEntry, ExtensionConfig, allowed_paths |
| `arcagent/core/errors.py` | +30 LOC: SkillError, ExtensionError, SettingsError |
| `arcagent/core/context_manager.py` | +1 LOC: "skills" in _SECTION_ORDER |
| `arcagent/tools/_validation.py` | +15 LOC: allowed_paths parameter |
| `arcagent/tools/__init__.py` | +6 LOC: grep, find, ls registration |
