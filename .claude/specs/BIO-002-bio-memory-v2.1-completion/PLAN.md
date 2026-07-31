# PLAN: Bio-Memory v2.1 Completion (BIO-002)

**Status**: COMPLETE
**Spec**: BIO-002
**Phases**: 5
**Estimated Files**: 2 new, 6 modified, 5 test files

---

## Phase 1: Entity Format + Retriever Scope

### 1A: Config Extension

- [x] **1A.1** Add entity/deep consolidation fields to `BioMemoryConfig`
  - `entities_dirname: str = "entities"`
  - `per_entity_budget: int = 800`
  - `deep_max_entities: int = 50`
  - `deep_cluster_size: int = 20`
  - `staleness_ttl_days: int = 90`
  - `archive_dirname: str = "archive"`
  - `rotation_state_file: str = ".consolidation-state.json"`
  - Test: update `tests/unit/modules/bio_memory/test_config.py`

### 1B: Retriever Entity Scope

- [x] **1B.1** Add `workspace` and `team_entities_dir` parameters to `Retriever.__init__()`
  - Default: `workspace = memory_dir.parent`
  - Compute `_entities_dir = workspace / config.entities_dirname`
  - Store optional `_team_entities_dir`

- [x] **1B.2** Update `_discover_files()` — add `"entities"` scope
  - When scope is None or "entities": glob `workspace/entities/**/*.md`
  - When team_entities_dir exists: also glob team entities (with `_TEAM_SCORE_PENALTY`)
  - Keep existing episodes/working/identity scopes unchanged

- [x] **1B.3** Update `_follow_wiki_links()` — resolve against entities directory
  - After checking episodes + memory_dir, check `entities_dir/{slug}.md`
  - Check subdirectories: `entities_dir/**/{slug}.md`
  - If team configured: check `team_entities_dir/{slug}.md`
  - IMPORTANT (security research): only follow links that resolve to existing files. Unknown links are NOT auto-created during traversal (entity registry defense against dangling link injection)

- [x] **1B.4** Update `recall()` — add entity path resolution
  - After checking episodes + memory_dir, check `entities_dir/{name}.md`
  - Check subdirectories
  - Add `_is_within_bounds(path)` method that validates against both memory_dir and entities_dir

- [x] **1B.5** Apply team score penalty for team entity results
  - In `_fulltext_grep()`: if file is under `_team_entities_dir`, apply `* 0.8` score penalty
  - Test: `tests/unit/modules/bio_memory/test_retriever.py` — add entity scope tests

### 1C: Module Integration

- [x] **1C.1** Update `BioMemoryModule.__init__()` — pass workspace to Retriever
  - `self._retriever = Retriever(self._memory_dir, self._config, workspace=self._workspace)`

- [x] **1C.2** Update `memory_search` tool — add `"entities"` to scope enum
  - Schema: `"enum": ["episodes", "identity", "working", "entities"]`

- [x] **1C.3** Update `_on_assemble_prompt()` — add entity location hint
  - Append to memory context: "Entity files available at workspace/entities/. Use memory_search with scope='entities' to find relevant entities."

- [x] **1C.4** Update `_MEMORY_SUBPATHS` — add `"entities/"` to protected paths

---

## Phase 2: arcagent-arcteam Wiring

- [x] **2.1** Add `team_config` parameter to `BioMemoryModule.__init__()`
  - Store `self._team_config: dict[str, Any] | None`
  - Store `self._team_service: Any = None`

- [x] **2.2** Implement `_get_team_service()` — lazy TeamMemoryService init
  - Import `arcteam.memory.config.TeamMemoryConfig` and `arcteam.memory.service.TeamMemoryService` inside method
  - Catch `ImportError` — return None (arcteam not installed)
  - Catch other exceptions — log warning, return None
  - Cache on `self._team_service`

- [x] **2.3** Implement `_get_team_entities_dir()` — resolve team entities path
  - From `team_config`, extract root path
  - Return `Path(root) / "entities"` if exists, else None

- [x] **2.4** Pass team_entities_dir to Retriever
  - `self._retriever = Retriever(..., team_entities_dir=self._get_team_entities_dir())`

- [x] **2.5** Pass team_service_factory to Consolidator
  - Add `team_service_factory` parameter to Consolidator
  - Pass `self._get_team_service` as the factory callable
  - Test: `tests/unit/modules/bio_memory/test_bio_memory_module.py` — add team config tests

---

## Phase 3: Complete Light Consolidation

### 3A: Entity Analysis

- [x] **3A.1** Add `workspace` parameter to `Consolidator.__init__()`
  - Store `self._workspace` and compute `self._entities_dir`

- [x] **3A.2** Implement `_pre_filter_significance()` — deterministic gate (from SimpleMem research)
  - Gate 1: skip LLM if < 3 messages or no signal words (correct, change, update, decide, etc.)
  - Wire into `light_consolidate()` before existing `_evaluate_significance()`
  - Reduces unnecessary LLM calls

- [x] **3A.3** Implement `_analyze_entities()` — single LLM call
  - Prompt analyzes conversation for: touched_entities, corrections, new_entities, co_occurrences
  - Returns parsed dict (all fields optional, default to empty lists)
  - Boundary marker with session UUID
  - Sanitize all LLM output

### 3B: Entity Updates (LC-4)

- [x] **3B.1** Implement `_normalize_entity_file(path)` helper
  - Read file content
  - If has frontmatter (`---` prefix): return as-is
  - Infer entity_type from parent directory name
  - Generate entity_id from filename slug
  - Extract first H1 for name
  - Build v2.1 YAML frontmatter
  - Prepend to existing content
  - Atomic write

- [x] **3B.2** Implement `_resolve_entity_path(entity_slug)` helper
  - Check `entities_dir/{slug}.md`
  - Check `entities_dir/**/{slug}.md` (subdirectories)
  - Return resolved Path or None
  - Validate path within workspace bounds

- [x] **3B.3** Implement `_update_touched_entities(touched_list)`
  - For each entity slug: resolve path, normalize, update `last_verified`, append Recent Activity
  - Use `read_frontmatter()` + YAML roundtrip for frontmatter updates
  - Atomic write each file
  - Audit event per entity update

### 3C: Corrections (LC-5)

- [x] **3C.1** Implement `_apply_corrections(corrections_list)`
  - For each correction: resolve entity path, normalize file
  - Append to "## Constraints and Lessons" section
  - If section doesn't exist: create it before "## Recent Activity"
  - Sanitize correction text
  - Atomic write + audit event

### 3D: Co-occurrence Linking (LC-6)

- [x] **3D.1** Implement `_add_co_occurrence_links(co_occurrence_pairs)`
  - For each pair: read both files' frontmatter
  - Check `links_to` for existing link (both files — `linked_from` is NOT stored, computed from index per research)
  - If not linked: append `"[[entity-b]]"` to entity-a's `links_to`, and vice versa
  - Rewrite frontmatter sections
  - Atomic write both files
  - No LLM call — pure set comparison
  - Rate-limit: max 10 new links per session (entity registry defense)
  - Audit event per new link

### 3E: New Entity Stubs (LC-7)

- [x] **3E.1** Implement `_create_entity_stubs(new_entities_list, agent_id)`
  - For each new entity:
    - Validate with `sanitize_wiki_link(entity_id)`
    - Skip if file already exists (entity registry defense)
    - Rate-limit: max 3 new entities per session (prevents link flood attacks — from security research)
    - Determine directory: `entities_dir/{entity_type}s/` or `entities_dir/` (D-022: entity_type maps to subdirectory)
    - Create stub with v2.1 schema (frontmatter + sections). NOTE: `linked_from` NOT stored in files (computed from index)
    - If team service available: call `team_service.promote()`
    - Audit event per stub created

### 3F: Integration

- [x] **3F.1** Wire `_analyze_entities()` into `light_consolidate()` flow
  - Call after `_create_episode()`, before `_evaluate_identity_update()`
  - Pass results to each sub-handler
  - All entity operations are non-blocking on failure (log + continue)
  - Test: `tests/unit/modules/bio_memory/test_consolidator.py` — add LC-4..7 tests

---

## Phase 4: Deep Consolidation Engine

### 4A: Core Class

- [x] **4A.1** Create `bio_memory/deep_consolidator.py` — `DeepConsolidator` class
  - Constructor: `memory_dir, workspace, config, identity, telemetry, team_service_factory`
  - Store entities_dir, archive_dir, rotation state path
  - Implement `consolidate(model, agent_id)` orchestrator method

- [x] **4A.2** Implement `_find_recent_episodes()` and `_compute_intensity()`
  - Glob episodes, parse date from frontmatter
  - 0 = skip, 1-3 = light, 4+ = full
  - Configurable lookback window

### 4B: Entity-Centric Pass (DC-3, DC-4)

- [x] **4B.1** Implement `_find_touched_entities(episodes)` and `_prioritize_entities()`
  - Grep episodes for `[[wiki-links]]` and entity_refs in frontmatter
  - Resolve to entity file paths
  - Deduplicate
  - Prioritize by: (1) oldest `last_updated`, (2) most pending episodes, (3) highest link count (from decisions log)

- [x] **4B.2** Implement content-hash gating helpers
  - `_compute_hash(content)` — SHA-256 of entity + episode input
  - `_hash_matches(entity_id, hash)` — compare against stored hash in state file
  - `_update_hash(entity_id, hash)` — update state file
  - From research: 80-90% cost reduction by skipping unchanged entities

- [x] **4B.3** Implement `_rewrite_entity(entity_content, episode_summaries, model)`
  - LLM prompt uses the entity rewrite safety pattern (from decisions log):
    "For each fact you drop, explicitly state why (superseded, redundant, or contradicted)"
  - Request bidirectional wiki-links for newly discovered connections
  - Stay within per-entity token budget
  - Boundary marker with UUID
  - Return updated markdown body (no frontmatter — prompt says "output ONLY the updated markdown body")

- [x] **4B.4** Implement `_validate_rewrite(content, entity_path)` — 5-step validation (from research)
  1. Parse output as markdown (no syntax errors)
  2. Check word count against budget (reject if >110% of budget)
  3. Verify no frontmatter in output (prompt says "no frontmatter")
  4. Extract wiki-links, verify all reference existing files
  5. If validation fails: retry once with explicit error, then skip and log warning

- [x] **4B.5** Implement write-ahead manifest helpers
  - `_write_manifest(entity_ids)` — write `pending_entities.json`
  - `_remove_from_manifest(entity_id)` — remove entry on success
  - `_clear_manifest()` — delete manifest on completion
  - `_resume_from_manifest()` — on startup, check for pending manifest and resume
  - From research: crash safety for multi-file operations

- [x] **4B.6** Implement `_enforce_entity_budget(content)`
  - Estimate tokens using CHARS_PER_TOKEN
  - If over budget: truncate (LLM was asked to stay within budget, this is safety net)

- [x] **4B.7** Implement `_entity_centric_pass(episodes, model, agent_id)`
  - Write manifest, iterate touched entities (up to deep_max_entities)
  - Content-hash gate → LLM rewrite → 5-step validate → enforce budget → atomic write
  - Remove from manifest on success
  - Process sequentially (from research: avoid coordination complexity for entities that reference each other)
  - Collect audit data

### 4C: Graph-Centric Pass (DC-5..8)

- [x] **4C.1** Implement `_load_rotation_state()` and `_save_rotation_state()`
  - Read/write JSON state file
  - Track last_domain, domains_scanned, cycle_count

- [x] **4C.2** Implement `_select_cluster(last_domain)`
  - Enumerate entity directories (domains, by tag overlap, by link neighborhood)
  - Skip last-scanned domain
  - Prefer domains with recent activity (check last_updated in frontmatter)
  - Return list of entity paths (up to cluster_size)

- [x] **4C.3** Implement `_extract_summary(entity_path)`
  - Read file, extract text from "## Summary" to next "##"
  - Truncate to ~100 tokens
  - Return frontmatter + summary text

- [x] **4C.4** Implement `_discover_structural_links(summaries, model)`
  - Format summaries into prompt
  - LLM discovers connections not currently linked
  - Return list of `{from: entity_id, to: entity_id, reason: str}`
  - Sanitize all LLM output

- [x] **4C.5** Implement `_add_bidirectional_link(entity_a, entity_b)`
  - Read both files' frontmatter
  - Add `[[entity-b]]` to entity-a's `links_to` if not present
  - Add `[[entity-a]]` to entity-b's `links_to` if not present
  - Rewrite frontmatter + atomic write both files

- [x] **4C.6** Implement `_graph_centric_pass(model)`
  - Orchestrate cluster selection → summary extraction → LLM discovery → link addition
  - Update rotation state

### 4D: Structural Maintenance

- [x] **4D.1** Implement `_detect_merges(model)` (DC-9)
  - Build adjacency map from all entity `links_to`
  - Find pairs sharing 3+ links
  - For each candidate pair: LLM judges "same entity?"
  - If yes: merge content, update all referencing files' links
  - Atomic writes
  - Return merge count

- [x] **4D.2** Implement `_flag_stale_entities()` (DC-10)
  - Check `last_verified` against `staleness_ttl_days`
  - If stale: set `status: stale` in frontmatter
  - If stale + 2x TTL without access: move to `workspace/archive/`
  - Return list of stale/archived entities

- [x] **4D.3** Implement `_refresh_identity(episodes, model)` (DC-12)
  - Read current how-i-work.md + recent episode narratives
  - LLM synthesizes patterns
  - Enforce identity_budget
  - Atomic write if changed
  - Return whether updated

### 4E: Registration

- [x] **4E.1** Create DeepConsolidator instance in BioMemoryModule
  - Instantiate in `__init__()` after Consolidator
  - Pass same workspace, config, identity, telemetry

- [x] **4E.2** Register `memory_consolidate_deep` tool
  - Schema: `{dry_run: boolean}`
  - Handler: invoke `self._deep_consolidator.consolidate(model, agent_id)`
  - Return summary of operations performed

- [x] **4E.3** Update `bio_memory/__init__.py` — export `DeepConsolidator`

- [x] **4E.4** Update `bio_memory/cli.py` — add `consolidate-deep` command
  - `arc memory consolidate-deep [--dry-run]`
  - Test: `tests/unit/modules/bio_memory/test_deep_consolidator.py` (new file)

---

## Phase 5: Tests

- [x] **5.1** Update `tests/unit/modules/bio_memory/test_config.py`
  - Test new config fields and defaults

- [x] **5.2** Update `tests/unit/modules/bio_memory/test_retriever.py`
  - Test entity scope search (workspace/entities/)
  - Test wiki-link resolution against entities directory
  - Test recall with entity files
  - Test team entity search with score penalty
  - Test _is_within_bounds with both directories

- [x] **5.3** Update `tests/unit/modules/bio_memory/test_consolidator.py`
  - Test _analyze_entities LLM call (mock model)
  - Test _update_touched_entities (frontmatter update + Recent Activity append)
  - Test _apply_corrections (Constraints section update)
  - Test _add_co_occurrence_links (bidirectional link addition)
  - Test _create_entity_stubs (v2.1 schema, promotion gate call)
  - Test _normalize_entity_file (legacy → v2.1 format)
  - Test full light_consolidate with entity pipeline

- [x] **5.4** Create `tests/unit/modules/bio_memory/test_deep_consolidator.py`
  - Test _find_recent_episodes (date filtering)
  - Test _compute_intensity (skip/light/full thresholds)
  - Test _entity_centric_pass (mock LLM rewrite)
  - Test _graph_centric_pass (cluster selection, structural links)
  - Test _detect_merges (adjacency, LLM judgment)
  - Test _flag_stale_entities (TTL, archival)
  - Test _refresh_identity (pattern synthesis)
  - Test rotation state persistence

- [x] **5.5** Update `tests/unit/modules/bio_memory/test_bio_memory_module.py`
  - Test team_config acceptance and lazy init
  - Test memory_search with scope="entities"
  - Test memory_consolidate_deep tool registration
  - Test context injection entity hint
  - Test entity path protection

---

## File Inventory

### New Files (2)

| File | LOC Est | Purpose |
|------|---------|---------|
| `bio_memory/deep_consolidator.py` | ~350 | Deep consolidation engine |
| `tests/unit/modules/bio_memory/test_deep_consolidator.py` | ~250 | Deep consolidation tests |

### Modified Files (6)

| File | Changes |
|------|---------|
| `bio_memory/config.py` | Add 7 new config fields |
| `bio_memory/retriever.py` | Add workspace path, entity scope, team search, wiki-link resolution |
| `bio_memory/consolidator.py` | Add entity analysis pipeline (LC-4..7), normalization, workspace |
| `bio_memory/bio_memory_module.py` | Add team_config, entity scope, deep consol tool, context hint |
| `bio_memory/__init__.py` | Export DeepConsolidator |
| `bio_memory/cli.py` | Add consolidate-deep, entities list, entities normalize commands |

### Test Files (Updated/New: 5)

| File | Type | Changes |
|------|------|---------|
| `test_config.py` | Unit | New config field tests |
| `test_retriever.py` | Unit | Entity scope + team search tests |
| `test_consolidator.py` | Unit | LC-4..7 tests |
| `test_deep_consolidator.py` | Unit | **NEW** — full deep consolidation tests |
| `test_bio_memory_module.py` | Unit | Team config + entity scope tests |

---

## Verification Checklist

- [x] All unit tests pass: `cd packages/arcagent && python -m pytest tests/unit/modules/bio_memory/ -v`
- [ ] All integration tests pass: `cd packages/arcagent && python -m pytest tests/integration/ -v`
- [x] Ruff clean: `cd packages/arcagent && python -m ruff check src/arcagent/modules/bio_memory/`
- [ ] mypy clean: `cd packages/arcagent && python -m mypy src/arcagent/modules/bio_memory/ --strict`
- [x] Existing BIO-001 tests still pass (no regression)
- [x] Entity search returns results from workspace/entities/
- [x] Wiki-links resolve across memory/ and entities/ directories
- [x] Light consolidation updates entity frontmatter (last_verified)
- [x] Light consolidation creates entity stubs for new entities
- [x] Co-occurrence links are bidirectional
- [x] Entity normalization adds v2.1 frontmatter to legacy files
- [x] Deep consolidation rewrites entities within budget
- [x] Graph pass discovers structural links
- [x] Merge detection works for 3+ shared link pairs
- [x] Stale entities get flagged and archived
- [x] Team features degrade gracefully when arcteam not installed
- [x] All file writes use atomic_write_text
- [x] All LLM outputs sanitized before disk write
- [x] Audit events emitted for all memory operations

---

## Dependencies

**Zero new external dependencies.** Uses:
- `pydantic` (existing)
- `yaml` (existing)
- `opentelemetry` (existing)
- `arcllm.types` (existing)
- `arcteam.memory` (optional, lazy import)

---

## Risks

| Risk | Mitigation |
|------|------------|
| LLM entity analysis quality varies | Structured JSON schema + validation. Failures skip entity updates (non-blocking). |
| Entity frontmatter YAML roundtrip corruption | Use `yaml.safe_load` + `yaml.dump` with explicit settings. Test roundtrip in unit tests. |
| Deep consolidation takes too long | Max entity limit per cycle. Adaptive intensity. Timeout per LLM call. |
| Merge detection false positives | LLM confirmation required. Conservative threshold (3+ shared links). |
| Team service unavailable at runtime | Lazy init + ImportError catch. All team operations have None-check fast paths. |
| Path traversal via entity slugs | All paths resolved + validated within workspace bounds. sanitize_wiki_link rejects traversal. |
| Rotation state file corruption | JSON parse with fallback to empty state. State is advisory, not critical. |

---

## Task Summary

**Total**: 45 tasks
**Completed**: 45/45
**Remaining**: 0

| Phase | Tasks | Description |
|-------|-------|-------------|
| Phase 1 | 9 | Entity format + retriever scope |
| Phase 2 | 5 | arcagent-arcteam wiring |
| Phase 3 | 8 | Complete light consolidation (+ pre-filter gate) |
| Phase 4 | 18 | Deep consolidation engine (+ hash gating, validation, manifest) |
| Phase 5 | 5 | Tests |
