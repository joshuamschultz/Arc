# ArcCLI Command Reference

> Target command set for arccli once Phase 1b features are built.
> Based on: pi-coding-agent analysis, arcagent v3 design, existing arccli commands.
> Location: `~/AI/arccli/`

---

## Current State (What Exists Today)

arccli has 17 commands across 3 groups. All functional but `arc agent` bypasses arcagent
entirely — it composes arcllm + arcrun directly. Phase 1b CLI integration means wiring
`arc agent` commands through `ArcAgent` orchestrator to get identity, telemetry, module bus,
tool policy, context management, extensions, and skills.

---

## Command Groups

### `arc llm` — LLM Provider Management (Exists, No Changes Needed)

| Command | Status | Description |
|---------|--------|-------------|
| `arc llm version` | Exists | Show arcllm version |
| `arc llm config` | Exists | Show loaded config (providers, modules, defaults) |
| `arc llm providers` | Exists | List all registered providers |
| `arc llm provider <name>` | Exists | Show provider details (models, capabilities) |
| `arc llm models` | Exists | List all available models across providers |
| `arc llm call` | Exists | Direct LLM call (bypasses arcrun/arcagent) |
| `arc llm validate` | Exists | Validate API key + connectivity for a provider |

### `arc run` — Direct ArcRun Execution (Exists, No Changes Needed)

Low-level commands for running tasks through arcrun without arcagent orchestration.
Keep for debugging and direct access.

| Command | Status | Description |
|---------|--------|-------------|
| `arc run task` | Exists | Run task through arcrun with built-in tools (calc, code-exec) |
| `arc run exec` | Exists | Execute Python code in arcrun sandbox |
| `arc run version` | Exists | Show arcrun version |

### `arc agent` — Agent Management (Exists, Needs Rewiring + New Commands)

#### Existing Commands (Need ArcAgent Wiring)

These exist but bypass arcagent. Need to route through `ArcAgent` orchestrator.

| Command | Status | Change Needed |
|---------|--------|---------------|
| `arc agent create <name>` | Exists | Add extension/skill directory scaffolding to workspace |
| `arc agent build <path>` | Exists | No change (builds agent from description via LLM) |
| `arc agent chat <path>` | Exists | **Wire through ArcAgent** instead of direct arcrun |
| `arc agent tools <path>` | Exists | Include extension-registered tools in listing |
| `arc agent strategies <path>` | Exists | No change |
| `arc agent events <path>` | Exists | Include Module Bus events in output |
| `arc agent config <path>` | Exists | Show full ArcAgent config (not just TOML) |

#### New Commands Needed

| Command | Priority | Description |
|---------|----------|-------------|
| `arc agent init [path]` | P0 | Bootstrap workspace: create identity.md, policy.md, context.md, skills/, extensions/, sessions/, tools/ directories. Optionally interactive (ask name, org, model). |
| `arc agent run <path> <task>` | P0 | One-shot task execution through ArcAgent (non-interactive). Equivalent to `chat --task` but cleaner UX. |
| `arc agent status <path>` | P1 | Show agent status: DID, loaded tools, loaded skills, loaded extensions, active session, token usage, last activity. |
| `arc agent reload <path>` | P0 | Hot-reload extensions and skills without restarting. Re-runs discovery + loading pipeline. |
| `arc agent skills <path>` | P1 | List discovered skills (name, description, location, source: global/workspace/agent-created). |
| `arc agent extensions <path>` | P1 | List loaded extensions (name, version, registered tools, event hooks). |
| `arc agent sessions <path>` | P1 | List sessions (id, timestamp, turns, tool calls, status: active/archived). |
| `arc agent session resume <path> <session-id>` | P1 | Resume a previous session from JSONL transcript. |
| `arc agent session compact <path>` | P2 | Manually trigger Letta-style compaction on current session. |
| `arc agent session fork <path>` | P2 | Branch current session to a new JSONL file from a specific point. |
| `arc agent settings <path>` | P1 | View/modify runtime settings (compaction threshold, model, tool activation). |
| `arc agent settings set <path> <key> <value>` | P1 | Set a runtime setting. |

### `arc ext` — Extension Management (New Group)

| Command | Priority | Description |
|---------|----------|-------------|
| `arc ext list` | P1 | List all discovered extensions (global + per-agent). |
| `arc ext install <source>` | P2 | Install extension from file, directory, or git URL to `~/.arcagent/extensions/`. |
| `arc ext create <name>` | P1 | Scaffold a new extension with boilerplate (factory function, registerTool example). |
| `arc ext validate <path>` | P2 | Validate extension: check exports, verify factory signature, test load. |

### `arc skill` — Skill Management (New Group)

| Command | Priority | Description |
|---------|----------|-------------|
| `arc skill list` | P1 | List all discovered skills (global + per-agent + agent-created). |
| `arc skill create <name>` | P1 | Scaffold a new SKILL.md with YAML frontmatter template. |
| `arc skill validate <path>` | P2 | Validate skill: check frontmatter, verify required fields, check references. |
| `arc skill search <query>` | P2 | Search skills by name/description (useful with many skills). |

---

## Workspace Directory Structure (Post-Phase-1b)

Created by `arc agent init` or `arc agent create`:

```
<agent-name>/
  arcagent.toml              # Agent config (TOML + Pydantic validated)
  workspace/
    identity.md              # System prompt / agent identity (read-only to agent)
    policy.md                # Self-learning behavioral rules
    context.md               # Agent-maintained working memory
    skills/                  # Knowledge files (SKILL.md with YAML frontmatter)
      _agent-created/        # Skills the agent built itself
    extensions/              # Per-agent Python extensions
    sessions/                # JSONL transcripts
    archive/                 # Compacted old sessions
    library/                 # Agent-created reusable artifacts
      scripts/
      templates/
      prompts/
  tools/                     # Custom tools (Python, export get_tools())
```

---

## REPL Commands (Interactive Mode via `arc agent chat`)

Existing REPL commands in arccli:

| Command | Status | Description |
|---------|--------|-------------|
| `/quit` | Exists | Exit REPL |
| `/tools` | Exists | List active tools |
| `/model` | Exists | Show/switch model |
| `/cost` | Exists | Show session cost |
| `/events` | Exists | Show event log |
| `/sandbox` | Exists | Show sandbox config |
| `/strategy` | Exists | Show/switch strategy |
| `/max-turns` | Exists | Show/set max turns |
| `/add-code-exec` | Exists | Add code execution tool |
| `/verbose` | Exists | Toggle verbose output |

New REPL commands needed:

| Command | Priority | Description |
|---------|----------|-------------|
| `/reload` | P0 | Hot-reload extensions and skills |
| `/skills` | P1 | List available skills |
| `/extensions` | P1 | List loaded extensions |
| `/compact` | P1 | Trigger manual compaction |
| `/session` | P1 | Show current session info (id, turns, tokens) |
| `/sessions` | P1 | List all sessions for this agent |
| `/switch <id>` | P2 | Switch to a different session |
| `/fork` | P2 | Fork current session |
| `/settings` | P1 | Show runtime settings |
| `/set <key> <value>` | P1 | Modify runtime setting |
| `/identity` | P2 | Show agent DID and identity info |
| `/status` | P1 | Show agent status (tools, skills, extensions, session) |

---

## Implementation Notes

### ArcAgent Wiring for `arc agent chat`

Current flow (bypasses arcagent):
```
arccli → arcllm.load_model() → arcrun.run(model, tools, ...) → result
```

Target flow (through arcagent):
```
arccli → ArcAgent(config) → agent.startup() → agent.run(task) → result
         │                   │                 └── internally calls arcrun.run()
         │                   └── initializes identity, telemetry, module bus,
         │                       tool registry, context manager, extensions, skills
         └── agent.shutdown()
```

### Dependencies Between CLI and arcagent Features

| CLI Command | Requires arcagent Feature |
|-------------|--------------------------|
| `arc agent chat` (rewired) | ArcAgent.run() with real arcrun |
| `arc agent init` | Workspace scaffolding logic |
| `arc agent reload` | Extension/skill discovery + hot reload |
| `arc agent skills` | SkillRegistry |
| `arc agent extensions` | Extension loader |
| `arc agent sessions` | SessionManager |
| `arc agent session resume` | SessionManager.resume() |
| `arc agent session compact` | SessionManager.compact() |
| `arc agent settings` | SettingsManager |
| `/reload` REPL | Extension/skill reload on ArcAgent |
| `/compact` REPL | SessionManager.compact() |

### What arccli Already Has That We Keep

- `.env` loading from 3 paths
- `_discover_tools()` from `tools/*.py` (extend to include extension tools)
- `_assemble_system_prompt()` from workspace markdown (replace with ArcAgent's ContextManager)
- `_make_event_logger()` for verbose output (extend to include Module Bus events)
- Interactive REPL loop with session tracking
- Agent directory structure with `arcagent.toml`

---

## Summary

| Group | Existing | New | Total |
|-------|----------|-----|-------|
| `arc llm` | 7 | 0 | 7 |
| `arc run` | 3 | 0 | 3 |
| `arc agent` | 7 | 12 | 19 |
| `arc ext` | 0 | 4 | 4 |
| `arc skill` | 0 | 4 | 4 |
| REPL commands | 10 | 12 | 22 |
| **Total** | **27** | **32** | **59** |
