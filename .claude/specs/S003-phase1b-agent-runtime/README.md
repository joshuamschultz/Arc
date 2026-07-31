# S003: Phase 1b — Agent Runtime

**Spec ID**: S003
**Feature**: Extension System, Skill Registry, Additional Tools, Settings Manager, CLI Wiring
**Status**: COMPLETE (reviewed)
**Created**: 2026-02-15
**Phase**: 1b (Agent Runtime)

---

## Prior Work

| Source | Location | Key Contribution |
|--------|----------|------------------|
| Brainstorm | `.claude/brainstorms/2026-02-14-phase1-technical-approach.md` | Architecture decisions, integration patterns, ArcRun bridge design |
| Build Decisions | `.claude/decisions-log.md` (B1b-001 through B1b-020) | 20 design decisions covering all S003 components |
| S002 Spec | `.claude/specs/S002-memory-module/` | SessionManager, compaction, memory module (S003 builds on, doesn't duplicate) |
| v3 Design Doc | `docs/arcagent-design-v3.md` | Extension Protocol (sect 8), Skills (sect 5), workspace layout |
| pi-coding-agent Research | Comparison analysis (session 2026-02-15) | Self-extensibility patterns, factory function model |
| CLI Commands | `docs/cli-commands.md` | 59 commands (27 existing + 32 new) for arccli integration |

## Decisions Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| B1b-001 | Extension system in core/extensions.py | Direct access to ToolRegistry, ModuleBus, ContextManager |
| B1b-002 | importlib for local + entry_points for packages | Single factory interface, stdlib only |
| B1b-003 | Phase 1b API: register_tool + on + workspace | Minimum viable self-extensibility |
| B1b-004 | Skill registry in core/skill_registry.py | Direct ContextManager integration |
| B1b-006 | Settings overlay on frozen config, persists to TOML | Never mutates validated config |
| B1b-007 | 3K LOC budget, tools outside core | ~2,359 projected |
| B1b-009 | Factory function pattern for extensions | Simplest, LLM-writable |
| B1b-010 | Full YAML frontmatter schema for skills | Marketplace-ready from day one |
| B1b-013 | Full re-discovery on /reload | Simple, stateless, no edge cases |
| B1b-014 | ArcAgent owns lifecycle, CLI is thin | Clean encapsulation |
| B1b-015 | Configurable sandboxing (workspace/paths/strict) | Federal strict + dev flexibility |
| B1b-017 | Workspace-scoped tools, expandable via policy | Secure by default |
| B1b-019 | Build order: Tools -> Skills -> Extensions -> Sessions -> Settings -> CLI | Each step independently testable |

## Relationship to S002

S002 covers SessionManager and Memory Module (context.md, notes, entities, search, policy engine).
S003 covers the remaining Phase 1b features: extensions, skills, tools, settings, CLI wiring.

Both share the same core: ArcAgent, ModuleBus, ToolRegistry, ContextManager.
S003 features integrate with S002 features but do not duplicate them.

## Review Results (2026-02-15)

**7-agent parallel review** | **Status: PASS with conditions**

### Key Findings
1. Strict sandbox (builtins patching) bypassable via `os.system()`, `Path.read_text()`, `urllib` — accepted as Phase 1 limitation, needs ADR
2. TOML string injection fixed (escape `"`, `\`, newlines)
3. Skill name XML injection fixed (`xml.sax.saxutils.escape`)
4. Core LOC at 3,269 (9% over budget) — needs ADR to increase to 3,500 or extract modules
5. Coverage: extensions 91%, skill_registry 92%, settings 84%, agent 84%, config 73%

### ADRs Needed
- ADR-001: Core LOC budget increase (3,000 → 3,500)
- ADR-002: Sandbox strategy (builtins Phase 1, process isolation Phase 2)
- ADR-003: TOML persistence strategy (regex → tomlkit migration)

### Fixes Applied During Review
- XML escaping in `skill_registry.py:format_for_prompt()`
- TOML string escaping in `settings_manager.py:_persist_to_toml()`

## Learnings

1. **Builtins patching cannot secure CPython** — too many I/O entry points (`os.open`, `io.open`, `socket`, `ctypes`). Process-level isolation (seccomp/landlock/Firecracker) is the only real sandbox.
2. **TOML write needs a proper library** — stdlib `tomllib` is read-only, regex manipulation breaks on edge cases. Migrate to `tomlkit`.
3. **Factory duplication grows fast** — entry point loading duplicated file-based loading ~80 LOC. Extract shared logic early.
4. **Performance targets need benchmark tests** — 5 targets in PRD, none verified by automated tests.
5. **Coverage gaps cluster in config loading and error paths** — `_apply_env_overrides()` and `load_config()` are 0% covered despite being security-relevant.
