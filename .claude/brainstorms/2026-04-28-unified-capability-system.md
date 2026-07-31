# Unified Capability System — Brainstorm

**Date:** 2026-04-28
**Status:** Brainstorm — no code, no plan yet
**Scope:** Rethink how tools, skills, extensions, and modules get added, registered, and loaded in arcagent

---

## Why this brainstorm

The current registration system in arcagent has four parallel concepts (tools, extensions, modules, skills) and four parallel registration paths in `agent.py` startup. This makes the mental model heavy, complicates self-extensibility (the agent can't easily write its own tool/skill), and produces duplicated discovery logic. We compared with OpenClaw and Claude Code to find a cleaner pattern.

---

## What's there now (Arc, current state)

### Built-in tools — `arcagent/tools/__init__.py`
Hardcoded list of 7: `read, write, edit, bash, grep, find, ls`. Registered explicitly in `core/agent.py:385-386`. New tool file does nothing until added to the list.

### Native config tools — `arcagent.toml [tools.native]`
Declared in TOML, loaded via `register_native_tools()` (`tool_registry.py:491`). Module path validated against allowlist before import. Empty schema unless function provides one.

### Extensions — file-discovered, factory-registered
`ExtensionLoader.discover_and_load()` scans:
1. `workspace/extensions/*.py`
2. `workspace/tools/*.py`
3. `~/.arcagent/extensions/*.py`
4. `extensions.paths` from config
5. Python `importlib.metadata` entry points

Each `.py` must export `extension(api)` factory that calls `api.register_tool(...)`.

### Modules — `arcagent/modules/<name>/MODULE.yaml`
First-class shipped subsystems (memory, browser, scheduler, voice, etc.) with own config, manifest, and lifecycle (startup/shutdown). Loaded via `_load_modules_by_convention`.

### Skills — `*.md` files with YAML frontmatter
Discovered from `workspace/skills/` and `~/.arcagent/skills/` by `SkillRegistry.discover()`. Pure prompt augmentation, no execution.

### Dynamic tools — `make_create_tool_tool` (built but not wired)
Source-as-string → `DynamicToolLoader.load()` → AST validate → restricted-builtins compile → register. **In-memory only.** Tier-gated (federal denies, enterprise allowed with audit, personal allowed). Not in default `create_builtin_tools()`. Doesn't write to disk.

### Prompt assembly
`ContextManager.assemble_system_prompt()` reads `identity.md` + `context.md` from workspace, fires `agent:assemble_prompt` event, three subscribers inject:
- `tools` — `tool_registry.format_for_prompt()` → XML `<available-tools>` (priority 85)
- `skills` — `skill_registry.format_for_prompt()` → XML `<available-skills>` (priority 90)
- `strategy_*` / `code_exec_guidance` — from `arcrun.get_strategy_prompts()` as `extra_sections`

System prompt rebuilt only on `reload()`; ArcRun reuses it across all turns in a `run()`.

---

## Reference: OpenClaw vs Arc (compared earlier)

### Prompt
- **OpenClaw**: 1,059-line `buildAgentSystemPrompt()` with ~25 conditional sections, embedded tool list (hardcoded `coreToolSummaries`), explicit `SYSTEM_PROMPT_CACHE_BOUNDARY` sentinel, sandbox/channel/owner awareness, ~50 named params.
- **Arc**: 50-line `assemble_system_prompt()`. Reads identity.md + context.md, lets bus subscribers inject the rest. Two inputs total.

### Tools
- **OpenClaw**: ~25 default tools (read/write/edit/apply_patch/grep/find/ls/exec/process/web_search/web_fetch/browser/canvas/nodes/cron/message/gateway/sessions_*/image/...), wrapped in a multi-step policy pipeline per call (profile → providerProfile → global → agent → group → sandbox → subagent → owner-only), with adaptive paging, sandbox FS bridges, approval flows. Pulls base tools from `@mariozechner/pi-coding-agent` npm package.
- **Arc**: 7 built-ins, single-layer policy at registration, typed `ToolError` codes (`TOOL_INVALID_PATH`, `TOOL_SYMLINK_DENIED`, `TOOL_PATH_OUTSIDE_WORKSPACE`).

### How skills get to the model — both follow the same pattern
1. Manifest (name + description + sometimes triggers/tools) injected into system prompt at session start
2. LLM picks a skill based on description match
3. LLM uses `read` tool to pull SKILL.md body when applying the skill
4. Body lives in conversation history one-shot (not pinned)
5. References pulled lazily by `read` if cited
6. Manifest stays for the session, rebuilds only on `reload()`

### Headline philosophy difference
- **OpenClaw**: framework-owned prompt; tools/sandbox/channels/approvals all baked into the prompt builder
- **Arc**: user-owned prompt (identity.md + context.md); operational concerns live in tools/modules/audit, not the prompt text

---

## Problems with Arc's current state

1. **Four parallel registration paths** make `agent.py` startup branchy and hard to reason about.
2. **`MODULE.yaml` vs `extension(api)` factory** is a distinction without a real difference — both end up calling `tool_registry.register()`.
3. **`[tools.native]` TOML block** is a third way to register tools, redundant with extensions.
4. **`make_create_tool_tool` exists but isn't wired** — the agent has no built-in path to extend itself.
5. **Skills lack structure enforcement** — frontmatter is loose; no required sections; no validator catching low-quality LLM-authored skills.
6. **No clear convention taught to the agent** — even with `write` and `bash`, the LLM doesn't know `workspace/tools/<name>.py` is the right destination, doesn't know the `@tool` decorator API, doesn't know about reload, doesn't know about the AST validator's blocked imports.
7. **Hot reload exists but is incomplete** — `agent.reload()` clears extensions and skills, but doesn't have a clean diff/audit story.

---

## The proposed model

### One concept: Capability

A capability is a unit registered into one registry. Two file types:
- `.py` with decorator (`@tool`, `@hook`, `@background_task`, or class with `@capability` + `setup`/`teardown`)
- `.md` skill folder with `SKILL.md` + structured frontmatter and sections

### Three decorators

```
@tool(name=, description=, when_to_use=, classification=, capability_tags=,
      requires_skill=optional, version=)
async def grep(...): ...

@hook(event="agent:ready", priority=85)
async def boot_check(ctx): ...

@background_task(name=, interval=)
async def memory_flusher(ctx): ...
```

For the ~10% case needing heavy resources / explicit cleanup, a `@capability` class with optional `setup()` / `teardown()`.

### Scan roots, in precedence order

```
arcagent/builtins/capabilities/      # ships with framework — always loaded
~/.arc/capabilities/                 # global, user-installed (incl. enabled modules)
<agent_root>/capabilities/           # per-agent persistent
<agent_root>/workspace/.capabilities/ # agent-authored at runtime
```

Last-wins on name collision. Audit emitted on shadow.

### Skills — folder structure (one tier)

```
<scan-root>/skills/<skill-name>/
  SKILL.md
  references/         # deep markdowns (lists, knowledge, deep dives)
  scripts/            # deterministic scripts (validators, helpers)
  templates/          # boilerplate for what the skill produces
  assets/             # data files, samples, icons
  state.json          # optional, persistent state
```

Required SKILL.md frontmatter:
- `name`
- `description` — one paragraph: what + when
- `triggers` — list of natural-language phrases (semantic hints for the LLM, not literal matching)
- `tools` — declares which tools the skill uses (descriptive only — policy is authoritative)
- `version` — bumped only by `update_skill`
- `model_hint` — optional, advisory

Required sections (title-case headings, enforced):
- `## Resources` — auto-generated by loader from folder contents (author never edits)
- `## Contract` — what the skill guarantees on success
- `## Knowledge` — assumed background
- `## Steps` — the procedure
- `## Anti Patterns`
- `## Examples`
- `## Validation` — checklist or reference to `scripts/validate.py`

Validator rejects missing sections, accepts short sections, flags filler ("N/A", "none", empty).

### Modules — packaging concept, not runtime

A module is a directory of capabilities. Runtime doesn't know about modules. Marketplace/CLI does.

```
~/.arc/capabilities/arc-memory/   # an installed module
  remember.py                     # @tool
  recall.py                       # @tool
  skills/
    memory-system/                # full skill folder
  MODULE.yaml                     # marketplace metadata only (version, author, deps)
```

`MODULE.yaml` is read by `arc module *` commands, never by the runtime.

User-facing CLI:
- `arc module list` — shows bundled + installed + enabled status
- `arc module enable <name>` — symlinks `arcagent/builtins/modules/<name>/` → `~/.arc/capabilities/<name>/`
- `arc module disable <name>` — removes symlink
- `arc module install <bundle>` — marketplace install → `~/.arc/capabilities/<name>/`
- `arc module uninstall <name>` — removes folder

Federal tier requires Sigstore signature verification on install; refuses unsigned bundles.

### Self-mod surface — `reload()`

The only built-in tool for self-modification:
- Idempotent
- Rescans all four scan roots
- Diffs registry old vs new
- Emits `capability:added/removed/replaced` events
- Returns a readable diff string (the LLM uses this to reason about changes)

Persistence is `write` to `<agent_root>/workspace/.capabilities/...` + `reload()`. No separate `create_tool` API; same path as everything else.

Tier gates (already in arctrust):
- **Federal** — `reload()` only registers files matching trusted Sigstore signatures. Agent-authored Python is denied at AST-load time.
- **Enterprise** — allowed with audit; expected to bolt on human approval workflow.
- **Personal** — allowed.

### Validators

- **`@tool` decorated `.py`** — AST validator (already exists in `_dynamic_loader.py`): reject blocked imports (ctypes, subprocess, socket, os, sys, pickle, marshal, shelve), blocked attributes (`__class__`, `__bases__`, `__subclasses__`, etc.), blocked calls (`compile`, `eval`, `exec`, `__import__`).
- **Skill folder** — frontmatter required-fields check; sections present check; tools-listed-exist check (warn, not refuse, on policy mismatch); resource paths in body match folder contents.

### System prompt at session start

```
identity.md
context.md
<available-tools>...</available-tools>            # bus inject (priority 85)
<available-skills>...</available-skills>          # bus inject (priority 90)
[3-line skill usage instruction]                  # bus inject (priority 91)
[strategy guidance from arcrun]                   # extra_sections
```

The 3-line instruction (also injected via bus, not hardcoded in the prompt builder):

> Scan `<available-skills>`. If one clearly applies, read its SKILL.md at the listed location, then follow it. Read references only when the body cites them. Never read more than one skill up front; pick the most specific.

### How ArcRun receives it

ArcRun is just a loop runner — doesn't know about skills.
1. ArcAgent assembles system prompt once per `run()` call
2. Hands it to `arcrun.run(system_prompt=...)` along with the tool list (full schemas via provider's tool slot)
3. ArcRun reuses the same system prompt + tool list every turn
4. When LLM calls a skill's SKILL.md via `read`, that's just a normal tool call to ArcRun
5. On `reload()`, registries rebuild; next `run()` gets the new prompt

Same pattern as Claude Code and OpenClaw. Manifest is a session property, not a turn property.

---

## Decisions captured

1. **Modules are optional shipped bundles**, not a runtime concept. CLI handles enable/disable/install/uninstall.
2. **Tools have an optional `requires_skill` field.** When set, runtime auto-attaches the skill body when the tool is called. Most tools won't set it.
3. **Workspace boundary stays.** Agent can only write to `<agent_root>/workspace/`. Agent-authored capabilities land in `<agent_root>/workspace/.capabilities/`.
4. **Last-wins on name collisions** with audit. Lets the user override builtins by writing a better one.
5. **`requires_skill` is optional metadata** — only filled in if needed, then loaded.
6. **Skills are folders, one tier.** Multiple types adds complexity; consistency wins. Short sections OK, missing sections rejected, filler flagged.
7. **`triggers` is a semantic hint for the LLM**, not a runtime matcher. Field stays.
8. **Section headings are title-case** (`## Steps`, `## Knowledge`, `## Anti Patterns`). Validator hardcodes this.
9. **Update is its own skill+tool** (`update-skill`, `update-tool`). Update bumps `version` in frontmatter. Create starts at `1.0.0`.
10. **Skills/tools manifest in system prompt at session start, body lazy via `read`, one-shot semantics** — same pattern as Claude Code and OpenClaw.
11. **Skill usage instruction injected via bus**, not hardcoded in prompt builder.
12. **Policy is authoritative.** A skill's `tools: [...]` is descriptive metadata only. Policy decides what runs. If policy denies a listed tool, skill still registers, audit event fires, LLM discovers the gap at call time.

---

## What gets ripped out

- `ExtensionLoader` (`extensions.py`)
- `_load_modules_by_convention` (`agent.py`)
- `register_native_tools` (`tool_registry.py`)
- `MODULE.yaml` runtime parsing
- `[tools.native]` config block
- `make_create_tool_tool` (the in-memory-only one — replaced by `reload()` + persistence)
- Four parallel registration paths in `agent.py` startup
- Hardcoded `create_builtin_tools` list

Replaced by:
- One `CapabilityLoader.scan_and_register()`
- One `CapabilityRegistry` (tools + skills + hooks + background tasks all in it, kind discriminator)
- Three decorators (`@tool`, `@hook`, `@background_task`) + one optional class form (`@capability` with lifecycle)
- One `reload()` tool

Estimated net change: **-1,500 to -2,000 LOC** in `arcagent/core` and `arcagent/tools`.

---

## Open questions / parked items

- **Exact required frontmatter list** for tools and skills — stub above is a starting point, may need adjustment as we sketch templates.
- **`reload()` diff format** — string format for what the LLM sees as the return value (e.g. `"+3 -1 ~2: added create-tool, ...; removed legacy-grep; replaced read (1.0→1.1)"`).
- **`state.json` ownership** — split into skill-owned (`state.json`) vs runtime-owned (`.skill-meta.json`)? Or merge with reserved keys? Defer until we hit a real case.
- **Validation script security** — builtins skills' `scripts/validate.py` runs unconditionally; agent-authored skills' scripts go through AST validator first. Confirm this is the right cut.
- **Marketplace + signing flow for federal tier** — Sigstore + Rekor verification on `arc module install`. Already have primitives in arctrust. Detail later.
- **Lifecycle for memory/browser/scheduler** — `@background_task` covers periodic work; `@capability` class with `setup`/`teardown` covers heavy resources. Confirm both fit current modules' needs when we start migration.
- **Versioning** — semver vs simple int? Probably semver. `update_*` is the only path that bumps. Pre-release/build metadata defer.

---

## What this brainstorm does not do

- Pick a concrete migration order
- Sketch the `CapabilityLoader` interface in code
- Decide MCP / remote tool integration (mentioned earlier as a natural fit, not detailed)
- Address how arctrust integrates per-tier (mentioned as the gate, not detailed)

Those are next-step things. This is the shape we agreed on; code and plans come later.
