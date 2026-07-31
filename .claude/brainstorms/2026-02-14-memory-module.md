---
topic: ArcAgent Memory Module (Markdown Memory)
date: 2026-02-14
status: complete
prior_design: docs/arcagent-design-v3.md (Sections 1-2, 6-7)
---

# Memory Module Brainstorm

## Problem

ArcAgent has no memory persistence. The core infrastructure (Module Bus, Context Manager, Tool Registry) is built, but:
- No tools for the agent to read/write its own working memory (context.md)
- No daily notes (OpenClaw-style session logs)
- No entity storage or extraction
- No search across memory tiers
- Conversation history lost between run() calls (arcrun creates fresh messages each time)

Without memory, the agent can't maintain identity, remember user preferences, track entities, or learn across sessions.

## Who

- **Agents**: Need persistent memory to be useful beyond single-shot tasks
- **Developers**: Building with ArcAgent, expect agents that remember
- **Federal users**: Need auditable memory (file-based, git-diffable, no opaque databases)

## Inspiration

Extensively explored in `docs/arcagent-design-v3.md` Section 1:
- **OpenClaw**: `memory/YYYY-MM-DD.md` daily logs, `MEMORY.md` working memory, compaction flush
- **MemGPT/Letta**: Core memory (editable system prompt) + archival memory (vector search)
- **Martian agent-memory**: File-based entity extraction with JSONL fact logs
- **Zep, Mem0, Supermemory**: Various approaches to structured memory

ArcAgent combines OpenClaw's simplicity (markdown files, daily logs) with Martian's entity layer and MemGPT's tiered access pattern.

## Architecture (from design-v3.md)

### Three Tiers + Two Self-Editable System Files

1. **In-Context (always in system prompt)**
   - `identity.md` (WHO) — **self-editable with audit** (like MemGPT persona). Every change logged. Admin can review/revert.
   - `policy.md` (HOW) — **self-learning behavioral rules**. Starts empty. Agent evaluates its work, writes scored lessons. Ranked by effectiveness.
   - `context.md` (WHAT) — agent-curated working memory, ~2K token budget

2. **Daily Notes (OpenClaw pattern)**
   - `notes/YYYY-MM-DD.md` — append-only daily logs
   - Today + yesterday auto-loaded
   - Older notes via `memory_search`

3. **Entity Storage (Martian pattern)**
   - `entities/{name}/facts.jsonl` — append-only fact log
   - `entities/{name}/summary.md` — synthesized from active facts
   - `entities/index.json` — master index
   - Contradiction detection (same predicate, new value supersedes old)

### Identity Self-Editing (design-v3.md change)

Original design said `Modify identity.md → ❌ Requires Admin approval`. Updated to:

- Agent CAN edit its own identity.md (like MemGPT's self-editable persona block)
- Every change is audit-logged (who changed what, when, from what to what)
- Changes persist to disk → loaded into system prompt next session
- Admin can review audit trail and revert changes
- Use case: user says "your name is Olivia" → agent updates identity.md → persists across sessions

### Policy Self-Learning (design-v3.md Section 7)

The `policy.md` self-learning system is part of the memory module scope:

- **Starts empty** for every new agent
- **Evaluation triggers**: After multi-step tasks, user feedback, session end, every N turns (default 10)
- **Evaluation prompt**: Lightweight internal LLM call to assess what worked/didn't
- **Bullet format**: `- [lesson text] [score:N, reviewed:DATE, uses:N]`
- **Scoring**:
  - Positive outcome → score++, move up
  - Partially helpful → rewrite text, keep score
  - Not helpful/harmful → score-- (remove at ≤0)
  - Unused 30+ days → score decays by 1/period
- **Safety**: Agent can add/modify/remove policy bullets freely. Cannot modify identity without audit. Cannot override security or disable audit.

### Tool Strategy: Hooks, Not New Tools

**Decision**: Use Module Bus hooks on existing `read`/`write`/`edit` tools. Only 1 new tool.

The agent already has workspace-scoped `read`, `write`, `edit`, `bash`. Memory operations are just file edits on specific paths. The Module Bus intercepts these via `agent:pre_tool` / `agent:post_tool` events and adds behavior based on file path:

| File Path | Hook Behavior |
|-----------|--------------|
| `identity.md` | Audit log every change (before/after, timestamp, session) |
| `policy.md` | Validate bullet format + scoring metadata |
| `context.md` | Enforce ~2K token budget, reject if over |
| `notes/*.md` | Enforce append-only (reject overwrites/deletions) |

**One new tool**: `memory_search` — Hybrid BM25 + vector search across notes, entities, and context. This is genuinely new functionality that can't be expressed as a file edit.

**Entity extraction**: Not a tool at all. Async Module Bus subscriber on `agent:post_respond`. Automatic.

**Agent instructions**: identity.md tells the agent it can edit context.md, policy.md, identity.md, and notes/ to manage its memory. The agent uses its existing tools. The hooks handle validation, audit, and constraints transparently.

### Search

Full hybrid: BM25 keyword + vector similarity (sqlite-vec).
- Configurable weights (default 70/30 keyword/semantic)
- ~400 token chunks
- Per-agent SQLite database

### Entity Extraction

LLM-driven, async post-response:
- Fires on `agent:post_respond` via Module Bus
- Cheaper model extracts entities from conversation
- Checks index.json for existing match
- New entity → create facts.jsonl + summary.md
- Existing entity → append fact, detect contradictions

## Build Scope

All three tiers built together as a single Module Bus module:

```
arcagent/modules/memory/
  __init__.py
  markdown_memory.py       # Main module (Module Bus subscriber)
  entity_extractor.py      # LLM-driven NER
  hybrid_search.py         # BM25 + sqlite-vec
  MODULE.yaml              # Module manifest
```

## Also Required (Pre-requisites)

Two changes outside the memory module itself:

1. **arcrun**: Add `messages` parameter to `run()` so conversation history persists across calls
2. **arcagent agent.py**: Add `chat()` method for multi-turn conversation with persistent message history

These enable within-session memory. The memory module adds cross-session persistence.

## Principles

- **Files, not databases** — Auditable, git-diffable, portable
- **Async, not blocking** — Entity extraction doesn't slow responses
- **Agent-curated** — The agent manages its own context.md, not a background process
- **Contradiction-aware** — Supersedes chains, not silent overwrites
- **Pluggable** — MemoryProvider protocol allows swapping to Neo4j/Graphiti later

## Next Steps

- `/build` — Walk through implementation decisions for the memory module
- `/specify` — Formal spec (PRD → SDD → PLAN)
