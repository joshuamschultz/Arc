---
topic: Convention-Driven System Prompt Injection — Tools & Team Roster
date: 2026-02-27
status: brainstormed
prior_work:
  - .claude/brainstorms/2026-02-17-arcteam-messaging.md (team messaging decisions)
related_code:
  - packages/arcagent/src/arcagent/core/context_manager.py (prompt assembly)
  - packages/arcagent/src/arcagent/core/tool_registry.py (tool metadata)
  - packages/arcagent/src/arcagent/core/agent.py (skill prompt injection pattern)
  - packages/arcagent/src/arcagent/modules/messaging/__init__.py (existing prompt injection)
  - packages/arcteam/src/arcteam/registry.py (entity registry)
---

# Convention-Driven System Prompt Injection

## Inspiration

Agents don't know about their tools or teammates unless someone manually writes it into a markdown file. The system should "just know" — when tools are registered, the agent automatically knows what it has. When teammates are registered, the agent automatically knows who's on the team.

**Root problem:** Manual prompt maintenance doesn't scale. Every time a module registers new tools or a teammate joins, someone has to update identity.md or context.md. That's fragile and guaranteed to drift.

**Solution:** Convention-driven injection. Registries hold the metadata. The system prompt is assembled from live registry state, not static files.

---

## Mental Model

Two catalogs, auto-injected into the system prompt:

| Catalog | Source | Owner | Injection Point |
|---------|--------|-------|-----------------|
| **Tool Catalog** | `ToolRegistry._tools` | arcagent core | `agent:assemble_prompt` event |
| **Team Roster** | `EntityRegistry` (arcteam) | messaging module | `agent:assemble_prompt` event |

Both follow the same pattern:
1. Registry holds rich metadata
2. A formatter renders it for the prompt
3. A bus subscriber injects the formatted text
4. Result is cached and invalidated on change

---

## Decisions

### 1. Tool Metadata Lives on RegisteredTool

**Decision:** Add optional fields to `RegisteredTool`:
- `when_to_use: str` — Guidance on when the LLM should reach for this tool
- `example: str` — Brief example of typical usage/arguments
- `category: str` — Grouping label (e.g., "messaging", "filesystem", "team")

These are optional. Tools without them still appear in the catalog with just `name` + `description`. Tool authors provide the metadata at registration time (via `@native_tool` decorator, `RegisteredTool` constructor, or MODULE.yaml).

### 2. Tool Catalog Prompt Structure

**Decision:** Static preamble + dynamic list.

```
## Available Tools

You have the following tools available. Use them as needed to accomplish your tasks.

| Tool | Purpose | When to Use |
|------|---------|-------------|
| messaging_send | Send a message... | When you need to contact... |
| read_file | Read file contents... | When you need to examine... |
| ... | ... | ... |
```

- Static preamble explains the catalog and general guidance
- Dynamic rows auto-generated from `ToolRegistry._tools`
- Columns: name, description (truncated), when_to_use (if set)
- If `example` is set, append a brief example line

### 3. Team Roster Injected by Messaging Module

**Decision:** The existing messaging module handles team roster injection alongside its current messaging context injection. Both are "team awareness" — same concern.

The roster section:
```
## Team Roster

You are on a team. Here are your teammates:

| Name | ID | Roles | Capabilities | Status |
|------|-----|-------|-------------|--------|
| Brad | agent://brad | executor, researcher | code_review, testing | online |
| ... | ... | ... | ... | ... |

To message a teammate, use `messaging_send(to="agent://brad", body="...")`.
```

- Shows ALL registered entities (not just online)
- Includes roles and capabilities for routing decisions
- Includes status so the agent can factor in availability

### 4. Cache Until Change

**Decision:** Format the prompt sections once, cache the text. Invalidate on:
- **Tools:** `agent:tools_reloaded`, tool registration/removal
- **Roster:** Entity registration, status change, role change

Cache is just a string. Invalidation sets it to `None`, next `agent:assemble_prompt` rebuilds it.

### 5. Separation of Concerns

**Decision:** Respects the existing architecture boundary:

| Concern | Where |
|---------|-------|
| Tool catalog injection | `ToolRegistry` (arcagent core) — it owns tool metadata |
| Team roster injection | `MessagingModule` (arcagent module) — it already bridges arcteam |
| Team roster data | `EntityRegistry` (arcteam) — standalone, no arcagent dependency |

arcteam's EntityRegistry is the data source. arcagent's messaging module reads it and formats it for the prompt. arcteam itself has no knowledge of system prompts.

---

## Use Cases

### 1. Agent with Custom Module Tools
A module registers tools via `tool_registry.register()`. The tool catalog auto-updates. The agent immediately knows the new tools exist and when to use them.

### 2. Agent Joins a Team
Messaging module starts, reads the entity registry, injects the roster. The agent sees all teammates, their roles, and can message them without being told who they are.

### 3. Hot Reload
Agent calls `reload()`. Tools are re-discovered. Tool catalog cache is invalidated. Next prompt assembly picks up the new tool landscape.

### 4. New Teammate Registers
Another agent registers in the entity registry. On next prompt assembly (next turn), the messaging module sees the new entity and includes it in the roster.

---

## Outcomes

When this exists:
- **Zero manual prompt maintenance** for tool and team awareness
- **Convention over configuration** — register a tool and it "just appears" in the agent's knowledge
- **Consistent across all agents** — every agent on the team sees the same roster
- **Scalable** — adding 50 tools or 20 teammates doesn't require updating markdown files
- **Auditable** — the prompt sections are deterministic from registry state

---

## Guiding Principles

1. **Convention over configuration** — Registering a tool or entity IS the configuration. No separate step.
2. **The system should just know** — No manual markdown updates. Live from registries.
3. **Rich metadata at source** — Tool authors declare `when_to_use` at registration, not in a separate file.
4. **Cache for performance, invalidate on change** — Don't rebuild every turn, but always be fresh.
5. **Respect package boundaries** — Tool catalog is arcagent. Roster data is arcteam. Injection bridge is the messaging module.

---

## Constraints

- Tool catalog must work WITHOUT arcteam (single agent, no team)
- Team roster injection only activates when messaging module is enabled
- Token budget: catalog + roster should stay under ~500 tokens for a typical setup
- Must not break existing `identity.md` / `context.md` flow — additive only

---

## Existing Patterns to Follow

The **skill prompt injection** pattern in `agent.py:462-482` is the exact model:
1. Subscribe to `agent:assemble_prompt`
2. Format registry contents
3. Inject into `sections["skills"]`

Tool catalog follows the same pattern → `sections["tools"]`
Team roster follows the same pattern → already in `sections["messaging"]`, extend it

---

## Next Steps

- `/build` — Design the RegisteredTool field additions, formatter API, cache invalidation events
- `/specify` — Formal spec with test cases
