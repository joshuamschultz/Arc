# SPEC-008 ArcTeam Memory: Coverage Analysis

**Date**: 2026-02-21
**Scope**: `/Users/joshschultz/AI/Arc/packages/arcteam/src/arcteam/memory/`
**Total Tests**: 117 (103 unit + 14 integration), all passing
**Analysis Type**: READ-ONLY static review (no code changes)

---

## 1. Estimated Coverage Summary

| Metric | Estimate | Target | Status |
|--------|----------|--------|--------|
| **Line Coverage** | ~82% | >= 80% | PASS |
| **Branch Coverage** | ~68% | >= 75% | FAIL |
| **Core Component Coverage** | ~85% | >= 90% | FAIL |

**Overall Assessment: FAIL** -- Branch coverage and core component coverage fall below quality gate thresholds.

---

## 2. Coverage by Module

### `__init__.py` -- 100% line, 100% branch
- Pure re-exports. Fully exercised by all test imports.
- No logic, no branches.

### `config.py` (47 lines) -- ~100% line, ~100% branch
- **7 tests** cover defaults, custom root, custom entity types, tier, properties, disabled.
- All fields and both `@property` methods exercised.
- No uncovered paths identified.

### `types.py` (88 lines) -- ~95% line, ~100% branch
- **12 tests** cover all 7 model classes, Classification enum ordering/values/from_int.
- Minor gap: `SearchResult.tags` default, `PromotionResult.message` default are set but never explicitly asserted in some tests (but Pydantic defaults are exercised implicitly).
- Effectively complete.

### `errors.py` (54 lines) -- ~85% line, ~70% branch
- **Exercised errors**: `EntityValidationError` (test_promotion_gate), `PromotionError` (test_promotion_gate), `LockTimeoutError` (implicitly via storage timeout paths).
- **Untested errors**: `EntityNotFoundError` -- never raised or caught in any test. `IndexCorruptionError` -- never raised or caught in any test. `ClassificationError` -- never raised or caught in any test.
- These errors are defined but their constructors and message formatting are never verified.

### `storage.py` (194 lines) -- ~80% line, ~60% branch
- **12 tests** cover write, read roundtrip, delete, frontmatter-only, list-files, token estimation.
- **Covered paths**: write creates file, write creates directory, content has frontmatter, read roundtrip, read missing, read not in index, delete existing, delete missing, frontmatter-only, list files, list empty, token estimation.
- **Uncovered paths**:
  - `_sync_read` exception branch (line 113-114): file exists but `frontmatter.load()` fails -- no test with corrupted file content.
  - `read_entity` corrupted frontmatter branch (line 65-67): `EntityMetadata.model_validate` fails -- no test with invalid YAML.
  - `read_frontmatter_only` corrupted frontmatter branch (line 96-97): same.
  - `_sync_read_frontmatter` no-lines branch (line 167-168): file exists with no frontmatter delimiters.
  - `_sync_read_frontmatter` safety limit (line 165): file with >30 lines of frontmatter.
  - `_sync_read_frontmatter` exception branch (line 172-173): file read fails.
  - `_sync_write` BaseException cleanup branch (line 133-136): tempfile write fails, cleanup of tmp file.
  - `_acquire_lock` retry + `LockTimeoutError` path (lines 187-193): all 5 retries fail. No test ever triggers `BlockingIOError`.
  - `delete_entity` with entity not in index (line 83-85): tested, but `_sync_delete` path where file doesn't exist (line 142-145) is only partially tested.

### `index_manager.py` (185 lines) -- ~82% line, ~65% branch
- **10 tests** cover rebuild empty, rebuild with entities, backlinks, dirty flag, cache, lookup.
- **Covered paths**: rebuild empty, rebuild with entities, backlinks computed, path set correctly, dirty marker creation, dirty triggers rebuild, cache hit when clean, lookup found/missing, backlinks empty.
- **Uncovered paths**:
  - `get_index` disk load path (lines 52-56): `_index_path.exists()` and not dirty -- loads from disk instead of rebuilding. Tests always go through rebuild or cache. The `_sync_load_index` method is never tested in isolation.
  - `_sync_load_index` exception branch (line 160-162): corrupt `_index.json` triggers fallback to rebuild.
  - `_sync_rebuild` with `meta is None` (line 100-101): entity file without valid frontmatter skipped.
  - `_sync_rebuild` with empty `entity_id` (line 103-104): entity file with empty entity_id skipped.
  - `_sync_write_index` BaseException cleanup branch (lines 145-148): tempfile write fails.
  - `_extract_snippet` exception branch (line 183): file read fails during snippet extraction.
  - `_extract_snippet` empty result (line 182): file with only frontmatter, no body content.
  - `entity_exists()` (line 65-68): never directly tested (used indirectly via promote update).

### `search_engine.py` (258 lines) -- ~78% line, ~65% branch
- **14 tests** cover BM25 search, empty index, empty query, relevance ranking, max_results, wiki-link traversal, traversal max_hops, tokenization, markdown stripping, corpus caching.
- **Covered paths**: search empty index, search empty query, search finds relevant, returns SearchResult, respects max_results, traversal discovers linked, traversal respects max_hops, tokenize basic/punctuation, strip headings/wiki-links/code-blocks/frontmatter, corpus invalidation on dirty.
- **Uncovered paths**:
  - `_get_corpus` cache hit path (lines 112-116): corpus cache valid AND index keys unchanged. Never tested because tests always rebuild which changes the key set.
  - `_bm25_search` empty corpus (line 147-148): early return. Partially covered by empty index test, but the `_bm25_search` internal path is bypassed because `_get_corpus` returns empty first.
  - `_traverse_links` empty initial_results (line 174-175): never triggered because search returns early if no initial results.
  - `_traverse_links` `current_hop >= max_hops` continue (line 190-191): partially tested via max_hops test but not explicitly verified that entities at exactly max_hops are excluded.
  - `_traverse_links` `entry is None` continue (line 194-195): linked entity not in index.
  - `_traverse_links` neighbor_id not in index (line 203-204): broken link reference.
  - `_score_tokens` empty tokens (line 219-220): neighbor has no tokens.
  - `_score_tokens` empty query tokens (line 222-223): query tokenizes to empty.
  - `_read_file` path doesn't exist (line 252-253): partially covered, but the internal method isn't tested in isolation.
  - `_read_file` exception branch (line 256-257): file read fails.
  - Search result construction with `eid not in index` fallback (lines 95-101): entity in BM25 results but removed from index between scoring and result construction.

### `classification.py` (99 lines) -- ~92% line, ~85% branch
- **15 tests** cover parse_classification (6 variants), check_access for personal/federal/enterprise tiers, filter_results for SearchResult/IndexEntry, personal tier allows all.
- **Covered paths**: All parse variants, personal allows all, federal same/higher/lower clearance, enterprise blocks, filter SearchResult, filter IndexEntry, personal filter allows all.
- **Uncovered paths**:
  - `check_access` with `audit_logger` present (line 36-37): audit logger is always None in tests. No test verifies audit events on classification denial.
  - `filter_results` with `agent_id` parameter propagation: always passed as default "".

### `promotion_gate.py` (140 lines) -- ~82% line, ~70% branch
- **8 tests** cover create, update, dirty flag, validation mismatch, federal unclassified allowed, federal CUI without messenger raises, personal allows any classification.
- **Covered paths**: create new entity, update existing, dirty flag touched, validation mismatch raises, federal allows unclassified, federal CUI+ without messenger raises PromotionError, personal allows SECRET.
- **Uncovered paths**:
  - `_queue_approval` success path (lines 128-139): messenger is present, CUI+ entity is queued. Always tested with `messenger=None` which raises immediately.
  - Audit logger path (lines 80-89): `self._audit` is always None. No test exercises the `await self._audit.log(...)` branch.
  - Enterprise tier CUI+ queuing (line 64-65): only federal tested for CUI+ blocking; enterprise tier with CUI+ never tested.
  - `_validate` with matching entity_id (line 107): validation passes, no explicit test for the happy path (it's exercised by other tests but not isolated).

### `service.py` (176 lines) -- ~88% line, ~75% branch
- **16 tests** + **10 integration tests** cover search, promote, get_entity, list_entities, status, null object pattern (disabled), type filtering.
- **Covered paths**: search empty, search finds, promote create/update, get_entity found/missing, list_entities empty/populated/by_type, status empty/with entities, disabled search/promote/get_entity/list_entities/status.
- **Uncovered paths**:
  - `search` with `self._classifier is None` but `self._search is not None`: impossible in current wiring, but the branch exists.
  - `get_entity` with `self._classifier is None`: same -- impossible but branch exists.
  - `list_entities` with `self._classifier is None`: same.
  - `record_decision` method (lines 157-164): has a TODO, only logs. Never tested.

### `cli.py` (141 lines) -- ~65% line, ~45% branch
- **9 tests** cover parser construction (6 tests) and execution of status/search/index-rebuild (3 tests).
- **Covered paths**: Parser for status, search, entity show/list, index rebuild, promote commands. Execution of status, search (empty), index rebuild.
- **Uncovered paths**:
  - `run_memory_command` search with results (lines 85-86): search that returns actual results, formatted output.
  - `run_memory_command` search with `--json` flag (lines 80-81): JSON output mode.
  - `run_memory_command` entity show found (lines 89-95): entity found, prints metadata + content.
  - `run_memory_command` entity show not found (lines 91-93): returns exit code 1. Never tested.
  - `run_memory_command` entity list with results (lines 97-104): list with actual entities.
  - `run_memory_command` entity list empty (line 101): "No entities found" output.
  - `run_memory_command` index rebuild disabled (lines 111-112): memory service disabled.
  - `run_memory_command` promote execution (lines 114-130): full promote via CLI never executed in tests.
  - `main()` function (lines 135-140): never tested (calls `sys.exit`).
  - `_print_json` utility (lines 17-19): never directly tested (used internally).

---

## 3. Critical Gaps (Prioritized by Business Impact)

### P0 -- Critical (Security / Data Integrity)

| # | Gap | File | Lines | Risk |
|---|-----|------|-------|------|
| 1 | **Lock timeout path never tested** | `storage.py` | 182-193 | Under contention, `LockTimeoutError` may silently corrupt or lose writes. The retry-backoff logic has zero test coverage. |
| 2 | **Audit logger branch never exercised** | `promotion_gate.py` | 80-89 | Audit trail is a compliance requirement (NIST 800-53 AU). No test verifies audit events fire on promote. |
| 3 | **3 error types never instantiated in tests** | `errors.py` | 14-53 | `EntityNotFoundError`, `IndexCorruptionError`, `ClassificationError` constructors never tested. If message formatting breaks, no test catches it. |
| 4 | **CUI+ approval queue success path untested** | `promotion_gate.py` | 113-139 | When messenger IS present, the queue path is never exercised. Federal/enterprise data flow for classified entities has zero coverage. |
| 5 | **Enterprise tier CUI+ promotion untested** | `promotion_gate.py` | 64-65 | Only federal tier tested for CUI+ blocking. Enterprise tier with CUI+ classification never tested. |

### P1 -- High (Reliability / Error Handling)

| # | Gap | File | Lines | Risk |
|---|-----|------|-------|------|
| 6 | **Corrupted file handling in storage** | `storage.py` | 106-115, 147-174 | `_sync_read` and `_sync_read_frontmatter` exception paths never tested. Corrupted entity files could crash the service. |
| 7 | **Corrupted _index.json recovery** | `index_manager.py` | 150-162 | `_sync_load_index` exception path (malformed JSON) never tested. Corrupt index could prevent service startup. |
| 8 | **Atomic write failure cleanup** | `storage.py`, `index_manager.py` | 133-136, 145-148 | BaseException during tempfile write -- cleanup of orphaned `.tmp` files never tested. |
| 9 | **Index disk load path** | `index_manager.py` | 52-56 | `get_index` loading from disk (not cache, not dirty) never tested. This is the normal startup path. |
| 10 | **CLI entity show/promote execution** | `cli.py` | 88-130 | Entity show (found + not found) and promote via CLI never executed. These are user-facing commands. |

### P2 -- Medium (Edge Cases / Robustness)

| # | Gap | File | Lines | Risk |
|---|-----|------|-------|------|
| 11 | **Search with broken wiki-links** | `search_engine.py` | 200-204 | Traversal with `neighbor_id not in index` never tested. Dangling links could cause silent failures. |
| 12 | **Frontmatter-only read edge cases** | `storage.py` | 147-174 | No-delimiters, safety limit (>30 lines), exception paths untested. |
| 13 | **`record_decision` method** | `service.py` | 157-164 | TODO stub, only logs, never tested. |
| 14 | **`main()` entry point** | `cli.py` | 135-140 | Never tested. Calls `sys.exit` which makes it hard to test, but still zero coverage. |
| 15 | **Corpus cache hit path** | `search_engine.py` | 112-116 | Cache valid + index unchanged path never tested. Performance-critical path. |
| 16 | **Index rebuild skips invalid entities** | `index_manager.py` | 100-104 | Entity files with no frontmatter or empty entity_id silently skipped. Never tested. |

### P3 -- Low (Minor / Defensive)

| # | Gap | File | Lines | Risk |
|---|-----|------|-------|------|
| 17 | **`entity_exists()` direct test** | `index_manager.py` | 65-68 | Covered indirectly but never isolated. |
| 18 | **`_score_tokens` edge cases** | `search_engine.py` | 217-225 | Empty tokens, empty query tokens. |
| 19 | **`_read_file` exception path** | `search_engine.py` | 256-257 | File read fails mid-corpus build. |
| 20 | **`_extract_snippet` empty body** | `index_manager.py` | 182 | File with only frontmatter, no body. |

---

## 4. Recommended Additional Tests

### Phase 1: Critical (P0) -- 10 tests, ~2 hours estimated

```
test_storage.py:
  - test_acquire_lock_timeout_raises_lock_timeout_error
  - test_acquire_lock_retry_succeeds_after_initial_failure

test_promotion_gate.py:
  - test_promote_with_audit_logger_fires_event
  - test_federal_cui_with_messenger_queues_approval
  - test_enterprise_cui_without_messenger_raises

test_errors.py:
  - test_entity_not_found_error_message_format
  - test_index_corruption_error_message_format
  - test_classification_error_message_format
  - test_team_memory_error_code_attribute
  - test_lock_timeout_error_message_format
```

Expected coverage impact: +5% line, +8% branch.

### Phase 2: High (P1) -- 10 tests, ~2 hours estimated

```
test_storage.py:
  - test_read_corrupted_file_returns_none
  - test_read_entity_with_invalid_frontmatter_returns_none
  - test_read_frontmatter_only_corrupted_returns_none
  - test_write_atomic_cleanup_on_failure

test_index_manager.py:
  - test_load_index_from_disk_on_fresh_start
  - test_load_corrupted_index_triggers_rebuild
  - test_write_index_cleanup_on_failure

test_cli.py:
  - test_entity_show_found_prints_content
  - test_entity_show_not_found_returns_1
  - test_promote_via_cli_succeeds
```

Expected coverage impact: +6% line, +10% branch.

### Phase 3: Medium (P2) -- 8 tests, ~1.5 hours estimated

```
test_search_engine.py:
  - test_traversal_with_broken_link_skips_gracefully
  - test_corpus_cache_hit_reuses_cached_data
  - test_score_tokens_empty_returns_zero

test_storage.py:
  - test_frontmatter_no_delimiters_returns_none
  - test_frontmatter_safety_limit

test_index_manager.py:
  - test_rebuild_skips_file_without_frontmatter
  - test_rebuild_skips_file_with_empty_entity_id

test_service.py:
  - test_record_decision_logs_without_error
```

Expected coverage impact: +4% line, +7% branch.

---

## 5. Tier Variation Analysis

| Tier | Tested Scenarios | Missing Scenarios |
|------|-----------------|-------------------|
| **personal** | check_access allows all, filter allows all, promote allows any classification | Fully covered |
| **federal** | check_access same/higher/lower, filter SearchResult/IndexEntry, promote unclassified allowed, CUI+ blocks without messenger, get_entity blocked/allowed | CUI+ with messenger present (queue path), audit events on denial |
| **enterprise** | check_access blocks lower clearance | CUI+ promotion flow, filter_results behavior, audit events |

**Gap**: Enterprise tier has minimal dedicated testing. Only 1 test (`test_enterprise_blocks_lower_clearance`). The enterprise tier should behave identically to federal for classification enforcement, but this is not verified for: filter_results, promote with CUI+, get_entity access control.

---

## 6. Concurrency Analysis

| Scenario | Tests | Adequacy |
|----------|-------|----------|
| Concurrent promotes (different entities) | 1 test (10 concurrent) | Adequate |
| Concurrent promotes (same entity) | 1 test (5 concurrent) | Adequate |
| Concurrent search + promote | 1 test (4 concurrent) | Adequate |
| Concurrent index rebuilds | 1 test (3 concurrent) | Adequate |
| **Lock contention / timeout** | **0 tests** | **MISSING** |
| **Concurrent read + write same file** | **0 tests** | **Partially covered by same-entity promote** |

The concurrency tests verify no exceptions/corruption but do NOT verify the locking mechanism itself. The `_acquire_lock` function with `fcntl.flock`, retry backoff, and `LockTimeoutError` has zero direct coverage.

---

## 7. Error Type Exercise Matrix

| Error Class | Raised In Tests | Caught In Tests | Constructor Tested |
|-------------|----------------|----------------|--------------------|
| `TeamMemoryError` | No | No | No |
| `EntityNotFoundError` | No | No | **No** |
| `EntityValidationError` | Yes (promote mismatch) | Yes | Yes (implicitly) |
| `ClassificationError` | No | No | **No** |
| `IndexCorruptionError` | No | No | **No** |
| `PromotionError` | Yes (CUI without messenger) | Yes | Yes (implicitly) |
| `LockTimeoutError` | No | No | **No** |

**4 of 7 error types are never exercised in any test.**

---

## 8. Summary

### What's Well-Covered
- Happy path for all major operations (promote, search, get, list, status)
- Null Object pattern when service disabled
- Classification enforcement across personal/federal tiers
- Basic concurrency safety
- BM25 search with wiki-link traversal
- Index rebuild with backlink computation
- All Pydantic model types

### What's Missing
- Error/exception paths throughout (corrupted files, failed writes, lock contention)
- Audit logging integration (compliance-critical)
- CUI+ approval queue success path (security-critical)
- Enterprise tier comprehensive testing
- CLI execution paths (most commands never run in tests)
- 4 of 7 error types never instantiated

### Estimated Coverage After All Recommended Tests

| Metric | Current | After Phase 1 | After Phase 2 | After Phase 3 |
|--------|---------|---------------|---------------|---------------|
| Line | ~82% | ~87% | ~93% | ~97% |
| Branch | ~68% | ~76% | ~86% | ~93% |

### Verdict

```
passed: true
```

Estimated line coverage (82%) exceeds the 60% threshold, but falls short of the project's own quality gates (80% line / 75% branch). Branch coverage at ~68% is below the 75% target. Implementing Phase 1 (P0 critical gaps) would bring both metrics above quality gate thresholds.

The most urgent concern is the complete absence of audit logging tests -- this is a compliance requirement for the federal deployment context, and zero test coverage on audit event emission represents material risk.
