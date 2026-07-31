# PRD: Phase 1b — Agent Runtime

**Spec ID**: S003
**Status**: PENDING
**Last Updated**: 2026-02-15

---

## 1. Overview

### 1.1 Problem Statement

ArcAgent has a functioning nucleus (S001) and a memory module design (S002), but lacks self-extensibility. The agent cannot discover or load extensions, register skills, or be configured at runtime. The CLI (arccli) bypasses ArcAgent entirely, going straight to arcllm+arcrun. Without these capabilities, ArcAgent cannot match the self-extension pattern demonstrated by pi-coding-agent — where the LLM writes a Python file and the agent loads it as a new tool.

### 1.2 Target Users

- **Agents** — Need to create their own tools and skills at runtime
- **Developers** — Want to extend agents via Python extensions and SKILL.md files
- **Federal deployers** — Need configurable sandboxing and audited extension loading
- **CLI users** — Want `arc agent chat` to use the full ArcAgent pipeline, not bypass it

### 1.3 Success Criteria

| Criteria | Target | Measurement |
|----------|--------|-------------|
| Self-extension | Agent writes extension.py, `/reload` loads new tool | Integration test |
| Skill discovery | SKILL.md files auto-discovered, names in prompt | Unit test |
| Progressive disclosure | Full SKILL.md loaded on demand via read tool | Integration test |
| grep/find/ls tools | Workspace-scoped search tools functional | Unit test |
| Settings at runtime | Model, compaction threshold configurable without restart | Unit test |
| Extension sandboxing | Strict mode blocks filesystem/network | Unit test |
| Hot reload | `/reload` clears and re-discovers extensions+skills | Integration test |
| CLI wiring | ArcAgent.startup/run/shutdown called by arccli | Integration test (manual) |

---

## 2. Requirements

### 2.1 Extension System (core/extensions.py)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-EXT-01 | Discover extensions from workspace/extensions/ (per-agent) | P0 | B1b-001, v3 sect 8 |
| REQ-EXT-02 | Discover extensions from ~/.arcagent/extensions/ (global) | P0 | B1b-001, v3 sect 8 |
| REQ-EXT-03 | Discover extensions from config-specified paths | P1 | B1b-001 |
| REQ-EXT-04 | Load .py files via importlib.import_module() | P0 | B1b-002 |
| REQ-EXT-05 | Load installed packages via importlib.metadata.entry_points(group="arcagent.extensions") | P1 | B1b-002 |
| REQ-EXT-06 | Each extension exports a factory function receiving ExtensionAPI | P0 | B1b-009 |
| REQ-EXT-07 | ExtensionAPI provides register_tool(tool: RegisteredTool) | P0 | B1b-003 |
| REQ-EXT-08 | ExtensionAPI provides on(event: str, handler: Callable) | P0 | B1b-003 |
| REQ-EXT-09 | ExtensionAPI provides workspace path access | P0 | B1b-003 |
| REQ-EXT-10 | Configurable sandboxing per-extension: workspace / paths / strict | P0 | B1b-015 |
| REQ-EXT-11 | Audit every extension load (name, source, tools registered, hooks registered) | P0 | B1b-015 |
| REQ-EXT-12 | Hot reload via full re-discovery: clear tools -> invalidate_caches -> re-import -> re-run factories | P0 | B1b-013 |
| REQ-EXT-13 | Extension errors isolated — one bad extension doesn't crash the agent | P0 | Design |
| REQ-EXT-14 | Emit agent:extensions_loaded event after discovery | P1 | Design |

### 2.2 Skill Registry (core/skill_registry.py)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-SKL-01 | Discover SKILL.md files from workspace/skills/ (per-agent) | P0 | B1b-004, v3 sect 5 |
| REQ-SKL-02 | Discover SKILL.md files from ~/.arcagent/skills/ (global) | P0 | B1b-004, v3 sect 5 |
| REQ-SKL-03 | Discover agent-created skills from workspace/skills/_agent-created/ | P0 | B1b-004 |
| REQ-SKL-04 | Parse YAML frontmatter: name (required), description (required), version, author, requires, tags, category | P0 | B1b-010 |
| REQ-SKL-05 | Progressive disclosure: only name + description injected into system prompt | P0 | v3 sect 5 |
| REQ-SKL-06 | Full SKILL.md content loaded on demand via read tool | P0 | v3 sect 5 |
| REQ-SKL-07 | format_for_prompt() returns XML-formatted skill list | P0 | v3 sect 5 |
| REQ-SKL-08 | Cache discovered skills until /reload | P0 | B1b-016 |
| REQ-SKL-09 | Agent-created skills trigger targeted re-scan of _agent-created/ only | P1 | B1b-016 |
| REQ-SKL-10 | Inject skill list into system prompt via ContextManager | P0 | Design |
| REQ-SKL-11 | Emit agent:skills_loaded event after discovery | P1 | Design |

### 2.3 Additional Tools (tools/grep.py, tools/find.py, tools/ls.py)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-TOOL-01 | grep tool: search file contents by regex pattern | P0 | B1b-017 |
| REQ-TOOL-02 | find tool: find files by glob pattern | P0 | B1b-017 |
| REQ-TOOL-03 | ls tool: list directory contents | P0 | B1b-017 |
| REQ-TOOL-04 | All tools workspace-scoped by default | P0 | B1b-017 |
| REQ-TOOL-05 | Scope expandable via [tools.policy] config paths | P0 | B1b-017 |
| REQ-TOOL-06 | Consistent with existing tool pattern (create_tool(workspace) -> RegisteredTool) | P0 | Existing pattern |
| REQ-TOOL-07 | grep returns matching lines with file:line format | P0 | Design |
| REQ-TOOL-08 | find returns matching file paths sorted by modification time | P0 | Design |
| REQ-TOOL-09 | ls returns directory entries with type indicators | P0 | Design |
| REQ-TOOL-10 | Size limits to prevent OOM (max results, max file size for grep) | P0 | Design |

### 2.4 Settings Manager (core/settings_manager.py)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-SET-01 | Overlay pattern: frozen Pydantic config + mutable overlay dict | P0 | B1b-006 |
| REQ-SET-02 | get(key) checks overlay first, falls back to config | P0 | B1b-006 |
| REQ-SET-03 | set(key, value) writes to overlay and persists to [settings] in arcagent.toml | P0 | B1b-006 |
| REQ-SET-04 | Supported runtime settings: model, compaction_threshold, tool_timeout, log_level | P0 | Design |
| REQ-SET-05 | Type validation on set (must match expected type for key) | P0 | Design |
| REQ-SET-06 | Emit agent:settings_changed event on set | P1 | Design |
| REQ-SET-07 | Audit every settings change (key, old_value, new_value, session_id) | P0 | Federal |
| REQ-SET-08 | Security-sensitive keys blocked from runtime change (identity, vault, keys) | P0 | Federal |

### 2.5 CLI Wiring (Integration Guide)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-CLI-01 | ArcAgent.startup() initializes all components (identity, telemetry, bus, tools, context, session, extensions, skills, settings) | P0 | B1b-014 |
| REQ-CLI-02 | ArcAgent.run(task) for one-shot execution | P0 | Existing |
| REQ-CLI-03 | ArcAgent.chat(message) for multi-turn conversation | P0 | S002 |
| REQ-CLI-04 | ArcAgent.shutdown() for clean teardown | P0 | Existing |
| REQ-CLI-05 | ArcAgent.reload() re-discovers extensions and skills | P0 | B1b-013 |
| REQ-CLI-06 | arccli creates ArcAgent(config) and delegates lifecycle | P0 | B1b-014 |
| REQ-CLI-07 | Extension-registered tools included in tool listing | P0 | B1b-014 |
| REQ-CLI-08 | Skill list available via ArcAgent.skills property | P1 | Design |
| REQ-CLI-09 | Settings accessible via ArcAgent.settings property | P1 | Design |

---

## 3. Non-Functional Requirements

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-20 | Extension system LOC (core) | <= 300 lines |
| NFR-21 | Skill registry LOC (core) | <= 150 lines |
| NFR-22 | Settings manager LOC (core) | <= 150 lines |
| NFR-23 | Additional tools LOC (tools/) | <= 200 lines total |
| NFR-24 | Core total LOC after S003 | < 2,500 (headroom within 3K) |
| NFR-25 | Extension load time | < 100ms per extension |
| NFR-26 | Skill discovery time | < 50ms for 100 skills |
| NFR-27 | Hot reload time | < 500ms total |
| NFR-28 | Type safety | mypy --strict passes on all new code |
| NFR-29 | Coverage | >= 90% on new core, >= 80% on tools |

---

## 4. Acceptance Criteria

### 4.1 Self-Extension Test

```
Given: Agent with extension system loaded, valid workspace
When: Agent writes a Python extension to workspace/extensions/custom.py that registers a "hello" tool
Then:
  - File created at workspace/extensions/custom.py
  - On /reload, extension discovered and factory called
  - "hello" tool available in tool registry
  - Tool call succeeds with expected result
  - Audit event emitted for extension load
```

### 4.2 Skill Discovery Test

```
Given: workspace/skills/ contains 3 SKILL.md files with YAML frontmatter
When: Agent starts up
Then:
  - All 3 skills discovered with name + description
  - System prompt contains skill list (names + descriptions only)
  - Agent can read full SKILL.md content via read tool
  - Skills cached (no re-scan on next prompt assembly)
```

### 4.3 Tool Scope Test

```
Given: Agent with grep/find/ls tools, workspace at /tmp/test-agent/workspace
When: Agent tries to grep /etc/passwd
Then:
  - Tool returns error: path outside workspace
When: Agent greps for "TODO" in workspace
Then:
  - Returns matching lines with file:line format
```

### 4.4 Settings Runtime Change Test

```
Given: Agent with settings manager, model = "anthropic/claude-sonnet"
When: settings.set("model", "anthropic/claude-opus")
Then:
  - Overlay updated, config unchanged
  - [settings] section written to arcagent.toml
  - Next ArcRun call uses new model
  - Audit event emitted
```

### 4.5 Extension Sandboxing Test

```
Given: Extension configured with sandbox_mode = "strict"
When: Extension factory tries to access filesystem or network
Then:
  - Access blocked, error logged
  - Extension still loads (register_tool/on still work)
  - Audit event emitted for blocked access
```

---

## 5. Constraints

- Core LOC must remain < 3,000 total after all S003 additions
- Extensions use importlib only — no exec(), no eval(), no dynamic compilation
- Extension sandbox in strict mode must block: open(), Path.read_text(), subprocess, urllib, httpx
- SKILL.md frontmatter must be valid YAML — skip files with parse errors (warn, don't crash)
- Settings persist to the same arcagent.toml file (not a separate file)
- CLI integration is an interface contract — actual arccli changes are separate work
- New tools follow the exact same pattern as existing read/write/edit/bash tools
