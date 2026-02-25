# Entity Behavior Analysis — my_agent

**Date:** 2026-02-23
**Issue:** Agent creating entity files in `workspace/entities/` despite `[modules.memory] enabled = false`

---

## Root Cause

The old `EntityExtractor` module is **not running**. The agent is writing entity files via direct `write` tool calls because `identity.md` explicitly instructs it to.

## Three Sources of Entity Behavior

### 1. identity.md — "Entity Management (CRITICAL)" section (lines 89-121)

The largest driver. Contains:
- "ALWAYS be looking for opportunities to create or update entity profiles"
- Detailed rules: when to create, what to include, naming conventions
- Proactive entity building instructions ("If user says X → create Y")
- Workspace layout listing `entities/` as a known directory

### 2. memory/how-i-work.md (line 9)

Bio-memory's identity file reinforces it:
> "Create entity profiles proactively for people, companies, and projects mentioned"

### 3. PULSE.md — `update_entities` check

A 30-minute cron pulse check:
> `update_entities` — 30 min — review sessions for entity updates

This means the agent periodically scans sessions and creates/updates entity files even without being asked.

## Evidence

- `erika-schultz.md` modified 2026-02-22 at 05:54 UTC
- File is **plain markdown** (headers + bullets), not YAML frontmatter — confirms it was a direct `write` tool call, not `EntityExtractor`
- Session `b55709a9` shows the agent saying "Updated Erika's profile" after user provided anniversary details
- `arcagent.toml` confirms `[modules.memory] enabled = false`

## Old EntityExtractor vs What's Happening

| Aspect | Old EntityExtractor | Current Behavior |
|--------|-------------------|------------------|
| **Trigger** | `agent:post_respond` bus event | Direct `write` tool call by LLM |
| **Format** | YAML frontmatter + fact lines with confidence scores | Plain markdown with headers/bullets |
| **Control** | `entity_extraction_enabled` config flag | identity.md instructions |
| **Module** | `MarkdownMemoryModule` (disabled) | No module — agent follows prompt instructions |

## What to Change

To stop entity file creation:

1. **identity.md** — Remove or modify the "Entity Management (CRITICAL)" section (lines 89-121) and the `entities/` line in workspace layout (line 57) and file creation guidelines (line 127)
2. **memory/how-i-work.md** — Remove line 9 about proactive entity creation
3. **PULSE.md** — Remove the `update_entities` pulse check
4. **pulse-state.json** — Remove the `update_entities` entry

To redirect entity behavior to bio_memory's episode system instead, replace the entity instructions with guidance to use `memory_note` tool with `target: episode`.

## What Else Dies if memory Module is Disabled

Everything in `MarkdownMemoryModule` stops:

| Feature | Effect When Disabled |
|---------|---------------------|
| Entity extraction (LLM-driven) | No automatic YAML entity files |
| Notes (append-only daily notes) | No notes protection, no auto-creation |
| Context.md budget enforcement | Writes to context.md are unbounded |
| Identity.md audit trail | No before/after snapshots, no JSONL audit log |
| Memory guidance in prompt | Agent won't get "You have persistent memory" instructions |
| `memory_search` tool (old) | Tool not registered (bio_memory registers its own) |
| Pre-compaction daily notes | No auto-creation of notes file before compaction |
| Bash command veto on memory files | No protection against rm/sed on old memory paths |

## What bio_memory Provides Instead

| Old Memory Feature | Bio Memory Equivalent |
|-------------------|----------------------|
| Entity extraction → standalone files | Entity names in episode frontmatter |
| Notes (daily .md files) | Working memory (working.md) |
| Context.md budget | Identity budget (how-i-work.md) |
| memory_search tool | memory_search (grep + wiki-link graph) |
| — | memory_note, memory_recall, memory_reflect tools |
| — | Shutdown consolidation (significance eval → episode creation) |
| — | Identity reflection (how-i-work.md updates) |

## Old Entity Files (workspace/entities/)

These files remain from before the switch and serve as "learned convention" for the LLM. Consider:
- Migrating useful data into bio_memory episodes
- Removing the directory to break the pattern
- Or keeping them as read-only reference with instructions not to write there
