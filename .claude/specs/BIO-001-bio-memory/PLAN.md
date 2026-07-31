# PLAN: Bio-Memory Module (BIO-001) — Phase 1 (Core)

**Status**: COMPLETE
**Spec**: BIO-001
**Phase**: 1 of 4
**Estimated Files**: 12 new, 2 modified

---

## Implementation Phases

### Phase 1A: Foundation (Config + Shared Utils + Scaffold)

- [x] **1A.1** Create shared sanitizer `arcagent/utils/sanitizer.py`
  - Extract `_sanitize_fact_text` logic from `entity_extractor.py`
  - Add `sanitize_text(text, max_length)` — NFKC, zero-width strip, control char strip, length limit
  - Add `sanitize_wiki_link(link)` — path traversal rejection, slug normalization, length limit
  - Test: `tests/unit/utils/test_sanitizer.py`

- [x] **1A.2** Update `entity_extractor.py` to import from shared sanitizer
  - Replace `_sanitize_fact_text` with `from arcagent.utils.sanitizer import sanitize_text`
  - Verify existing tests still pass (no behavior change)

- [x] **1A.3** Create `arcagent/modules/bio_memory/` directory scaffold
  - `__init__.py` — public API exports
  - `MODULE.yaml` — module manifest
  - `config.py` — `BioMemoryConfig` extending `ModuleConfig`
  - `errors.py` — `BioMemoryError` base exception
  - Test: `tests/unit/modules/bio_memory/test_config.py`

### Phase 1B: Working Memory

- [x] **1B.1** Implement `working_memory.py`
  - `WorkingMemory` class with `read()`, `write()`, `clear()`, `estimate_tokens()`
  - YAML frontmatter generation (topics, tags, entity_refs, importance, turn_number, timestamp)
  - Token budget enforcement (truncate from top if exceeded)
  - Atomic writes via `atomic_write_text`
  - Test: `tests/unit/modules/bio_memory/test_working_memory.py`

### Phase 1C: Identity Manager

- [x] **1C.1** Implement `identity_manager.py`
  - `IdentityManager` with `read()`, `inject_context()`, `update()`, `is_over_budget()`
  - Before/after snapshot for audit trail
  - Audit event emission (`identity.modified`) via OTel + JSONL
  - Token budget enforcement on injection
  - Atomic writes
  - Test: `tests/unit/modules/bio_memory/test_identity_manager.py`

### Phase 1D: Retriever

- [x] **1D.1** Implement `retriever.py`
  - `Retriever` with `search()`, `recall()`
  - Two-pass search: `_frontmatter_grep()` → `_fulltext_grep()`
  - Wiki-link extraction and one-hop following: `_follow_wiki_links()`
  - File discovery: `_discover_files()` (episodes/*.md, how-i-work.md, working.md)
  - Budget enforcement: `_enforce_budget()` with configurable overflow strategy
  - `RetrievalResult` dataclass
  - Boundary markers with session UUID
  - Test: `tests/unit/modules/bio_memory/test_retriever.py`

### Phase 1E: Consolidator (Light)

- [x] **1E.1** Implement `consolidator.py`
  - `Consolidator` with `light_consolidate()` method
  - `_evaluate_significance()` — LLM judges session significance
  - `_create_episode()` — creates episode file with frontmatter + narrative
  - `_evaluate_identity_update()` — LLM evaluates identity change need
  - Content-hash check (skip if working.md unchanged)
  - Atomic writes for episodes
  - Telemetry emission
  - Test: `tests/unit/modules/bio_memory/test_consolidator.py` (mock eval model)

### Phase 1F: Facade + Integration

- [x] **1F.1** Implement `bio_memory_module.py` (facade)
  - `BioMemoryModule` implementing `Module` protocol
  - Constructor matching `ModuleLoader._instantiate` injection pattern
  - `startup()`: mutual exclusivity check, register bus handlers, register tools
  - `shutdown()`: cancel background tasks
  - Bus handlers: `_on_assemble_prompt`, `_on_post_respond`, `_on_pre_tool`, `_on_post_tool`, `_on_shutdown`
  - Four tool registrations: `memory_search`, `memory_note`, `memory_recall`, `memory_reflect`
  - Bash veto for `memory/` paths
  - Test: `tests/unit/modules/bio_memory/test_bio_memory_module.py`

- [x] **1F.2** Implement `cli.py`
  - CLI commands: `status`, `identity show`, `episodes list`, `working show`, `search`, `consolidate`
  - Follows existing pattern from `modules/memory/cli.py`
  - Test: `tests/unit/modules/bio_memory/test_cli.py`

### Phase 1G: Integration Tests

- [x] **1G.1** Integration test: module lifecycle through bus
  - Test: `tests/integration/modules/test_bio_memory_integration.py`
  - Module loads via `ModuleLoader`
  - Bus events trigger correct handlers
  - Working.md lifecycle (write per turn, clear on shutdown)
  - how-i-work.md injection into prompt context
  - Mutual exclusivity with markdown-memory module
  - Light consolidation triggers on shutdown (mock eval model)

- [x] **1G.2** Integration test: retrieval through tools
  - Test: `tests/integration/modules/test_bio_memory_retrieval.py`
  - Seed memory workspace with test files
  - `memory_search` returns relevant results
  - `memory_recall` returns specific entity
  - Wiki-link following works
  - Token budget enforced
  - Boundary markers present

---

## File Inventory

### New Files (12)

| File | LOC Est | Purpose |
|------|---------|---------|
| `arcagent/utils/sanitizer.py` | ~40 | Shared sanitization |
| `arcagent/modules/bio_memory/__init__.py` | ~20 | Public API |
| `arcagent/modules/bio_memory/MODULE.yaml` | ~20 | Module manifest |
| `arcagent/modules/bio_memory/config.py` | ~30 | BioMemoryConfig |
| `arcagent/modules/bio_memory/errors.py` | ~15 | Error classes |
| `arcagent/modules/bio_memory/working_memory.py` | ~80 | WorkingMemory |
| `arcagent/modules/bio_memory/identity_manager.py` | ~100 | IdentityManager |
| `arcagent/modules/bio_memory/retriever.py` | ~180 | Retriever |
| `arcagent/modules/bio_memory/consolidator.py` | ~150 | Consolidator (light) |
| `arcagent/modules/bio_memory/bio_memory_module.py` | ~200 | Facade |
| `arcagent/modules/bio_memory/cli.py` | ~150 | CLI commands |

**Total new LOC**: ~985 estimated

### Modified Files (2)

| File | Change |
|------|--------|
| `arcagent/modules/memory/entity_extractor.py` | Replace `_sanitize_fact_text` with import from `utils/sanitizer.py` |
| `arcagent/modules/bio_memory/__init__.py` | (created above) |

### Test Files (9)

| File | Type |
|------|------|
| `tests/unit/utils/test_sanitizer.py` | Unit |
| `tests/unit/modules/bio_memory/test_config.py` | Unit |
| `tests/unit/modules/bio_memory/test_working_memory.py` | Unit |
| `tests/unit/modules/bio_memory/test_identity_manager.py` | Unit |
| `tests/unit/modules/bio_memory/test_retriever.py` | Unit |
| `tests/unit/modules/bio_memory/test_consolidator.py` | Unit |
| `tests/unit/modules/bio_memory/test_bio_memory_module.py` | Unit |
| `tests/unit/modules/bio_memory/test_cli.py` | Unit |
| `tests/integration/modules/test_bio_memory_integration.py` | Integration |
| `tests/integration/modules/test_bio_memory_retrieval.py` | Integration |

---

## Verification Checklist

- [ ] All unit tests pass
- [ ] All integration tests pass
- [ ] `ruff check arcagent/modules/bio_memory/` — 0 errors
- [ ] `ruff check arcagent/utils/sanitizer.py` — 0 errors
- [ ] `mypy arcagent/modules/bio_memory/ --strict` — 0 errors
- [ ] Existing `modules/memory/` tests still pass (no regression)
- [ ] `MODULE.yaml` validates correctly
- [ ] Module loads via `ModuleLoader.load_all()`
- [ ] Mutual exclusivity enforced (both enabled → ConfigError)
- [ ] Audit events emitted for memory writes
- [ ] Token budgets enforced (identity ≤500, working ≤500, retrieval ≤3000)
- [ ] Sanitizer catches: zero-width chars, control chars, path traversal in wiki-links
- [ ] Boundary markers present on search results
- [ ] No new dependencies added (uses existing utils + patterns)

---

## Dependencies

**Zero new external dependencies.** Uses:
- `pydantic` (existing)
- `yaml` (existing)
- `opentelemetry` (existing)
- `arcllm.types` (existing)

---

## Risks

| Risk | Mitigation |
|------|------------|
| LLM consolidation quality varies | Significance evaluation can be tuned; failures are non-blocking |
| Grep performance at scale | Phase 3 adds vector fallback; Phase 1 target is <100 files |
| Wiki-link injection | Entity registry (only follow existing files), sanitize_wiki_link |
| Module conflict detection race | Check in startup() before any handlers register |

---

## Task Summary

**Total**: 11 tasks
**Completed**: 11/11
**Remaining**: 0
