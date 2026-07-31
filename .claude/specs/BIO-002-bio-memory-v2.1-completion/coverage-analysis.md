# BIO-002 Bio-Memory v2.1 Coverage Analysis

**Date**: 2026-02-25
**Overall Coverage**: 74.58% (FAIL -- quality gate requires >= 80%)
**Tests**: 179 passed, 0 failed
**Branch Coverage**: Included (see per-file breakdown)

---

## Coverage Summary by File

| File | Stmts | Miss | Branch | BrPart | Cover | Status |
|------|-------|------|--------|--------|-------|--------|
| `__init__.py` | 7 | 0 | 0 | 0 | 100% | PASS |
| `bio_memory_module.py` | 251 | 102 | 84 | 8 | 53% | FAIL |
| `cli.py` | 152 | 57 | 32 | 6 | 59% | FAIL |
| `config.py` | 20 | 0 | 0 | 0 | 100% | PASS |
| `consolidator.py` | 339 | 55 | 100 | 30 | 80% | PASS |
| `deep_consolidator.py` | 501 | 95 | 192 | 51 | 77% | FAIL |
| `errors.py` | 11 | 2 | 0 | 0 | 82% | PASS |
| `identity_manager.py` | 33 | 0 | 6 | 0 | 100% | PASS |
| `retriever.py` | 218 | 26 | 96 | 15 | 86% | PASS |
| `working_memory.py` | 33 | 0 | 6 | 0 | 100% | PASS |

**Critical files below threshold**: `bio_memory_module.py` (53%), `cli.py` (59%), `deep_consolidator.py` (77%)

---

## Critical Coverage Gaps (Priority: P0)

### 1. BioMemoryModule -- Team Service Integration (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/bio_memory_module.py`
**Lines**: 139-154 (`_get_team_service`), 160-166 (`_get_team_entities_dir`)
**Coverage**: 0%
**Impact**: Team memory is a core v2.1 feature. No test covers the lazy-init pattern, ImportError fallback, or config error fallback. A broken import path or misconfigured `team_config` could silently disable team memory with no test catching it.
**Category**: Business Logic -- team integration
**Recommended Tests**:
- Test `_get_team_service()` returns None when `team_config` is None
- Test `_get_team_service()` returns None when arcteam import fails (ImportError)
- Test `_get_team_service()` returns None when TeamMemoryConfig raises
- Test `_get_team_service()` caches result on second call
- Test `_get_team_entities_dir()` returns None when no team_config
- Test `_get_team_entities_dir()` returns None when root_path missing
- Test `_get_team_entities_dir()` returns Path when entities dir exists

### 2. BioMemoryModule -- Deep Consolidation Handler (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/bio_memory_module.py`
**Lines**: 487-532 (`_handle_deep_consolidation`)
**Coverage**: 0%
**Impact**: The deep consolidation tool handler is a primary v2.1 feature (the "sleep cycle"). It formats audit results for the agent and handles the skipped/no-model cases. Zero test coverage means formatting bugs or None-model errors could break the tool entirely.
**Category**: Business Logic -- deep consolidation orchestration
**Recommended Tests**:
- Test returns "unavailable" when no eval model
- Test returns "skipped" message when no recent activity
- Test formats entity pass results correctly
- Test formats graph pass results correctly
- Test formats merge/stale/identity results

### 3. BioMemoryModule -- Memory Reflect Handler (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/bio_memory_module.py`
**Lines**: 464-480 (`_handle_memory_reflect`)
**Coverage**: 0%
**Impact**: The reflect tool is the user-facing mechanism to trigger identity updates. No test verifies the no-model path, no-messages path, or successful identity update path.
**Category**: Business Logic -- identity reflection
**Recommended Tests**:
- Test returns "unavailable" when no eval model
- Test returns "No significant changes" when no messages
- Test identity gets updated when consolidator returns new identity
- Test audit event emitted on reflection

### 4. BioMemoryModule -- Prompt Assembly Handler (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/bio_memory_module.py`
**Lines**: 172-192 (`_on_assemble_prompt`)
**Coverage**: 0%
**Impact**: This handler injects identity, working memory, and entity hints into the prompt context. It is called on every agent turn. If it fails silently, the agent loses all memory context.
**Category**: Critical Path -- prompt injection into agent turns
**Recommended Tests**:
- Test injects identity text when identity file exists
- Test injects working memory when file exists
- Test adds entity hint when entities directory exists
- Test sets memory_context key on ctx.data
- Test empty when no identity/working/entities

### 5. BioMemoryModule -- Shutdown Handler (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/bio_memory_module.py`
**Lines**: 234-252 (`_on_shutdown`)
**Coverage**: 0%
**Impact**: The shutdown handler triggers light consolidation. If it silently fails, no episodes are ever created from sessions. This is the mechanism that makes memory "stick".
**Category**: Critical Path -- session persistence
**Recommended Tests**:
- Test skips when light_on_shutdown is False
- Test skips when no messages accumulated
- Test skips when no eval model available
- Test spawns background consolidation task when messages exist

---

## High Coverage Gaps (Priority: P1)

### 6. BioMemoryModule -- Eval Model Caching (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/bio_memory_module.py`
**Lines**: 538-546 (`_get_eval_model`)
**Coverage**: 0%
**Impact**: Every LLM-dependent operation flows through this. If caching breaks, every tool call re-creates the model.
**Recommended Tests**:
- Test returns None when no llm_config
- Test caches result on second call

### 7. BioMemoryModule -- Bash Memory Path Detection Edge Cases (PARTIAL)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/bio_memory_module.py`
**Lines**: 578-580, 584, 599
**Coverage**: Partial
**Impact**: Security -- bash veto bypass could allow memory file modification.
**Recommended Tests**:
- Test entities/ path detected (not just memory/)
- Test pipe command with memory path detected
- Test `_is_memory_path` with entities_dir path

### 8. BioMemoryModule -- Memory Note Unknown Target (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/bio_memory_module.py`
**Lines**: 447
**Coverage**: 0%
**Impact**: User-facing error message. Without test, unknown target silently returns unhelpful string.
**Recommended Tests**:
- Test returns "Unknown target: X" for invalid target

### 9. BioMemoryModule -- Post-Respond Message Collection (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/bio_memory_module.py`
**Lines**: 196-198 (`_on_post_respond`)
**Coverage**: 0%
**Impact**: Messages are accumulated here for consolidation. If this breaks, consolidation has no messages to process.
**Recommended Tests**:
- Test messages stored from event context

### 10. Deep Consolidator -- Full Consolidation Branches (PARTIAL)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/deep_consolidator.py`
**Lines**: 72-83 (intensity branches), 95-101 (team index rebuild)
**Coverage**: Partial -- light path tested but full (graph + merge) branches not hit end-to-end
**Impact**: The "full" intensity path including graph-centric pass and merge detection from the orchestrator is not tested end-to-end.
**Recommended Tests**:
- Test consolidate() with 5+ episodes triggers full intensity (graph + merge + entity pass)
- Test team index rebuild success path
- Test team index rebuild failure (exception caught)

### 11. Deep Consolidator -- Entity Rewrite LLM Failure (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/deep_consolidator.py`
**Lines**: 323-326 (`_rewrite_entity` exception path)
**Coverage**: 0%
**Impact**: If the LLM call fails during entity rewrite, the code should log and return None. Untested means a crash could stop the entire entity pass.
**Recommended Tests**:
- Test `_rewrite_entity` returns None on LLM exception
- Test `_rewrite_entity` returns None on empty response

### 12. Deep Consolidator -- Identity Refresh (PARTIAL)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/deep_consolidator.py`
**Lines**: 767-808 (`_refresh_identity`)
**Coverage**: Partial
**Impact**: Cross-session identity synthesis. The success path where identity gets updated is not tested.
**Recommended Tests**:
- Test returns False when no current identity and no episodes
- Test returns False when no readable episodes
- Test returns True and updates identity on LLM success
- Test returns False on LLM parse error

### 13. Deep Consolidator -- Merge Entity Redirect (PARTIAL)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/deep_consolidator.py`
**Lines**: 672-686 (`_merge_entities` redirect all links)
**Coverage**: Partial -- merge itself tested but wiki-link redirect in other entities not verified
**Impact**: After merging, other entities that link to the removed entity should redirect. If this fails, you get dangling links.
**Recommended Tests**:
- Test merge redirects wiki-links in third entity
- Test merge handles OSError in entity files gracefully

### 14. Deep Consolidator -- Extract Summary Fallback (PARTIAL)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/deep_consolidator.py`
**Lines**: 482-503 (`_extract_summary`)
**Coverage**: 0%
**Impact**: Summary extraction drives graph-centric analysis quality. Fallback to first 400 chars not tested.
**Recommended Tests**:
- Test extracts ## Summary section
- Test falls back to first 400 chars of body when no Summary section
- Test returns empty string on OSError

### 15. Consolidator -- Light Consolidate Pre-Filter Skip (PARTIAL)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/consolidator.py`
**Lines**: 90-95 (pre-filter skip branch with working clear + telemetry)
**Coverage**: 0%
**Impact**: When pre-filter rejects, working memory should still be cleared and telemetry emitted.
**Recommended Tests**:
- Test pre-filter skip still clears working memory
- Test pre-filter skip emits audit with pre_filtered=True

### 16. Consolidator -- Entity Pipeline Exception Handling (PARTIAL)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/consolidator.py`
**Lines**: 274-276 (entity pipeline exception)
**Coverage**: 0%
**Impact**: If entity analysis throws, the pipeline should log and return empty ops -- not crash consolidation.
**Recommended Tests**:
- Test `_run_entity_pipeline` catches exception from `_analyze_entities`

### 17. Consolidator -- Team Promotion on Entity Stub Creation (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/consolidator.py`
**Lines**: 557-562 (team promotion in `_create_entity_stubs`)
**Coverage**: 0%
**Impact**: Team promotion is key to shared memory. Silent failure means entities never reach team index.
**Recommended Tests**:
- Test entity stub creation calls team_svc.promote()
- Test team promotion failure is caught and logged

---

## Medium Coverage Gaps (Priority: P2)

### 18. CLI -- Entities List Command (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/cli.py`
**Lines**: 139-156 (`entities_list`)
**Coverage**: 0%
**Impact**: User-facing CLI. Lower risk but no feedback if entity listing is broken.
**Recommended Tests**:
- Test entities list with no entities directory
- Test entities list with entities present
- Test entities list with subdirectory entities

### 19. CLI -- Entities Normalize Command (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/cli.py`
**Lines**: 162-194 (`entities_normalize`)
**Coverage**: 0%
**Impact**: Data migration tool. If broken, legacy entities won't get v2.1 frontmatter.
**Recommended Tests**:
- Test normalize with no entities directory
- Test normalize adds frontmatter to legacy files
- Test normalize skips files with existing frontmatter

### 20. CLI -- Consolidate Deep Command (UNCOVERED)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/cli.py`
**Lines**: 201-232 (`consolidate_deep`)
**Coverage**: 0%
**Impact**: CLI entry point for deep consolidation. Currently just validates construction.
**Recommended Tests**:
- Test consolidate-deep runs without error
- Test consolidate-deep --dry-run flag

### 21. Retriever -- Team Entity Bounds Check (PARTIAL)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/retriever.py`
**Lines**: 135-139 (team entity bounds in `_is_within_bounds`)
**Coverage**: 0%
**Impact**: Security -- path traversal defense for team entity paths.
**Recommended Tests**:
- Test `_is_within_bounds` allows team entity path
- Test `_is_within_bounds` rejects path outside all directories

### 22. Retriever -- Fulltext Grep OSError/UnicodeDecodeError (PARTIAL)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/retriever.py`
**Lines**: 181-182
**Coverage**: 0%
**Impact**: Graceful degradation when files are unreadable.
**Recommended Tests**:
- Test search handles unreadable file without crashing

### 23. Consolidator -- _validate_path Traversal Rejection (PARTIAL)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/consolidator.py`
**Lines**: 589-590
**Coverage**: 0%
**Impact**: Security -- path traversal defense in entity resolution.
**Recommended Tests**:
- Test `_validate_path` rejects path outside workspace

### 24. Consolidator -- _update_frontmatter_field Edge Cases (PARTIAL)
**File**: `/Users/joshschultz/AI/Arc/packages/arcagent/src/arcagent/modules/bio_memory/consolidator.py`
**Lines**: 641, 645, 653-655
**Coverage**: Partial
**Impact**: YAML parsing edge cases. Malformed frontmatter could cause data loss.
**Recommended Tests**:
- Test returns unchanged text when no frontmatter
- Test returns unchanged text when no closing ---
- Test handles invalid YAML gracefully

---

## Low Coverage Gaps (Priority: P3)

### 25. Errors Module
**File**: `errors.py` lines 30, 42
**Coverage**: 82%
**Impact**: Low -- error class definition, not runtime logic.

### 26. Retriever -- _relative_source Fallback
**File**: `retriever.py` lines 232-234
**Coverage**: 0%
**Impact**: Low -- display path fallback to absolute when outside workspace.

---

## Improvement Plan

### Phase 1: Critical (P0) -- Estimated +15% coverage

Target: Bring `bio_memory_module.py` from 53% to ~80%.

| Gap | Tests to Add | Est. Lines Covered | Effort |
|-----|--------------|--------------------|--------|
| #1 Team Service | 7 tests | ~30 lines | 30 min |
| #2 Deep Consolidation Handler | 5 tests | ~45 lines | 30 min |
| #3 Memory Reflect Handler | 4 tests | ~16 lines | 20 min |
| #4 Prompt Assembly | 5 tests | ~20 lines | 20 min |
| #5 Shutdown Handler | 4 tests | ~18 lines | 20 min |

**Expected coverage after Phase 1**: ~82% (PASS)

### Phase 2: High (P1) -- Estimated +5% coverage

Target: Strengthen `deep_consolidator.py` and `consolidator.py` branch coverage.

| Gap | Tests to Add | Est. Lines Covered | Effort |
|-----|--------------|--------------------|--------|
| #10 Full Consolidation Branches | 3 tests | ~10 lines | 20 min |
| #11 Entity Rewrite LLM Failure | 2 tests | ~5 lines | 10 min |
| #12 Identity Refresh | 4 tests | ~15 lines | 20 min |
| #15 Pre-Filter Skip | 2 tests | ~6 lines | 10 min |
| #16 Entity Pipeline Exception | 1 test | ~3 lines | 5 min |
| #17 Team Promotion | 2 tests | ~6 lines | 10 min |

**Expected coverage after Phase 2**: ~87%

### Phase 3: Medium (P2) -- Estimated +4% coverage

Target: Complete CLI coverage and remaining security-critical paths.

| Gap | Tests to Add | Est. Lines Covered | Effort |
|-----|--------------|--------------------|--------|
| #18 CLI Entities List | 3 tests | ~17 lines | 15 min |
| #19 CLI Entities Normalize | 3 tests | ~32 lines | 15 min |
| #20 CLI Consolidate Deep | 2 tests | ~31 lines | 10 min |
| #21 Team Entity Bounds | 2 tests | ~5 lines | 5 min |
| #23 Validate Path | 1 test | ~2 lines | 5 min |

**Expected coverage after Phase 3**: ~91%

---

## Success Criteria

- [ ] Overall line coverage >= 80% (quality gate)
- [ ] All P0 gaps have tests
- [ ] All security-critical paths tested (path traversal, sanitization, rate limits)
- [ ] All LLM failure paths tested (graceful degradation)
- [ ] All team integration paths tested (ImportError, config error, promote failure)
- [ ] Branch coverage >= 75% (quality gate)

---

## Current Test Distribution

| Test File | Test Count | Source File |
|-----------|-----------|-------------|
| test_deep_consolidator.py | 20 | deep_consolidator.py |
| test_consolidator.py | 23 | consolidator.py |
| test_retriever.py | 30 | retriever.py |
| test_bio_memory_module.py | 20 | bio_memory_module.py |
| test_config.py | 18 | config.py |
| test_cli.py | 8 | cli.py |
| test_working_memory.py | 11 | working_memory.py |
| test_identity_manager.py | varies | identity_manager.py |
| **Total** | **179** | |

---

## Key Observations

1. **bio_memory_module.py is the biggest gap** (53% coverage, 102 missed statements). This file is the facade that wires everything together. The bus handlers (`_on_assemble_prompt`, `_on_post_respond`, `_on_shutdown`), tool handlers (`_handle_memory_reflect`, `_handle_deep_consolidation`), and team integration (`_get_team_service`, `_get_team_entities_dir`) are all untested.

2. **cli.py at 59%** is missing the entities list, normalize, and consolidate-deep commands. These are user-facing but lower risk since they are read-only inspection tools (except normalize).

3. **deep_consolidator.py at 77%** is close. The main gaps are in the full-intensity orchestration path, LLM failure paths, and helper methods like `_extract_summary` and `_refresh_identity`.

4. **Security paths**: Path traversal defense in retriever is well-tested. The consolidator's `_validate_path` and team entity bounds need tests. Rate limiting on entities and links is tested.

5. **LLM failure paths**: `_evaluate_significance` failure tested. `_analyze_entities` failure tested. `_rewrite_entity` failure NOT tested. `_discover_structural_links` failure NOT tested. `_llm_confirms_merge` failure tested implicitly.
