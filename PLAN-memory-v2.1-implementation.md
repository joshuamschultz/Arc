# Plan: Complete ARC Memory System v2.1 Implementation

## Context

The bio_memory module implements ~20/68 requirements from the ARC Memory System v2.1 spec. The user requested implementation of gaps 2, 3, 4, and 6 — the entity format, arcagent-arcteam wiring, complete light consolidation, and deep consolidation engine. Gap 5 (automatic retrieval) is explicitly excluded — the user wants LLM-driven retrieval via `memory_search` tool calls.

Currently:
- **Retriever** only searches `memory/` (episodes, working.md, how-i-work.md) — NOT `workspace/entities/`
- **Consolidator** does significance eval + episode creation + identity update + clear working.md — but NO entity updates, co-occurrence linking, or new entity stubs
- **Deep consolidation** doesn't exist at all
- **Entity files** are LLM-created without YAML frontmatter or wiki-links (plain markdown)
- **arcteam** has a full TeamMemoryService with promotion gate, BM25 search, and index — but bio_memory doesn't use it

## Implementation Order

### Phase 1: Entity Format + Retriever Scope (Gap 6 + prerequisite for all)

**Files:**
- `packages/arcagent/src/arcagent/modules/bio_memory/config.py` — add `entities_dirname` config field
- `packages/arcagent/src/arcagent/modules/bio_memory/retriever.py` — add "entities" scope to `_discover_files()`, add wiki-link resolution across entities
- `packages/arcagent/src/arcagent/modules/bio_memory/bio_memory_module.py` — add "entities" to `memory_search` scope enum, update context injection to mention entity locations

**Changes:**

1. **BioMemoryConfig** — add `entities_dirname: str = "entities"` (default: `entities` relative to workspace, NOT memory_dir). Add `entities_dir_name` for the workspace-level entities directory.

2. **Retriever._discover_files()** — add `"entities"` scope. When scope is None or "entities", glob `workspace/entities/**/*.md`. The retriever currently uses `self._memory_dir` which is `workspace/memory/`. Entities live at `workspace/entities/`. Solution: pass workspace path to Retriever (not just memory_dir).

3. **Retriever._follow_wiki_links()** — resolve `[[wiki-links]]` against entities directory too (currently only resolves within memory_dir). Check `workspace/entities/{slug}.md` and subdirectories.

4. **BioMemoryModule._on_assemble_prompt()** — add entity location hint to context so the agent knows to call `memory_search` when it needs entity data. Add a small injected note: "Entity files available at workspace/entities/. Use memory_search to find relevant entities."

5. **BioMemoryModule memory_search tool** — add `"entities"` to the `scope` enum options.

### Phase 2: arcagent-arcteam Wiring (Gap 2)

**Files:**
- `packages/arcagent/src/arcagent/modules/bio_memory/bio_memory_module.py` — accept `team_config`, lazy-init TeamMemoryService
- `packages/arcagent/src/arcagent/modules/bio_memory/config.py` — add team memory settings
- `packages/arcagent/src/arcagent/modules/bio_memory/retriever.py` — add team entity search capability

**Changes:**

1. **BioMemoryModule.__init__()** — accept optional `team_config` parameter (convention-based injection from `TeamSection`). Store `self._team_root`. Lazy-init a `TeamMemoryService` via `_get_team_service()` (import arcteam.memory inside method, since arcteam is an optional dependency).

2. **Retriever** — add optional `team_entities_dir` parameter. When searching with scope=None or scope="entities", also search team entities if available. Team entity results get a slightly lower score boost to prefer agent-local knowledge.

3. **BioMemoryModule memory_search tool** — when `scope="entities"`, search both agent workspace entities AND team entities (if team configured).

4. **Consolidator** — accept optional `team_service` for promotion gate calls during consolidation.

### Phase 3: Complete Light Consolidation (Gap 3)

**Files:**
- `packages/arcagent/src/arcagent/modules/bio_memory/consolidator.py` — add LC-4 through LC-7

**Changes to `light_consolidate()`:**

Current flow:
1. Evaluate significance -> 2. Create episode -> 3. Identity update -> 4. Clear working.md

New flow (after step 2, before identity update):
1. Evaluate significance
2. If significant: create episode
3. **LC-4: Update touched entities** — LLM identifies entities referenced in session. For each, update `last_verified` in YAML frontmatter and append to "Recent Activity" section. Uses a single LLM call to identify entities + generate updates.
4. **LC-5: Correction handling** — LLM detects corrections in conversation (user correcting agent behavior/knowledge). If found, update the relevant entity file directly (e.g., add correction to "Constraints and Lessons" section).
5. **LC-6: Co-occurrence linking** — entities that appeared together in this session but aren't linked -> add bidirectional `[[wiki-links]]` to each file's `links_to` frontmatter. Simple set comparison, no LLM needed.
6. **LC-7: New entity stubs** — if the LLM identifies new entities not yet in the graph, create stub files with v2.1 schema. If team service available, use promotion gate for team-relevant entities.
7. Evaluate identity update (existing)
8. Clear working.md (existing)

**New method: `_update_entities()`**
- Single LLM call that analyzes the conversation and returns:
  ```json
  {
    "touched_entities": ["josh-schultz", "ctg-federal"],
    "corrections": [{"entity": "pricing", "correction": "Show methodology first"}],
    "new_entities": [{"id": "new-project", "type": "project", "summary": "..."}],
    "co_occurrences": [["josh-schultz", "ctg-federal"]]
  }
  ```
- Process each result: update frontmatter, append activity, create stubs
- Entity file I/O uses existing `atomic_write_text` + `read_frontmatter` from utils

**New helper: `_normalize_entity_file()`**
- Reads an entity file. If it lacks YAML frontmatter (legacy LLM-created format), adds v2.1 frontmatter (infers entity_type, creates entity_id from filename slug, sets initial links_to/tags).
- Called before any entity update to ensure consistent format.

### Phase 4: Deep Consolidation Engine (Gap 4)

**New file:** `packages/arcagent/src/arcagent/modules/bio_memory/deep_consolidator.py`

This is the "sleep cycle" — runs on schedule (via CLI trigger or scheduler) or manual invocation.

**Class: `DeepConsolidator`**

Constructor: `(memory_dir, workspace, config, telemetry, team_service?)`

**Method: `consolidate(model, agent_id)`** — orchestrates the full cycle:

1. **Activity check** (DC-2) — count recent episodes. If zero, skip. Adapt intensity: few episodes = light pass, many = full pass.

2. **Pass 1: Entity-Centric** (DC-3, DC-4)
   - Find entities touched by recent episodes (grep episode files for entity mentions)
   - For each touched entity: LLM reads entity file + all referencing episodes -> rewrites the file
   - Episode-mediated link discovery: LLM adds `[[wiki-links]]` for entities mentioned across episodes
   - Enforce per-entity token budget (800 tokens default)

3. **Pass 2: Graph-Centric** (DC-5, DC-6, DC-7, DC-8)
   - Select a domain cluster (entities sharing tags or link neighborhoods)
   - Feed LLM frontmatter + summary of each entity in cluster (~100 tokens each)
   - LLM discovers structural connections not currently linked
   - Add bidirectional wiki-links for confirmed connections
   - Rotate domains across cycles (track last scanned domain in state file)

4. **Merge detection** (DC-9) — entity pairs with 3+ shared links -> LLM judges "same entity?" -> merge

5. **Staleness** (DC-10) — entities past TTL with no recent access -> flag as stale

6. **how-i-work.md refresh** (DC-12) — LLM reads current identity + recent episodes -> synthesizes patterns -> rewrite within budget

7. **Index rebuild** (DC-13) — if team service available, trigger `rebuild_index()`

8. **Telemetry** (DC-15) — emit detailed audit events for each operation

**Registration:**
- Register `memory_consolidate_deep` tool in bio_memory_module.py (LLM-invocable)
- Add CLI command for manual trigger
- Optionally trigger via scheduler/pulse

### Phase 5: Tests

**New/Updated test files:**
- `tests/unit/modules/bio_memory/test_retriever.py` — add tests for entity scope search
- `tests/unit/modules/bio_memory/test_consolidator.py` — add tests for LC-4 through LC-7
- `tests/unit/modules/bio_memory/test_deep_consolidator.py` — new file for deep consolidation
- `tests/unit/modules/bio_memory/test_config.py` — test new config fields

## Key Design Decisions

1. **arcteam is optional** — all arcteam imports are lazy (inside methods). If arcteam isn't installed, team features silently degrade. This matches the messaging module pattern.

2. **Entities live at workspace/entities/ (agent level)** — NOT inside memory/. This matches current LLM-created entity behavior. Team entities are at `team.root/entities/` (resolved via team_config).

3. **Entity normalization is lazy** — entity files only get v2.1 frontmatter when consolidation touches them. Existing LLM-created files work as-is for search (full-text grep still finds them). This avoids a disruptive migration.

4. **Single LLM call for entity analysis** — LC-4/5/6/7 use one LLM call to analyze the session and identify all entity operations, rather than multiple calls. Cost-efficient.

5. **Deep consolidation is manual/scheduled** — not automatic. User triggers via tool, CLI, or scheduler. This keeps the system predictable.

6. **Retriever gets workspace path** — currently only has `memory_dir`. Needs `workspace` to access `workspace/entities/`. Added as constructor parameter.

## Files Modified

| File | Changes |
|------|---------|
| `bio_memory/config.py` | Add `entities_dirname`, `per_entity_budget`, `deep_consolidation_*` fields |
| `bio_memory/retriever.py` | Add entity scope, workspace path, team entity search |
| `bio_memory/consolidator.py` | Add `_update_entities()`, `_normalize_entity_file()`, co-occurrence linking |
| `bio_memory/bio_memory_module.py` | Accept `team_config`, entity scope in tools, context injection, deep consol tool |
| `bio_memory/deep_consolidator.py` | **NEW** — full deep consolidation engine |
| `bio_memory/__init__.py` | Export new classes |

## Reusable Existing Code

- `arcagent.utils.io.atomic_write_text` — all file writes
- `arcagent.utils.io.extract_json` — LLM response parsing
- `arcagent.utils.io.format_messages` — conversation formatting
- `arcagent.utils.sanitizer.sanitize_text` — content sanitization
- `arcagent.utils.sanitizer.slugify` — entity ID generation
- `arcagent.utils.sanitizer.read_frontmatter` — YAML frontmatter parsing
- `arcagent.utils.sanitizer.sanitize_wiki_link` — wiki-link slug validation
- `arcagent.utils.model_helpers.spawn_background` — background task management
- `arcteam.memory.service.TeamMemoryService` — team entity operations (lazy import)
- `arcteam.memory.types.EntityMetadata` — entity schema for promotion gate

## v2.1 Entity File Schema (Target Format)

```markdown
---
entity_type: contact
entity_id: josh-schultz
name: Josh Schultz
status: active
last_updated: 2026-02-21
last_verified: 2026-02-21
created: 2025-09-15
links_to: ["[[brad-baker]]", "[[blackarc-systems]]", "[[ctg-federal]]"]
linked_from: ["[[blackarc-systems]]", "[[arcagent]]"]
tags: [contact, leadership, founder]
source_agents: [my_agent]
classification: unclassified
---

# Josh Schultz

## Summary
Founder of BlackArc Systems. Works at CTG Federal and CTG National.
Reports to Brad Baker (CTO). Building ArcAgent.

## Key Facts
- Home timezone: CST
- Prefers 12-hour time format with AM/PM
- Values doing things right over fast/easy

## Constraints and Lessons
- Prefers long-term quality over quick wins
- Don't make code changes when he's just asking questions to understand

## Recent Activity
- 2026-02-22: Discussed memory system implementation gaps
```

## Verification

1. **Unit tests pass**: `cd packages/arcagent && python -m pytest tests/unit/modules/bio_memory/ -v`
2. **Full test suite**: `cd packages/arcagent && python -m pytest`
3. **Type check**: `cd packages/arcagent && python -m mypy src/arcagent/modules/bio_memory/ --strict`
4. **Lint**: `cd packages/arcagent && python -m ruff check src/arcagent/modules/bio_memory/`
5. **Manual verification**: Start an agent, have a conversation mentioning entities, shutdown, verify:
   - Episode created with wiki-links
   - Entity files updated with last_verified and Recent Activity
   - working.md cleared
   - how-i-work.md updated if behavioral insights detected

## Reference Documents

- **v2.1 Spec**: `packages/arcagent/.claude/ARC-Memory-System-v2.1-Final.md`
- **Original Brainstorm**: `packages/arcagent/.claude/brainstorms/2026-02-14-memory-module.md`
- **arcteam Memory**: `packages/arcteam/src/arcteam/memory/` (service, promotion_gate, types, storage, search_engine, index_manager)
